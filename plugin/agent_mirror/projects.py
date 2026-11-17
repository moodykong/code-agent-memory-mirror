"""Which store projects does this session touch?

The environment is per user, not per repo. A session may run inside one
clone, or in a folder ABOVE several clones (a cloud session that cloned
several repos). A repo only has a project entry in the store when it has
something to carry; other repos are listed and ignored.

Discovery:
  1. --project NAME forces one project (at --project-path, or the current repo or folder).
  2. If the start folder is inside a git repo, that repo is the only candidate.
  3. Otherwise child folders at depth 1 and 2 are scanned for git repos
     (hidden and vendor folders skipped, symlinks not followed, scan capped).
Each candidate repo is matched to a project by the config's projects map,
then by its origin URL against env.json "repo", then by folder name.
"""

import os
import re

from agent_mirror.config import match_config_project
from agent_mirror.util import err, expand, git_toplevel, out, run, tilde

VENDOR_DIRS = {
    "node_modules",
    "venv",
    "env",
    "__pycache__",
    "vendor",
    "dist",
    "build",
    "target",
    "site-packages",
    "bower_components",
}
SCAN_DIR_CAP = 2000
SCAN_REPO_CAP = 50


def remote_repo_path(url):
    """'owner/name' (lower case) from an https, ssh or scp-style git URL; None for a local path."""
    url = (url or "").strip()
    m = re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://(?:[^@/]*@)?[^/]+/(.+)$", url)
    if not m:
        m = re.match(r"^(?:[^@/\s]+@)?[^:/\s\\]{2,}:(?![\\/])(.+)$", url)
    if not m:
        return None
    path = m.group(1).strip().strip("/")
    path = re.sub(r"\.git$", "", path, flags=re.I).strip("/")
    return path.lower() or None


def repo_matches(url, repo):
    path = remote_repo_path(url)
    want = re.sub(r"\.git$", "", str(repo or "").strip().strip("/"), flags=re.I).lower()
    if not path or not want:
        return False
    return path == want or path.endswith("/" + want)


def origin_url(path):
    rc, stdout, _ = run(["git", "-C", path, "remote", "get-url", "origin"], timeout=5)
    return stdout.strip() if rc == 0 else ""


def scan_child_repos(start, max_depth=2, dir_cap=SCAN_DIR_CAP, repo_cap=SCAN_REPO_CAP):
    """Git repos in child folders of start (depth 1 and 2). Returns (repos, truncated)."""
    repos = []
    seen = 0
    level = [start]
    for _ in range(max_depth):
        nxt = []
        for d in level:
            try:
                with os.scandir(d) as it:
                    entries = sorted(it, key=lambda e: e.name)
            except OSError:
                continue
            for e in entries:
                try:
                    if not e.is_dir(follow_symlinks=False):
                        continue
                except OSError:
                    continue
                if e.name.startswith(".") or e.name in VENDOR_DIRS:
                    continue
                seen += 1
                if seen > dir_cap:
                    return repos, True
                if os.path.exists(os.path.join(e.path, ".git")):
                    repos.append(os.path.realpath(e.path))
                    if len(repos) >= repo_cap:
                        return repos, True
                    continue  # never look inside a repo for more repos
                nxt.append(e.path)
        level = nxt
    return repos, False


class Discovery:
    def __init__(self, start):
        self.start = start
        self.mode = "scan"  # forced, repo or scan
        self.matched = []  # [(project, clone path)]
        self.unmatched = []  # [clone path]
        self.truncated = False

    def names(self):
        found = []
        for name, _ in self.matched:
            if name not in found:
                found.append(name)
        return found

    def parent_link(self):
        """(project, session folder) when the session runs above exactly one matched project."""
        if self.mode == "scan" and len(self.names()) == 1:
            return self.names()[0], self.start
        return None

    def report(self, indent=""):
        if self.mode == "forced":
            name, path = self.matched[0]
            out("%sproject: %s at %s (forced with --project)" % (indent, name, path))
            return
        where = "repo" if self.mode == "repo" else "folder (scanned child folders for git repos)"
        out("%ssession %s: %s" % (indent, where, self.start))
        for name, path in self.matched:
            out("%s  project %s: %s" % (indent, name, path))
        for path in self.unmatched:
            out("%s  no project entry, nothing to carry: %s" % (indent, path))
        if not self.matched and not self.unmatched:
            out("%s  no git repos found here or in child folders" % indent)
        if self.truncated:
            out(
                "%s  (the scan stopped early: more than %d folders or %d repos)" % (indent, SCAN_DIR_CAP, SCAN_REPO_CAP)
            )


def match_repo(ctx, store, path):
    name = match_config_project(ctx.config, path)
    if name or store is None:
        return name
    url = origin_url(path)
    if url:
        for project in store.project_names():
            if repo_matches(url, store.load_env(project).get("repo")):
                return project
    base = os.path.basename(path)
    if os.path.isfile(store.env_path(base)):
        return base
    return None


def discover_projects(ctx, name=None, path=None):
    store = ctx.store_or_none(quiet=True)
    start = path or ctx.plat.env.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    start = os.path.realpath(expand(start))
    top = git_toplevel(start, timeout=5) if os.path.isdir(start) else None
    d = Discovery(start)
    if name:
        d.mode = "forced"
        d.matched = [(name, start if path else (top or start))]
        return d
    configured = match_config_project(ctx.config, start)
    if top:
        d.mode = "repo"
        repos = [top]
    elif configured:
        d.mode = "repo"  # a configured project folder that is not a git clone
        d.matched = [(configured, start)]
        return d
    else:
        repos, d.truncated = scan_child_repos(start)
    for repo in repos:
        project = match_repo(ctx, store, repo)
        if project:
            d.matched.append((project, repo))
        else:
            d.unmatched.append(repo)
    return d


def single_project(ctx, name=None, path=None):
    """(project, clone path) for commands that work on one project; exits when several match."""
    d = discover_projects(ctx, name, path)
    if len(d.matched) == 1:
        return d.matched[0]
    if len(d.matched) > 1:
        listed = ", ".join("%s (%s)" % (n, tilde(p)) for n, p in d.matched)
        err("several projects found under %s: %s; pass --project NAME --project-path PATH" % (d.start, listed))
        raise SystemExit(2)
    return None, (d.unmatched[0] if d.mode == "repo" and d.unmatched else d.start)
