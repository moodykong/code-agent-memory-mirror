"""cloud bootstrap, and the setup drift repair at cloud session start.

The setup script pasted on a vendor page is a small fixed bootstrap: it clones the store, checks out
agent-mirror at the ref store.json pins, and runs `agent-mirror cloud bootstrap --project NAME --vendor V`.
That command reads each project's env.json from the store's default branch and does what the setup
scripts of 0.5.1 and earlier did themselves: companion repos, the Python version through uv, the
`setup_commands`, then init-store --adopt, install and (Claude) the session hooks. Each step is timed into
the setup note file (~/agent_setup_probe.txt).

The pasted script never changes, so the vendor never rebuilds its cached machine on its own. The drift
repair takes its place: bootstrap writes a stamp on the machine (never in the store) with a hash of the
setup inputs it acted on (python, setup_commands, companion_repos) and the tool commit it ran with. On a
cloud runtime, session-start pulls the store, lets the self-update move the tool to the pinned ref, then
compares the stamp with the freshly pulled default branch: a project whose setup inputs changed has its
setup steps run again (they must be idempotent), and the stamp is updated. It never fails the session.

Each new session starts from the vendor's cached machine, so the stamp a repair writes is gone at the
next session: the repair repeats at every session start until the vendor rebuilds the machine (raise
the `# revision:` line of the pasted script to make it rebuild).

Setup commands run with `bash -c` in the folder the setup script ran in, with STORE, TOOL, AM and NOTE
set as the setup scripts of 0.5.1 set them. Their output goes to the vendor's setup log during
bootstrap and nowhere during a repair; the step log and the note file hold names, times and exit
status only.

Safety: both run store content as the machine's user (root on a cloud machine). They read env.json and
store.json with `git show` from the store's default branch as fetched (refs/remotes/origin/HEAD, else
the upstream of the checked-out branch), never from the working tree, and refuse a checkout on an
inbox/* branch (where cloud sessions push). A developer machine's sync never applies anything outside
memory/ from an inbox branch (inbox.py), so env.json, projects/*/cloud/ and store.json on the default
branch come only from a person.
"""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time

from agent_mirror import PLUGIN_ROOT, __version__, companions
from agent_mirror.platforms import current, new_group_kwargs, real_inside, stop_tree
from agent_mirror.runtime import CLOUD_RUNTIMES, detect_runtime
from agent_mirror.util import err, exe_path, git, is_git_toplevel, out, run, short, tilde, warn, write_text

STAMP_NAME = "bootstrap-stamp.json"
STAMP_SCHEMA = 1
STEPS_VERSION = 1  # part of the hash: a change to what the steps do runs them again
INBOX_PREFIX = "inbox/"
INPUT_KEYS = ("python", "setup_commands", "companion_repos")
UV_INSTALL = "curl -LsSf https://astral.sh/uv/install.sh | sh"
UV_TIMEOUT = 120
PYTHON_TIMEOUT = 600
CLONE_TIMEOUT = 120
STEP_TIMEOUT = 1800
REPAIR_TIMEOUT = 900  # drift repair at session start, all steps together (the Claude hook allows 1800 s)
VENDOR_AGENT = {"claude": "claude", "codex": "codex"}
OUT_OF_TIME = "skipped (out of time)"


def script_env(store_root, note):
    """The shell variables the setup scripts of 0.5.1 gave their setup_commands, as environment variables."""
    tool = os.path.dirname(PLUGIN_ROOT)
    env = {"STORE": store_root, "TOOL": tool, "AM": os.path.join(PLUGIN_ROOT, "bin", "agent-mirror")}
    if note:
        env["NOTE"] = note
    return env


# the store's default branch ------------------------------------------------------------------------


NO_DEFAULT = "cannot tell the store's default branch (a detached checkout and no origin/HEAD)"


