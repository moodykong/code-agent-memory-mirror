"""The azure-blob box backend, through the Azure CLI (az).

One storage account, Azure AD sign-in only (--auth-mode login, never account
keys or SAS), two containers:
  shared    developer machines upload untracked project files here (box push), each the
            folders it owns; agents read it (box index, box fetch).
  sessions  one folder per session (box send); every machine and session reads them.
Blobs live under <prefix>/ in each container, at the same relative paths as
in the project folder.
"""

import fnmatch
import json
import os
import re
import shutil
import tempfile
import time

from agent_mirror.box import archive, owners, sessions
from agent_mirror.box.common import run_transfer
from agent_mirror.box.local import FetchRecord, disk_guard
from agent_mirror.box.sides import AzureSide
from agent_mirror.box.rules import exclude_rule, folder_files, is_env_file
from agent_mirror.box.units import archive_unit_of
from agent_mirror.runtime import CLOUD_RUNTIMES
from agent_mirror.util import err, local_path, out, short, show_cmd, warn

INDEX_PAGE_SIZE = 5000
SP_VARS = ("AZURE_CLIENT_ID", "AZURE_CLIENT_SECRET", "AZURE_TENANT_ID")
AZ_MISSING = (
    "az (the Azure CLI) is not installed or not on PATH; the azure-blob box needs it "
    "(install steps: docs/project-config.md)"
)
INDEX_QUERY = "[].{name:name, size:properties.contentLength, modtime:properties.lastModified, next:nextMarker}"


def az_failed(what, rc, stdout, stderr):
    detail = (stderr or stdout or "").strip() or "no output"
    err("%s failed (%s): %s" % (what, "rc %s" % rc if rc is not None else "no exit code", short(detail, 600)))
    if "az login" in detail or "AADSTS" in detail or "not logged in" in detail.lower():
        err("check the az sign-in (`az account show`); on a cloud runtime agent-mirror signs in from AZURE_* itself")
    return 1


def ready(dry_run):
    return az_ready(dry_run)


def prepare(args, runtime):
    """az is there and, on a cloud runtime, signed in: before any command that reads or writes the box."""
    dry = getattr(args, "dry_run", False)
    return az_ready(dry) and sign_in(runtime, args.timeout, dry_run=dry)


def az_ready(dry_run):
    if shutil.which("az"):
        return True
    if dry_run:
        warn(AZ_MISSING)
        return True
    err(AZ_MISSING)
    return False


def run_az(args, timeout, capture=True, input_text=None):
    return run_transfer(["az"] + list(args), timeout, capture=capture, input_text=input_text)


def sign_in(runtime, timeout, dry_run=False, deadline=None):
    """On a cloud runtime, sign az in as the service principal if it is not signed in yet.

    The secret goes to az on stdin (--password=@-, az reads '@-' as stdin), so it
    is never on a command line, in a file, or in any output. deadline (a time.time() value), when
    given, bounds both calls (a session start lists the box within a short time).
    """

    def limit(default):
        return default if deadline is None else max(1, int(deadline - time.time()))

    if runtime not in CLOUD_RUNTIMES:
        return True
    values = dict((n, os.environ.get(n, "").strip()) for n in SP_VARS)
    missing = [n for n in SP_VARS if not values[n]]
    shown = ["az", "login", "--service-principal", "--username", "$AZURE_CLIENT_ID", "--tenant", "$AZURE_TENANT_ID"]
    shown += ["--password=@-", "--allow-no-subscriptions", "--only-show-errors", "-o", "none"]
    if dry_run:
        if missing:
            out("would use the existing az sign-in (%s not set)" % ", ".join(missing))
        else:
            out("would run, only if `az account show` fails: %s  (secret on stdin)" % show_cmd(shown))
        return True
    rc, _, _ = run_az(["account", "show", "--only-show-errors", "-o", "none"], limit(max(timeout, 60)))
    if rc == 0:
        return True
    if missing:
        err("az is not signed in, and %s not set, so agent-mirror cannot sign in" % ", ".join(missing))
        return False
    login = list(shown[1:])
    login[login.index("$AZURE_CLIENT_ID")] = values["AZURE_CLIENT_ID"]
    login[login.index("$AZURE_TENANT_ID")] = values["AZURE_TENANT_ID"]
    out("signing in: %s  (secret on stdin)" % show_cmd(shown))
    rc, stdout, stderr = run_az(login, limit(max(timeout, 120)), input_text=values["AZURE_CLIENT_SECRET"])
    if rc != 0:
        return not az_failed("az login --service-principal", rc, stdout, stderr)
    return True


