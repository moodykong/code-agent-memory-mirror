"""The rclone box backend: OneDrive, S3, Google Drive, SFTP and anything else rclone reaches.

Settings: inbound_remote and outbound_remote, each "remote:path" as rclone
names them (a local folder also works). Credentials live in rclone's own
config (`rclone config`, or RCLONE_CONFIG_* environment variables in a cloud
session); agent-mirror never reads them.

Always `rclone copy` (never `sync` unless --delete is given), so nothing on
the far side is deleted by default.
"""

import json
import os
import shutil
import tempfile
import time

from agent_mirror.box import archive
from agent_mirror.box.common import run_transfer
from agent_mirror.box.local import FetchRecord, disk_guard
from agent_mirror.box.sides import RcloneSide
from agent_mirror.box.rules import folder_files
from agent_mirror.runtime import CLOUD_RUNTIMES
from agent_mirror.util import err, out, short, show_cmd, utc_stamp, warn

RCLONE_MISSING = "rclone is not installed or not on PATH; the rclone box needs it (https://rclone.org/install/)"
ENV_FILE_FILTERS = ["- .env", "- *.env"]


def rclone_ready(dry_run):
    if shutil.which("rclone"):
        return True
    if dry_run:
        warn(RCLONE_MISSING)
        return True
    err(RCLONE_MISSING)
    return False


def remote_join(base, rel):
    if not rel:
        return base
    if base.endswith((":", "/")):
        return base + rel
    return base + "/" + rel


def rclone_pattern(pattern):
    """Our glob (relative to the project folder) as an rclone filter glob.

    '**/x' and a bare name match at any depth, which rclone does for a pattern
    without a leading '/'. Any other pattern with a '/' is anchored at the root.
    """
    p = pattern.strip().lstrip("/")
    if p.startswith("**/"):
        return p[3:]
    if "/" not in p:
        return p
    return "/" + p


def local_folders(s):
    """Folders inside the project that agent-mirror itself manages; never pushed file by file."""
    from agent_mirror.box.extras import mount_dir, receive_dir

    found = ["/%s/**" % archive.ARCHIVE_DIR, "/.agent-mirror/**"]
    root = os.path.realpath(s["project_path"])
    for folder in (mount_dir(s), receive_dir(s)):
        real = os.path.realpath(folder)
        if real.startswith(root + os.sep):
            found.append("/%s/**" % os.path.relpath(real, root).replace(os.sep, "/"))
    found += ["/%s/**" % g for g in s.get("archives") or []]
    return found


def filter_args(s):
    """--filter flags: env files, managed and archived folders and exclude rules out, include rules in, the rest out."""
    rules = list(ENV_FILE_FILTERS)
    rules += ["- " + p for p in local_folders(s)]
    rules += ["- " + rclone_pattern(p) for p in s["exclude"]]
    rules += ["+ " + rclone_pattern(p) for p in s["include"]]
    rules.append("- **")
    args = []
    for r in rules:
        args += ["--filter", r]
    return args


def failed(what, rc, stdout, stderr):
    detail = (stderr or stdout or "").strip() or "no output"
    err("%s failed (%s): %s" % (what, "rc %s" % rc if rc is not None else "no exit code", short(detail, 600)))
    return 1


# push ----------------------------------------------------------------------------------