def default_branch(root, timeout):
    """(ref to read from, None) for the store's default branch, or (None, reason).

    The ref is refs/remotes/origin/HEAD's target when the clone has it (a fresh clone does), else the
    upstream of the checked-out branch, else that branch. Files are read from the ref with `git show`,
    never from the working tree, so local commits and edits on the machine are not read while the
    remote-tracking ref exists. Refused: a checkout on an inbox/* branch, and a ref that names one.
    """
    if not is_git_toplevel(root, timeout):
        return None, "the store %s is not a git clone" % root
    rc, cur, _ = git(root, ["symbolic-ref", "-q", "--short", "HEAD"], timeout)
    current = cur.strip() if rc == 0 and cur.strip() else None
    if current and current.startswith(INBOX_PREFIX):
        return None, "the store's checkout is on the inbox branch %s, not its default branch" % current
    found = None
    rc, head, _ = git(root, ["symbolic-ref", "-q", "refs/remotes/origin/HEAD"], timeout)
    if rc == 0 and head.strip().startswith("refs/remotes/"):
        found = head.strip()
    if found is None and current:
        rc, up, _ = git(root, ["rev-parse", "--symbolic-full-name", "@{u}"], timeout)
        if rc == 0 and up.strip().startswith("refs/"):
            found = up.strip()
    if found is None and current:
        found = "refs/heads/%s" % current
    if found is None:
        return None, NO_DEFAULT
    if found.startswith("refs/remotes/"):
        branch = found.split("/", 3)[3] if found.count("/") >= 3 else ""
    else:
        branch = found[len("refs/heads/") :]
    if not branch or branch.startswith(INBOX_PREFIX):
        return None, "the store's default branch %r is an inbox branch or empty; not read" % branch
    return found, None


def setup_ref(root, timeout):
    """The ref the setup steps read env.json and store.json from: the default branch, or, when it cannot be
    told (a detached checkout without origin/HEAD, as a vendor's own checkout of the store may be), the
    checked-out commit, as the setup script reads the tool ref. An inbox branch is still refused."""
    ref, why = default_branch(root, timeout)
    if ref is None and why == NO_DEFAULT:
        return "HEAD", None
    return ref, why


def skipped_entry(why):
    """A stamp entry for a project whose setup steps did not run: no inputs, so any input counts as drift."""
    return {"inputs": None, "keys": {}, "at": utc(), "failed": [], "skipped": why}


def read_json_at(root, ref, rel, timeout):
    """(dict or None, problem or None) for a JSON file of the store at ref."""
    rc, text, stderr = git(root, ["show", "%s:%s" % (ref, rel)], max(timeout, 30))
    if rc != 0:
        return None, "%s is not on the default branch (%s)" % (rel, short(stderr or "no output", 120))
    try:
        data = json.loads(text)
    except ValueError as exc:
        return None, "%s on the default branch is not valid JSON: %s" % (rel, exc)
    if not isinstance(data, dict):
        return None, "%s on the default branch does not hold a JSON object" % rel
    return data, None


def pinned_tool(store, timeout):
    """(tool.ref, tool.repo, None) from store.json on the default branch, or (None, None, reason).

    A store folder that is not a git clone, or a clone whose default branch cannot be told (a detached
    checkout without origin/HEAD), has its store.json read from the working tree, as 0.5.1 did. A
    checkout on an inbox/* branch is refused.
    """
    from agent_mirror import DEFAULT_REF, TOOL_REPO

    if not is_git_toplevel(store.root, timeout):
        return store.tool_ref(), store.tool_repo(), None
    ref, why = default_branch(store.root, timeout)
    if ref is None and why == NO_DEFAULT:
        return store.tool_ref(), store.tool_repo(), None
    if ref is None:
        return None, None, why
    meta, problem = read_json_at(store.root, ref, "store.json", timeout)
    if meta is None:
        if problem and "is not on the default branch" in problem:
            return DEFAULT_REF, TOOL_REPO, None
        return None, None, problem
    tool = meta.get("tool") or {}
    return str(tool.get("ref") or DEFAULT_REF), str(tool.get("repo") or TOOL_REPO), None


# setup inputs ----------------------------------------------------------------------------------------


def input_problems(env):
    from agent_mirror.cloud import SAFE_PYTHON

    found = []
    python = str(env.get("python") or "").strip()
    if python and not SAFE_PYTHON.match(python):
        found.append("python %r is not a plain version" % python)
    commands = env.get("setup_commands")
    if commands is not None and (not isinstance(commands, list) or not all(isinstance(c, str) for c in commands)):
        found.append("setup_commands must be a list of shell lines")
    return found + companions.problems(env)


def setup_inputs(env, agent):
    """What the setup steps act on, as plain JSON values."""
    return {
        "steps": STEPS_VERSION,
        "agent": agent,
        "python": str(env.get("python") or "").strip(),
        "setup_commands": [c for c in env.get("setup_commands") or [] if isinstance(c, str)],
        "companion_repos": [list(e) for e in companions.entries(env)],
    }


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def input_record(inputs):
    return {"inputs": digest(inputs), "keys": dict((k, digest(inputs[k])) for k in INPUT_KEYS)}


