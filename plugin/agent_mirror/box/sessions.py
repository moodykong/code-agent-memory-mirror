"""The sessions side of the box: one namespace per session that every machine and session can read.

    <sessions>/<session id>/<path>             what `box send` uploaded in that session
    <sessions>/<session id>/<folder>.tar.gz    a folder sent with `box send --archive`
    <sessions>/<session id>/_session.json      the manifest

A session id is <runtime>-<YYYYMMDDTHHMMSSZ>-<6 hex>. The first `box send` of a session makes one and
remembers it in <project>/.agent-mirror/session.json (git-excluded). Every later send of the same session
writes into the same namespace. A cloud runtime starts a new session on a new machine; a developer runtime
starts one with --new-session or when the remembered one is older than 24 hours.

The manifest names the session (label, runtime, machine, project, repo, git branch and commit at the first
send, times, tool) and lists its files. It is updated after each successful send the way _owners.json is:
read, merge, write, read back, try again. It never holds environment values or absolute paths. Since 0.5.3
it may hold notes (notes.py): `note`, the latest note given with `box send --note`, and `notes`, one
{"at", "note", "paths"} entry per note; both are optional, so older readers ignore them.

Cloud sessions never write the shared tree. A developer machine copies what is worth keeping into it with
`box promote`, through its own copy of the folder and the normal ownership of box push, and removes old
sessions with `box prune`. Older namespaces (<runtime>-<YYYYMMDDTHHMMSSZ>, from versions before 0.5.0) have
no manifest; they are listed from their files.
"""

import argparse
import datetime
import json
import os
import re
import shlex
import shutil
import socket
import tarfile
import tempfile

from agent_mirror import __version__, machine
from agent_mirror.box import archive, notes, units
from agent_mirror.box.local import STATE_DIR, FetchRecord, disk_guard, ensure_git_exclude, human
from agent_mirror.box.rules import clean_rel, folder_files
from agent_mirror.box.sides import side_for
from agent_mirror.config import load_user_config
from agent_mirror.platforms import current
from agent_mirror.runtime import CLOUD_RUNTIMES, RUNTIMES
from agent_mirror.util import err, out, run, scrub, utc_stamp, warn, within, write_text

MANIFEST = "_session.json"
LOCAL_FILE = "session.json"
SCHEMA = 1
TRIES = 3
DEV_HOURS = 24
LIST_LIMIT = 20
READ_CAP = 500
INDEX_CAP = 50
FETCH_DIR = "_sessions"
TOOL_FOR_BACKEND = {"azure-blob": "az", "rclone": "rclone"}
_RUNTIMES = "|".join(re.escape(r) for r in RUNTIMES)
ID_RX = re.compile(r"^(%s)-(\d{8}T\d{6}Z)-([0-9a-f]{6})$" % _RUNTIMES)
OLD_RX = re.compile(r"^(%s)-(\d{8}T\d{6}Z)$" % _RUNTIMES)
LABEL_RX = re.compile(r"^[A-Za-z0-9._-]{1,60}$")


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_utc(text):
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y%m%dT%H%M%SZ"):
        try:
            return datetime.datetime.strptime(str(text or ""), fmt).replace(tzinfo=datetime.timezone.utc)
        except ValueError:
            continue
    return None


def looks_like_session(name):
    return bool(ID_RX.match(name or "") or OLD_RX.match(name or ""))


def started_of(name):
    m = ID_RX.match(name or "") or OLD_RX.match(name or "")
    return parse_utc(m.group(2)) if m else None


def runtime_of(name):
    m = ID_RX.match(name or "") or OLD_RX.match(name or "")
    return m.group(1) if m else None


def newest_first(names):
    epoch = datetime.datetime(1970, 1, 1, tzinfo=datetime.timezone.utc)
    return sorted(names, key=lambda n: (started_of(n) or epoch, n), reverse=True)


def label_problem(label):
    if label is not None and not LABEL_RX.match(label):
        return "a session label is 1 to 60 letters, digits, '-', '_' or '.' (got %r)" % (label,)
    return None


def iso(ts):
    return units.iso(ts)


# this session, on this machine -------------------------------------------------------------------


def local_record(project):
    return os.path.join(project, STATE_DIR, LOCAL_FILE)