def storage_args(s):
    return ["--account-name", s["account"], "--auth-mode", "login"]


# push -----------------------------------------------------------------------------


def find_dirs(source, name_globs, pruned):
    """Folders under source whose name matches a glob, as 'rel/' paths.

    Folders under a pruned 'rel/' prefix are not searched, links are not followed,
    and a matching folder is not searched further.
    """
    found = []
    stack = [""]
    while stack:
        rel_dir = stack.pop()
        try:
            with os.scandir(os.path.join(source, rel_dir)) as it:
                entries = sorted((e for e in it if e.is_dir(follow_symlinks=False)), key=lambda e: e.name)
        except OSError:
            continue
        for e in entries:
            rel = rel_dir + e.name + "/"
            if any(rel.startswith(p) for p in pruned):
                continue
            if any(fnmatch.fnmatchcase(e.name, g) for g in name_globs):
                found.append(rel)
            else:
                stack.append(rel)
    return sorted(found)


def sync_plan(s, roots, archived=()):
    """One `az storage blob sync` per include root, with excludes made relative to that root.

    archived: the archived folders found here; those inside a root are left out of its sync.
    """
    rules = [exclude_rule(p) for p in s["exclude"]]
    names = [v for k, v in rules if k == "name"]
    dir_globs = [v for k, v in rules if k == "dir"]
    paths = [v for k, v in rules if k == "path"]
    plan = []
    for root in roots:
        step = {"root": root, "source": os.path.join(s["project_path"], *root.split("/"))}
        covering = [p for p in paths if root == p or root.startswith(p + "/")]
        top = root.split("/")[0]
        if top == archive.ARCHIVE_DIR:
            step["skip"] = "the folder name %s is reserved for archives" % archive.ARCHIVE_DIR
        elif archive_unit_of(root, s.get("archives") or []):
            step["skip"] = "archived as one bundle (box.archives)"
        elif covering:
            step["skip"] = "the whole folder matches the exclude pattern %s/**" % covering[0]
        elif not os.path.isdir(step["source"]):
            step["skip"] = "no such folder in the project"
        else:
            ex = [p[len(root) + 1 :] + "/" for p in paths if p.startswith(root + "/")]
            ex += [a[len(root) + 1 :] + "/" for a in archived if a.startswith(root + "/")]
            if dir_globs:
                ex += find_dirs(step["source"], dir_globs, ex)
            bad = [p for p in ex if ";" in p]
            if bad:
                step["error"] = "folder %s has a ';' in its name, which az exclude lists cannot hold" % bad[0]
            elif len(";".join(ex)) > 100000:  # Linux caps one argument at 128 KiB
                step["error"] = "%d excluded folders are too many for one --exclude-path argument" % len(ex)
            step["exclude_paths"] = ex
            step["exclude_names"] = list(names)
        plan.append(step)
    return plan


def sync_cmd(s, step, delete):
    cmd = ["az", "storage", "blob", "sync"] + storage_args(s)
    cmd += ["--container", s["shared"], "--source", step["source"]]
    # the trailing '/' makes azcopy treat a destination that does not exist yet as a folder
    cmd += ["--destination", "%s/%s/" % (s["prefix"], step["root"])]
    # az's own default is true; never delete unless asked
    cmd += ["--delete-destination", "true" if delete else "false"]
    if step["exclude_paths"]:
        cmd += ["--exclude-path", ";".join(step["exclude_paths"])]
    if step["exclude_names"]:
        cmd += ["--exclude-pattern", ";".join(step["exclude_names"])]
    return cmd + ["--only-show-errors"]


def push_folders(args, s, layout, folder_units):
    """az storage blob sync for the include folders of these units. Returns (names that succeeded, failures).

    owners.push decides which units this machine pushes; this never touches another unit's folder.
    """
    done, failures = [], 0
    for unit in folder_units:
        ok = True
        for step in sync_plan(s, unit["roots"], layout.archives):
            if step.get("skip"):
                out("%s: skipped (%s)" % (step["root"], step["skip"]))
                continue
            if step.get("error"):
                err("%s: not synced: %s" % (step["root"], step["error"]))
                ok = False
                continue
            cmd = sync_cmd(s, step, args.delete)
            out("%s: %s" % ("would run" if args.dry_run else "running", show_cmd(cmd)))
            if args.dry_run:
                continue
            start = time.time()
            rc, _, stderr = run_az(cmd[1:], args.transfer_timeout, capture=False)
            took = time.time() - start
            if rc != 0:
                err("sync of %s failed after %.1f s (%s)" % (step["root"], took, stderr or "rc %s" % rc))
                ok = False
            else:
                out("synced %s to %s/%s/%s/ in %.1f s" % (step["root"], s["shared"], s["prefix"], step["root"], took))
        if ok:
            done.append(unit["name"])
        else:
            failures += 1
    return done, failures


