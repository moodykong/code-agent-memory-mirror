"""Inbox branches: cloud sessions push there, never to the store's main branch.

A cloud session holds a token that can push to the store. With "cloud_push": "inbox"
(the default) its commits go to refs/heads/inbox/<runtime>-<machine id>. A developer
machine's sync then:

  - applies changes under memory/ automatically (new files, files main has not touched,
    union-merged MEMORY.md indexes; a both-sides change keeps main's file and saves the
    branch's version next to it as <stem>.from-<branch>.md),
  - blocks a branch whose memory changes trip the secret scan,
  - NEVER applies anything outside memory/ (instructions, skills, projects, store.json);
    those are held for `agent-mirror review`,
  - deletes a branch once everything on it was handled.

Why: a cloud session must not be able to change the instructions or runbooks that a local
agent with real credentials will follow. The cost: memory written in a cloud session reaches
other machines only after a developer machine syncs.
"""

import hashlib
import os
import shutil
import socket
import subprocess
import time

from agent_mirror.runtime import CLOUD_RUNTIMES, detect_runtime
from agent_mirror.platforms import current
from agent_mirror.util import err, git, out, short, warn

PREFIX = "inbox/"
ID_FILE = "agent-mirror-machine-id"


# git helpers ------------------------------------------------------------------------------------