def load_local(project):
    try:
        with open(local_record(project), encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_local(project, data):
    write_text(local_record(project), json.dumps(data, indent=1, sort_keys=True) + "\n")
    ensure_git_exclude(project, os.path.join(project, STATE_DIR))


def current_session(project, runtime, new=False, label=None, dry_run=False):
    """(this session {"id", "label", "runtime", "started", ...}, True when it starts now).

    Nothing is saved in a dry run.
    """
    rec = load_local(project)
    host = socket.gethostname()
    reuse = bool(rec.get("id")) and looks_like_session(rec.get("id")) and rec.get("runtime") == runtime and not new
    if reuse and runtime in CLOUD_RUNTIMES and rec.get("host") != host:
        reuse = False  # a new cloud machine is a new session
    if reuse and runtime not in CLOUD_RUNTIMES:
        started = parse_utc(rec.get("started"))
        age = datetime.datetime.now(datetime.timezone.utc) - started if started else None
        if age is None or age > datetime.timedelta(hours=DEV_HOURS):
            reuse = False
    if reuse:
        if label and not rec.get("label"):
            rec["label"] = label
        elif label and rec.get("label") != label:
            warn(
                "this session already has the label %s; kept it (start another session with --new-session)"
                % rec.get("label")
            )
        rec["used"] = utc_now()
        if not dry_run:
            save_local(project, rec)
        return rec, False
    now = utc_now()
    rec = {
        "id": "%s-%s-%s" % (runtime, utc_stamp(), os.urandom(3).hex()),
        "label": label,
        "runtime": runtime,
        "host": host,
        "started": now,
        "used": now,
    }
    if not dry_run:
        save_local(project, rec)
    return rec, True


# the manifest -----------------------------------------------------------------------------------


def git_info(project):
    """{"branch", "commit"} of the project clone, from read-only git calls; None values when unknown."""
    rc, branch, _ = run(["git", "-C", project, "rev-parse", "--abbrev-ref", "HEAD"], timeout=10)
    rc2, commit, _ = run(["git", "-C", project, "rev-parse", "HEAD"], timeout=10)
    return {
        "branch": branch.strip() if rc == 0 and branch.strip() else None,
        "commit": commit.strip() if rc2 == 0 and re.match(r"^[0-9a-f]{7,64}$", commit.strip()) else None,
    }


def new_manifest(s, session, runtime):
    mid, mname = machine.identity(load_user_config(current()))
    return {
        "schema": SCHEMA,
        "id": session["id"],
        "label": session.get("label"),
        "runtime": runtime,
        "machine": {"id": mid, "name": mname} if (mid or mname) else None,
        "project": s.get("project"),
        "repo": s.get("repo"),
        "git": git_info(s["project_path"]) if os.path.isdir(s["project_path"]) else {"branch": None, "commit": None},
        "started": session.get("started") or utc_now(),
        "updated": None,
        "tool": "agent-mirror %s" % __version__,
        "files": [],
        "promoted": [],
    }


def bare_manifest(sid):
    """For a namespace without a manifest (an older version wrote it), when something must be recorded."""
    started = started_of(sid)
    return {
        "schema": SCHEMA,
        "id": sid,
        "label": None,
        "runtime": runtime_of(sid),
        "machine": None,
        "project": None,
        "repo": None,
        "git": {"branch": None, "commit": None},
        "started": started.strftime("%Y-%m-%dT%H:%M:%SZ") if started else None,
        "updated": None,
        "tool": "agent-mirror %s" % __version__,
        "files": [],
        "promoted": [],
    }


def read_manifest(side, sid, timeout):
    data = archive.parse_json(side.cat("%s/%s" % (sid, MANIFEST), timeout))
    return data if (data and data.get("id") == sid) else None


def put_json(side, rel, data, timeout):
    tmp = current().mkdtemp("agent-mirror-session-")
    try:
        path = os.path.join(tmp, MANIFEST)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=1, sort_keys=True)
            fh.write("\n")
        return side.put(path, rel, timeout)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def update_manifest(
    side, sid, base, timeout, files=(), promoted=None, label=None, new=False, note=None, note_paths=()
):
    """Read, merge, write and read back the manifest. Returns (ok, problem or None, the manifest).

    new: the session started in this command, so its namespace has no manifest yet (the first read is skipped).
    note: this send's note, recorded on note_paths (the sent paths); merged into what the manifest holds now.
    """
    doc = None
    for attempt in range(TRIES):
        cur = None if (new and attempt == 0) else read_manifest(side, sid, timeout)
        doc = dict(cur) if cur else dict(base)
        doc["schema"] = SCHEMA
        doc["tool"] = "agent-mirror %s" % __version__
        doc["updated"] = utc_now()
        if label and not doc.get("label"):
            doc["label"] = label
        by_path = dict((f["path"], f) for f in doc.get("files") or [] if isinstance(f, dict) and f.get("path"))
        for f in files:
            by_path[f["path"]] = f
        doc["files"] = [by_path[k] for k in sorted(by_path)]
        if promoted:
            doc["promoted"] = list(doc.get("promoted") or []) + [promoted]
        notes.add_note(doc, note, note_paths, doc["updated"])
        ok, detail = put_json(side, "%s/%s" % (sid, MANIFEST), doc, timeout)
        if not ok:
            return False, "cannot write %s: %s" % (scrub(side.where("%s/%s" % (sid, MANIFEST))), detail), doc
        if read_manifest(side, sid, timeout) == doc:
            return True, None, doc
        if attempt + 1 < TRIES:
            warn("the session manifest changed while it was written; trying again")
    return False, "could not update %s after %d tries" % (scrub(side.where("%s/%s" % (sid, MANIFEST))), TRIES), doc