# index -------------------------------------------------------------------------------


def index(args, s, runtime):
    if args.page_size < 1:
        err("--page-size must be at least 1")
        return 2
    base = ["az", "storage", "blob", "list"] + storage_args(s)
    base += ["--container-name", s["shared"], "--prefix", s["prefix"] + "/"]
    base += ["--num-results", str(args.page_size), "--show-next-marker"]
    base += ["--query", INDEX_QUERY, "--only-show-errors", "-o", "json"]
    index_path = s["index_path"]
    if args.dry_run:
        out("dry run: nothing will change")
        az_ready(True)
        sign_in(runtime, args.timeout, dry_run=True)
        out("would run: %s  (again with --marker <next marker> until the listing ends)" % show_cmd(base))
        out("would write %s" % index_path)
        return 0
    if not az_ready(False) or not sign_in(runtime, args.timeout):
        return 1
    out("running: %s  (paged)" % show_cmd(base))
    cut = len(s["prefix"]) + 1
    kept = left_out = calls = 0
    sidecars = []
    marker = None
    has_marker = set()
    start = time.time()
    partial = index_path + ".partial"
    try:
        with open(partial, "w", encoding="utf-8") as fh:
            while True:
                cmd = base + (["--marker", marker] if marker else [])
                rc, stdout, stderr = run_az(cmd[1:], args.transfer_timeout)
                calls += 1
                if rc != 0:
                    return az_failed("az storage blob list", rc, stdout, stderr)
                try:
                    items = json.loads(stdout or "[]")
                except ValueError:
                    err("az storage blob list did not print JSON: %s" % short(stdout))
                    return 1
                marker = None
                for it in items:
                    if it.get("next"):
                        marker = it["next"]
                    name = it.get("name")
                    if not name:
                        continue
                    rel = name[cut:]
                    if rel in owners.MARKERS:
                        has_marker.add(rel)
                        continue
                    if rel.startswith(archive.ARCHIVE_DIR + "/"):
                        if archive.sidecar_name(rel):
                            sidecars.append(archive.sidecar_name(rel))
                        continue
                    if not s["rules"].included(rel):
                        left_out += 1
                        continue
                    fh.write(json.dumps({"path": rel, "size": it.get("size"), "modtime": it.get("modtime")}) + "\n")
                    kept += 1
                if not marker:
                    break
            for entry in archive.index_entries(
                AzureSide(s, "shared"), sidecars, os.path.dirname(index_path), args.transfer_timeout
            ):
                fh.write(json.dumps(entry) + "\n")
            for line in owners.index_lines(
                AzureSide(s, "shared"), args.timeout, owners.OWNERS in has_marker, owners.LEGACY in has_marker
            ):
                fh.write(json.dumps(line) + "\n")
            summary = sessions.index_line(s, args.transfer_timeout)
            if summary is not None:
                fh.write(json.dumps(summary) + "\n")
        os.replace(partial, index_path)
    finally:
        if os.path.exists(partial):
            os.unlink(partial)
    out("wrote %d entries to %s (%d listing calls, %.1f s)" % (kept, index_path, calls, time.time() - start))
    if sidecars:
        out(
            "listed %d archived folder(s) with their tables of contents in %s"
            % (len(sidecars), os.path.join(os.path.dirname(index_path), "box_toc"))
        )
    if left_out:
        out("left out %d blobs that the include/exclude rules do not cover" % left_out)
    return 0


# fetch ---------------------------------------------------------------------------------


def pattern_escape(text):
    """Escape fnmatch wildcards, for az --pattern."""
    return re.sub(r"([*?\[])", r"[\1]", text)


def download_cmd(s, blob, path):
    cmd = ["az", "storage", "blob", "download"] + storage_args(s)
    cmd += ["--container-name", s["shared"], "--name", blob, "--file", path, "--overwrite", "false"]
    return cmd + ["--no-progress", "--only-show-errors", "-o", "none"]


