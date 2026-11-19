"""box candidates: which untracked or ignored project entries could go in the box. Read-only.

Entries come from `git status --porcelain --ignored` (git reports a folder whose whole content is
untracked or ignored as one entry); `git check-ignore -v` says which ignore rule matched. Outside a
git repo the project's top-level entries are listed instead. Each entry gets its size (os.scandir,
links never followed, a depth cap and a shared time budget; sizes that stopped early are a lower
bound), its count of large files, its newest change, a kind guess and whether the current
box.include or box.archives already cover it.
"""

import fnmatch
import json
import os
import time

from agent_mirror.box.archive import DEFAULT_LARGE
from agent_mirror.box.local import human
from agent_mirror.box.rules import Rules, include_prefix
from agent_mirror.box.units import archive_unit_of, archive_units
from agent_mirror.projects import box_project
from agent_mirror.sizes import measure
from agent_mirror.util import err, out, run

ENTRY_CAP = 2000
REBUILDABLE = {
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".mypy_cache",
    ".ruff_cache",
    ".pytest_cache",
    "dist",
    "build",
    ".tox",
    ".nox",
    ".gradle",
    "target",
    ".eggs",
    ".hypothesis",
    ".ipynb_checkpoints",
    ".parcel-cache",
    ".next",
    ".turbo",
}
REBUILDABLE_GLOBS = ("*.egg-info", "*.pyc")
SECRET_NAMES = {".env", ".netrc", ".pypirc", ".npmrc", "id_rsa", "id_ed25519", "id_ecdsa"}
SECRET_GLOBS = ("*.env", ".env.*", "*.pem", "*.key", "*.p12", "*.pfx", "*credential*", "*secret*", "*.kdbx")


def kind_of(path, name):
    """(kind, note) for one entry: rebuildable, secret, worktree or candidate."""
    low = name.lower()
    if name in REBUILDABLE or any(fnmatch.fnmatch(low, g) for g in REBUILDABLE_GLOBS):
        return "rebuildable", "rebuilt by a tool; never worth carrying"
    if low in SECRET_NAMES or any(fnmatch.fnmatch(low, g) for g in SECRET_GLOBS):
        return "secret", "looks like a secret; never goes in the box"
    dot_git = os.path.join(path, ".git")
    if os.path.isfile(dot_git):
        return "worktree", "a git worktree; it travels as its own branch, not through the box"
    if os.path.isdir(dot_git):
        return "worktree", "a nested git clone; it travels through its own remote, not the box"
    return "candidate", ""


def git_entries(project, timeout=120):
    """[(relative path, 'untracked' or 'ignored')] from git status, or None when this is not a git repo."""
    state, found = git_status(project, timeout)
    return found if state == "ok" else None


def git_status(project, timeout=120, pathspec=None):
    """('ok', [(relative path, 'untracked' or 'ignored')]), ('not-git', None) or ('failed', why).

    git reports a folder whose whole content is untracked or ignored as one entry. The listing never takes the
    index lock (--no-optional-locks), so it never gets in the way of git commands running at the same time.
    timeout bounds the status call (seconds). pathspec: one path relative to the project folder, taken
    literally, to look at instead of the whole folder.
    """
    rc, top, _ = run(["git", "-C", project, "rev-parse", "--show-toplevel"], timeout=min(20, max(1, timeout)))
    if rc != 0 or not top.strip():
        return ("failed", "git did not answer in time") if rc is None else ("not-git", None)
    literal = ["--literal-pathspecs"] if pathspec else []
    rc, text, stderr = run(
        ["git", "-C", project, "--no-optional-locks"]
        + literal
        + ["status", "--porcelain=v1", "-z", "--ignored"]
        + ["--untracked-files=normal", "--", pathspec or "."],
        timeout=timeout,
    )
    if rc != 0:
        return "failed", (stderr or "git status failed").strip()
    found = []
    prefix_rc, prefix, _ = run(["git", "-C", project, "rev-parse", "--show-prefix"], timeout=min(20, max(1, timeout)))
    prefix = prefix.strip() if prefix_rc == 0 else ""
    for tok in text.split("\0"):
        if len(tok) < 4 or tok[:2] not in ("??", "!!"):
            continue
        path = tok[3:]
        if prefix and path.startswith(prefix):
            path = path[len(prefix) :]
        path = path.rstrip("/")
        if path:
            found.append((path, "untracked" if tok[:2] == "??" else "ignored"))
    return "ok", found


def ignore_rules(project, paths):
    """{path: 'source:line:pattern'} from git check-ignore -v for the ignored entries."""
    if not paths:
        return {}
    rc, text, _ = run(["git", "-C", project, "check-ignore", "-v", "--"] + paths[:500], timeout=60)
    found = {}
    for line in text.splitlines():
        rule, sep, path = line.rpartition("\t")
        if sep and path:
            found[path.strip('"').rstrip("/")] = rule
    return found


def plain_entries(project):
    found = []
    try:
        with os.scandir(project) as it:
            for e in sorted(it, key=lambda x: x.name):
                if e.name in (".git", ".agent-mirror"):
                    continue
                found.append((e.name, "not in git"))
    except OSError:
        pass
    return found