def sent_entries(s, rel, archive_info=None):
    """The manifest entries for one finished send: files, or one entry for a folder sent as an archive."""
    src = os.path.join(s["project_path"], *rel.split("/"))
    if archive_info:
        return [dict(archive_info, path=rel, object=rel + ".tar.gz", archive=True)]
    if os.path.isdir(src):
        keep, _ = folder_files(src, rel, s["rules"])
        found = []
        for sub in keep:
            try:
                st = os.stat(os.path.join(src, *sub.split("/")))
            except OSError:
                continue
            found.append({"path": rel + "/" + sub, "bytes": st.st_size, "mtime": iso(st.st_mtime)})
        return found
    try:
        st = os.stat(src)
    except OSError:
        return []
    return [{"path": rel, "bytes": st.st_size, "mtime": iso(st.st_mtime)}]


# listing --------------------------------------------------------------------------------------------


class View:
    """What one command learns about the sessions side, kept so that no call is made twice.

    One recursive listing gives every namespace, every file and its size; the manifests are read in one
    batched call (rclone: one `copy --files-from`; azure-blob: one `download-batch` with a pattern). So the
    number of transfer calls of a sessions command does not grow with the number of sessions.
    """

    def __init__(self, side, timeout):
        self.side = side
        self.timeout = timeout
        self._listing = None
        self._listed = False
        self.manifests = {}  # session id -> manifest dict, or None when it has none

    def listing(self):
        """[{path, size, modtime}] of every file on the sessions side (one call), or None on an error."""
        if not self._listed:
            listed = self.side.list("", self.timeout)
            self._listed = True
            self._listing = None if listed is None else [f for f in listed if f.get("path")]
        return self._listing

    def names(self):
        """(session namespaces newest first, count of other top-level folders), from the listing."""
        tops = set(f["path"].split("/", 1)[0] for f in self._listing or [] if "/" in f["path"])
        good = [n for n in tops if looks_like_session(n)]
        return newest_first(good), len(tops) - len(good)

    def files(self, sid):
        cut = len(sid) + 1
        return [dict(f, path=f["path"][cut:]) for f in self._listing or [] if f["path"].startswith(sid + "/")]

    def read(self, ids):
        """{id: manifest or None}, reading the ones not read yet in one batched call."""
        need = [i for i in ids if i not in self.manifests]
        names = set(f["path"] for f in self._listing) if self._listing is not None else None
        rels = [i + "/" + MANIFEST for i in need if names is None or i + "/" + MANIFEST in names]
        for i in need:
            self.manifests[i] = None
        if rels:
            tmp = current().mkdtemp("agent-mirror-manifests-")
            try:
                found, problem = self.side.get_present("", rels, tmp, self.timeout, listed=self._listing)
                if problem:
                    warn("cannot read every session manifest: %s" % problem)
                for rel in found:
                    sid = rel.split("/", 1)[0]
                    try:
                        with open(os.path.join(tmp, sid, MANIFEST), encoding="utf-8") as fh:
                            data = json.load(fh)
                    except (OSError, ValueError):
                        continue
                    if isinstance(data, dict) and data.get("id") == sid:
                        self.manifests[sid] = data
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
        return dict((i, self.manifests.get(i)) for i in ids)


def summary_of(view, sid):
    """One row for `box sessions`: from the manifest (read beforehand with view.read), or from the files of
    an older namespace in the listing."""
    m = view.manifests.get(sid)
    if m:
        files = [f for f in m.get("files") or [] if isinstance(f, dict)]
        git = m.get("git") or {}
        said = notes.manifest_notes(m)
        return {
            "id": sid,
            "label": m.get("label"),
            "runtime": m.get("runtime") or runtime_of(sid),
            "machine": (m.get("machine") or {}).get("name"),
            "branch": git.get("branch"),
            "commit": git.get("commit"),
            "files": sum(int(f.get("files") or 1) if f.get("archive") else 1 for f in files),
            "bytes": sum(int(f.get("bytes") or 0) for f in files),
            "started": m.get("started"),
            "updated": m.get("updated"),
            "promoted": len(m.get("promoted") or []),
            "manifest": True,
            "note": (
                m["note"] if isinstance(m.get("note"), str) and m["note"].strip() else (said[-1][2] if said else None)
            ),
            "notes": len(said),
        }
    listed = view.files(sid)
    stamps = sorted(str(f.get("modtime") or "") for f in listed if f.get("modtime"))
    started = started_of(sid)
    return {
        "id": sid,
        "label": None,
        "runtime": runtime_of(sid),
        "machine": None,
        "branch": None,
        "commit": None,
        "files": len(listed),
        "bytes": sum(int(f.get("size") or 0) for f in listed),
        "started": started.strftime("%Y-%m-%dT%H:%M:%SZ") if started else None,
        "updated": stamps[-1] if stamps else None,
        "promoted": 0,
        "manifest": False,
        "note": None,
        "notes": 0,
    }


