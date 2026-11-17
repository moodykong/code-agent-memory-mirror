"""sync: adopt stray memory files, secret scan, commit, pull --rebase, push. Never force.

Only memory/, skills/, AGENTS.md and CLAUDE.md are committed. A lock file in
the store's git folder stops two runs at once (a hook and a timer, say).
"""

import contextlib
import glob
import os
import re
import socket
import sys
import time

from agent_mirror.install import merge_tree
from agent_mirror.links import is_linklike
from agent_mirror.platforms import claude_slug, pid_alive
from agent_mirror.runtime import detect_runtime
from agent_mirror.util import err, git, is_git_toplevel, out, short, tilde, upstream, warn

SYNC_PATHS = ("memory", "AGENTS.md", "CLAUDE.md", "skills")
LOCK_NAME = "agent-mirror-sync.lock"
LOCK_STALE_SECONDS = 900

# (name, regex). Only names are ever printed, never the matched text.
SECRET_PATTERNS = [
    ("openai-style-key", re.compile(r"sk-[A-Za-z0-9]{20,}")),
    ("azure-storage-account-key", re.compile(r"AccountKey=")),
    ("azure-shared-access-signature", re.compile(r"SharedAccessSignature")),
    ("sas-signature", re.compile(r"sig=[A-Za-z0-9%]{20,}")),
    ("mongodb-uri-with-password", re.compile(r"mongodb(\+srv)?://[^ ]*:[^ ]*@")),
    ("jwt-like-token", re.compile(r"eyJ[A-Za-z0-9_-]{20,}")),
    ("private-key-block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY")),
    ("github-classic-token", re.compile(r"ghp_[A-Za-z0-9]{30,}")),
    ("github-fine-grained-token", re.compile(r"github_pat_[A-Za-z0-9_]{30,}")),
    ("aws-access-key-id", re.compile(r"AKIA[0-9A-Z]{16}")),
]


class Lock:
    def __init__(self, path):
        self.path = path
        self.held = False

    def _try(self):
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            return False
        with os.fdopen(fd, "w") as fh:
            fh.write("%d %d\n" % (os.getpid(), int(time.time())))
        return True

    def acquire(self):
        if self._try():
            self.held = True
            return True
        try:
            with open(self.path) as fh:
                pid = int((fh.read().split() or ["0"])[0])
            age = time.time() - os.path.getmtime(self.path)
        except (OSError, ValueError):
            pid, age = 0, LOCK_STALE_SECONDS + 1
        if age > LOCK_STALE_SECONDS or not pid_alive(pid):
            try:
                os.unlink(self.path)
            except OSError:
                pass
            if self._try():
                self.held = True
                return True
        return False

    def release(self):
        if self.held:
            try:
                os.unlink(self.path)
            except OSError:
                pass
            self.held = False


def adopt_strays(ctx, store, dry_run):
    """Copy files from real (unlinked) Claude memory folders into matching stores."""
    store_root = store.path("memory")
    if not os.path.isdir(store_root):
        return
    projects = [n for n in os.listdir(store_root) if os.path.isdir(os.path.join(store_root, n))]
    pattern = os.path.join(ctx.plat.claude_home(), "projects", "*", "memory")
    for mem in sorted(glob.glob(pattern)):
        if is_linklike(mem) or not os.path.isdir(mem):
            continue
        slug = os.path.basename(os.path.dirname(mem))
        matches = [p for p in projects if slug == claude_slug(p) or slug.endswith("-" + claude_slug(p))]
        if not matches:
            continue
        project = max(matches, key=len)
        copied, conflicts, _ = merge_tree(mem, os.path.join(store_root, project), dry_run)
        if copied or conflicts:
            out(
                "adopt %s into memory/%s: %d %s, %d conflicts (store copy kept)"
                % (tilde(mem), project, len(copied), "to copy" if dry_run else "copied", len(conflicts))
            )
            for rel in conflicts:
                warn("memory conflict: %s in %s differs from memory/%s/%s" % (rel, tilde(mem), project, rel))


def changed_files(root, paths, timeout):
    rc, stdout, stderr = git(root, ["status", "--porcelain", "-z", "--untracked-files=all", "--"] + paths, timeout)
    if rc != 0:
        raise SystemExit("error: git status failed in the store: %s" % short(stderr))
    tokens = stdout.split("\0")
    files = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        i += 1
        if len(tok) < 4:
            continue
        status, path = tok[:2], tok[3:]
        if status[0] in "RC":
            i += 1  # the next token is the old name
        files.append(path)
    return files