def coverage(rel, is_dir, box, archived=()):
    """'covered', 'partly covered', 'excluded' or '' for one entry against box.include and box.archives.

    archived: the archived folders found in the project; one inside rel makes rel partly covered.
    """
    if not box:
        return ""
    archives = [str(g).strip().strip("/") for g in box.get("archives") or []]
    if archives and archive_unit_of(rel, archives):
        return "covered (archive)"
    inside = [a for a in archived if a.startswith(rel + "/")]
    rules = Rules(box.get("include") or [], box.get("exclude") or [])
    probe = rel + "/" if is_dir else rel
    hit = rules.excluded(probe)
    if hit:
        return "excluded (%s)" % hit
    partly = None
    if inside:
        partly = "partly covered (archived: %s%s)" % (
            ", ".join(inside[:3]),
            " and %d more" % (len(inside) - 3) if len(inside) > 3 else "",
        )
    for pat in box.get("include") or []:
        root = include_prefix(pat)
        if not root:
            continue
        if rel == root or rel.startswith(root + "/"):
            return "covered"
        if root.startswith(rel + "/") and partly is None:
            partly = "partly covered (%s)" % pat
    if not is_dir and rules.included(rel):
        return "covered"
    return partly or ""


def cmd_candidates(args, ctx):
    project, path = box_project(ctx, args.project, args.project_path)
    if not os.path.isdir(path):
        err("project folder %s does not exist" % path)
        return 1
    store = ctx.store_or_none(quiet=True)
    box = (store.load_env(project).get("box") or {}) if (store and project) else {}
    large = box.get("archive_large_file_bytes")
    large = DEFAULT_LARGE if large is None else large
    entries = git_entries(path)
    in_git = entries is not None
    if entries is None:
        entries = plain_entries(path)
    truncated = len(entries) > ENTRY_CAP
    entries = entries[:ENTRY_CAP]
    rules_by_path = ignore_rules(path, [p for p, how in entries if how == "ignored"]) if in_git else {}
    globs = [str(g).strip().strip("/") for g in box.get("archives") or [] if isinstance(g, str)]
    archived = archive_units(path, globs)[0] if globs else []
    start = time.monotonic()
    deadline = start + max(0.5, args.time_budget)
    rows = []
    for i, (rel, how) in enumerate(entries):
        full = os.path.join(path, *rel.split("/"))
        name = rel.rsplit("/", 1)[-1]
        is_link = os.path.islink(full)
        is_dir = os.path.isdir(full) and not is_link
        kind, note = kind_of(full, name)
        left = len(entries) - i
        own = time.monotonic() + max(0.05, (deadline - time.monotonic()) / max(1, left))
        if is_link:
            size = {"files": 0, "bytes": 0, "large_files": 0, "newest": None, "lower_bound": True}
            note = (note + "; " if note else "") + "a link (not followed)"
        else:
            size = measure(full, deadline=min(own, deadline), large=large)
        rows.append(
            {
                "path": rel + ("/" if is_dir else ""),
                "git": how,
                "ignored_by": rules_by_path.get(rel),
                "kind": kind,
                "files": size["files"],
                "bytes": size["bytes"],
                "large_files": size["large_files"],
                "newest": size["newest"],
                "lower_bound": size["lower_bound"],
                "box": coverage(rel, is_dir, box, archived),
                "note": note,
            }
        )
    rows = [r for r in rows if r["bytes"] >= args.min_bytes or r["lower_bound"]]
    rows.sort(key=lambda r: (-r["bytes"], r["path"]))
    data = {
        "project": project,
        "project_path": path,
        "git": in_git,
        "large_file_bytes": large,
        "box_configured": bool(box and str(box.get("backend") or "none").lower() != "none"),
        "entries": rows,
        "truncated": truncated,
        "seconds": round(time.monotonic() - start, 2),
    }
    if args.json:
        out(json.dumps(data, indent=2))
        return 0
    out(
        "box candidates for %s (%s): %s"
        % (
            project or "this folder",
            path,
            "entries git does not track or ignores" if in_git else "not a git repo, so every top-level entry",
        )
    )
    if not rows:
        out("  nothing found%s" % (" at or above %s" % human(args.min_bytes) if args.min_bytes else ""))
    for r in rows:
        size = human(r["bytes"]) + ("+" if r["lower_bound"] else "")
        extra = []
        if r["large_files"]:
            extra.append("%d large" % r["large_files"])
        if r["box"]:
            extra.append("box: " + r["box"])
        if r["ignored_by"]:
            extra.append("ignored by " + r["ignored_by"])
        out(
            "  %-11s %11s %7d files  %-17s %s%s"
            % (
                r["kind"],
                size,
                r["files"],
                (r["newest"] or "-").replace("T", " ")[:16],
                r["path"],
                ("  (" + "; ".join(extra) + ")") if extra else "",
            )
        )
    if any(r["lower_bound"] for r in rows):
        out("a + after a size: the walk stopped early (depth cap, time budget or a link), so it is a lower bound")
    if truncated:
        out("(only the first %d entries are listed)" % ENTRY_CAP)
    out(
        "kinds: rebuildable (a tool rebuilds it), secret (never in the box), worktree (has its own git), "
        "candidate (could go in the box: add it to box.include or box.archives)"
    )
    return 0