def tool_ready(s):
    return bool(shutil.which(TOOL_FOR_BACKEND[s["backend"]]))


def from_index(index_path):
    try:
        with open(index_path, encoding="utf-8") as fh:
            for line in fh:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if isinstance(r, dict) and isinstance(r.get("sessions"), list):
                    return r
    except OSError:
        pass
    return None


def cmd_sessions(args, s, runtime):
    side = side_for(s, "sessions")
    limit = max(1, args.limit)
    if not tool_ready(s):
        line = from_index(s["index_path"])
        if line is None:
            err(
                "%s is not available here and the index %s has no sessions; nothing to list"
                % (TOOL_FOR_BACKEND[s["backend"]], s["index_path"])
            )
            return 1
        warn("%s is not available here; listing the sessions recorded by the last `box index`" % TOOL_FOR_BACKEND[s["backend"]])
        rows = [dict(r, manifest=True) for r in line["sessions"][:limit]]
        total, others = len(line["sessions"]) + int(line.get("more") or 0), 0
        where = "the index %s" % s["index_path"]
    else:
        view = View(side, args.transfer_timeout)  # one call walks the whole side: the transfer timeout
        if view.listing() is None:
            err("cannot list %s" % scrub(side.where()))
            return 1
        names, others = view.names()
        total = len(names)
        view.read(names[:limit])
        rows = [summary_of(view, n) for n in names[:limit]]
        where = scrub(side.where())
    if args.json:
        out(json.dumps({"project": s["project"], "where": where, "total": total, "sessions": rows}, indent=2))
        return 0
    out(
        "sessions of %s on %s, newest first: %s"
        % (s["project"], where, "%d of %d" % (len(rows), total) if total > len(rows) else "%d" % total)
    )
    for r in rows:
        if not r.get("manifest"):
            git = "no manifest"
        elif r.get("branch") or r.get("commit"):
            git = "%s@%s" % (r.get("branch") or "?", (r.get("commit") or "?")[:7])
        else:
            git = "-"  # not sent from a git clone
        out(
            "  %-40s %-16s %-13s %-22s %5d files %10s  updated %s%s"
            % (
                r["id"],
                r.get("label") or "-",
                r.get("runtime") or "?",
                git,
                int(r.get("files") or 0),
                human(r.get("bytes")),
                r.get("updated") or "?",
                "  promoted: %d" % r["promoted"] if r.get("promoted") else "",
            )
        )
        if r.get("note"):
            out("      note: %s" % notes.shown(r["note"]))
            if int(r.get("notes") or 0) > 1:
                out(
                    "      (%d notes in this session; `agent-mirror box fetch --session %s --dry-run` prints them all)"
                    % (int(r["notes"]), r["id"])
                )
    if total > len(rows):
        out("(%d more; show them with --limit %d)" % (total - len(rows), total))
    if others:
        out("(%d other folder(s) on the sessions side are not session namespaces and are not listed)" % others)
    if rows:
        out("fetch one with: agent-mirror box fetch --session ID [PATH]   (ID: the id, a unique start of it, or a label)")
    return 0


def resolve(view, ident):
    """(session id, its files [{path, size, modtime}], None), or (None, None, problem).

    A full id is checked directly (one listing of that namespace, nothing about other sessions); a start of
    an id needs the one listing of the sessions side; a label also needs the one batched manifest read.
    """
    side = view.side
    if ID_RX.match(ident) or OLD_RX.match(ident):
        listed = side.list(ident, view.timeout)
        if listed is None:
            return None, None, "cannot list %s" % scrub(side.where(ident))
        listed = [f for f in listed if f.get("path")]
        if listed:
            return ident, listed, None
        if ID_RX.match(ident):
            return None, None, "no session %s in the box (see `agent-mirror box sessions`)" % ident
    if view.listing() is None:
        return None, None, "cannot list %s" % scrub(side.where())
    names, _ = view.names()
    starts = [n for n in names if n.startswith(ident)]
    if len(starts) == 1:
        return starts[0], view.files(starts[0]), None
    if len(starts) > 1:
        return None, None, "%s matches %d sessions (%s); give more of the id" % (ident, len(starts), ", ".join(starts[:5]))
    if LABEL_RX.match(ident):
        found = view.read(names[:READ_CAP])
        hits = [n for n in names[:READ_CAP] if (found[n] or {}).get("label") == ident]
        if len(hits) == 1:
            return hits[0], view.files(hits[0]), None
        if hits:
            return None, None, "the label %s is on %d sessions (%s); use an id" % (ident, len(hits), ", ".join(hits[:5]))
    return None, None, "no session matches %s (see `agent-mirror box sessions`)" % ident


