"""box unsent and box skip: what this session made in the project clone that is persisted nowhere.

A cloud machine is thrown away after the session, so files a session produced and never sent are lost.
The program finds them, the agent judges each one (send it with `box send`, or declare it disposable with
`box skip`), and on a cloud machine a Stop hook (hooks.py, `hook stop-check`) makes sure the judging happens.

A file of the project clone is unsent when all of these hold:

- git does not track it: it is untracked or ignored (tracked files travel with git);
- its modification time is at or after the session baseline (with SLACK seconds of slack for coarse
  file-system clocks);
- it is not left out: env files (`.env`, `*.env`), links (never followed or listed), every folder named in
  BUILTIN_DIRS at any depth, nested git clones and worktrees (a folder with its own `.git`), the box's mount
  and receive folders inside the clone, the box.exclude patterns and the optional box.unsent_ignore
  patterns (the same pattern syntax);
- it is not a copy fetched from the box and unchanged since (<clone>/.agent-mirror/fetched.json);
- it was not sent in this session with the same size and modification time (`sent` in the local session
  record <clone>/.agent-mirror/session.json, which `box send` keeps);
- it is not under a path declared disposable with `box skip` (`skipped` in the same record).

Each unsent file is then classed (classify, Finder.finish). For the box: a file git ignores (any size), and an
untracked file that is large: above git_text_max_bytes, or binary (a NUL byte in its first SNIFF bytes, read
only for files between the two limits) and above git_binary_max_bytes, or one of the small untracked files of
a top-level group (a top-level folder, or a file at the top on its own) whose small untracked files together
exceed git_total_max_bytes. Left to git: every other untracked file; a commit-and-push stop hook of the vendor
asks for those, and the stop check here stays out of its way. Outside a git repo every file is for the box. A
limit of 0 leaves nothing to git under its rule.

On a cloud runtime, `box send` and `box skip` add the paths they dealt with to the clone's local git exclude
file (exclude_after), so that such a hook, which is assumed to look at untracked files as `git status` reports
them, does not ask to commit them; the sent and skipped records keep them out of this listing.

The session baseline is a time that session-start records for each project clone that has a box, in
<clone>/.agent-mirror/baseline.json, at the end of its run (after the setup steps and the dependency sync,
so what they make is not counted as the session's work). A later session start of the same session keeps
it: on a cloud runtime the same machine, on a developer runtime a baseline at most 24 hours old (the rule
`box send` uses for its session, sessions.same_session). Without a baseline, the time the clone was made
stands in (the first entry of git's log of HEAD); on a cloud machine the clone is made for the session, so
that is close to the session's start, and anywhere else it errs towards listing more. Without that either,
every untracked or ignored file counts.

Nothing here makes a remote call: one git status (without the index lock), a walk of what git reports
(links never followed, left-out folders never entered) and local records, all within a time limit
(TIME_LIMIT seconds); a walk cut short says so.
"""

import contextlib
import datetime
import hashlib
import io
import json
import os
import shutil
import socket
import stat
import time

from agent_mirror import __version__
from agent_mirror.box import sessions
from agent_mirror.box.archive import is_link_dir
from agent_mirror.box.candidates import git_status
from agent_mirror.box.common import side_setting
from agent_mirror.box.local import STATE_DIR, FetchRecord, add_git_excludes, ensure_git_exclude, human
from agent_mirror.box.rules import Rules, clean_rel, is_env_file
from agent_mirror.util import err, local_path, out, run, scrub, short, warn, within, write_text

BASELINE = "baseline.json"
# folders never walked, at any depth: git's own, agent-mirror's own, Python byte code, and dependency or
# cache folders that a tool rebuilds in minutes. dist and build are not here: some projects keep results
# under those names. box.unsent_ignore adds patterns.
BUILTIN_DIRS = (
    ".git",
    STATE_DIR,
    "__pycache__",
    ".venv",
    "venv",
    "node_modules",
    ".cache",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".tox",
    ".nox",
)
SLACK = 2.0
TIME_LIMIT = 5.0
SHOW = 30  # lines of a listing
PATHS_JSON = 2000  # file paths in --json
FOUND_EXIT = 3  # --check: unsent files found, or the check could not finish
REASONS = (
    "before_baseline",
    "fetched",
    "sent",
    "skipped",
    "env",
    "links",
    "builtin",
    "nested_git",
    "managed",
    "excluded",
    "ignored",
)
REASON_WORDS = {
    "before_baseline": "older than the baseline",
    "fetched": "fetched from the box, unchanged",
    "sent": "sent in this session, unchanged",
    "skipped": "declared disposable (box skip)",
    "env": "env files",
    "links": "links",
    "builtin": "folders left out by name",
    "nested_git": "nested git clones",
    "managed": "box mount or receive folders",
    "excluded": "box.exclude",
    "ignored": "box.unsent_ignore",
}
FOR_BOX = "for_the_box"
LEFT_TO_GIT = "left_to_git"
# the optional byte limits of box (key, default): above them an untracked file is for the box
LIMIT_KEYS = (
    ("git_text_max_bytes", 1024**2),
    ("git_binary_max_bytes", 100 * 1024),
    ("git_total_max_bytes", 20 * 1024**2),
)
SNIFF = 8192  # bytes read to tell a binary file (a NUL byte), only for files between the binary and text limits
CLASS_REASONS = ("not_git", "git_ignored", "large", "binary", "many_small", "small")
TAGS = {"git_ignored": "ignored by git", "large": "large", "binary": "binary", "many_small": "many small files"}
EXCLUDE_CAP = 100  # paths one send or skip adds to the local git exclude file at most
SEND = 'agent-mirror box send PATH --label NAME --note "what, why, what next"'
SKIP = 'agent-mirror box skip PATH... --reason "why it can go"'
GIT_RULE = (
    "Small untracked files are left to git (commit them as usual); committing a file listed here is not the "
    "remedy: send it or skip it."
)
CRITERIA = (
    "KEEP results that took real time or money to produce, analysis outputs, generated reports and figures, "
    "anything a later session would otherwise have to redo. LEAVE caches, anything rebuildable in minutes, files "
    "already in git, secrets and env files."
)


