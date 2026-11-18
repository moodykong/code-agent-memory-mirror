"""Cloud session-start: bring agent-mirror's own clone to the ref pinned in the store's store.json.

Only on a cloud runtime, only when the tool folder is a git clone whose origin matches store.json's
tool.repo, and never failing the session. The ref is read from store.json on the store's default
branch (bootstrap.pinned_tool), never from an inbox branch. `git fetch --depth 1 origin <ref>`
(without --depth for a clone that is not shallow, so a full clone stays full), then a checkout of
FETCH_HEAD; a ref that is the full commit already checked out needs no fetch. In a test sandbox it
never touches a clone outside the sandbox. A running program does not change under its own feet, so
when the commit changed the session-start command is run again once with the new code; the
AGENT_MIRROR_SELF_UPDATED marker in its environment stops a loop, and the step timings so far go
along (steplog.py), so one session start stays one timing record.
"""

import os
import re
import subprocess
import sys

from agent_mirror import PLUGIN_ROOT
from agent_mirror.platforms import current, real_inside
from agent_mirror.util import git, out, short, warn

MARKER = "AGENT_MIRROR_SELF_UPDATED"


def same_repo(a, b):
    """True when two remote URLs (or local paths) name the same repo."""

    def key(url):
        u = str(url or "").strip()
        if not u:
            return None
        if os.path.isabs(u) or u.startswith("file://"):
            path = u[7:] if u.startswith("file://") else u
            return os.path.normcase(os.path.realpath(path)).rstrip("/\\")
        u = re.sub(r"^[A-Za-z][A-Za-z0-9+.-]*://", "", u)
        u = re.sub(r"^[^@/]*@", "", u)
        u = re.sub(r"^([^:/]+):(?!\d)", r"\1/", u)  # scp form host:owner/name
        u = re.sub(r":\d+/", "/", u)
        u = re.sub(r"\.git/?$", "", u.rstrip("/"), flags=re.I)
        return u.lower()

    ka, kb = key(a), key(b)
    return bool(ka) and ka == kb


def tool_folder():
    return os.path.dirname(PLUGIN_ROOT)


def self_update(store, timeout, dry_run, argv=None, env=None, handoff=None):
    """None to go on with this process, or the exit code of the re-run with the new code.

    handoff: a function returning extra environment variables for the re-run (the step timings).
    """
    env = os.environ if env is None else env
    if env.get(MARKER):
        rc, head, _ = git(tool_folder(), ["rev-parse", "--short", "HEAD"], timeout)
        out("agent-mirror: running the updated code (%s)" % (head.strip() if rc == 0 else "?"))
        return None
    sandbox = current().sandbox()
    if sandbox and not real_inside(tool_folder(), sandbox):
        # a test run must never move or fetch into the clone the tests run from
        out("agent-mirror: test sandbox: no self-update of a tool clone outside %s" % sandbox)
        return None
    try:
        return _update(store, timeout, dry_run, argv, env, handoff)
    except Exception as exc:  # noqa: BLE001 (never fail the session)
        warn("agent-mirror self-update skipped: %s" % short(exc))
        return None


def _update(store, timeout, dry_run, argv, env, handoff=None):
    from agent_mirror.bootstrap import pinned_tool

    tool = tool_folder()
    ref, repo, why = pinned_tool(store, timeout)
    if ref is None:
        warn("agent-mirror self-update skipped: %s" % why)
        return None
    rc, top, _ = git(tool, ["rev-parse", "--show-toplevel"], timeout)
    if rc != 0 or os.path.normcase(os.path.realpath(top.strip())) != os.path.normcase(os.path.realpath(tool)):
        out("agent-mirror: %s is not a git clone; no self-update" % tool)
        return None
    rc, origin, _ = git(tool, ["remote", "get-url", "origin"], timeout)
    if rc != 0 or not same_repo(origin.strip(), repo):
        out("agent-mirror: the tool clone's origin is not store.json's tool.repo; no self-update")
        return None
    rc, before, _ = git(tool, ["rev-parse", "HEAD"], timeout)
    before = before.strip() if rc == 0 else ""
    if before and ref.lower() == before.lower():
        out("agent-mirror: tool at %s (ref %s, already current)" % (before[:12], ref))
        return None
    if dry_run:
        out(
            "would run: git -C %s fetch origin %s (--depth 1 for a shallow clone), then check out FETCH_HEAD"
            % (tool, ref)
        )
        return None
    # --depth 1 only for a shallow clone (what the setup scripts make); it would make a full clone shallow
    rc, shallow, _ = git(tool, ["rev-parse", "--is-shallow-repository"], timeout)
    depth = ["--depth", "1"] if rc == 0 and shallow.strip() == "true" else []
    rc, _, stderr = git(tool, ["fetch", "-q"] + depth + ["origin", ref], timeout)
    if rc != 0:
        warn(
            "agent-mirror self-update: fetch of %s failed (%s); keeping %s"
            % (ref, short(stderr or "no output"), before[:12])
        )
        return None
    rc, after, _ = git(tool, ["rev-parse", "FETCH_HEAD"], timeout)
    after = after.strip() if rc == 0 else ""
    if not after or after == before:
        out("agent-mirror: tool at %s (ref %s, already current)" % (before[:12] or "?", ref))
        return None
    rc, dirty, _ = git(tool, ["status", "--porcelain", "--untracked-files=no"], timeout)
    if rc != 0 or dirty.strip():
        warn("agent-mirror self-update: the tool clone has local changes; keeping %s" % before[:12])
        return None
    rc, _, stderr = git(tool, ["checkout", "-q", after], timeout)
    if rc != 0:
        warn("agent-mirror self-update: checkout failed (%s); keeping %s" % (short(stderr or "no output"), before[:12]))
        return None
    out(
        "agent-mirror: tool updated from %s to %s (ref %s); running session-start again with the new code"
        % (before[:12], after[:12], ref)
    )
    child = dict(env)
    child[MARKER] = "1"
    if handoff is not None:
        child.update(handoff())
    cmd = [sys.executable, os.path.join(PLUGIN_ROOT, "bin", "agent-mirror")] + list(
        argv if argv is not None else sys.argv[1:]
    )
    sys.stdout.flush()
    try:
        return subprocess.call(cmd, env=child)
    except OSError as exc:
        warn("agent-mirror self-update: could not start the new code (%s); this run goes on with the old code" % exc)
        return None
