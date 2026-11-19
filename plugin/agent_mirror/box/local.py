"""Local bookkeeping for on-demand retrieval: disk guard, the fetched-files record, git exclude.

The record lives in <project>/.agent-mirror/fetched.json. It is never
committed: agent-mirror adds /.agent-mirror/ to the clone's own
.git/info/exclude (not to the tracked .gitignore). On a cloud runtime,
`box send` and `box skip` add the paths they dealt with there too (unsent.py).
"""

import json
import os
import shutil

from agent_mirror.util import err, iso_now, local_path, norm, run, warn, within, write_text

STATE_DIR = ".agent-mirror"
RECORD = "fetched.json"
DEFAULT_MIN_FREE = 2 * 1024**3


def human(n):
    n = float(n or 0)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(n) < 1024 or unit == "TiB":
            return ("%d %s" % (n, unit)) if unit == "B" else ("%.1f %s" % (n, unit))
        n /= 1024.0
    return "%d B" % n


def free_bytes(path):
    p = os.path.abspath(path)
    while not os.path.exists(p):
        parent = os.path.dirname(p)
        if parent == p:
            break
        p = parent
    return shutil.disk_usage(p).free


def min_free(box):
    value = box.get("min_free_bytes") if isinstance(box, dict) else None
    return DEFAULT_MIN_FREE if value is None else max(0, int(value))


def disk_guard(path, need, reserve, force, free_fn=None, label="box.min_free_bytes"):
    """True when need bytes fit on the disk of path with reserve bytes left over (or force)."""
    free = (free_fn or free_bytes)(path)
    if need + reserve <= free:
        return True
    msg = "not enough free disk at %s: this needs %s plus a reserve of %s (%s), and %s is free" % (
        path,
        human(need),
        human(reserve),
        label,
        human(free),
    )
    if force:
        warn(msg + "; going ahead because of --force")
        return True
    err(msg + ". Free space first (`agent-mirror box status`, then `agent-mirror box evict PATH`), or pass --force")
    return False


def exclude_line(rel, is_dir):
    """The line of a git exclude file that matches exactly one path relative to the repo top: anchored with a
    leading slash, a trailing slash for a folder, and git's special characters escaped. None for a name a line
    cannot hold (a line break)."""
    if not rel or "\n" in rel or "\r" in rel:
        return None
    text = "".join("\\" + c if c in "\\*?[" else c for c in rel)
    spaces = len(text) - len(text.rstrip(" "))
    if spaces and not is_dir:
        text = text[: len(text) - spaces] + "\\ " * spaces  # git drops unescaped trailing spaces
    return "/" + text + ("/" if is_dir else "")


def add_git_excludes(project_path, targets, comment="added by agent-mirror (local only)"):
    """Add one anchored line per (path relative to the project folder, is a folder) to the clone's own
    .git/info/exclude (never .gitignore, never committed). Lines already there are not added again.

    Returns (the lines added, the exclude file's path), or ([], None) when this is not a git clone, git does not
    answer, or a target is outside the repo or is its top."""
    rc, top, _ = run(["git", "-C", project_path, "rev-parse", "--show-toplevel"], timeout=10)
    if rc != 0 or not top.strip():
        return [], None
    top = os.path.realpath(top.strip())
    root = os.path.realpath(project_path)
    lines = []
    for rel, is_dir in targets:
        target = local_path(root, rel) if rel else root
        if not within(target, top) or norm(target) == norm(top):
            return [], None
        line = exclude_line(os.path.relpath(target, top).replace(os.sep, "/"), is_dir)
        if line is None:
            return [], None
        if line not in lines:
            lines.append(line)
    rc, path, _ = run(["git", "-C", top, "rev-parse", "--git-path", "info/exclude"], timeout=10)
    if rc != 0 or not path.strip():
        return [], None
    path = path.strip()
    if not os.path.isabs(path):
        path = os.path.join(top, path)
    text = ""
    if os.path.isfile(path):
        with open(path, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    have = set(text.splitlines())
    new = [line for line in lines if line not in have]
    if new:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            if text and not text.endswith("\n"):
                fh.write("\n")
            fh.write("# %s\n%s\n" % (comment, "\n".join(new)))
    return new, path


def ensure_git_exclude(project_path, folder):
    """Add /<folder>/ (relative to the repo top) to the clone's .git/info/exclude. Never touches .gitignore."""
    rel = os.path.relpath(os.path.realpath(folder), os.path.realpath(project_path)).replace(os.sep, "/")
    if rel == "." or rel == ".." or rel.startswith("../"):
        return False
    return add_git_excludes(project_path, [(rel, True)])[1] is not None


class FetchRecord:
    """Which files in a project came from the box: {relative path: {size, mtime, source, fetched_at}}."""

    def __init__(self, project_path):
        self.project_path = project_path
        self.path = os.path.join(project_path, STATE_DIR, RECORD)
        self.files = {}
        if os.path.isfile(self.path):
            try:
                with open(self.path, encoding="utf-8") as fh:
                    self.files = dict(json.load(fh).get("files") or {})
            except (OSError, ValueError):
                warn("cannot read %s; starting a new record" % self.path)

    def add(self, rel, source):
        dest = os.path.join(self.project_path, *rel.split("/"))
        try:
            st = os.stat(dest)
        except OSError:
            return
        self.files[rel] = {"size": st.st_size, "mtime": st.st_mtime, "source": source, "fetched_at": iso_now()}

    def unchanged(self, rel):
        """True when the file is still exactly as fetched (same size and modification time)."""
        entry = self.files.get(rel) or {}
        dest = os.path.join(self.project_path, *rel.split("/"))
        try:
            st = os.stat(dest)
        except OSError:
            return False
        return st.st_size == entry.get("size") and abs(st.st_mtime - float(entry.get("mtime") or 0)) < 2

    def under(self, rel):
        return sorted(p for p in self.files if not rel or p == rel or p.startswith(rel + "/"))

    def save(self):
        write_text(self.path, json.dumps({"version": 1, "files": self.files}, indent=1, sort_keys=True) + "\n")
        ensure_git_exclude(self.project_path, os.path.join(self.project_path, STATE_DIR))