# the stamp on this machine ----------------------------------------------------------------------------


def stamp_path(plat):
    return os.path.join(plat.config_dir(), STAMP_NAME)


def load_stamp(plat):
    """The stamp dict, None when there is none. A damaged stamp raises ValueError."""
    path = stamp_path(plat)
    if not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict) or not isinstance(data.get("projects"), dict):
        raise ValueError("%s is not a bootstrap stamp" % path)
    return data


def save_stamp(plat, data):
    write_text(stamp_path(plat), json.dumps(data, indent=2, sort_keys=True) + "\n")


def tool_commit(timeout=10):
    rc, head, _ = git(os.path.dirname(PLUGIN_ROOT), ["rev-parse", "HEAD"], timeout)
    return head.strip() if rc == 0 and head.strip() else None


def utc():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# running the steps ------------------------------------------------------------------------------------


def run_step(argv, cwd, timeout, capture, extra_env=None):
    """util.run, but in its own process group, so that a timeout also stops what the command started
    (a setup command's children would otherwise hold its output open). Never raises."""
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env.update(extra_env or {})
    argv = [str(a) for a in argv]
    blocked = current().tool_blocked(argv[0])
    if blocked:
        return None, "", blocked
    argv[0] = exe_path(argv[0])
    pipe = subprocess.PIPE if capture else None
    try:
        proc = subprocess.Popen(
            argv,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=pipe,
            stderr=pipe,
            encoding="utf-8",
            errors="replace",
            **new_group_kwargs()
        )
    except OSError as exc:
        return None, "", str(exc)
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        stop_tree(proc)
        return None, "", "timed out after %s s" % timeout
    except KeyboardInterrupt:
        stop_tree(proc)
        raise
    return proc.returncode, stdout or "", stderr or ""


class Runner:
    """Runs setup steps one by one: timed into the step log and the note file, never raising.

    capture: keep the programs' output out of this process's output (session start); bootstrap lets it
    through to the vendor's setup log. Output is never written to a log file.
    """

    def __init__(self, plat, note=None, log=None, capture=False, deadline=None, dry_run=False, cwd=None, env=None):
        self.plat = plat
        self.note_path = note
        self.log = log
        self.capture = capture
        self.deadline = deadline
        self.dry_run = dry_run
        self.cwd = cwd if cwd and os.path.isdir(cwd) else None
        self.env = env  # extra environment variables for the setup_commands (script_env)
        self.failed = []
        self.ran = []

    def out_of_time(self, since):
        """True when a step after the first `since` steps was skipped or stopped because the time ran out."""
        return any("out of time" in status for _, status in self.ran[since:])

    def note(self, text):
        if not self.note_path or self.dry_run:
            return
        try:
            with open(self.note_path, "a", encoding="utf-8") as fh:
                fh.write(text + "\n")
        except OSError:
            pass

    def finish(self, name, start, status):
        end = time.time()
        if self.log is not None:
            self.log.add(name, start, end, status)
        if status != "ok" and not status.startswith("skipped"):
            self.failed.append(name)
        self.ran.append((name, status))
        line = "%s: %s in %d s" % (name, status, int(end - start))
        out("  " + line)
        self.note(line)
        return status

    def skip(self, name, why):
        return self.finish(name, time.time(), "skipped (%s)" % why)

    def run(self, name, argvs, timeout, shown=None, env=None):
        """Run one or more argv lists as one step; stops at the first failure. Returns the status."""
        start = time.time()
        if self.dry_run:
            for argv in argvs:
                out("  would run (%s): %s" % (name, shown or " ".join(argv)))
            return "skipped (dry run)"
        for argv in argvs:
            left = timeout
            if self.deadline is not None:
                left = min(timeout, int(self.deadline - time.time()))
                if left <= 0:
                    return self.finish(name, start, OUT_OF_TIME)
            rc, _, stderr = run_step(argv, self.cwd, left, self.capture, env)
            if rc is None:
                if "timed out" not in (stderr or ""):
                    return self.finish(name, start, "failed (could not start)")
                cut = left < timeout  # the time for all the steps ran out, not the step's own limit
                return self.finish(
                    name, start, "failed (%s after %s s)" % ("out of time" if cut else "timed out", left)
                )
            if rc != 0:
                return self.finish(name, start, "failed (exit %s)" % rc)
        return self.finish(name, start, "ok")