def push(args, s, runtime):
    if runtime in CLOUD_RUNTIMES:
        err("refusing box push on %s: only the developer machine writes the inbound side" % runtime)
        return 2
    if not os.path.isdir(s["project_path"]):
        err("project folder %s does not exist" % s["project_path"])
        return 1
    if not s["include"] and not s.get("archives"):
        out("box.include and box.archives are empty; nothing to push")
        return 0
    if args.dry_run:
        out("dry run: nothing will change")
    if not rclone_ready(args.dry_run):
        return 1
    if not s["include"]:
        return 1 if archive.push_archives(args, s, RcloneSide(s, "inbound")) else 0
    verb = "sync" if args.delete else "copy"
    if args.delete:
        warn("--delete: files under %s that are gone locally will be deleted (rclone sync)" % s["inbound"])
    cmd = ["rclone", verb, s["project_path"], s["inbound"]] + filter_args(s)
    out("%s: %s" % ("would run" if args.dry_run else "running", show_cmd(cmd)))
    if args.dry_run:
        archive.push_archives(args, s, RcloneSide(s, "inbound"))
        return 0
    start = time.time()
    rc, _, stderr = run_transfer(cmd, args.transfer_timeout, capture=False)
    took = time.time() - start
    if rc != 0:
        err("rclone %s failed after %.1f s (%s)" % (verb, took, stderr or "rc %s" % rc))
        return 1
    out("pushed the include folders to %s in %.1f s" % (s["inbound"], took))
    return 1 if archive.push_archives(args, s, RcloneSide(s, "inbound")) else 0


# index ------------------------------------------------------------------------------------


def index(args, s, runtime):
    cmd = ["rclone", "lsjson", "-R", "--files-only", "--no-mimetype", s["inbound"]]
    index_path = s["index_path"]
    if args.dry_run:
        out("dry run: nothing will change")
        rclone_ready(True)
        out("would run: %s" % show_cmd(cmd))
        out("would write %s" % index_path)
        return 0
    if not rclone_ready(False):
        return 1
    out("running: %s" % show_cmd(cmd))
    start = time.time()
    rc, stdout, stderr = run_transfer(cmd, args.transfer_timeout)
    if rc in (3, 4):
        out("%s does not exist yet; the index is empty" % s["inbound"])
        stdout, rc = "[]", 0
    if rc != 0:
        return failed("rclone lsjson", rc, stdout, stderr)
    try:
        items = json.loads(stdout or "[]")
    except ValueError:
        err("rclone lsjson did not print JSON: %s" % short(stdout))
        return 1
    kept = left_out = 0
    sidecars = []
    partial = index_path + ".partial"
    try:
        with open(partial, "w", encoding="utf-8") as fh:
            for it in sorted(items, key=lambda x: str(x.get("Path", ""))):
                rel = str(it.get("Path") or "")
                if not rel or it.get("IsDir"):
                    continue
                if rel.startswith(archive.ARCHIVE_DIR + "/"):
                    if archive.sidecar_name(rel):
                        sidecars.append(archive.sidecar_name(rel))
                    continue
                if not s["rules"].included(rel):
                    left_out += 1
                    continue
                fh.write(json.dumps({"path": rel, "size": it.get("Size"), "modtime": it.get("ModTime")}) + "\n")
                kept += 1
            for entry in archive.index_entries(
                RcloneSide(s, "inbound"), sidecars, os.path.dirname(index_path), args.transfer_timeout
            ):
                fh.write(json.dumps(entry) + "\n")
        os.replace(partial, index_path)
    finally:
        if os.path.exists(partial):
            os.unlink(partial)
    out("wrote %d entries to %s (%.1f s)" % (kept, index_path, time.time() - start))
    if sidecars:
        out(
            "listed %d archived folder(s) with their tables of contents in %s"
            % (len(sidecars), os.path.join(os.path.dirname(index_path), "box_toc"))
        )
    if left_out:
        out("left out %d files that the include/exclude rules do not cover" % left_out)
    return 0


# fetch ---------------------------------------------------------------------------------------


