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
from agent_mirror.box.local import STATE_DIR, FetchRecord, ensure_git_exclude, human
from agent_mirror.box.rules import Rules, clean_rel, is_env_file
from agent_mirror.util import err, local_path, out, run, scrub, short, warn, write_text

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
SEND = 'agent-mirror box send PATH --label NAME --note "what, why, what next"'
SKIP = 'agent-mirror box skip PATH... --reason "why it can go"'
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


class Finder:
    """One walk of a clone: the unsent files, how many were left out and why, and per-folder counts."""

    def __init__(self, project, box, base, deadline):
        self.project = project
        self.base = base
        self.deadline = deadline  # a time.monotonic() value
        self.exclude = Rules([], [p for p in (box.get("exclude") or []) if isinstance(p, str)])
        good, self.problems = ignore_patterns(box)
        self.ignore = Rules([], good)
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
        self.files = []  # (rel, size, mtime, root folder or None)
        self.counts = {}  # folder -> [files considered, files unsent]
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
        if rel in self.fetched.files and self.fetched.unchanged(rel):
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

    def consider(self, rel, name, root, entry=None):
        why = self.file_reason(rel, name)
        if why:
            self.leave(why)
            return
        try:
            st = entry.stat(follow_symlinks=False) if entry is not None else os.lstat(local_path(self.project, rel))
        except OSError:
            return
        if not stat.S_ISREG(st.st_mode):
            return
        self.considered += 1
        up = self.ancestors(rel, root)
        for a in up:
            self.counts.setdefault(a, [0, 0])[0] += 1
        why = self.state_reason(rel, st)
        if why:
            self.leave(why)
            return
        for a in up:
            self.counts[a][1] += 1
        self.files.append((rel, st.st_size, st.st_mtime, root))

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
        state, found = git_status(self.project, timeout=left)
        if state == "ok":
            return sorted(found)
        if state == "failed":
            self.complete = False
            self.problem = "git status did not finish (%s)" % short(found or "no output", 120)
            return None
        try:
            with os.scandir(self.project) as it:
                return sorted((e.name, "not in git") for e in it)
        except OSError as exc:
            self.problem = "cannot read %s: %s" % (self.project, exc)
            return None

    def run(self):
        found = self.roots()
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

    def entries(self):
        """Display units: a folder whose files are all unsent is one entry (the highest such folder inside what
        git reported as a whole), every other unsent file is its own entry; largest first."""
        units = {}
        for rel, size, _, root in self.files:
            key, folder = rel, False
            for a in self.ancestors(rel, root):
                c = self.counts.get(a)
                if c and c[0] == c[1]:
                    key, folder = a + "/", True
                    break
            u = units.setdefault(key, {"path": key, "folder": folder, "files": 0, "bytes": 0})
            u["files"] += 1
            u["bytes"] += size
        return sorted(units.values(), key=lambda u: (-u["bytes"], u["path"]))


def find_unsent(project_name, project, box, time_limit=TIME_LIMIT, keys=False):
    """The unsent files of one clone, as a dict (the `box unsent --json` document). Never raises for files.

    keys: also give "keys", a short digest of every unsent file (the stop check remembers what it blocked on)."""
    start = time.monotonic()
    deadline = start + max(0.0, float(time_limit))
    base = baseline_of(project, timeout=min(5, max(1, int(time_limit) or 1)))
    f = Finder(project, box, base["seconds"], deadline)
    f.run()
    files = sorted(f.files, key=lambda x: x[0])
    result = {
        "project": project_name,
        "project_path": project,
        "box_configured": True,
        "baseline": base,
        "complete": f.complete,
        "problem": f.problem,
        "seconds": round(time.monotonic() - start, 3),
        "time_limit": time_limit,
        "files": len(files),
        "bytes": sum(x[1] for x in files),
        "checked": f.considered,
        "entries": f.entries(),
        "paths": [{"path": r, "bytes": size, "mtime": iso(m)} for r, size, m, _ in files[:PATHS_JSON]],
        "paths_more": max(0, len(files) - PATHS_JSON),
        "left_out": f.left_out,
        "warnings": f.problems,
    }
    if keys:
        result["keys"] = [file_key(project_name, r) for r, _, _, _ in files]
    return result


def file_key(project_name, rel):
    """A short digest of one unsent file (the stop check keeps these, never the paths)."""
    text = "%s\0%s" % (project_name or "", rel)
    return hashlib.sha256(text.encode("utf-8", "surrogateescape")).hexdigest()[:16]


# the listing -------------------------------------------------------------------------------------------


def item_line(e):
    if e["folder"]:
        return "%s (%d file%s, %s)" % (e["path"], e["files"], "" if e["files"] == 1 else "s", human(e["bytes"]))
    return "%s (%s)" % (e["path"], human(e["bytes"]))


def listing_lines(result, cap=SHOW, indent="  "):
    """The unsent entries grouped by top-level folder, largest first, at most cap lines plus one 'and N more'."""
    groups = {}
    for e in result["entries"]:
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
    rest = result["files"] - shown_files
    if rest > 0:
        lines.append(
            "%sand %d more file%s, %s" % (indent, rest, "" if rest == 1 else "s", human(result["bytes"] - shown_bytes))
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


def report_lines(result, cap=SHOW):
    """The human report of one clone."""
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
    if result["files"]:
        lines.append(
            "unsent in %s (%s): %d file%s, %s, %s, in no git commit and not in the box:"
            % (
                result["project"],
                result["project_path"],
                result["files"],
                "" if result["files"] == 1 else "s",
                human(result["bytes"]),
                baseline_words(base),
            )
        )
        lines += listing_lines(result, cap)
    else:
        lines.append(
            "nothing unsent in %s (%s): %d file%s outside git checked, %s"
            % (
                result["project"],
                result["project_path"],
                result["checked"],
                "" if result["checked"] == 1 else "s",
                baseline_words(base),
            )
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
        "entries": [],
        "paths": [],
    }


def cmd_unsent(args, ctx):
    """box unsent [--json] [--check] [--time-limit S]: exit 0, or with --check 3 when something is unsent (or the
    check could not finish) and 0 when nothing is; 1 when there is no project or folder to check."""
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
        for line in report_lines(result):
            out(line)
        if result["files"]:
            out("keep a file or folder: %s" % SEND)
            out("leave it behind:       %s" % SKIP)
    if args.check and (result["files"] or not result["complete"]):
        return FOUND_EXIT
    return 0


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
    from agent_mirror.runtime import detect_runtime

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