def session_files(view, sid, listed):
    """(files without the manifest, archive entries {object: entry}, manifest) of one session."""
    manifest = view.read([sid])[sid]
    archives = {}
    for e in (manifest or {}).get("files") or []:
        if isinstance(e, dict) and e.get("archive") and e.get("object") and clean_rel(e.get("path") or ""):
            archives[e["object"]] = e
    files = [f for f in listed if f["path"] != MANIFEST and clean_rel(f["path"]) == f["path"]]
    return files, archives, manifest


def select(files, archives, rel):
    """(plain files under rel, [(archive entry, sub-path inside it or None)]) for a session."""
    plain = [f for f in files if f["path"] not in archives and (not rel or f["path"] == rel or f["path"].startswith(rel + "/"))]
    parts = []
    for obj, e in sorted(archives.items()):
        p = e["path"]
        if not rel or p == rel or p.startswith(rel + "/"):
            parts.append((e, None))
        elif rel.startswith(p + "/"):
            parts.append((e, rel))
    return plain, parts


def wanted_members(bundle, top, sub):
    """Member names of a session archive under sub (all under top when sub is None)."""
    want = sub or top
    with tarfile.open(bundle, "r:*") as tf:
        return set(n for n in tf.getnames() if n == want or n.startswith(want + "/"))


def download(side, sid, plain, parts, stage, timeout, overwrite, listed=None):
    """Download into stage, in one call: plain files at their paths, and the archives, which are then
    extracted (safely) at their paths. Returns (list of relative paths now in stage, problem or None)."""
    objects = [e["object"] for e, _ in parts]
    got = side.get_many(sid, [f["path"] for f in plain] + objects, stage, timeout, listed)
    if got != "ok":
        return [], "cannot download from %s: %s" % (scrub(side.where(sid)), got)
    done = [f["path"] for f in plain]
    for e, sub in parts:
        bundle = os.path.join(stage, *e["object"].split("/"))
        try:
            wanted = wanted_members(bundle, e["path"], sub)
            names, _, rejected = archive.safe_extract(bundle, stage, e["path"], wanted, overwrite=overwrite)
        except (OSError, tarfile.TarError) as exc:
            return done, "cannot read the archive %s: %s" % (e["object"], exc)
        finally:
            try:
                os.remove(bundle)
            except OSError:
                pass
        if rejected:
            warn("left out %d unsafe member(s) of %s (absolute paths, '..', links pointing outside, devices)" % (rejected, e["object"]))
        done += names
    return done, None


def into_dir(s, args, sid):
    """Where a session fetch writes: <project>/_sessions/<id>, or --into (relative to the project folder)."""
    project = s["project_path"]
    into = getattr(args, "into", None)
    if not into:
        return os.path.join(project, FETCH_DIR, sid)
    path = current().expand_user(into)
    return os.path.normpath(path if os.path.isabs(path) else os.path.join(project, *into.replace("\\", "/").split("/")))