def fetch(args, s, runtime, rel):
    cloud = runtime in CLOUD_RUNTIMES
    project_path = s["project_path"]
    dest = os.path.join(project_path, rel)
    if not cloud and os.path.isfile(dest):
        out("%s already exists locally; fetch never overwrites on a %s runtime, skipped" % (dest, runtime))
        return 0
    src = remote_join(s["inbound"], rel)
    keep_local = [] if cloud else ["--ignore-existing"]
    stat_cmd = ["rclone", "lsjson", "--stat", src]
    file_cmd = ["rclone", "copyto", src, dest] + keep_local
    dir_cmd = ["rclone", "copy", src, dest] + keep_local
    if args.dry_run:
        out("dry run: nothing will change")
        rclone_ready(True)
        out("would run: %s" % show_cmd(stat_cmd))
        out("then, if it is a file: %s" % show_cmd(file_cmd))
        out("or, if it is a folder: %s" % show_cmd(dir_cmd))
        out("(%s)" % ("overwriting local files" if cloud else "never overwriting existing local files"))
        return 0
    if not os.path.isdir(project_path):
        err("project folder %s does not exist" % project_path)
        return 1
    if not rclone_ready(False):
        return 1
    out("running: %s" % show_cmd(stat_cmd))
    rc, stdout, stderr = run_transfer(stat_cmd, args.transfer_timeout)
    if rc != 0:
        if "not found" in (stderr or stdout).lower():
            err("nothing found at %s" % src)
            return 1
        return failed("rclone lsjson --stat", rc, stdout, stderr)
    try:
        item = json.loads(stdout or "{}")
    except ValueError:
        err("rclone lsjson --stat did not print JSON: %s" % short(stdout))
        return 1
    if not isinstance(item, dict) or not item:
        err("nothing found at %s" % src)
        return 1
    cmd = dir_cmd if item.get("IsDir") else file_cmd
    if item.get("IsDir"):
        listed = RcloneSide(s, "inbound").list(rel, args.transfer_timeout)
        if listed is None:
            err("cannot list %s" % src)
            return 1
        todo = [(rel + "/" + f["path"], int(f.get("size") or 0)) for f in listed]
    else:
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        todo = [(rel, int(item.get("Size") or 0))]
    if not cloud:
        todo = [(p, n) for p, n in todo if not os.path.lexists(os.path.join(project_path, *p.split("/")))]
    if not disk_guard(project_path, sum(n for _, n in todo), s["min_free"], getattr(args, "force", False)):
        return 1
    out("running: %s" % show_cmd(cmd))
    rc, stdout, stderr = run_transfer(cmd, args.transfer_timeout)
    if rc != 0:
        return failed("rclone " + cmd[1], rc, stdout, stderr)
    record = FetchRecord(project_path)
    for p, _ in todo:
        record.add(p, "file")
    if todo:
        record.save()
    out("fetched %s into %s%s" % (src, dest, "" if cloud else " (existing local files kept)"))
    return 0


# send -----------------------------------------------------------------------------------------


def send(args, s, runtime, rel):
    src = os.path.join(s["project_path"], rel)
    is_dir = os.path.isdir(src)
    dest = remote_join(s["outbound"], "%s-%s/%s" % (runtime, utc_stamp(), rel))
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
        rclone_ready(True)
    elif not rclone_ready(False):
        return 1
    listfile = None
    try:
        if is_dir:
            if args.dry_run:
                shown_list = "<list of %d files>" % len(keep)
            else:
                fd, listfile = tempfile.mkstemp(prefix="agent-mirror-send-", suffix=".txt")
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write("\n".join(keep) + "\n")
                shown_list = listfile
            cmd = ["rclone", "copy", src, dest, "--files-from", shown_list, "--ignore-existing"]
        else:
            cmd = ["rclone", "copyto", src, dest, "--ignore-existing"]
        if args.dry_run:
            out("would run: %s" % show_cmd(cmd))
            return 0
        out("running: %s" % show_cmd(cmd))
        rc, stdout, stderr = run_transfer(cmd, args.transfer_timeout)
    finally:
        if listfile:
            try:
                os.unlink(listfile)
            except OSError:
                pass
    if rc != 0:
        return failed("rclone " + cmd[1], rc, stdout, stderr)
    if is_dir:
        out("uploaded %d files to %s/" % (len(keep), dest))
    else:
        out("uploaded %s to %s" % (rel, dest))
    return 0


def describe(s):
    return [
        "rclone",
        "inbound  %s  (box push writes, box index and box fetch read)" % s["inbound"],
        "outbound %s  (box send writes)" % s["outbound"],
    ]