def upstream_parts(root, timeout):
    rc, up, _ = git(root, ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"], timeout)
    up = up.strip()
    if rc != 0 or "/" not in up:
        return None, None
    remote, _, branch = up.partition("/")
    return remote, branch


def show_bytes(root, rev, path):
    try:
        proc = subprocess.run(
            ["git", "-C", root, "show", "%s:%s" % (rev, path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout if proc.returncode == 0 else None


def read_bytes(path):
    try:
        with open(path, "rb") as fh:
            return fh.read()
    except OSError:
        return None


def write_bytes(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(data)


def ident(root, timeout):
    rc, email, _ = git(root, ["config", "user.email"], timeout)
    if rc != 0 or not email.strip():
        return ["-c", "user.name=agent-mirror", "-c", "user.email=agent-mirror@localhost"]
    return []


def commit_paths(root, paths, message, timeout):
    if not paths:
        return False
    git(root, ["add", "-A", "--"] + paths, timeout)
    rc, _, _ = git(root, ["diff", "--cached", "--quiet", "--"] + paths, timeout)
    if rc != 1:
        return False
    rc, _, stderr = git(root, ident(root, timeout) + ["commit", "--quiet", "-m", message, "--"] + paths, timeout)
    if rc != 0:
        err("git commit failed: %s" % short(stderr))
        return False
    return True


# the cloud side -----------------------------------------------------------------------------------


def machine_id(gitdir):
    path = os.path.join(gitdir, ID_FILE)
    try:
        with open(path, encoding="utf-8") as fh:
            value = fh.read().strip()
        if value:
            return value
    except OSError:
        pass
    seed = "%s %.6f %d" % (socket.gethostname(), time.time(), os.getpid())
    value = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:10]
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(value + "\n")
    except OSError:
        pass
    return value


def branch_for(runtime, gitdir):
    return "%s%s-%s" % (PREFIX, runtime, machine_id(gitdir))


def push_to_inbox(root, gitdir, runtime, timeout, dry_run):
    remote, main = upstream_parts(root, timeout)
    if not remote:
        warn("the store has no upstream branch; the commit stays local")
        return 0
    name = branch_for(runtime, gitdir)
    if dry_run:
        out("would push the local commits to %s on %s (never to %s)" % (name, remote, main))
        return 0
    git(root, ["fetch", "--quiet", remote, main], timeout)
    rc, ahead, _ = git(root, ["rev-list", "--count", "%s/%s..HEAD" % (remote, main)], timeout)
    if rc != 0 or ahead.strip() in ("", "0"):
        out("nothing new for the inbox")
        return 0
    # only this machine writes this branch, so replacing it after a rebase is safe; main is never pushed
    rc, _, stderr = git(root, ["push", "--quiet", remote, "+HEAD:refs/heads/%s" % name], timeout)
    if rc != 0:
        warn(
            "push to %s failed (%s); the commit stays local, run sync again later"
            % (name, short(stderr or "no output"))
        )
        return 0
    out(
        "pushed %s commit(s) to %s on %s; a developer machine's sync applies the memory changes "
        "(a cloud session never pushes to %s)" % (ahead.strip(), name, remote, main)
    )
    return 0


# the developer side ---------------------------------------------------------------------------------


def fetch_inbox(root, remote, timeout):
    spec = "+refs/heads/%s*:refs/remotes/%s/%s*" % (PREFIX, remote, PREFIX)
    rc, _, stderr = git(root, ["fetch", "--quiet", "--prune", remote, spec], timeout)
    if rc != 0:
        warn("could not fetch inbox branches (%s)" % short(stderr or "no output"))
        return False
    return True


def inbox_branches(root, remote, timeout):
    rc, refs, _ = git(root, ["for-each-ref", "--format=%(refname)", "refs/remotes/%s/%s" % (remote, PREFIX)], timeout)
    if rc != 0:
        return []
    cut = len("refs/remotes/%s/" % remote)
    return sorted(r.strip()[cut:] for r in refs.splitlines() if r.strip())


def resolve_branch(name, remote):
    n = name.strip()
    for prefix in ("refs/remotes/%s/" % remote, "refs/heads/", "%s/" % remote):
        if n.startswith(prefix):
            n = n[len(prefix) :]
    return n if n.startswith(PREFIX) else PREFIX + n


def leaf(name):
    return name[len(PREFIX) :] if name.startswith(PREFIX) else name


def union_merge(base, ours, theirs):
    """A union merge for index files: keep ours, add theirs' new lines, never duplicate a line."""
    if theirs == base:
        return ours
    if ours == base:
        return theirs
    ours_lines = ours.splitlines()
    seen = set(ours_lines)
    base_lines = set(base.splitlines())
    extra = []
    for line in theirs.splitlines():
        if line.strip() and line not in seen and line not in base_lines:
            seen.add(line)
            extra.append(line)
    text = "\n".join(ours_lines + extra)
    return text + "\n" if text else ""


def side_path(path, branch):
    head, tail = os.path.split(path)
    stem, ext = os.path.splitext(tail)
    return os.path.join(head, "%s.from-%s%s" % (stem, leaf(branch), ext or ""))


class BranchReport:
    def __init__(self, name):
        self.name = name
        self.base = None
        self.memory = []  # [(status, path)]
        self.held = []  # [(status, path)]
        self.applied = []
        self.side_files = []  # [(path, side path)]
        self.kept_deletions = []
        self.blocked = []  # [(path, [pattern names])]

    def handled(self):
        return self.base is not None and not self.held and not self.blocked and not self.kept_deletions


def changes(root, base, ref, timeout):
    rc, text, _ = git(root, ["diff", "--name-status", "--no-renames", "-z", base, ref], timeout)
    if rc != 0:
        return None
    tokens = [t for t in text.split("\0")]
    found = []
    i = 0
    while i + 1 < len(tokens):
        status, path = tokens[i].strip(), tokens[i + 1]
        i += 2
        if status and path:
            found.append((status[0], path))
    return found


def analyse(root, remote, name, timeout):
    """BranchReport with the branch's changes split into memory and held, nothing applied."""
    rep = BranchReport(name)
    ref = "refs/remotes/%s/%s" % (remote, name)
    rc, base, _ = git(root, ["merge-base", "HEAD", ref], timeout)
    if rc != 0 or not base.strip():
        return rep
    rep.base = base.strip()
    found = changes(root, rep.base, ref, timeout) or []
    rep.memory = [(st, p) for st, p in found if p.startswith("memory/")]
    rep.held = [(st, p) for st, p in found if not p.startswith("memory/")]
    return rep


def apply_memory(root, remote, rep, timeout, dry_run):
    """Apply a branch's memory changes to the working tree (and commit them unless dry_run)."""
    from agent_mirror.sync import SECRET_PATTERNS

    if rep.base is None:
        return []
    ref = "refs/remotes/%s/%s" % (remote, rep.name)
    rc, changed, _ = git(root, ["diff", "--name-only", "-z", rep.base, "HEAD"], timeout)
    main_changed = set(p for p in changed.split("\0") if p) if rc == 0 else set()
    contents = {}
    for st, p in rep.memory:
        if st == "D":
            continue
        data = show_bytes(root, ref, p) or b""
        contents[p] = data
        text = data.decode("utf-8", "replace")
        hits = [n for n, rx in SECRET_PATTERNS if rx.search(text)]
        if hits:
            rep.blocked.append((p, hits))
    if rep.blocked:
        return []
    touched = []
    for st, p in rep.memory:
        dest = os.path.join(root, *p.split("/"))
        ours = read_bytes(dest)
        if st == "D":
            if p in main_changed:
                rep.kept_deletions.append(p)
            elif ours is not None:
                rep.applied.append(p)
                touched.append(p)
                if not dry_run:
                    os.remove(dest)
            continue
        theirs = contents[p]
        if ours == theirs:
            continue
        if ours is None and p not in main_changed:
            result = theirs
        elif os.path.basename(p) == "MEMORY.md":
            base = (show_bytes(root, rep.base, p) or b"").decode("utf-8", "replace")
            merged = union_merge(base, (ours or b"").decode("utf-8", "replace"), theirs.decode("utf-8", "replace"))
            result = merged.encode("utf-8")
            if result == ours:
                continue
        elif p not in main_changed:
            result = theirs
        else:
            side = side_path(p, rep.name)
            side_dest = os.path.join(root, *side.split("/"))
            if read_bytes(side_dest) != theirs:
                rep.side_files.append((p, side))
                touched.append(side)
                if not dry_run:
                    write_bytes(side_dest, theirs)
            continue
        rep.applied.append(p)
        touched.append(p)
        if not dry_run:
            write_bytes(dest, result)
    if touched and not dry_run:
        commit_paths(root, touched, "memory: apply %s (%d file(s))" % (rep.name, len(touched)), timeout)
    return touched


def report(rep):
    if rep.base is None:
        warn(
            "%s shares no history with the main branch; left alone (agent-mirror review discard %s)"
            % (rep.name, rep.name)
        )
        return
    if rep.blocked:
        warn(
            "%s: the secret scan blocked it; nothing applied (%s)"
            % (rep.name, "; ".join("%s: %s" % (p, ", ".join(h)) for p, h in rep.blocked))
        )
    if rep.applied:
        out("%s: applied %d memory file(s): %s" % (rep.name, len(rep.applied), ", ".join(rep.applied)))
    for p, side in rep.side_files:
        warn(
            "%s: %s changed on both sides; kept main's version and saved the branch's as %s (not in the index)"
            % (rep.name, p, side)
        )
    for p in rep.kept_deletions:
        warn("%s: the branch deletes %s, but main changed it; kept" % (rep.name, p))
    if rep.held:
        warn(
            "%s: held for review (not applied): %s; see `agent-mirror review show %s`"
            % (rep.name, ", ".join(p for _, p in rep.held), rep.name)
        )


def delete_branch(root, remote, name, timeout):
    rc, _, stderr = git(root, ["push", "--quiet", remote, "--delete", "refs/heads/%s" % name], timeout)
    git(root, ["update-ref", "-d", "refs/remotes/%s/%s" % (remote, name)], timeout)
    if rc != 0:
        warn("could not delete %s on %s (%s)" % (name, remote, short(stderr or "no output")))
        return False
    return True


def process_inbox(root, remote, timeout):
    """Apply every inbox branch's memory changes. Returns the branches that can be deleted after the push."""
    if not fetch_inbox(root, remote, timeout):
        return []
    done = []
    for name in inbox_branches(root, remote, timeout):
        rep = analyse(root, remote, name, timeout)
        apply_memory(root, remote, rep, timeout, dry_run=False)
        report(rep)
        if rep.handled():
            done.append(name)
    return done


def held_summary(root, remote, timeout):
    """[(branch, held paths)] from the remote-tracking refs as of the last fetch (no network)."""
    found = []
    for name in inbox_branches(root, remote, timeout):
        rep = analyse(root, remote, name, timeout)
        found.append((name, [p for _, p in rep.held], len(rep.memory)))
    return found


# agent-mirror review ----------------------------------------------------------------------------------


def cmd_review(args, ctx):
    runtime = detect_runtime()
    if runtime in CLOUD_RUNTIMES:
        err("agent-mirror review runs on a developer machine (this is %s)" % runtime)
        return 2
    store = ctx.require_store()
    root, timeout = store.root, args.timeout
    remote, main = upstream_parts(root, timeout)
    if not remote:
        err("the store has no upstream branch, so there are no inbox branches to review")
        return 1
    fetch_inbox(root, remote, timeout)
    action = args.action or "list"
    if action == "list":
        names = inbox_branches(root, remote, timeout)
        if not names:
            out("no inbox branches on %s" % remote)
            return 0
        for name in names:
            rep = analyse(root, remote, name, timeout)
            held = ", ".join(p for _, p in rep.held) or "none"
            out("%s: %d memory change(s), held for review: %s" % (name, len(rep.memory), held))
        return 0
    if not args.branch:
        err("review %s needs a BRANCH (agent-mirror review list shows them)" % action)
        return 2
    name = resolve_branch(args.branch, remote)
    if name not in inbox_branches(root, remote, timeout):
        err("no inbox branch %s on %s" % (name, remote))
        return 1
    rep = analyse(root, remote, name, timeout)
    ref = "refs/remotes/%s/%s" % (remote, name)
    if action == "discard":
        if delete_branch(root, remote, name, timeout):
            out("discarded %s (deleted on %s); nothing from it was applied by this command" % (name, remote))
            return 0
        return 1
    if rep.base is None:
        err("%s shares no history with the main branch; discard it instead" % name)
        return 1
    if action == "show":
        out("%s (from %s)" % (name, rep.base[:12]))
        for st, p in rep.memory:
            out("  memory %s %s (applied by sync)" % (st, p))
        for st, p in rep.held:
            out("  held   %s %s" % (st, p))
        if rep.held:
            rc, diff, _ = git(root, ["diff", rep.base, ref, "--"] + [p for _, p in rep.held], timeout)
            out(diff.rstrip())
        return 0
    # accept
    rc, dirty, _ = git(root, ["status", "--porcelain", "--", "memory"] + [p for _, p in rep.held], timeout)
    if dirty.strip():
        err(
            "uncommitted local changes touch these paths; run `agent-mirror sync` or commit first:\n%s" % dirty.rstrip()
        )
        return 1
    held_paths = [p for _, p in rep.held]
    chosen = list(args.paths) if args.paths else list(held_paths)
    unknown = [p for p in chosen if p not in held_paths]
    if unknown:
        err("not held on %s: %s" % (name, ", ".join(unknown)))
        return 2
    apply_memory(root, remote, rep, timeout, dry_run=False)
    report(rep)
    if rep.blocked:
        return 3
    from agent_mirror.sync import scan_file

    rc, stat, _ = git(root, ["diff", "--stat", rep.base, ref, "--"] + chosen, timeout) if chosen else (0, "", "")
    if chosen:
        out("accepting from %s:\n%s" % (name, stat.rstrip()))
    rc, changed, _ = git(root, ["diff", "--name-only", "-z", rep.base, "HEAD"], timeout)
    main_changed = set(p for p in changed.split("\0") if p)
    status = dict((p, st) for st, p in rep.held)
    tmp = current().mkdtemp("agent-mirror-review-")
    try:
        for p in chosen:
            if status[p] == "D":
                continue
            data = show_bytes(root, ref, p) or b""
            probe = os.path.join(tmp, "f")
            write_bytes(probe, data)
            hits = scan_file(probe)
            if hits:
                err("the secret scan blocked %s (%s); nothing accepted" % (p, ", ".join(hits)))
                return 3
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    for p in chosen:
        dest = os.path.join(root, *p.split("/"))
        if p in main_changed:
            warn("main also changed %s since the branch started; the branch version replaces it" % p)
        if status[p] == "D":
            if os.path.lexists(dest):
                os.remove(dest)
        else:
            write_bytes(dest, show_bytes(root, ref, p) or b"")
    if chosen:
        commit_paths(root, chosen, "review: accept %d file(s) from %s" % (len(chosen), name), timeout)
    rc, ahead, _ = git(root, ["rev-list", "--count", "@{u}..HEAD"], timeout)
    if rc == 0 and ahead.strip() not in ("", "0"):
        rc, _, stderr = git(root, ["push", "--quiet"], timeout)
        if rc != 0:
            warn(
                "push failed (%s); the accepted commit stays local and %s is kept"
                % (short(stderr or "no output"), name)
            )
            return 1
        out("pushed %s commit(s) to %s/%s" % (ahead.strip(), remote, main))
    if set(chosen) == set(held_paths) and not rep.kept_deletions:
        if delete_branch(root, remote, name, timeout):
            out("accepted and deleted %s" % name)
    else:
        out("accepted %d file(s); %s is kept for the rest" % (len(chosen), name))
    return 0