def cmd_fetch(args, s, runtime):
    """box fetch --session ID [PATH]."""
    cloud = runtime in CLOUD_RUNTIMES
    side = side_for(s, "sessions")
    project = s["project_path"]
    rel = ""
    if args.path:
        rel = clean_rel(args.path)
        if rel is None:
            err("box fetch takes a path inside the session, without '..' (got %r)" % args.path)
            return 2
    if args.dry_run:
        out("dry run: nothing will change")
    view = View(side, args.transfer_timeout)
    sid, listed, problem = resolve(view, args.session)
    if problem:
        err(problem)
        return 1
    files, archives, manifest = session_files(view, sid, listed)
    plain, parts = select(files, archives, rel)
    if not plain and not parts:
        err("nothing at %s in the session %s" % (rel or "the top", sid))
        return 1
    dest = into_dir(s, args, sid)
    exists = lambda p: os.path.lexists(os.path.join(dest, *p.split("/")))  # noqa: E731
    todo = [f for f in plain if cloud or not exists(f["path"])]
    kept = len(plain) - len(todo)
    sizes = dict((f["path"], int(f.get("size") or 0)) for f in files)
    need = sum(int(f.get("size") or 0) for f in todo)
    need += sum(sizes.get(e["object"], 0) + int(e.get("bytes") or 0) for e, _ in parts)
    out(
        "session %s%s: %d file(s)%s into %s"
        % (
            sid,
            " (%s)" % manifest["label"] if manifest and manifest.get("label") else "",
            len(todo),
            " and %d archive(s)" % len(parts) if parts else "",
            dest,
        )
    )
    for line in notes.note_lines(manifest):
        out(line)
    if args.dry_run:
        out("would download them from %s (%s; %s)" % (scrub(side.where(sid)), human(need), "overwriting local files" if cloud else "never overwriting existing local files"))
        return 0
    if (todo or parts) and not disk_guard(nearest_dir(dest), need, s["min_free"], getattr(args, "force", False)):
        return 1
    os.makedirs(dest, exist_ok=True)
    if within(os.path.realpath(dest), os.path.realpath(os.path.join(project, FETCH_DIR))):
        ensure_git_exclude(project, os.path.join(project, FETCH_DIR))
    stage = tempfile.mkdtemp(prefix=".agent-mirror-fetch-", dir=dest)
    record = FetchRecord(project) if within(os.path.realpath(dest), os.path.realpath(project)) else None
    written = []
    try:
        got, problem = download(side, sid, todo, parts, stage, args.transfer_timeout, overwrite=True, listed=listed)
        if problem:
            err(problem)
        for p in got:
            src = os.path.join(stage, *p.split("/"))
            target = os.path.join(dest, *p.split("/"))
            if not os.path.isfile(src) or os.path.islink(src):
                continue
            if os.path.lexists(target) and not cloud:
                kept += 1
                continue
            os.makedirs(os.path.dirname(target), exist_ok=True)
            os.replace(src, target)
            written.append(target)
            if record is not None:
                record.add(os.path.relpath(target, project).replace(os.sep, "/"), "session:%s" % sid)
        if problem:
            return 1
    finally:
        shutil.rmtree(stage, ignore_errors=True)
        if record is not None and written:
            record.save()
    out("fetched %d file(s) of the session %s into %s" % (len(written), sid, dest))
    if kept:
        out("kept %d existing local file(s) (fetch never overwrites on a %s runtime)" % (kept, runtime))
    return 0


def nearest_dir(path):
    p = os.path.abspath(path)
    while not os.path.isdir(p) and os.path.dirname(p) != p:
        p = os.path.dirname(p)
    return p


# promote ---------------------------------------------------------------------------------------------


def unit_for(path, layout_folders, globs, exclude):
    """The folder unit (archived or include folder) that would hold this shared path, or None."""
    return units.archive_unit_of(path, globs) or units.unit_of_file(path, layout_folders, globs, exclude)