def iso(ts):
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def box_configured(box):
    """True when a project's box settings name a backend and where it is (what box_context needs to go on)."""
    box = box if isinstance(box, dict) else {}
    backend = str(box.get("backend") or "none").strip().lower()
    if backend == "azure-blob":
        return bool(str(box.get("storage_account") or "").strip())
    if backend == "rclone":
        return bool(side_setting(box, backend, "shared") or side_setting(box, backend, "sessions"))
    return False


def ignore_patterns(box):
    """(usable patterns, problems) of the optional box.unsent_ignore list."""
    value = (box or {}).get("unsent_ignore")
    if value is None:
        return [], []
    if not isinstance(value, list):
        return [], ["box.unsent_ignore must be a list of patterns; it is not used"]
    good, bad = [], []
    for pat in value:
        if not isinstance(pat, str) or not pat.strip():
            bad.append("box.unsent_ignore holds an empty or non-text pattern; it is not used")
        elif ".." in pat.replace("\\", "/").split("/") or any(c in pat for c in "[]{}"):
            bad.append("box.unsent_ignore pattern %r is not supported (no '..', brackets or braces); not used" % pat)
        else:
            good.append(pat.strip())
    return good, bad


def git_limits(box):
    """([text, binary, total] byte limits, problems) from the optional box.git_*_max_bytes keys; a value that is not
    a whole number of 0 or more is reported and its default used."""
    box = box if isinstance(box, dict) else {}
    found, problems = [], []
    for key, default in LIMIT_KEYS:
        value = box.get(key)
        if value is None:
            found.append(default)
        elif isinstance(value, bool) or not isinstance(value, int) or value < 0:
            problems.append(
                "box.%s must be a whole number of bytes (0 or more); the default %s is used" % (key, human(default))
            )
            found.append(default)
        else:
            found.append(value)
    return found, problems


# the session baseline ----------------------------------------------------------------------------------


def baseline_path(project):
    return os.path.join(project, STATE_DIR, BASELINE)


