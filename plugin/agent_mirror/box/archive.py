"""Archive mode: a folder with very many small files travels as ONE bundle.

Configured with box.archives: folder paths or globs relative to the project folder ("ws-*",
"decompile/project-*"). Each matching folder is its own archive, named by its relative path
(units.py decides which folders match). On the remote side, under the root of the shared side:

  _archives/<name>.tar.gz          the bundle: every file below the large-file threshold
  _archives/<name>.large/<path>    each file at or above box.archive_large_file_bytes, on its own
  _archives/<name>.json            the sidecar: counts, sizes, fingerprints and the table of contents
  _archives/<name>.toc.json.gz     the table of contents, only when it is too big for the sidecar

The sidecar is uploaded last, so its fingerprint only changes after everything
else arrived. Fetching extracts only what was asked for, and large files are
fetched only when named or with --all, because cloud machines have little disk.
"""

import datetime
import gzip
import hashlib
import json
import os
import posixpath
import re
import shlex
import shutil
import stat
import subprocess
import tarfile
import tempfile
import time

from agent_mirror import __version__
from agent_mirror.box.local import FetchRecord, disk_guard, human
from agent_mirror.box.rules import is_env_file
from agent_mirror.runtime import CLOUD_RUNTIMES
from agent_mirror.platforms import current
from agent_mirror.util import err, out, show_cmd, utc_stamp, warn, within

ARCHIVE_DIR = "_archives"
DEFAULT_LARGE = 50 * 1024**2
DEFAULT_TOC_INLINE = 5_000_000
INDEX_TOC_GZIP = 1_000_000
BUILD_MARGIN = 16 * 1024**2
LIST_LIMIT = 50
RESERVED = (ARCHIVE_DIR, ".agent-mirror", ".git")


def bundle_rel(name):
    return "%s/%s.tar.gz" % (ARCHIVE_DIR, name)


def sidecar_rel(name):
    return "%s/%s.json" % (ARCHIVE_DIR, name)


def toc_rel(name):
    return "%s/%s.toc.json.gz" % (ARCHIVE_DIR, name)


def large_rel(name, path):
    return "%s/%s.large/%s" % (ARCHIVE_DIR, name, path)


def sidecar_name(rel):
    """The archive name when rel looks like a sidecar path (_archives/<name>.json), else None.

    A large file inside <name>.large/ can also end in .json, so a parent folder ending in .large rules
    a path out; callers that read the sidecar also check that its "name" is the same.
    """
    m = re.match(r"^%s/(.+)\.json$" % ARCHIVE_DIR, rel)
    if not m:
        return None
    parts = m.group(1).split("/")
    if any(not p or p in (".", "..") for p in parts) or any(p.endswith(".large") for p in parts[:-1]):
        return None
    return m.group(1)


def entry_problem(g):
    """Why one box.archives entry is not usable, or None."""
    if not isinstance(g, str) or not g.strip():
        return "box.archives entry %r must be a folder path or glob" % (g,)
    text = g.strip()
    parts = text.strip("/").split("/")
    if "\\" in text or text.startswith("/") or "**" in text or any(p in ("", ".", "..") for p in parts):
        return (
            "box.archives entry %r must be a folder path or glob relative to the project folder, such as "
            "ws-* or decompile/project-* (no '..', no '**', no backslashes)" % (g,)
        )
    if parts[0] in RESERVED:
        return "box.archives entry %r names a reserved folder (%s)" % (g, ", ".join(RESERVED))
    if any(p.endswith(".large") for p in parts[:-1]):
        return "box.archives entry %r has a parent folder ending in .large, which archives use themselves" % (g,)
    return None


def archive_problems(box):
    problems = []
    value = box.get("archives") or []
    if not isinstance(value, list):
        return ["box.archives must be a list of folder paths or globs"]
    for g in value:
        p = entry_problem(g)
        if p:
            problems.append(p)
    for key in ("archive_large_file_bytes", "toc_inline_bytes", "min_free_bytes"):
        if key in box and (not isinstance(box[key], int) or box[key] < 0):
            problems.append("box.%s must be a whole number of bytes (0 or more)" % key)
    return problems


def is_link_dir(path):
    if os.path.islink(path):
        return True
    if os.name == "nt":
        from agent_mirror.links import is_junction

        return is_junction(path)
    return False