def cmd_promote(args, s, runtime, backend, plat):
    """box promote --session ID PATH [--as FOLDER]: copy session files into the shared tree, owned here.

    It goes through the local disk: the files are downloaded into a temp folder, placed into this
    machine's copy of the folder (never over a different local file), and that folder unit is pushed
    with the normal ownership logic. The owning machine's copy stays the source of its folder, so a
    later push from here keeps the promoted files.
    """
    from agent_mirror.box import owners

    if runtime in CLOUD_RUNTIMES:
        err("refusing box promote on %s: only a developer machine writes the shared side" % runtime)
        return 2
    project = s["project_path"]
    if not os.path.isdir(project):
        err("project folder %s does not exist" % project)
        return 1
    role = machine.box_role(load_user_config(plat), s["project"])
    if role in ("reader", "none"):
        return owners.role_refusal(s["project"], role)
    rel = clean_rel(args.path or "")
    target = clean_rel(args.as_folder) if args.as_folder else rel
    if rel is None or target is None:
        err("box promote takes relative paths without '..' (got %r%s)" % (args.path, ", --as %r" % args.as_folder if args.as_folder else ""))
        return 2
    if args.dry_run:
        out("dry run: nothing will change")
    if not backend.ready(args.dry_run):
        return 1
    side = side_for(s, "sessions")
    view = View(side, args.transfer_timeout)
    sid, listed, problem = resolve(view, args.session)
    if problem:
        err(problem)
        return 1
    files, archives, manifest = session_files(view, sid, listed)
    plain, parts = select(files, archives, rel)
    if not plain and not parts:
        err("nothing at %s in the session %s" % (rel, sid))
        return 1

    def shared_path(p):
        return target + p[len(rel):] if p != rel else target

    layout = units.Layout(s)
    globs, exclude = s.get("archives") or [], s.get("exclude") or []
    probes = [shared_path(f["path"]) for f in plain] + [shared_path(sub or e["path"]) for e, sub in parts]
    unit_names, uncovered = set(), []
    for p in probes:
        u = unit_for(p, layout.folders, globs, exclude)
        if u is None and any(p == (sub or e["path"]) for e, sub in parts):
            # an archived folder: covered when an include folder holds it
            u = next((f["name"] for f in layout.folders if f["name"] != units.TOP and p.startswith(f["name"] + "/")), None)
        if u is None:
            uncovered.append(p)
        else:
            unit_names.add(u)
    if uncovered:
        top = target.split("/")[0]
        err(
            "%s is not covered by box.include or box.archives, so it has no place in the shared tree. Add "
            '"%s/**" to box.include, or an entry that matches %s to box.archives, in projects/%s/env.json'
            % (uncovered[0], top if top != target else target, target, s["project"])
        )
        return 2
    mid, mname = machine.identity(load_user_config(plat))
    shared = side_for(s, "shared")
    state, problem = owners.read_state(shared, s, args.timeout, args.transfer_timeout)
    if problem:
        err(problem)
        return 1
    for u in sorted(unit_names):
        cur = state.owner(u)
        if cur and cur.get("machine_id") != mid:
            err(
                "refusing to promote into %s: the folder %s is owned by %s. Ask that machine to promote it, "
                "or to run `agent-mirror box release %s`" % (target, u, owners.describe_owner(cur), u)
            )
            return 2
    # an archived folder is uploaded as one bundle of this machine's copy: refuse when the box holds files
    # of it that are not here, or the push would replace the archive with less than it has
    for u in sorted(unit_names):
        if units.archive_unit_of(u, globs) != u:
            continue
        sidecar = archive.parse_json(shared.cat(archive.sidecar_rel(u), args.timeout))
        if not sidecar:
            continue
        toc = archive.load_toc(sidecar, shared, args.timeout) or []
        missing = [e for e in toc if isinstance(e, dict) and not os.path.isfile(archive.local_file(project, u, e.get("path") or ""))]
        if missing:
            err(
                "refusing to promote into %s: the box holds the archived folder %s, and %d of its file(s) are not "
                "here (for example %s/%s). A push would replace the archive with less than it has; fetch it first "
                "with `agent-mirror box fetch %s --all`, then promote" % (target, u, len(missing), u, missing[0].get("path"), u)
            )
            return 2
    out(
        "promote %s from the session %s to %s in the shared tree (folder %s: %s)"
        % (rel, sid, target, ", ".join(sorted(unit_names)), ", ".join(
            "owned by this machine" if (state.owner(u) or {}).get("machine_id") == mid else "claimed by this machine"
            for u in sorted(unit_names)
        ))
    )
    if args.dry_run:
        out(
            "would download %d file(s) and %d archive(s), place them under %s in this project (never over a "
            "different local file), push %s, and record the promotion in %s"
            % (len(plain), len(parts), target, ", ".join(sorted(unit_names)), scrub(side.where("%s/%s" % (sid, MANIFEST))))
        )
        return 0
    stage = current().mkdtemp("agent-mirror-promote-")
    try:
        got, problem = download(side, sid, plain, parts, stage, args.transfer_timeout, overwrite=True, listed=listed)
        if problem:
            err(problem)
            return 1
        moves, conflicts, same = [], [], 0
        for p in got:
            src = os.path.join(stage, *p.split("/"))
            if not os.path.isfile(src) or os.path.islink(src):
                continue
            dest_rel = shared_path(p)
            if unit_for(dest_rel, layout.folders, globs, exclude) is None:
                err("%s is not covered by box.include or box.archives; nothing was placed" % dest_rel)
                return 2
            dest = os.path.join(project, *dest_rel.split("/"))
            if os.path.lexists(dest):
                if os.path.isfile(dest) and same_bytes(src, dest):
                    same += 1
                    continue
                conflicts.append(dest_rel)
                continue
            moves.append((src, dest))
        if conflicts:
            err(
                "nothing promoted: %d file(s) already exist here with other content, for example %s. "
                "Promote to another place with --as FOLDER, or move the local file first" % (len(conflicts), conflicts[0])
            )
            return 1
        for src, dest in moves:
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            shutil.copy2(src, dest)
        out("placed %d file(s) under %s in this project%s" % (len(moves), target, " (%d already the same)" % same if same else ""))
    finally:
        shutil.rmtree(stage, ignore_errors=True)
    push_args = argparse.Namespace(**vars(args))
    push_args.only = sorted(unit_names)
    push_args.take_over = None
    push_args.delete = False
    rc = owners.push(push_args, s, runtime, backend, plat)
    if rc != 0:
        err(
            "the files are in place here, but the push failed; run `agent-mirror box push --only %s` again"
            % " --only ".join(sorted(unit_names))
        )
        return rc
    mid, mname = machine.identity(load_user_config(plat))  # box push may have made the id just now
    entry = {
        "path": rel,
        "to": target,
        "folders": sorted(unit_names),
        "machine_id": mid,
        "machine_name": mname,
        "date": utc_now(),
    }
    ok, problem, _ = update_manifest(side, sid, manifest or bare_manifest(sid), args.timeout, promoted=entry)
    if not ok:
        err("promoted, but the session manifest could not record it: %s" % problem)
        return 1
    out("promoted %s of the session %s to %s; recorded in its manifest" % (rel, sid, target))
    return 0