def sandboxed_away(plat, program):
    """In a test sandbox: True when program is not inside it (so nothing real is downloaded)."""
    root = plat.sandbox()
    if not root:
        return False
    found = program if os.path.isabs(str(program)) else shutil.which(str(program))
    return not (found and real_inside(found, root))


def run_setup(plat, runner, project, inputs, step_timeout):
    """The setup steps of one project: companion repos, Python through uv, setup_commands."""
    for repo, ref, folder in inputs["companion_repos"]:
        # public repos, no token: git init, origin, fetch --depth 1 of the ref, check out FETCH_HEAD
        name = "companion %s (%s)" % (folder, project)
        d = os.path.join(plat.companions_dir(), folder)
        fetch = [
            ["git", "-C", d, "fetch", "-q", "--depth", "1", "origin", ref],
            ["git", "-C", d, "checkout", "-q", "--force", "FETCH_HEAD"],
        ]
        if runner.dry_run:
            runner.run(
                name, [["git", "-C", d, "init", "-q"], ["git", "-C", d, "remote", "add", "origin", repo]] + fetch, 0
            )
            continue
        if plat.sandbox():
            runner.skip(name, "test sandbox: no network clone")
            continue
        try:
            os.makedirs(d, exist_ok=True)
        except OSError as exc:
            runner.skip(name, "cannot make %s: %s" % (d, exc))
            continue
        run(["git", "-C", d, "init", "-q"], timeout=30)
        rc, _, _ = run(["git", "-C", d, "remote", "add", "origin", repo], timeout=30)
        if rc != 0:
            run(["git", "-C", d, "remote", "set-url", "origin", repo], timeout=30)
        runner.run(name, fetch, CLONE_TIMEOUT)
    python = inputs["python"]
    if python:
        local_bin = os.path.join(plat.home(), ".local", "bin")
        if shutil.which("uv") is None:
            if plat.sandbox():
                runner.skip("uv install (%s)" % project, "test sandbox: no download")
            elif shutil.which("bash") is None:
                runner.skip("uv install (%s)" % project, "bash not found")
            else:
                runner.run(
                    "uv install (%s)" % project, [[shutil.which("bash"), "-c", UV_INSTALL]], UV_TIMEOUT, UV_INSTALL
                )
        parts = os.environ.get("PATH", "").split(os.pathsep)
        if local_bin not in parts:
            os.environ["PATH"] = os.pathsep.join([local_bin] + [p for p in parts if p])
        uv = shutil.which("uv")
        name = "python %s (%s)" % (python, project)
        if uv is None:
            runner.skip(name, "uv not found")
        elif sandboxed_away(plat, uv):
            runner.skip(name, "test sandbox: uv outside the sandbox")
        else:
            runner.run(name, [[uv, "python", "install", python]], PYTHON_TIMEOUT)
    commands = inputs["setup_commands"]
    bash = shutil.which("bash")
    for i, cmd in enumerate(commands, 1):
        name = "setup command %d of %d (%s)" % (i, len(commands), project)
        if bash is None:
            runner.skip(name, "bash not found")
            continue
        runner.run(name, [[bash, "-c", cmd]], step_timeout, "bash -c %s" % json.dumps(cmd), env=runner.env)


def self_cmd(store_root, *args):
    return [sys.executable, os.path.join(PLUGIN_ROOT, "bin", "agent-mirror"), "--store", store_root] + list(args)


# agent-mirror cloud bootstrap ----------------------------------------------------------------------------