def scan_file(path):
    """Return the names of secret patterns found in one file."""
    try:
        with open(path, "rb") as fh:
            text = fh.read().decode("latin-1")
    except OSError:
        return []
    hits = []
    for line in text.splitlines():
        for name, rx in SECRET_PATTERNS:
            if name not in hits and rx.search(line):
                hits.append(name)
    return hits


def scan_files(root, rel_paths):
    found = []
    for rel in rel_paths:
        p = os.path.join(root, rel)
        if os.path.islink(p) or not os.path.isfile(p):
            continue
        hits = scan_file(p)
        if hits:
            found.append((rel, hits))
    return found


def cmd_sync(args, ctx):
    log = getattr(args, "log", None)
    if log:
        os.makedirs(os.path.dirname(os.path.abspath(log)), exist_ok=True)
        with open(log, "a", encoding="utf-8") as fh, contextlib.redirect_stdout(fh), contextlib.redirect_stderr(fh):
            print("--- %s sync" % time.strftime("%Y-%m-%dT%H:%M:%S"))
            return _locked_sync(args, ctx)
    return _locked_sync(args, ctx)


def _locked_sync(args, ctx):
    store = ctx.require_store()
    timeout = args.timeout
    if not is_git_toplevel(store.root, timeout):
        err("the store %s is not a git clone" % store.root)
        return 1
    rc, gitdir, _ = git(store.root, ["rev-parse", "--absolute-git-dir"], timeout)
    gitdir = gitdir.strip() if rc == 0 and gitdir.strip() else os.path.join(store.root, ".git")
    lock = Lock(os.path.join(gitdir, LOCK_NAME))
    if not lock.acquire():
        warn("another sync run holds %s; not running twice" % lock.path)
        return 0
    try:
        return _sync(args, ctx, store, timeout, gitdir)
    finally:
        lock.release()


def _sync(args, ctx, store, timeout, gitdir):
    root = store.root
    dry = args.dry_run
    if dry:
        out("dry run: nothing will change")
    adopt_strays(ctx, store, dry)
    paths = [p for p in SYNC_PATHS if os.path.lexists(os.path.join(root, p))]
    files = changed_files(root, paths, timeout)
    found = scan_files(root, files)
    if found:
        err("secret scan blocked the commit; remove these before syncing:")
        for rel, hits in found:
            print("  %s: %s" % (rel, ", ".join(hits)), file=sys.stderr, flush=True)
        return 3
    out("secret scan: %d changed files clean" % len(files))
    runtime = detect_runtime()
    message = args.message or "memory: sync from %s %s" % (runtime, socket.gethostname())
    if dry:
        if files:
            out("would commit %d files with message: %s" % (len(files), message))
        out("would run: git pull --rebase --autostash, then git push (if ahead)")
        return 0
    if files:
        rc, _, stderr = git(root, ["add", "-A", "--"] + paths, timeout)
        if rc != 0:
            err("git add failed: %s" % short(stderr))
            return 1
        rc, _, _ = git(root, ["diff", "--cached", "--quiet", "--"] + paths, timeout)
        if rc == 1:
            ident = []
            rc_e, email, _ = git(root, ["config", "user.email"], timeout)
            if rc_e != 0 or not email.strip():
                ident = ["-c", "user.name=agent-mirror", "-c", "user.email=agent-mirror@localhost"]
            rc, _, stderr = git(root, ident + ["commit", "--quiet", "-m", message, "--"] + paths, timeout)
            if rc != 0:
                err("git commit failed: %s" % short(stderr or "timed out"))
                return 1
            out("committed: %s" % message)
    else:
        out("nothing new to commit")
    up = upstream(root, timeout)
    if not up:
        warn("the store has no upstream branch; the commit stays local (push once by hand with -u)")
        return 0
    rc, _, stderr = git(root, ["pull", "--rebase", "--autostash", "--quiet"], timeout)
    if rc != 0:
        in_rebase = any(os.path.isdir(os.path.join(gitdir, d)) for d in ("rebase-merge", "rebase-apply"))
        if in_rebase:
            git(root, ["rebase", "--abort"], timeout)
            warn("pull --rebase hit a conflict; rebase aborted, the local commit is kept, nothing pushed")
        else:
            warn("pull --rebase failed (%s); the local commit is kept, nothing pushed" % short(stderr or "no output"))
        return 0
    rc, ahead, _ = git(root, ["rev-list", "--count", "@{u}..HEAD"], timeout)
    if rc == 0 and ahead.strip() not in ("", "0"):
        rc, _, stderr = git(root, ["push", "--quiet"], timeout)
        if rc != 0:
            warn("push failed (%s); the commit stays local, run sync again later" % short(stderr or "no output"))
        else:
            out("pushed %s commit(s) to %s" % (ahead.strip(), up))
    else:
        out("already in sync with %s" % up)
    return 0