def same_bytes(a, b):
    if os.path.getsize(a) != os.path.getsize(b):
        return False
    with open(a, "rb") as fa, open(b, "rb") as fb:
        while True:
            x, y = fa.read(1 << 20), fb.read(1 << 20)
            if x != y:
                return False
            if not x:
                return True


# prune -------------------------------------------------------------------------------------------------


def cmd_prune(args, s, runtime):
    if runtime in CLOUD_RUNTIMES:
        err("refusing box prune on %s: only a developer machine deletes sessions" % runtime)
        return 2
    ids = list(args.session or [])
    bad = [i for i in ids if not looks_like_session(i)]
    if bad:
        err("refusing: %s does not look like a session id (<runtime>-<YYYYMMDDTHHMMSSZ>-<6 hex>)" % bad[0])
        return 2
    if not ids and args.older_than is None:
        err("say which sessions to delete: --older-than DAYS and/or --session ID")
        return 2
    side = side_for(s, "sessions")
    view = View(side, args.transfer_timeout)
    if view.listing() is None:
        err("cannot list %s" % scrub(side.where()))
        return 1
    names, others = view.names()
    chosen = []
    for i in ids:
        if i in names:
            chosen.append(i)
        else:
            warn("%s is not in the box; skipped" % i)
    now = datetime.datetime.now(datetime.timezone.utc)
    if args.older_than is not None:
        cutoff = now - datetime.timedelta(days=float(args.older_than))
        found = view.read(names)
        for n in names:
            when = parse_utc((found[n] or {}).get("updated")) or started_of(n)
            if when and when < cutoff and n not in chosen:
                chosen.append(n)
    found = view.read(chosen)
    rows = []
    for n in newest_first(chosen):
        m = found[n]
        if args.keep_promoted and m and m.get("promoted"):
            out("kept %s: something in it was promoted (--keep-promoted)" % n)
            continue
        listed = view.files(n)
        rows.append((n, len(listed), sum(int(f.get("size") or 0) for f in listed), (m or {}).get("label"), (m or {}).get("updated")))
    if not rows:
        out("no session to delete")
        return 0
    total = sum(r[2] for r in rows)
    out("%s %d session(s), %s, on %s:" % ("would delete" if (args.dry_run or not args.yes) else "deleting", len(rows), human(total), scrub(side.where())))
    for n, count, size, label, updated in rows:
        out("  %-40s %-16s %5d files %10s  updated %s" % (n, label or "-", count, human(size), updated or "?"))
    if args.dry_run:
        out("dry run: nothing deleted")
        return 0
    if not args.yes:
        out("nothing deleted: run the same command with --yes to delete these sessions (the shared tree is never touched)")
        return 0
    doomed = [r[0] for r in rows]
    if any(not looks_like_session(n) for n in doomed):  # the listing only yields such names; checked again anyway
        err("refusing to delete names that do not look like session ids")
        return 2
    ok, detail = side.delete_trees(doomed, args.transfer_timeout)
    after = View(side, args.transfer_timeout)
    if after.listing() is None:
        warn("could not list %s again to check the deletion" % scrub(side.where()))
        left = set()
    else:
        left = set(n for n in after.names()[0] if n in doomed)
    if not ok:
        err("deleting failed: %s" % detail)
    for n in sorted(left):
        err("could not delete %s completely: %d file(s) are still there" % (n, len(after.files(n))))
    out("deleted %d session(s) (%s)" % (len(doomed) - len(left), human(total)))
    return 1 if (left or not ok) else 0


# box index -----------------------------------------------------------------------------------------------


def index_line(s, timeout):
    """A compact summary of the newest sessions for the committed index, or None when the side cannot be listed."""
    side = side_for(s, "sessions")
    view = View(side, timeout)
    if view.listing() is None:
        warn("cannot list %s; the index has no session summary" % scrub(side.where()))
        return None
    names, _ = view.names()
    if not names:
        return None
    view.read(names[:INDEX_CAP])
    rows = []
    for n in names[:INDEX_CAP]:
        r = summary_of(view, n)
        rows.append(
            dict(
                (k, r.get(k))
                for k in ("id", "label", "runtime", "branch", "commit", "updated", "files", "bytes", "note")
            )
        )
    return {"path": "_sessions", "sessions": rows, "more": max(0, len(names) - INDEX_CAP)}


def fetch_command(s, sid):
    flag = " --project %s" % shlex.quote(s["project"]) if s.get("project") else ""
    return "agent-mirror box fetch --session %s%s" % (sid, flag)


def describe_send(side, sid, created, label):
    return "session %s%s%s on %s" % (
        sid,
        " (new)" if created else "",
        ", label %s" % label if label else "",
        scrub(side.where(sid)),
    )