def cmd_cloud_bootstrap(args, ctx):
    plat = ctx.plat
    runtime = detect_runtime()
    if runtime not in CLOUD_RUNTIMES:
        err(
            "cloud bootstrap runs only on a cloud machine (this is %s); the setup script sets "
            "AGENT_RUNTIME before it runs this command" % runtime
        )
        return 2
    store = ctx.require_store()
    agent = VENDOR_AGENT[args.vendor]
    timeout = args.timeout
    note = None if args.dry_run else os.path.abspath(args.note or os.path.join(plat.home(), "agent_setup_probe.txt"))
    out("agent-mirror %s cloud bootstrap: %s, store %s" % (__version__, runtime, store.root))
    if args.dry_run:
        out("dry run: nothing will change")
    runner = Runner(
        plat, note=note, capture=False, dry_run=args.dry_run, cwd=os.getcwd(), env=script_env(store.root, note)
    )
    ref, why = setup_ref(store.root, timeout)
    stamp = {
        "schema": STAMP_SCHEMA,
        "vendor": args.vendor,
        "agent": agent,
        "cwd": os.getcwd(),
        "note": note,
        "at": utc(),
        "tool": {"commit": tool_commit(), "version": __version__},
        "store": {"ref": ref},
        # a project whose steps cannot run now is recorded without inputs: once the store is fixed, the
        # next session start sees drift and runs them
        "projects": dict((p, skipped_entry(why or "not set up")) for p in args.project),
    }
    if ref is None:
        err("%s; nothing from the store's project settings is run" % why)
        runner.note("bootstrap: %s; no setup steps ran" % why)
    else:
        rc, commit, _ = git(store.root, ["rev-parse", ref], timeout)
        stamp["store"]["commit"] = commit.strip() if rc == 0 else None
        meta, _ = read_json_at(store.root, ref, "store.json", timeout)
        stamp["tool"]["ref"] = str(((meta or {}).get("tool") or {}).get("ref") or "")
        for project in args.project:
            env, problem = read_json_at(store.root, ref, "projects/%s/env.json" % project, timeout)
            if env is None:
                warn("project %s: %s; skipped" % (project, problem))
                runner.note("project %s: skipped (%s)" % (project, short(problem, 160)))
                stamp["projects"][project] = skipped_entry(short(problem, 160))
                continue
            problems = input_problems(env)
            if problems:
                for p in problems:
                    warn(
                        "project %s: %s (in projects/%s/env.json); its setup steps are skipped" % (project, p, project)
                    )
                runner.note("project %s: setup steps skipped (env.json has problems)" % project)
                stamp["projects"][project] = skipped_entry("env.json has problems")
                continue
            inputs = setup_inputs(env, agent)
            out("project %s: setup steps from the default branch (%s)" % (project, ref))
            before = len(runner.failed)
            run_setup(plat, runner, project, inputs, args.step_timeout)
            entry = input_record(inputs)
            entry.update({"at": utc(), "failed": runner.failed[before:]})
            stamp["projects"][project] = entry
    # links into the store and (Claude) the session hooks, as the setup scripts of 0.5.1 did
    steps = [
        ("init-store --adopt", self_cmd(store.root, "init-store", store.root, "--adopt")),
        ("install --agents %s" % agent, self_cmd(store.root, "install", "--agents", agent)),
    ]
    if agent == "claude":
        steps.append(("hooks install", self_cmd(store.root, "hooks", "install", "--agent", "claude")))
    for name, argv in steps:
        runner.run(name, [argv], STEP_TIMEOUT)
    if args.dry_run:
        return 0
    try:
        save_stamp(plat, stamp)
        out(
            "bootstrap stamp: %s (setup inputs of %s)"
            % (tilde(stamp_path(plat)), ", ".join(sorted(stamp["projects"])) or "no project")
        )
    except OSError as exc:
        warn(
            "could not write the bootstrap stamp %s: %s; session start cannot check for drift" % (stamp_path(plat), exc)
        )
    runner.note("bootstrap done: %d step(s), %d failed" % (len(runner.ran), len(runner.failed)))
    return 0


# drift repair at cloud session start --------------------------------------------------------------------


def repair_drift(ctx, store, args, log):
    """Cloud session start: run the setup steps again for projects whose setup inputs changed. Never raises."""
    try:
        with log.step("setup drift check") as st:
            st.status = _repair(ctx, store, args, log)
    except Exception as exc:  # noqa: BLE001 (never fail the session)
        warn("setup drift check failed: %s; the session goes on" % short(exc))