def download_batch_cmd(s, blob, folder):
    cmd = ["az", "storage", "blob", "download-batch"] + storage_args(s)
    cmd += ["--source", s["shared"], "--destination", folder, "--pattern", pattern_escape(blob) + "/*"]
    return cmd + ["--no-progress", "--only-show-errors", "-o", "none"]


def place_files(stage, names, targets, overwrite, moved_names=None):
    """Move downloaded files from the staging folder into the project. Returns counts."""
    moved, kept, failed = 0, 0, []
    for name in names:
        src = local_path(stage, name)
        dest = targets[name]
        if not os.path.isfile(src):
            failed.append("%s was not downloaded" % name)
            continue
        if os.path.lexists(dest) and not overwrite:
            kept += 1
            continue
        try:
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            os.replace(src, dest)
            moved += 1
            if moved_names is not None:
                moved_names.append(name)
        except OSError as exc:
            failed.append("%s: %s" % (dest, exc))
    return moved, kept, failed


def fetch(args, s, runtime, rel):
    cloud = runtime in CLOUD_RUNTIMES
    project_path = s["project_path"]
    dest = local_path(project_path, rel)
    if not cloud and os.path.isfile(dest):
        out("%s already exists locally; fetch never overwrites on a %s runtime, skipped" % (dest, runtime))
        return 0
    blob = s["prefix"] + "/" + rel
    list_cmd = ["az", "storage", "blob", "list"] + storage_args(s)
    list_cmd += ["--container-name", s["shared"], "--prefix", blob, "--num-results", "*"]
    list_cmd += ["--query", "[].{name:name, size:properties.contentLength}", "--only-show-errors", "-o", "json"]
    if args.dry_run:
        out("dry run: nothing will change")
        az_ready(True)
        sign_in(runtime, args.timeout, dry_run=True)
        stage = os.path.join(project_path, ".agent-mirror-fetch-XXXX")
        out("would run: %s" % show_cmd(list_cmd))
        out("then, if it is one blob: %s" % show_cmd(download_cmd(s, blob, os.path.join(stage, blob))))
        out("or, if it is a folder: %s" % show_cmd(download_batch_cmd(s, blob, stage)))
        out(
            "then move the files into %s (%s)"
            % (dest, "overwriting local files" if cloud else "never overwriting existing local files")
        )
        return 0
    if not os.path.isdir(project_path):
        err("project folder %s does not exist" % project_path)
        return 1
    if not az_ready(False) or not sign_in(runtime, args.timeout):
        return 1
    out("running: %s" % show_cmd(list_cmd))
    rc, stdout, stderr = run_az(list_cmd[1:], args.transfer_timeout)
    if rc != 0:
        return az_failed("az storage blob list", rc, stdout, stderr)
    try:
        listed = json.loads(stdout or "[]")
        sizes = dict(
            (it["name"], it.get("size") or 0) if isinstance(it, dict) else (it, 0)
            for it in listed
            if (isinstance(it, dict) and it.get("name")) or isinstance(it, str)
        )
        names = list(sizes)
    except (ValueError, TypeError, KeyError):
        err("az storage blob list did not print JSON: %s" % short(stdout))
        return 1
    is_file = blob in names
    members = [n for n in names if n.startswith(blob + "/")]
    where = "%s/%s" % (s["shared"], blob)
    if is_file and members:
        err("%s is both a blob and a folder; fetch a path inside it instead" % where)
        return 1
    if not is_file and not members:
        err("nothing found at %s" % where)
        return 1
    wanted = [blob] if is_file else members
    cut = len(s["prefix"]) + 1
    targets = dict((n, local_path(project_path, n[cut:])) for n in wanted)
    if not cloud and all(os.path.lexists(t) for t in targets.values()):
        out(
            "all %d files already exist under %s; fetch never overwrites on a %s runtime" % (len(wanted), dest, runtime)
        )
        return 0
    need = sum(int(sizes.get(n) or 0) for n, t in targets.items() if cloud or not os.path.lexists(t))
    if not disk_guard(project_path, need, s["min_free"], getattr(args, "force", False)):
        return 1
    moved_names = []
    stage = tempfile.mkdtemp(prefix=".agent-mirror-fetch-", dir=project_path)
    try:
        if is_file:
            path = local_path(stage, blob)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            cmd = download_cmd(s, blob, path)
        else:
            cmd = download_batch_cmd(s, blob, stage)
        out("running: %s" % show_cmd(cmd))
        rc, stdout, stderr = run_az(cmd[1:], args.transfer_timeout)
        if rc != 0:
            return az_failed(" ".join(cmd[:4]), rc, stdout, stderr)
        moved, kept, failed = place_files(stage, wanted, targets, overwrite=cloud, moved_names=moved_names)
    finally:
        shutil.rmtree(stage, ignore_errors=True)
        if moved_names:
            record = FetchRecord(project_path)
            for n in moved_names:
                record.add(n[cut:], "file")
            record.save()
    out("fetched %d of %d files into %s" % (moved, len(wanted), dest))
    if kept:
        out("kept %d existing local files (fetch never overwrites on a %s runtime)" % (kept, runtime))
    for f in failed[:20]:
        err(f)
    return 1 if failed else 0