def scan(project_path, folder, rules, deadline=None, keep=None, prune=()):
    """Regular files under a project folder: [{path (relative to the folder), size, mtime}], skip counts.

    Symbolic links (and Windows junctions) are never followed or packed, env files are always left
    out, and the box exclude rules apply (relative to the project folder). folder "." is the project
    folder itself (its reserved folders are left out). keep(project-relative path) can leave more
    files out, prune lists project-relative folders that are not walked, and when deadline (a
    time.monotonic() value) passes the walk stops and the skip counts say "incomplete".
    """
    top = folder in (".", "")
    base = project_path if top else os.path.join(project_path, *folder.split("/"))
    lead = "" if top else folder + "/"
    entries = []
    skipped = {"env": 0, "excluded": 0, "links": 0}
    for root, dirs, files in os.walk(base):
        if deadline is not None and time.monotonic() > deadline:
            skipped["incomplete"] = True
            dirs[:] = []
            break
        rel_root = os.path.relpath(root, base).replace(os.sep, "/")
        rel_root = "" if rel_root == "." else rel_root
        kept = []
        for d in sorted(dirs):
            sub = rel_root + "/" + d if rel_root else d
            if (top and not rel_root and d in RESERVED) or (lead + sub) in prune:
                continue
            if is_link_dir(os.path.join(root, d)):
                skipped["links"] += 1
            elif rules.excluded(lead + sub + "/"):
                skipped["excluded"] += 1
            else:
                kept.append(d)
        dirs[:] = kept
        for f in sorted(files):
            full = os.path.join(root, f)
            sub = rel_root + "/" + f if rel_root else f
            if os.path.islink(full):
                skipped["links"] += 1
                continue
            if is_env_file(f):
                skipped["env"] += 1
                continue
            if rules.excluded(lead + sub):
                skipped["excluded"] += 1
                continue
            if keep is not None and not keep(lead + sub):
                continue
            try:
                st = os.stat(full)
            except OSError:
                continue
            if stat.S_ISREG(st.st_mode):
                entries.append({"path": sub, "size": st.st_size, "mtime": int(st.st_mtime)})
    entries.sort(key=lambda e: e["path"])
    return entries, skipped


def fingerprint(entries):
    h = hashlib.sha256()
    for e in sorted(entries, key=lambda x: x["path"]):
        h.update(("%s\t%d\t%d\n" % (e["path"], e["size"], e["mtime"])).encode("utf-8", "surrogateescape"))
    return h.hexdigest()


def split(entries, threshold):
    large = [e for e in entries if threshold and e["size"] >= threshold]
    small = [e for e in entries if not (threshold and e["size"] >= threshold)]
    return small, large