def load_baseline(project):
    try:
        with open(baseline_path(project), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    if isinstance(data, dict) and isinstance(data.get("seconds"), (int, float)):
        return data
    return None


def record_baseline(project, runtime, now=None):
    """(the baseline record, True when it was written now). The baseline of the same session is kept."""
    host = socket.gethostname()
    cur = load_baseline(project)
    if cur and sessions.same_session(cur, runtime, host, key="at"):
        return cur, False
    now = time.time() if now is None else now
    rec = {
        "schema": 1,
        "seconds": now,
        "at": iso(now),
        "runtime": runtime,
        "host": host,
        "tool": "agent-mirror %s" % __version__,
    }
    write_text(baseline_path(project), json.dumps(rec, indent=1, sort_keys=True) + "\n")
    ensure_git_exclude(project, os.path.join(project, STATE_DIR))
    return rec, True


def session_start_baselines(store, matched, runtime, dry_run):
    """session-start: record the session baseline in each project clone that has a box. Silent, never fails."""
    if dry_run:
        return
    for project, path in matched:
        try:
            if os.path.isdir(path) and box_configured(store.load_env(project).get("box")):
                record_baseline(path, runtime)
        except Exception:  # noqa: BLE001 (never fail the session)
            continue


def clone_time(project, timeout=5):
    """When the clone was made: the time of the first entry of git's log of HEAD, or None."""
    rc, path, _ = run(["git", "-C", project, "rev-parse", "--git-path", "logs/HEAD"], timeout=max(1, timeout))
    if rc != 0 or not path.strip():
        return None
    path = path.strip()
    if not os.path.isabs(path):
        path = os.path.join(project, path)
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            first = fh.readline()
    except OSError:
        return None
    head = first.split("\t", 1)[0].split()
    try:
        return float(head[-2])
    except (IndexError, ValueError):
        return None


def baseline_of(project, timeout=5):
    """{"source": "session-start" | "clone" | "none", "seconds", "at"} for a clone."""
    rec = load_baseline(project)
    if rec:
        return {"source": "session-start", "seconds": float(rec["seconds"]), "at": rec.get("at") or iso(rec["seconds"])}
    made = clone_time(project, timeout)
    if made:
        return {"source": "clone", "seconds": made, "at": iso(made)}
    return {"source": "none", "seconds": 0.0, "at": None}


# finding unsent files -------------------------------------------------------------------------------


def managed_dirs(project, box):
    """The box's mount and receive folders inside the clone, relative to it."""
    from agent_mirror.box.extras import mount_dir, receive_dir

    s = {"project_path": project, "box": box}
    found = []
    root = os.path.realpath(project)
    for folder in (mount_dir(s), receive_dir(s)):
        real = os.path.realpath(folder)
        rel = os.path.relpath(real, root).replace(os.sep, "/")
        if rel != "." and not rel.startswith("../") and rel != "..":
            found.append(rel)
    return found


def top_of(rel):
    """The top-level group of a path: its first folder, or the path itself for a file at the top."""
    return rel.split("/", 1)[0]


def common_unit(paths):
    """(the deepest folder that holds every path, True), or (the path, False) for a single path."""
    if len(paths) == 1:
        return paths[0], False
    common = paths[0].split("/")[:-1]
    for p in paths[1:]:
        parts = p.split("/")[:-1]
        n = 0
        while n < len(common) and n < len(parts) and common[n] == parts[n]:
            n += 1
        common = common[:n]
    return ("/".join(common) or top_of(paths[0])), True


class Finder:
    """One walk of a clone: the unsent files with their class, how many were left out and why, per-folder counts.

    records=False classes every file outside git, whatever the baseline and the session records say, and scope
    (a path relative to the clone) limits the walk to it: what `box send` and `box skip` use (exclude_targets)."""

    def __init__(self, project, box, base, deadline, records=True, scope=None):
        self.project = project
        self.base = base
        self.deadline = deadline  # a time.monotonic() value
        self.scope = scope
        self.exclude = Rules([], [p for p in (box.get("exclude") or []) if isinstance(p, str)])
        good, self.problems = ignore_patterns(box)
        self.ignore = Rules([], good)
        self.limits, bad = git_limits(box)
        self.problems += bad
        self.fetched, self.sent, self.dismissed = None, {}, []
        if records:
            said = io.StringIO()
            with contextlib.redirect_stdout(said):  # an unreadable record is reported, never printed into --json
                self.fetched = FetchRecord(project)
            if said.getvalue().strip():
                self.problems.append("the record of fetched files cannot be read; fetched copies count as unsent")
            rec = sessions.load_local(project)
            self.sent = rec.get("sent") if isinstance(rec.get("sent"), dict) else {}
            dismissed = set()
            for entry in rec.get("skipped") or []:
                if isinstance(entry, dict):
                    dismissed.update(p for p in entry.get("paths") or [] if isinstance(p, str) and p)
            self.dismissed = sorted(dismissed)
        self.managed = managed_dirs(project, box)
        self.left_out = dict((r, 0) for r in REASONS)
        self.files = []  # [rel, size, mtime, root folder or None, class, reason]
        self.counts = {}  # folder -> [files considered, files for the box, files left to git]
        self.small = {}  # top-level group -> [small untracked files, their bytes]
        self.units = {}  # top-level group over the total limit -> (its display unit, is a folder)
        self.git_class = {}  # path git reported -> 'untracked', 'ignored' or 'not in git'
        self.nested = False  # git reported a path inside another one it reported (an ignored file in a new folder)
        self.in_git = True
        self.considered = 0
        self.complete = True
        self.problem = None

    def out_of_time(self):
        if time.monotonic() > self.deadline:
            self.complete = False
        return not self.complete

    def leave(self, why):
        self.left_out[why] += 1

    def dir_reason(self, rel, name, full):
        if name in BUILTIN_DIRS:
            return "builtin"
        if rel in self.managed:
            return "managed"
        if is_link_dir(full):
            return "links"
        if os.path.lexists(os.path.join(full, ".git")):
            return "nested_git"
        if self.exclude.excluded(rel + "/"):
            return "excluded"
        if self.ignore.excluded(rel + "/"):
            return "ignored"
        return None

    def file_reason(self, rel, name):
        if is_env_file(name):
            return "env"
        if self.exclude.excluded(rel):
            return "excluded"
        if self.ignore.excluded(rel):
            return "ignored"
        return None

    def state_reason(self, rel, st):
        if st.st_mtime < self.base - SLACK:
            return "before_baseline"
        if self.fetched is not None and rel in self.fetched.files and self.fetched.unchanged(rel):
            return "fetched"
        sent = self.sent.get(rel)
        if isinstance(sent, dict) and sent.get("bytes") == st.st_size:
            try:
                if abs(float(sent.get("mtime")) - st.st_mtime) < 0.01:
                    return "sent"
            except (TypeError, ValueError):
                pass
        if any(rel == p or rel.startswith(p + "/") for p in self.dismissed):
            return "skipped"
        return None

    @staticmethod
    def ancestors(rel, root):
        """The folders from root down to rel's parent (none for a file git reported on its own)."""
        if root is None:
            return []
        found = [root]
        for part in rel[len(root) + 1 :].split("/")[:-1]:
            found.append(found[-1] + "/" + part)
        return found

    def git_of(self, rel, root):
        """'untracked', 'ignored' or 'not in git' for one file: what git reported for it or its nearest folder."""
        if root is None or not self.nested:
            return self.git_class.get(rel if root is None else root, "untracked")
        p = rel
        while True:
            how = self.git_class.get(p)
            if how:
                return how
            if "/" not in p:
                return "untracked"
            p = p.rsplit("/", 1)[0]

    def binary(self, path):
        """True when the first SNIFF bytes hold a NUL byte (an unreadable file counts as text)."""
        if self.out_of_time():
            return False
        try:
            with open(path, "rb") as fh:
                return b"\0" in fh.read(SNIFF)
        except OSError:
            return False

    def classify(self, rel, root, size, path):
        """(class, reason) of one unsent file; the total limit comes later (finish)."""
        how = self.git_of(rel, root)
        if how == "not in git":
            return FOR_BOX, "not_git"
        if how == "ignored":
            return FOR_BOX, "git_ignored"
        text, binary, _ = self.limits
        if text == 0 or size > text:
            return FOR_BOX, "large"
        if size > binary and self.binary(path):
            return FOR_BOX, "binary"
        return LEFT_TO_GIT, "small"

    def consider(self, rel, name, root, entry=None):
        why = self.file_reason(rel, name)
        if why:
            self.leave(why)
            return
        path = entry.path if entry is not None else local_path(self.project, rel)
        try:
            st = entry.stat(follow_symlinks=False) if entry is not None else os.lstat(path)
        except OSError:
            return
        if not stat.S_ISREG(st.st_mode):
            return
        self.considered += 1
        up = self.ancestors(rel, root)
        for a in up:
            self.counts.setdefault(a, [0, 0, 0])[0] += 1
        why = self.state_reason(rel, st)
        if why:
            self.leave(why)
            return
        cls, reason = self.classify(rel, root, st.st_size, path)
        slot = 1 if cls == FOR_BOX else 2
        for a in up:
            self.counts[a][slot] += 1
        if cls == LEFT_TO_GIT:
            group = self.small.setdefault(top_of(rel), [0, 0])
            group[0] += 1
            group[1] += st.st_size
        self.files.append([rel, st.st_size, st.st_mtime, root, cls, reason])

    def walk(self, top):
        """Every file under the folder top (relative), links never followed, left-out folders never entered."""
        stack = [top]
        while stack:
            if self.out_of_time():
                return
            folder = stack.pop()
            try:
                with os.scandir(local_path(self.project, folder)) as it:
                    entries = sorted(it, key=lambda e: e.name)
            except OSError:
                continue
            subs = []
            for e in entries:
                rel = folder + "/" + e.name
                try:
                    if e.is_symlink():
                        self.leave("links")
                        continue
                    is_dir = e.is_dir(follow_symlinks=False)
                except OSError:
                    continue
                if is_dir:
                    why = self.dir_reason(rel, e.name, e.path)
                    if why:
                        self.leave(why)
                    else:
                        subs.append(rel)
                else:
                    self.consider(rel, e.name, top, e)
            stack.extend(reversed(subs))

    def roots(self):
        """[(relative path, 'untracked' | 'ignored' | 'not in git')] to look at, or None when git failed."""
        left = max(0.5, self.deadline - time.monotonic())
        state, found = git_status(self.project, timeout=left, pathspec=self.scope)
        if state == "ok":
            return sorted(found)
        if state == "failed":
            self.complete = False
            self.problem = "git status did not finish (%s)" % short(found or "no output", 120)
            return None
        self.in_git = False
        if self.scope:
            return [(self.scope, "not in git")] if os.path.lexists(local_path(self.project, self.scope)) else []
        try:
            with os.scandir(self.project) as it:
                return sorted((e.name, "not in git") for e in it)
        except OSError as exc:
            self.problem = "cannot read %s: %s" % (self.project, exc)
            return None

    def run(self, found=None):
        """Walk what git reported (found: the result of roots(), when it was already asked)."""
        found = self.roots() if found is None else found
        for rel, how in found or []:
            rel = clean_rel(rel)
            if rel:
                self.git_class[rel] = how
        for rel in self.git_class:
            parts = rel.split("/")
            if any("/".join(parts[:i]) in self.git_class for i in range(1, len(parts))):
                self.nested = True
                break
        walked = []
        for rel, _ in found or []:
            if self.out_of_time():
                return
            rel = clean_rel(rel)
            if not rel:
                continue
            parts = rel.split("/")
            if any(p in BUILTIN_DIRS for p in parts):
                self.leave("builtin")
                continue
            if any(rel == w or rel.startswith(w + "/") for w in walked):
                continue  # inside a folder already walked
            if any(rel == m or rel.startswith(m + "/") for m in self.managed):
                self.leave("managed")
                continue
            full = local_path(self.project, rel)
            if os.path.islink(full) or is_link_dir(full):
                self.leave("links")
                continue
            if os.path.isdir(full):
                why = self.dir_reason(rel, parts[-1], full)
                if why:
                    self.leave(why)
                    continue
                walked.append(rel)
                self.walk(rel)
            elif os.path.isfile(full):
                self.consider(rel, parts[-1], None)

    def finish(self):
        """The total limit: when the small untracked files of a top-level group together exceed it, they are all
        for the box, and the group is shown as one line (common_unit)."""
        total = self.limits[2]
        over = set(g for g, (n, size) in self.small.items() if n and (total == 0 or size > total))
        if not over:
            return
        members = {}
        for f in self.files:
            if f[4] != LEFT_TO_GIT or top_of(f[0]) not in over:
                continue
            f[4], f[5] = FOR_BOX, "many_small"
            for a in self.ancestors(f[0], f[3]):
                c = self.counts[a]
                c[1] += 1
                c[2] -= 1
            members.setdefault(top_of(f[0]), []).append(f[0])
        for group, paths in members.items():
            self.units[group] = common_unit(paths)

    def entries(self, cls):
        """Display units of one class: a folder whose files are all unsent and of that class is one entry (the
        highest such folder inside what git reported as a whole), the files of a group over the total limit are
        one entry, every other file is its own entry; each with the reasons of its files; largest first."""
        slot = 1 if cls == FOR_BOX else 2
        units = {}
        for rel, size, _, root, c, why in self.files:
            if c != cls:
                continue
            key, folder = rel, False
            for a in self.ancestors(rel, root):
                n = self.counts.get(a)
                if n and n[0] == n[slot]:
                    key, folder = a + "/", True
                    break
            unit = self.units.get(top_of(rel)) if cls == FOR_BOX else None
            if unit and (rel.startswith(unit[0] + "/") if unit[1] else rel == unit[0]):
                k = key.rstrip("/")
                if not (folder and (k == unit[0] or unit[0].startswith(k + "/"))):  # not already a folder around it
                    key, folder = unit[0] + ("/" if unit[1] else ""), unit[1]
            u = units.setdefault(
                key, {"path": key, "folder": folder, "files": 0, "bytes": 0, "class": cls, "reasons": []}
            )
            u["files"] += 1
            u["bytes"] += size
            if why not in u["reasons"]:
                u["reasons"].append(why)
        for u in units.values():
            u["reasons"].sort(key=CLASS_REASONS.index)
        return sorted(units.values(), key=lambda u: (-u["bytes"], u["path"]))


def find_unsent(project_name, project, box, time_limit=TIME_LIMIT, keys=False):
    """The unsent files of one clone, as a dict (the `box unsent --json` document). Never raises for files.

    keys: also give "keys", a short digest of every file for the box (the stop check remembers what it blocked
    on; files left to git never count)."""
    start = time.monotonic()
    deadline = start + max(0.0, float(time_limit))
    base = baseline_of(project, timeout=min(5, max(1, int(time_limit) or 1)))
    f = Finder(project, box, base["seconds"], deadline)
    f.run()
    f.finish()
    files = sorted(f.files, key=lambda x: x[0])
    boxed = [x for x in files if x[4] == FOR_BOX]
    to_git = [x for x in files if x[4] == LEFT_TO_GIT]
    listed = boxed + to_git
    result = {
        "project": project_name,
        "project_path": project,
        "box_configured": True,
        "in_git": f.in_git,
        "baseline": base,
        "complete": f.complete,
        "problem": f.problem,
        "seconds": round(time.monotonic() - start, 3),
        "time_limit": time_limit,
        "files": len(files),
        "bytes": sum(x[1] for x in files),
        FOR_BOX: {"files": len(boxed), "bytes": sum(x[1] for x in boxed)},
        LEFT_TO_GIT: {"files": len(to_git), "bytes": sum(x[1] for x in to_git)},
        "limits": dict((key, value) for (key, _), value in zip(LIMIT_KEYS, f.limits)),
        "checked": f.considered,
        "entries": f.entries(FOR_BOX) + f.entries(LEFT_TO_GIT),
        "paths": [
            {"path": r, "bytes": size, "mtime": iso(m), "class": c, "reason": why}
            for r, size, m, _, c, why in listed[:PATHS_JSON]
        ],
        "paths_more": max(0, len(listed) - PATHS_JSON),
        "left_out": f.left_out,
        "warnings": f.problems,
    }
    if keys:
        result["keys"] = [file_key(project_name, x[0]) for x in boxed]
    return result


def file_key(project_name, rel):
    """A short digest of one unsent file (the stop check keeps these, never the paths)."""
    text = "%s\0%s" % (project_name or "", rel)
    return hashlib.sha256(text.encode("utf-8", "surrogateescape")).hexdigest()[:16]


# the listing -------------------------------------------------------------------------------------------


def item_line(e):
    tags = [TAGS[r] for r in e.get("reasons") or [] if r in TAGS]
    size = human(e["bytes"])
    if e["folder"]:
        size = "%d file%s, %s" % (e["files"], "" if e["files"] == 1 else "s", size)
    return "%s (%s%s)" % (e["path"], size, "; " + ", ".join(tags) if tags else "")


def small_files(n):
    return "%d small untracked file%s" % (n, "" if n == 1 else "s")


def listing_lines(result, cap=SHOW, indent="  ", cls=FOR_BOX):
    """The entries of one class grouped by top-level folder, largest first, at most cap lines plus one 'and N
    more'."""
    total = result.get(cls) or {"files": result["files"], "bytes": result["bytes"]}
    groups = {}
    for e in result["entries"]:
        if e.get("class", FOR_BOX) != cls:
            continue
        path = e["path"].rstrip("/")
        name = path.split("/", 1)[0] if "/" in path else e["path"]
        g = groups.setdefault(name, {"name": name, "files": 0, "bytes": 0, "items": []})
        g["files"] += e["files"]
        g["bytes"] += e["bytes"]
        g["items"].append(e)
    lines, shown_files, shown_bytes = [], 0, 0
    for g in sorted(groups.values(), key=lambda x: (-x["bytes"], x["name"])):
        if len(lines) >= cap:
            break
        if len(g["items"]) == 1:
            e = g["items"][0]
            lines.append(indent + item_line(e))
            shown_files += e["files"]
            shown_bytes += e["bytes"]
            continue
        lines.append("%sin %s/: %d files, %s" % (indent, g["name"], g["files"], human(g["bytes"])))
        for e in g["items"]:
            if len(lines) >= cap:
                break
            lines.append(indent * 2 + item_line(e))
            shown_files += e["files"]
            shown_bytes += e["bytes"]
    rest = total["files"] - shown_files
    if rest > 0:
        lines.append(
            "%sand %d more file%s, %s" % (indent, rest, "" if rest == 1 else "s", human(total["bytes"] - shown_bytes))
        )
    return lines


def baseline_words(base):
    if base["source"] == "session-start":
        return "changed since %s (session start)" % base["at"]
    if base["source"] == "clone":
        return "changed since %s (the clone was made; no session baseline)" % base["at"]
    return "any time (no session baseline and no git HEAD log)"


def left_out_line(result):
    parts = ["%d %s" % (n, REASON_WORDS[r]) for r, n in ((r, result["left_out"].get(r, 0)) for r in REASONS) if n]
    return "left out: %s" % ", ".join(parts) if parts else None


def report_lines(result, cap=SHOW, show_all=False):
    """The human report of one clone: the files for the box, then one line for the files left to git (show_all:
    their listing)."""
    lines = []
    base = result["baseline"]
    if base["source"] == "clone":
        lines.append(
            "no session baseline in %s (a session start records one); counting what changed since the clone was "
            "made, %s" % (result["project_path"], base["at"])
        )
    elif base["source"] == "none":
        lines.append(
            "no session baseline in %s and no git HEAD log; counting every untracked or ignored file"
            % result["project_path"]
        )
    for p in result.get("warnings") or []:
        lines.append("warning: %s" % p)
    if result.get("problem"):
        lines.append("%s: %s; nothing could be checked" % (result["project"], result["problem"]))
        return lines
    boxed, to_git = result[FOR_BOX], result[LEFT_TO_GIT]
    if boxed["files"]:
        lines.append(
            "unsent in %s (%s): %d file%s for the box, %s, %s, in no git commit and not in the box:"
            % (
                result["project"],
                result["project_path"],
                boxed["files"],
                "" if boxed["files"] == 1 else "s",
                human(boxed["bytes"]),
                baseline_words(base),
            )
        )
        lines += listing_lines(result, cap)
    else:
        lines.append(
            "nothing %s in %s (%s): %d file%s outside git checked, %s"
            % (
                "for the box" if to_git["files"] else "unsent",
                result["project"],
                result["project_path"],
                result["checked"],
                "" if result["checked"] == 1 else "s",
                baseline_words(base),
            )
        )
    if to_git["files"] and show_all:
        lines.append(
            "left to git: %s, %s (commit them as usual):" % (small_files(to_git["files"]), human(to_git["bytes"]))
        )
        lines += listing_lines(result, cap, cls=LEFT_TO_GIT)
    elif to_git["files"]:
        lines.append(
            "left to git: %s, %s (commit them; --all lists them)"
            % (small_files(to_git["files"]), human(to_git["bytes"]))
        )
    if not result["complete"]:
        lines.append(
            "the check was cut short after %s s (--time-limit); the list may be incomplete"
            % ("%g" % result["time_limit"])
        )
    line = left_out_line(result)
    if line:
        lines.append(line)
    return lines


# box unsent ---------------------------------------------------------------------------------------------


def unconfigured(project, path):
    return {
        "project": project,
        "project_path": path,
        "box_configured": False,
        "complete": True,
        "files": 0,
        "bytes": 0,
        FOR_BOX: {"files": 0, "bytes": 0},
        LEFT_TO_GIT: {"files": 0, "bytes": 0},
        "entries": [],
        "paths": [],
    }


def cmd_unsent(args, ctx):
    """box unsent [--all] [--json] [--check] [--time-limit S]: exit 0, or with --check 3 when something is for the
    box (or the check could not finish) and 0 otherwise (files left to git never count); 1 when there is no
    project or folder to check."""
    from agent_mirror.projects import box_project

    project, path = box_project(ctx, args.project, args.project_path)
    if not project:
        err("no project of the store matched %s; pass --project NAME (and --project-path PATH)" % path)
        return 1
    if not os.path.isdir(path):
        err("project folder %s does not exist" % path)
        return 1
    store = ctx.require_store()
    box = store.load_env(project).get("box") or {}
    if not box_configured(box):
        if args.json:
            out(json.dumps(unconfigured(project, path), indent=2))
        else:
            out(
                "the box of %s is not configured (box in projects/%s/env.json), so nothing can be sent; "
                "nothing to check" % (project, project)
            )
        return 0
    result = find_unsent(project, path, box, max(0.0, float(args.time_limit)))
    if args.json:
        out(json.dumps(result, indent=2))
    else:
        for line in report_lines(result, show_all=getattr(args, "all", False)):
            out(line)
        if result[FOR_BOX]["files"]:
            out("keep a file or folder: %s" % SEND)
            out("leave it behind:       %s" % SKIP)
    if args.check and (result[FOR_BOX]["files"] or not result["complete"] or result.get("problem")):
        return FOUND_EXIT
    return 0


# the local git exclude after box send and box skip ----------------------------------------------------------


def exclude_targets(project, rels, box, sent, time_limit=TIME_LIMIT):
    """([(path, is a folder)], None) to add to the clone's local git exclude file after `box send` (sent=True) or
    `box skip` of rels, or ([], why nothing is added).

    Only what git reports as untracked counts (an ignored path is already out of sight, a tracked one is git's).
    A skip adds every untracked path at or under rels. A send adds an untracked path whose files are all for the
    box by the rules of `box unsent` (applied to every file under it, whatever the session records say), and
    otherwise only its files that are for the box: a small sent file may still be committed."""
    deadline = time.monotonic() + max(0.5, float(time_limit))
    targets = []
    for rel in rels:
        f = Finder(project, box or {}, 0.0, deadline, records=False, scope=rel)
        found = f.roots()
        if found is None:
            return [], f.problem or "git did not answer"
        if not f.in_git:
            return [], None
        roots = [r for r, how in ((clean_rel(p), how) for p, how in found) if r and how == "untracked"]
        if not sent:
            targets += [
                (r, os.path.isdir(local_path(project, r)) and not os.path.islink(local_path(project, r))) for r in roots
            ]
            continue
        f.run(found)
        f.finish()
        if not f.complete:
            return [], "the check of %s did not finish within %g s" % (rel, time_limit)
        for r in roots:
            mine = [x for x in f.files if x[0] == r or x[0].startswith(r + "/")]
            if mine and all(x[4] == FOR_BOX for x in mine):
                targets.append((r, os.path.isdir(local_path(project, r))))
            else:
                targets += [(x[0], False) for x in mine if x[4] == FOR_BOX and x[5] != "git_ignored"]
    seen, unique = set(), []
    for t in targets:
        if t[0] not in seen:
            seen.add(t[0])
            unique.append(t)
    if len(unique) > EXCLUDE_CAP:
        return [], "%d paths would be added, more than %d; add the folders that hold them by hand" % (
            len(unique),
            EXCLUDE_CAP,
        )
    return unique, None


def exclude_after(project, rels, box, sent, runtime, time_limit=TIME_LIMIT):
    """On a cloud runtime, after `box send` (sent=True) or `box skip` of rels: add what exclude_targets finds to the
    clone's local git exclude file, so that a stop hook that asks to commit untracked files leaves it alone.
    Returns the lines to print (one, or none). Never raises; does nothing on a developer runtime."""
    from agent_mirror.runtime import CLOUD_RUNTIMES

    if runtime not in CLOUD_RUNTIMES:
        return []
    try:
        targets, why = exclude_targets(project, rels, box, sent, time_limit)
        if why:
            return ["the local git exclude file was not changed: %s" % why]
        if not targets:
            return []
        comment = "added by agent-mirror box %s (local only): %s" % (
            "send" if sent else "skip",
            "in the box" if sent else "declared disposable",
        )
        added, path = add_git_excludes(project, targets, comment)
    except Exception as exc:  # noqa: BLE001 (the send or skip itself succeeded)
        return ["the local git exclude file was not changed: %s" % short(exc, 120)]
    if not added:
        return []
    real = os.path.realpath(project)
    where = os.path.relpath(path, real).replace(os.sep, "/") if within(os.path.realpath(path), real) else path
    shown = ", ".join(added[:5]) + (" and %d more" % (len(added) - 5) if len(added) > 5 else "")
    one = len(added) == 1
    return [
        "added %s to this clone's local git exclude file (%s, never committed): %s %s, so a stop hook that "
        "asks to commit untracked files leaves %s alone"
        % (
            shown if one else "%d paths (%s)" % (len(added), shown),
            where,
            "it is" if one else "they are",
            "in the box now" if sent else "declared disposable",
            "it" if one else "them",
        )
    ]


# box skip -------------------------------------------------------------------------------------------------


def skip_target(text, project):
    """(path relative to the clone, None), or (None, (message, exit code)) for one argument of `box skip`.

    A path is taken relative to the clone first, then relative to the current folder (or as an absolute path);
    it must exist and be inside the clone.
    """
    from agent_mirror.box import path_inside

    text = str(text or "").strip()
    if not text:
        return None, ("box skip takes paths of files or folders inside the clone", 2)
    here = os.path.abspath(text)
    inside = path_inside(here, project)
    rel = clean_rel(text)
    if rel and os.path.lexists(local_path(project, rel)):
        return rel, None
    if inside is None and (os.path.isabs(text) or os.path.lexists(here) or ".." in text.replace("\\", "/").split("/")):
        return None, ("refusing %s: it is outside the project clone %s; nothing was recorded" % (text, project), 2)
    if inside == "":
        return None, ("box skip takes files or folders inside the clone, not the clone itself; nothing was recorded", 2)
    if inside is not None and os.path.lexists(here):
        return clean_rel(inside), None
    return None, (
        "%s does not exist in the project clone %s or the current folder; nothing was recorded" % (text, project),
        1,
    )


def cmd_skip(args, ctx):
    """box skip PATH... --reason TEXT: declare files or folders of the clone disposable for this session.

    Recorded in the local session record (always) and in the session's manifest on the box (when the box can be
    reached); never fails because the box cannot be reached."""
    from agent_mirror.projects import box_project
    from agent_mirror.box.notes import clean_note
    from agent_mirror.runtime import CLOUD_RUNTIMES, detect_runtime

    runtime = detect_runtime()
    reason, problem = clean_note(args.reason, what="reason")
    if problem:
        err(problem)
        return 2
    if not reason:
        err('give the reason: --reason "why these files can go" (one line, up to 500 characters)')
        return 2
    problem = sessions.label_problem(getattr(args, "label", None))
    if problem:
        err(problem)
        return 2
    project, path = box_project(ctx, args.project, args.project_path)
    if not os.path.isdir(path):
        err("project folder %s does not exist" % path)
        return 1
    rels = []
    for text in args.paths:
        rel, problem = skip_target(text, path)
        if problem:
            err(problem[0])
            return problem[1]
        if rel not in rels:
            rels.append(rel)
    entry = {"at": sessions.utc_now(), "reason": reason, "paths": rels}
    shown = ", ".join(rels[:5]) + (" and %d more" % (len(rels) - 5) if len(rels) > 5 else "")
    if args.dry_run:
        out("dry run: nothing will change")
        out("would record %d path(s) as left behind on purpose (%s): %s" % (len(rels), reason, shown))
        return 0
    session, created = sessions.current_session(path, runtime, label=getattr(args, "label", None))
    session["skipped"] = list(session.get("skipped") or []) + [entry]
    sessions.save_local(path, session)
    out("left behind on purpose in session %s: %s (reason: %s)" % (session["id"], shown, reason))
    if runtime in CLOUD_RUNTIMES:
        try:
            store = ctx.store_or_none(quiet=True)
            box = (store.load_env(project).get("box") or {}) if (store and project) else {}
        except Exception:  # noqa: BLE001 (the limits only matter for a send)
            box = {}
        for line in exclude_after(path, rels, box, False, runtime):
            out(line)
    try:
        record_in_box(args, ctx, project, path, runtime, session, created, entry)
    except Exception as exc:  # noqa: BLE001 (the local record is what matters here)
        warn("could not record it in the box (%s); recorded on this machine only" % short(exc, 120))
    return 0


def record_in_box(args, ctx, project, path, runtime, session, created, entry):
    """Add a skip entry to the session manifest on the box; says so in one line when it cannot."""
    from agent_mirror.bootstrap import sandboxed_away
    from agent_mirror.box import BACKEND_MODULES
    from agent_mirror.box.notes import quiet_context
    from agent_mirror.box.sessions import TOOL_FOR_BACKEND
    from agent_mirror.box.sides import side_for

    local_only = "recorded on this machine only (`agent-mirror box unsent` honours it)"
    store = ctx.store_or_none(quiet=True)
    box = (store.load_env(project).get("box") or {}) if (store and project) else {}
    if not box_configured(box):
        out("the box of %s is not configured: %s" % (project or "this folder", local_only))
        return
    s = quiet_context(ctx, project, path)
    if s is None:
        warn("the box settings of %s cannot be used: %s" % (project, local_only))
        return
    tool = TOOL_FOR_BACKEND[s["backend"]]
    found = shutil.which(tool)
    if not found or sandboxed_away(ctx.plat, found):  # a test run never starts a real transfer program
        warn("%s is not available here, so the box cannot be reached: %s" % (tool, local_only))
        return
    if not BACKEND_MODULES[s["backend"]].prepare(args, runtime):
        warn("the box cannot be reached: %s" % local_only)
        return
    side = side_for(s, "sessions")
    sid = session["id"]
    ok, problem, _ = sessions.update_manifest(
        side,
        sid,
        sessions.new_manifest(s, session, runtime),
        args.timeout,
        label=session.get("label"),
        new=created,
        skipped=entry,
    )
    if not ok:
        warn("the session manifest could not be updated (%s): %s" % (problem, local_only))
        return
    out(
        "also recorded in %s, so later sessions see what was left behind and why"
        % scrub(side.where("%s/%s" % (sid, sessions.MANIFEST)))
    )