def _repair(ctx, store, args, log):
    plat = ctx.plat
    timeout = args.timeout
    try:
        stamp = load_stamp(plat)
    except (OSError, ValueError) as exc:
        warn("setup drift: the bootstrap stamp cannot be read (%s); not checked" % short(exc))
        return "skipped (unreadable stamp)"
    if stamp is None:
        out("setup drift: no bootstrap stamp on this machine (an older setup script built it); not checked")
        return "skipped (no stamp)"
    ref, why = setup_ref(store.root, timeout)
    if ref is None:
        warn("setup drift: not checked: %s" % why)
        return "skipped (%s)" % why
    agent = stamp.get("agent") or "claude"
    now_tool = tool_commit()
    was_tool = (stamp.get("tool") or {}).get("commit")
    changed = False
    if now_tool and now_tool != was_tool:
        out("setup drift: agent-mirror is at %s (the bootstrap ran with %s)" % (now_tool[:12], (was_tool or "?")[:12]))
        stamp.setdefault("tool", {})["commit"] = now_tool
        stamp["tool"]["version"] = __version__
        changed = True
    deadline = time.time() + max(10, int(getattr(args, "repair_timeout", None) or REPAIR_TIMEOUT))
    note = stamp.get("note") or os.path.join(plat.home(), "agent_setup_probe.txt")
    note = note if os.path.isfile(note) else None
    runner = Runner(
        plat,
        note=note,
        log=log,
        capture=True,  # the programs' output is kept out of the session's output and never logged
        deadline=deadline,
        dry_run=args.dry_run,
        cwd=stamp.get("cwd"),
        env=script_env(store.root, note),
    )
    status = "ok"
    for project in sorted(stamp["projects"]):
        old = stamp["projects"][project] or {}
        env, problem = read_json_at(store.root, ref, "projects/%s/env.json" % project, timeout)
        if env is None:
            warn("setup drift: project %s: %s; not checked" % (project, problem))
            continue
        problems = input_problems(env)
        if problems:
            warn("setup drift: project %s: %s; its setup steps are not run" % (project, "; ".join(problems)))
            continue
        inputs = setup_inputs(env, agent)
        rec = input_record(inputs)
        if rec["inputs"] == old.get("inputs"):
            out("setup drift: none for %s" % project)
            continue
        keys = [k for k in INPUT_KEYS if rec["keys"][k] != (old.get("keys") or {}).get(k)] or ["the setup steps"]
        what = "%s changed for %s" % (", ".join(keys), project)
        if old.get("inputs") is None:
            what = "the setup steps of %s never ran here (%s)" % (project, old.get("skipped") or "not set up")
        if args.dry_run:
            out("setup drift: %s; would run its setup steps:" % what)
        else:
            out("setup drift: %s; running its setup steps now" % what)
            runner.note("drift repair at %s: %s" % (utc(), what))
        before, steps_before = len(runner.failed), len(runner.ran)
        run_setup(plat, runner, project, inputs, STEP_TIMEOUT)
        if args.dry_run:
            continue
        failed = runner.failed[before:]
        if runner.out_of_time(steps_before):
            # some steps never ran: keep the old record, so that the next session start tries again
            status = "failed (out of time)"
            warn(
                "setup drift: the time for the repair (--repair-timeout) ran out before every setup step of %s "
                "ran; the next session start tries again" % project
            )
            continue
        rec.update({"at": utc(), "failed": failed, "repaired": True})
        stamp["projects"][project] = rec
        changed = True
        if failed:
            status = "failed (%d step(s))" % len(failed)
            warn(
                "setup drift: %d setup step(s) of %s failed (%s); the session goes on"
                % (len(failed), project, ", ".join(failed))
            )
        else:
            out("setup drift: %s repaired" % project)
    if changed and not args.dry_run:
        rc, commit, _ = git(store.root, ["rev-parse", ref], timeout)
        stamp["store"] = {"ref": ref, "commit": commit.strip() if rc == 0 else None}
        stamp["checked"] = utc()
        save_stamp(plat, stamp)
    return status


def stamp_lines(plat):
    """Short lines about the bootstrap stamp, for probe."""
    try:
        stamp = load_stamp(plat)
    except (OSError, ValueError) as exc:
        return ["bootstrap stamp: cannot be read (%s)" % short(exc, 120)]
    if stamp is None:
        return ["bootstrap stamp: none (this machine was not set up by `agent-mirror cloud bootstrap`)"]
    tool = stamp.get("tool") or {}
    lines = [
        "bootstrap stamp: %s, vendor %s, tool %s (%s)"
        % (stamp.get("at", "?"), stamp.get("vendor", "?"), (tool.get("commit") or "?")[:12], tool.get("version", "?"))
    ]
    for name, rec in sorted((stamp.get("projects") or {}).items()):
        failed = (rec or {}).get("failed") or []
        if not (rec or {}).get("inputs"):
            lines.append("  %s: not set up (%s)" % (name, (rec or {}).get("skipped") or "no inputs recorded"))
            continue
        lines.append(
            "  %s: setup inputs %s%s%s"
            % (
                name,
                str((rec or {}).get("inputs") or "?")[:12],
                ", repaired at session start" if (rec or {}).get("repaired") else "",
                ", failed: %s" % ", ".join(failed) if failed else "",
            )
        )
    return lines