def iso(ts):
    if ts is None:
        return None
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_bundle(project_path, folder, entries, out_path, timeout=None):
    """Write a .tar.gz of these files (paths <folder>/<path>). Returns 'tar' or 'tarfile'."""
    names = [folder + "/" + e["path"] for e in entries]
    tar = shutil.which("tar")
    if tar and not folder.startswith("-"):
        listfile = out_path + ".list"
        with open(listfile, "wb") as fh:
            fh.write(b"".join(os.fsencode(n) + b"\0" for n in names))
        try:
            proc = subprocess.run([tar, "--version"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=20)
            gnu = b"GNU tar" in (proc.stdout or b"")
            argv = (
                [tar, "--null"]
                + (["--no-unquote"] if gnu else [])
                + ["-czf", out_path, "-C", project_path, "-T", listfile]
            )
            proc = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
            if proc.returncode == 0:
                return "tar"
            warn(
                "tar failed (%s); packing with Python instead" % (proc.stderr or b"").decode("utf-8", "replace")[-300:]
            )
        except (OSError, subprocess.SubprocessError) as exc:
            warn("tar could not run (%s); packing with Python instead" % exc)
        finally:
            try:
                os.remove(listfile)
            except OSError:
                pass
    with tarfile.open(out_path, "w:gz", compresslevel=6) as tf:
        for n in names:
            tf.add(os.path.join(project_path, *n.split("/")), arcname=n, recursive=False)
    return "tarfile"


def make_sidecar(name, entries, small, large, threshold, archive_bytes, toc_limit):
    """(sidecar dict, gzipped table of contents or None when it fits inside the sidecar)."""
    large_paths = set(e["path"] for e in large)
    toc = [
        {
            "path": e["path"],
            "size": e["size"],
            "mtime": e["mtime"],
            "in": "large" if e["path"] in large_paths else "bundle",
        }
        for e in entries
    ]
    small_bytes = sum(e["size"] for e in small)
    large_bytes = sum(e["size"] for e in large)
    sidecar = {
        "name": name,
        "tool": "agent-mirror %s" % __version__,
        "created": utc_stamp(),
        "files": len(entries),
        "bytes": small_bytes + large_bytes,
        "newest_mtime": iso(max([e["mtime"] for e in entries] or [None]) if entries else None),
        "fingerprint": fingerprint(entries),
        "bundle_fingerprint": fingerprint(small),
        "archive_bytes": archive_bytes or 0,
        "bundle": (
            {"path": bundle_rel(name), "files": len(small), "bytes": small_bytes, "archive_bytes": archive_bytes or 0}
            if small
            else None
        ),
        "large": {
            "threshold": threshold,
            "dir": "%s/%s.large" % (ARCHIVE_DIR, name),
            "files": len(large),
            "bytes": large_bytes,
        },
        "toc_entries": len(toc),
    }
    data = json.dumps(toc, separators=(",", ":")).encode("utf-8")
    if len(data) <= toc_limit:
        sidecar["toc"] = toc
        sidecar["toc_file"] = None
        return sidecar, None
    sidecar["toc"] = None
    sidecar["toc_file"] = toc_rel(name)
    return sidecar, gzip.compress(data)


def parse_json(text):
    try:
        data = json.loads(text or "")
        return data if isinstance(data, dict) else None
    except ValueError:
        return None


def load_toc(sidecar, side, timeout):
    if not sidecar:
        return None
    if sidecar.get("toc") is not None:
        return sidecar["toc"]
    rel = sidecar.get("toc_file")
    if not rel:
        return None
    tmp = current().mkdtemp("agent-mirror-toc-")
    try:
        path = os.path.join(tmp, "toc.json.gz")
        if side.get(rel, path, timeout) != "ok":
            return None
        with gzip.open(path, "rb") as fh:
            return json.loads(fh.read().decode("utf-8"))
    except (OSError, ValueError):
        return None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# push ---------------------------------------------------------------------------------------


def read_sidecars(side, names, timeout):
    """{name: sidecar dict or None} for these archives, with as few transfer calls as the backend allows."""
    if not names:
        return {}
    tmp = current().mkdtemp("agent-mirror-sidecars-")
    try:
        rels = [n + ".json" for n in names]
        found, problem = side.get_present(ARCHIVE_DIR, rels, tmp, timeout)
        if problem:
            return dict((n, parse_json(side.cat(sidecar_rel(n), timeout))) for n in names)
        result = {}
        for n in names:
            path = os.path.join(tmp, *(n + ".json").split("/"))
            data = None
            if n + ".json" in found:
                with open(path, encoding="utf-8", errors="replace") as fh:
                    data = parse_json(fh.read())
            result[n] = data
        return result
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def read_tocs(side, sidecars, timeout):
    """{toc_file: table of contents} for sidecars whose table of contents is a file of its own, in one batch."""
    rels = [sc["toc_file"][len(ARCHIVE_DIR) + 1 :] for sc in sidecars if str(sc.get("toc_file") or "").startswith(ARCHIVE_DIR + "/")]
    if not rels:
        return {}
    tmp = current().mkdtemp("agent-mirror-tocs-")
    try:
        found, _ = side.get_present(ARCHIVE_DIR, rels, tmp, timeout)
        result = {}
        for rel in found:
            try:
                with gzip.open(os.path.join(tmp, *rel.split("/")), "rb") as fh:
                    result[ARCHIVE_DIR + "/" + rel] = json.loads(fh.read().decode("utf-8"))
            except (OSError, ValueError):
                continue
        return result
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def local_file(project, name, path):
    return os.path.join(project, *name.split("/"), *path.split("/"))


def push_one(args, s, side, name, scanned=None, remote_sidecar=False):
    """Push one archived folder. Returns {"rc", "fingerprint", "uploaded"}.

    scanned is (entries, skipped) from an earlier scan of the same folder, to walk it only once, and
    remote_sidecar the sidecar already read from the box (None when there is none), to read it only once.
    """
    project = s["project_path"]
    entries, skipped = scanned if scanned is not None else scan(project, name, s["rules"])
    base = posixpath.basename(name)
    small, large = split(entries, s["large_bytes"])
    small_bytes = sum(e["size"] for e in small)
    fp_all, fp_bundle = fingerprint(entries), fingerprint(small)
    out(
        "%s: %d files (%s): %d in the bundle (%s), %d large file(s) on their own (%s)"
        % (
            name,
            len(entries),
            human(small_bytes + sum(e["size"] for e in large)),
            len(small),
            human(small_bytes),
            len(large),
            human(sum(e["size"] for e in large)),
        )
    )
    if any(skipped.values()):
        out(
            "  left out: %d env files, %d excluded, %d symbolic links"
            % (skipped["env"], skipped["excluded"], skipped["links"])
        )
    done = {"rc": 0, "fingerprint": fp_all, "uploaded": False}
    if args.dry_run:
        out("would read %s and skip the upload if its fingerprint is %s" % (side.where(sidecar_rel(name)), fp_all[:16]))
        if small:
            out("would run: %s" % show_cmd(side.put_cmd("<temp>/%s.tar.gz" % base, bundle_rel(name))))
        for e in large[:5]:
            out("would run: %s" % show_cmd(side.put_cmd(local_file(project, name, e["path"]), large_rel(name, e["path"]))))
        if len(large) > 5:
            out("... and %d more large files" % (len(large) - 5))
        out("would run: %s" % show_cmd(side.put_cmd("<temp>/%s.json" % base, sidecar_rel(name))))
        return done
    if remote_sidecar is False:
        remote_sidecar = parse_json(side.cat(sidecar_rel(name), args.timeout))
    remote = remote_sidecar or {}
    if remote.get("fingerprint") == fp_all:
        out("%s: unchanged since the last push (fingerprint %s); skipped" % (name, fp_all[:12]))
        return done
    failed = dict(done, rc=1)
    remote_toc = dict((e.get("path"), e) for e in (load_toc(remote, side, args.timeout) or []) if isinstance(e, dict))
    tmp = current().mkdtemp("agent-mirror-archive-")
    try:
        archive_bytes = (remote.get("bundle") or {}).get("archive_bytes", 0)
        if small and (remote.get("bundle_fingerprint") != fp_bundle or not remote.get("bundle")):
            if not disk_guard(
                tmp, small_bytes, BUILD_MARGIN, False, label="a 16 MiB margin (set TMPDIR to a bigger disk)"
            ):
                return failed
            path = os.path.join(tmp, base + ".tar.gz")
            method = build_bundle(project, name, small, path, args.transfer_timeout)
            archive_bytes = os.path.getsize(path)
            ok, detail = side.put(path, bundle_rel(name), args.transfer_timeout)
            if not ok:
                err("upload of the %s bundle failed: %s" % (name, detail))
                return failed
            os.remove(path)
            out(
                "uploaded %s (%d files, %s packed to %s with %s)"
                % (side.where(bundle_rel(name)), len(small), human(small_bytes), human(archive_bytes), method)
            )
        elif small:
            out("%s: the bundle is unchanged; only the large files or the table of contents changed" % name)
        sent = 0
        for e in large:
            r = remote_toc.get(e["path"]) or {}
            if r.get("in") == "large" and r.get("size") == e["size"] and r.get("mtime") == e["mtime"]:
                continue
            ok, detail = side.put(local_file(project, name, e["path"]), large_rel(name, e["path"]), args.transfer_timeout)
            if not ok:
                err("upload of %s/%s failed: %s" % (name, e["path"], detail))
                return failed
            sent += 1
        if large:
            out(
                "uploaded %d of %d large file(s) to %s (the rest were unchanged)"
                % (sent, len(large), side.where("%s/%s.large" % (ARCHIVE_DIR, name)))
            )
        sidecar, toc_gz = make_sidecar(name, entries, small, large, s["large_bytes"], archive_bytes, s["toc_inline"])
        if toc_gz is not None:
            toc_path = os.path.join(tmp, base + ".toc.json.gz")
            with open(toc_path, "wb") as fh:
                fh.write(toc_gz)
            ok, detail = side.put(toc_path, toc_rel(name), args.transfer_timeout)
            if not ok:
                err("upload of the %s table of contents failed: %s" % (name, detail))
                return failed
        side_path = os.path.join(tmp, base + ".json")
        with open(side_path, "w", encoding="utf-8") as fh:
            json.dump(sidecar, fh, indent=1)
        ok, detail = side.put(side_path, sidecar_rel(name), args.transfer_timeout)
        if not ok:
            err("upload of the %s sidecar failed: %s" % (name, detail))
            return failed
        out("%s: pushed (fingerprint %s)" % (name, fp_all[:12]))
        return dict(done, uploaded=True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# index --------------------------------------------------------------------------------------


def index_entries(side, names, index_dir, timeout):
    """Index entries for archives, writing each table of contents to <index dir>/box_toc/<name>.json[.gz].

    names are candidate sidecar names from a listing; a candidate whose file is not a sidecar for that
    name (a large file that happens to end in .json) is left out quietly.
    """
    toc_dir = os.path.join(index_dir, "box_toc")
    entries, written = [], set()
    names = sorted(set(names))
    sidecars = read_sidecars(side, names, timeout)  # one batched read, not one per archive
    tocs = read_tocs(side, [sc for sc in sidecars.values() if sc and sc.get("toc") is None and sc.get("toc_file")], timeout)
    for name in names:
        sc = sidecars.get(name)
        if sc is None:
            warn("cannot read the sidecar of archive %s; left out of the index" % name)
            continue
        if sc.get("name") != name:
            continue
        toc = sc["toc"] if sc.get("toc") is not None else tocs.get(sc.get("toc_file"))
        large = sc.get("large") or {}
        entry = {
            "path": name,
            "archive": True,
            "files": sc.get("files"),
            "bytes": sc.get("bytes"),
            "archive_bytes": sc.get("archive_bytes"),
            "large_files": large.get("files", 0),
            "large_bytes": large.get("bytes", 0),
            "modtime": sc.get("newest_mtime"),
            "fingerprint": sc.get("fingerprint"),
        }
        if toc is not None:
            data = json.dumps(toc, separators=(",", ":")).encode("utf-8")
            fname = name + (".json.gz" if len(data) > INDEX_TOC_GZIP else ".json")
            path = os.path.join(toc_dir, *fname.split("/"))
            os.makedirs(os.path.dirname(path), exist_ok=True)
            if fname.endswith(".gz"):
                with gzip.open(path, "wb") as fh:
                    fh.write(data)
            else:
                with open(path, "wb") as fh:
                    fh.write(data + b"\n")
            entry["toc"] = "box_toc/" + fname
            written.add(fname)
        entries.append(entry)
    if os.path.isdir(toc_dir):
        for root, dirs, files in os.walk(toc_dir, topdown=False):
            for f in files:
                rel = os.path.relpath(os.path.join(root, f), toc_dir).replace(os.sep, "/")
                if f.endswith((".json", ".json.gz")) and rel not in written:
                    os.remove(os.path.join(root, f))
            if root != toc_dir:
                try:
                    os.rmdir(root)  # only when empty
                except OSError:
                    pass
    return entries


def read_local_toc(index_dir, entry):
    rel = entry.get("toc")
    if not rel:
        return None
    path = os.path.join(index_dir, *rel.split("/"))
    try:
        if path.endswith(".gz"):
            with gzip.open(path, "rb") as fh:
                return json.loads(fh.read().decode("utf-8"))
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


# fetch --------------------------------------------------------------------------------------


def safe_member(name, top):
    """The normalised member path when it is safe and inside <top>/, else None."""
    n = name.replace("\\", "/")
    if n.startswith("/") or re.match(r"^[A-Za-z]:", n) or ".." in n.split("/"):
        return None
    n = posixpath.normpath(n)
    if n == top or n.startswith(top + "/"):
        return n
    return None


def safe_extract(bundle, project_path, top, wanted, overwrite):
    """Extract the members named in wanted (set of '<top>/<path>') from a bundle.

    Rejects absolute names, '..', anything outside <top>/, device files, and
    links that point outside <top>/. Returns (written paths, kept count, rejected count).
    """
    root = os.path.realpath(project_path)
    written, kept, rejected = [], 0, 0
    with tarfile.open(bundle, "r:*") as tf:
        for m in tf:
            n = safe_member(m.name, top)
            if n is None:
                rejected += 1
                continue
            if m.isdir() or (wanted is not None and n not in wanted):
                continue
            dest = os.path.join(root, *n.split("/"))
            parent = os.path.dirname(dest)
            os.makedirs(parent, exist_ok=True)
            real_parent = os.path.realpath(parent)
            if not within(real_parent, root):
                rejected += 1
                continue
            if os.path.lexists(dest) and not overwrite:
                kept += 1
                continue
            if m.issym() or m.islnk():
                if m.issym():
                    target = posixpath.normpath(posixpath.join(posixpath.dirname(n), m.linkname))
                else:
                    target = posixpath.normpath(m.linkname)
                if m.linkname.startswith("/") or not target.startswith(top + "/"):
                    rejected += 1
                    continue
                try:
                    if os.path.lexists(dest):
                        os.remove(dest)
                    if m.issym():
                        os.symlink(m.linkname, dest)
                    else:
                        shutil.copy2(os.path.join(root, *target.split("/")), dest)
                    written.append(n)
                except OSError:
                    rejected += 1
                continue
            if not m.isfile():
                rejected += 1
                continue
            src = tf.extractfile(m)
            part = dest + ".agent-mirror-part"
            with open(part, "wb") as fh:
                shutil.copyfileobj(src, fh)
            os.replace(part, dest)
            os.utime(dest, (m.mtime, m.mtime))
            if m.mode & 0o111:
                os.chmod(dest, 0o755)
            written.append(n)
    return written, kept, rejected


def fetch_archive(args, s, runtime, side, rel, top):
    """Fetch from an archived folder. Returns an exit code, or None to fall back to a plain fetch."""
    cloud = runtime in CLOUD_RUNTIMES
    project = s["project_path"]
    sub = rel[len(top) + 1 :] if rel != top else ""
    want_all = getattr(args, "all", False)
    if args.dry_run:
        out("dry run: nothing will change")
        out("would read %s (sizes and the table of contents)" % side.where(sidecar_rel(top)))
        out(
            "then download %s and extract %s into %s (%s)"
            % (
                side.where(bundle_rel(top)),
                "/".join([top, sub]) if sub else top + "/ (everything in the bundle)",
                project,
                "overwriting local files" if cloud else "never overwriting existing local files",
            )
        )
        out(
            "large files under it: %s"
            % (
                "fetched one by one from %s" % side.where("%s/%s.large/" % (ARCHIVE_DIR, top))
                if want_all
                else "listed with their fetch commands, not fetched (pass --all)"
            )
        )
        return 0
    sc = parse_json(side.cat(sidecar_rel(top), args.timeout))
    if sc is None:
        out("no archive of %s in the box (%s is missing); fetching files one by one" % (top, sidecar_rel(top)))
        return None
    toc = load_toc(sc, side, args.timeout)
    if toc is None:
        err("the archive of %s has no readable table of contents; run `agent-mirror box push` again" % top)
        return 1
    under = [e for e in toc if not sub or e["path"] == sub or e["path"].startswith(sub + "/")]
    if not under:
        err("nothing found at %s in the archive of %s" % (rel, top))
        return 1
    exact_large = len(under) == 1 and under[0]["path"] == sub and under[0]["in"] == "large"
    bundle_part = [e for e in under if e["in"] == "bundle"]
    large_part = [e for e in under if e["in"] == "large"]
    fetch_large = large_part if (want_all or exact_large) else []
    skip_large = [] if (want_all or exact_large) else large_part

    def local(e):
        return local_file(project, top, e["path"])

    if not cloud:
        bundle_todo = [e for e in bundle_part if not os.path.lexists(local(e))]
        large_todo = [e for e in fetch_large if not os.path.lexists(local(e))]
    else:
        bundle_todo, large_todo = list(bundle_part), list(fetch_large)
    kept = len(bundle_part) + len(fetch_large) - len(bundle_todo) - len(large_todo)
    need = sum(e["size"] for e in bundle_todo + large_todo)
    if bundle_todo:
        need += int((sc.get("bundle") or {}).get("archive_bytes") or 0)
    if bundle_todo or large_todo:
        if not disk_guard(project, need, s["min_free"], getattr(args, "force", False)):
            return 1
    record = FetchRecord(project)
    stage = tempfile.mkdtemp(prefix=".agent-mirror-fetch-", dir=project)
    written = []
    rejected = 0
    try:
        if bundle_todo:
            path = os.path.join(stage, posixpath.basename(top) + ".tar.gz")
            out("downloading %s" % side.where(bundle_rel(top)))
            got = side.get(bundle_rel(top), path, args.transfer_timeout)
            if got != "ok":
                err("cannot download the bundle of %s: %s" % (top, got))
                return 1
            wanted = set(top + "/" + e["path"] for e in bundle_todo)
            names, kept_x, rejected = safe_extract(path, project, top, wanted, overwrite=cloud)
            kept += kept_x
            for n in names:
                record.add(n, "bundle")
            written += names
            os.remove(path)
        for e in large_todo:
            tmp = os.path.join(stage, "large")
            got = side.get(large_rel(top, e["path"]), tmp, args.transfer_timeout)
            if got != "ok":
                err("cannot download %s/%s: %s" % (top, e["path"], got))
                return 1
            dest = local(e)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            os.replace(tmp, dest)
            os.utime(dest, (e["mtime"], e["mtime"]))
            n = top + "/" + e["path"]
            record.add(n, "large")
            written.append(n)
    finally:
        shutil.rmtree(stage, ignore_errors=True)
        if written:
            record.save()
    out(
        "fetched %d file(s) (%s) into %s"
        % (
            len(written),
            human(sum(e["size"] for e in bundle_todo + large_todo)),
            os.path.join(project, *(top + "/" + sub if sub else top).split("/")),
        )
    )
    if kept:
        out("kept %d existing local file(s) (fetch never overwrites on a %s runtime)" % (kept, runtime))
    if rejected:
        warn("left out %d unsafe bundle member(s) (absolute paths, '..', links pointing outside, devices)" % rejected)
    if skip_large:
        total = sum(e["size"] for e in skip_large)
        out(
            "not fetched: %d large file(s), %s in all. Fetch one with the command shown, or all with --all:"
            % (len(skip_large), human(total))
        )
        project_flag = " --project %s" % shlex.quote(s["project"]) if s.get("project") else ""
        for e in skip_large[:LIST_LIMIT]:
            path = top + "/" + e["path"]
            here = " (here)" if os.path.lexists(local(e)) else ""
            out(
                "  %10s  %s%s   agent-mirror box fetch %s%s"
                % (human(e["size"]), path, here, shlex.quote(path), project_flag)
            )
        if len(skip_large) > LIST_LIMIT:
            out("  ... and %d more (agent-mirror box ls %s)" % (len(skip_large) - LIST_LIMIT, shlex.quote(rel)))
    return 0


# send --archive -----------------------------------------------------------------------------


def send_archive(args, s, runtime, side, rel, sid):
    """Send a folder as one .tar.gz into the session sid. Returns (exit code, manifest facts or None)."""
    project = s["project_path"]
    src = os.path.join(project, *rel.split("/"))
    if not os.path.isdir(src):
        err("--archive takes a folder; %s is not one" % rel)
        return 2, None
    entries, skipped = scan(project, rel, s["rules"])
    if not entries:
        out("nothing to upload in %s" % rel)
        return 0, None
    total = sum(e["size"] for e in entries)
    dest = "%s/%s.tar.gz" % (sid, rel)
    out(
        "%s: %d files (%s) as one archive; left out %d env files, %d excluded, %d links"
        % (rel, len(entries), human(total), skipped["env"], skipped["excluded"], skipped["links"])
    )
    if args.dry_run:
        out("dry run: nothing will change")
        out("would run: %s" % show_cmd(side.put_cmd("<temp>/%s.tar.gz" % posixpath.basename(rel), dest)))
        return 0, None
    tmp = current().mkdtemp("agent-mirror-send-")
    try:
        if not disk_guard(tmp, total, BUILD_MARGIN, getattr(args, "force", False), label="a 16 MiB margin"):
            return 1, None
        path = os.path.join(tmp, posixpath.basename(rel) + ".tar.gz")
        build_bundle(project, rel, entries, path, args.transfer_timeout)
        out("running: %s" % show_cmd(side.put_cmd(path, dest)))
        ok, detail = side.put(path, dest, args.transfer_timeout)
        if not ok:
            err("upload failed: %s" % detail)
            return 1, None
        size = os.path.getsize(path)
        out("uploaded %s (%s compressed)" % (side.where(dest), human(size)))
        newest = max(e["mtime"] for e in entries)
        return 0, {"files": len(entries), "bytes": total, "archive_bytes": size, "mtime": iso(newest)}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