# send ----------------------------------------------------------------------------------


def link_or_copy(src, dest):
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    try:
        os.link(src, dest)
    except OSError:
        shutil.copy2(src, dest)


def send(args, s, runtime, rel, sid):
    """Upload a file or folder into this session's namespace (a second send of a path replaces it)."""
    src = local_path(s["project_path"], rel)
    is_dir = os.path.isdir(src)
    dest = "%s/%s/%s" % (s["prefix"], sid, rel)
    where = "%s/%s" % (s["sessions"], dest)
    keep, skipped = folder_files(src, rel, s["rules"]) if is_dir else ([], [])
    if is_dir:
        if skipped:
            out("skipping %d files that match the exclude rules or are env files:" % len(skipped))
            for sub in skipped[:20]:
                out("  %s/%s" % (rel, sub))
        if not keep:
            out("nothing to upload in %s" % rel)
            return 0
    if args.dry_run:
        out("dry run: nothing will change")
        az_ready(True)
        sign_in(runtime, args.timeout, dry_run=True)
    elif not az_ready(False) or not sign_in(runtime, args.timeout):
        return 1
    stage = None
    try:
        if is_dir:
            source = src
            if skipped and args.dry_run:
                source = os.path.join(s["project_path"], ".agent-mirror-send-XXXX")
            elif skipped:
                # upload-batch has no exclude option: upload a filtered copy made of hard links
                try:
                    source = stage = tempfile.mkdtemp(prefix=".agent-mirror-send-", dir=s["project_path"])
                    for sub in keep:
                        link_or_copy(local_path(src, sub), local_path(stage, sub))
                except OSError as exc:
                    err("could not prepare the filtered copy of %s: %s" % (rel, exc))
                    return 1
            cmd = ["az", "storage", "blob", "upload-batch"] + storage_args(s)
            cmd += ["--destination", s["sessions"], "--destination-path", dest, "--source", source]
            cmd += ["--overwrite", "true", "--no-progress", "--only-show-errors", "--query", "length(@)", "-o", "tsv"]
        else:
            cmd = ["az", "storage", "blob", "upload"] + storage_args(s)
            cmd += ["--container-name", s["sessions"], "--name", dest, "--file", src, "--overwrite", "true"]
            cmd += ["--no-progress", "--only-show-errors", "-o", "none"]
        if args.dry_run:
            out("would run: %s" % show_cmd(cmd))
            return 0
        out("running: %s" % show_cmd(cmd))
        rc, stdout, stderr = run_az(cmd[1:], args.transfer_timeout)
    finally:
        if stage:
            shutil.rmtree(stage, ignore_errors=True)
    if rc != 0:
        return az_failed(" ".join(cmd[:4]), rc, stdout, stderr)
    if is_dir:
        # upload-batch logs a failed file and carries on, so count what it uploaded
        done = stdout.strip()
        if done != str(len(keep)):
            err("uploaded %s of %d files to %s/; see the errors above" % (done or "?", len(keep), where))
            return 1
        out("uploaded %d files to %s/" % (len(keep), where))
    else:
        out("uploaded %s to %s" % (rel, where))
    return 0


def describe(s):
    """Lines for doctor."""
    prefix = s["prefix"]
    return [
        "azure-blob, storage account %s" % s["account"],
        "shared   %s/%s/  (box push writes, box index and box fetch read)" % (s["shared"], prefix),
        "sessions %s/%s/  (box send writes, one folder per session; box sessions lists them)" % (s["sessions"], prefix),
    ]


def is_env(rel):
    return is_env_file(rel.rsplit("/", 1)[-1])
