"""session-start: run at the start of every agent session.

Every runtime: pull the store. On a cloud runtime also: bring agent-mirror's own
clone to the ref pinned in store.json (then run again once with the new code),
repair setup drift (run the setup steps again for a project whose python,
setup_commands or companion_repos changed since `cloud bootstrap`; bootstrap.py),
dependency sync in the project folder, the project's session_start_commands,
install for the runtime's agent (every unit that needs no decision, including the
memory link), and links to the project's companion repos. Always prints the
memory index path. On a cloud runtime it ends with each project's newest box
sessions and their notes, and the shared folders that have notes (box/notes.py;
bounded by --box-timeout). Each step's name, times and exit status (never its
output) go to <cache>/session-start.jsonl (steplog.py); `agent-mirror probe`
shows the last record.
"""

import os
import shlex
import shutil
import time

from agent_mirror import __version__
from agent_mirror.apply import apply_plan
from agent_mirror.companions import link_companions
from agent_mirror.plan import Plan
from agent_mirror.platforms import current
from agent_mirror.projects import discover_projects
from agent_mirror.runtime import CLOUD_RUNTIMES, agent_for_runtime, detect_runtime
from agent_mirror.selfupdate import self_update
from agent_mirror.steplog import StepLog
from agent_mirror.util import git, git_refusal, is_git_toplevel, out, run, short, tilde, upstream, warn


def command_argv(spec, store_root, project_path):
    """argv for one configured command.

    A string is split into words like a shell would (no pipes, &&, redirects
    or variables); a list is used as it is. {store} and {project_dir} are
    replaced after splitting, so Windows paths survive. A leading ~ is expanded.
    """
    if isinstance(spec, (list, tuple)):
        argv = [str(a) for a in spec]
    else:
        argv = shlex.split(str(spec))
    done = []
    for a in argv:
        a = a.replace("{store}", store_root or "").replace("{project_dir}", project_path or "")
        if a == "~" or a.startswith(("~/", "~\\")):
            a = current().expand_user(a)
        done.append(a)
    return done


def run_project_command(spec, store_root, path, timeout, dry_run, what):
    """Run one configured command in the project folder. Returns its status for the step log."""
    argv = command_argv(spec, store_root, path)
    if not argv:
        return "skipped (empty command)"
    shown = " ".join(shlex.quote(a) for a in argv)
    if dry_run:
        out("would run in %s: %s" % (path, shown))
        return "skipped (dry run)"
    if not os.path.isdir(path):
        warn("project folder %s does not exist; skipped %s" % (path, what))
        return "skipped (no project folder)"
    if shutil.which(argv[0]) is None and not os.path.isfile(argv[0]):
        warn("%s not found; skipped %s (%s)" % (argv[0], what, shown))
        return "skipped (%s not found)" % os.path.basename(argv[0])
    start = time.time()
    rc, stdout, stderr = run(argv, cwd=path, timeout=timeout)
    took = int(time.time() - start)
    if rc == 0:
        out("%s ok in %d s: %s" % (what, took, shown))
        return "ok"
    tail = "\n".join((stderr or stdout).strip().splitlines()[-15:])
    warn("%s failed after %d s (%s): %s\n%s" % (what, took, shown, rc, tail))
    return "failed (%s)" % ("exit %s" % rc if rc is not None else short(stderr or "did not finish", 60))


def pull_store(store, timeout, dry_run):
    """Pull the store. Returns its status for the step log."""
    if not is_git_toplevel(store.root, timeout):
        refusal = git_refusal(store.root, timeout)
        if refusal:
            warn("%s; skipped the pull" % refusal)
            return "skipped (git refuses the store: dubious ownership)"
        warn("store %s is not a git clone; skipped the pull" % store.root)
        return "skipped (not a git clone)"
    up = upstream(store.root, timeout)
    if not up:
        warn("the store has no upstream branch; skipped the pull")
        return "skipped (no upstream)"
    cloud = detect_runtime() in CLOUD_RUNTIMES
    how = ["pull", "--rebase", "--autostash", "--quiet"] if cloud else ["pull", "--ff-only", "--quiet"]
    if dry_run:
        out("would run: git %s (store)" % " ".join(how[:-1]))
        return "skipped (dry run)"
    rc, _, stderr = git(store.root, how, timeout)
    if rc != 0:
        if cloud:
            rc_a, gitdir, _ = git(store.root, ["rev-parse", "--absolute-git-dir"], timeout)
            if rc_a == 0 and any(
                os.path.isdir(os.path.join(gitdir.strip(), d)) for d in ("rebase-merge", "rebase-apply")
            ):
                git(store.root, ["rebase", "--abort"], timeout)
        warn("store pull failed (%s); using the local copy" % short(stderr or "no output"))
        return "failed (using the local copy)"
    out("store: pulled from %s" % up)
    return "ok"


def print_memory_index(store, project):
    idx = store.path("memory", store.memory_project(project), "MEMORY.md")
    if os.path.isfile(idx):
        out("memory index: %s" % idx)
    else:
        out("memory index: none yet for %s (it would be %s)" % (project, idx))
    pointers = str(store.load_env(project).get("pointers_file") or "").strip()
    if pointers:
        path = store.path(*pointers.replace("\\", "/").split("/"))
        if os.path.isfile(path):
            out("data pointers: %s" % path)
        else:
            warn("pointers_file %s in projects/%s/env.json does not exist" % (pointers, project))


def cmd_session_start(args, ctx):
    store = ctx.require_store()
    runtime = detect_runtime()
    log = StepLog(ctx.plat, "session-start", runtime)
    rc, handed_off = 0, False
    try:
        rc, handed_off = _session_start(args, ctx, store, runtime, log)
        return rc
    finally:
        if not handed_off and not args.dry_run:
            path = log.write()
            if runtime in CLOUD_RUNTIMES:
                out("timing: %s%s" % (log.summary(), " (%s)" % tilde(path) if path else ""))


def _session_start(args, ctx, store, runtime, log):
    """(exit code, True when a re-run with the updated tool did the work)."""
    out("agent-mirror %s session-start: runtime %s, store %s" % (__version__, runtime, store.root))
    if args.dry_run:
        out("dry run: nothing will change")
    with log.step("store pull") as st:
        st.status = pull_store(store, args.timeout, args.dry_run)
    if runtime in CLOUD_RUNTIMES:
        with log.step("tool update"):
            rc = self_update(store, args.timeout, args.dry_run, handoff=log.handoff)
        if rc is not None:
            return rc, True
        from agent_mirror.bootstrap import repair_drift

        repair_drift(ctx, store, args, log)
    d = discover_projects(ctx, args.project, args.project_path)
    d.report()
    rc = 0
    if runtime in CLOUD_RUNTIMES:
        agent = args.agent or agent_for_runtime(runtime)
        for project, path in d.matched:
            env = store.load_env(project)
            dep = env.get("dependency_sync")
            if dep:
                with log.step("dependency sync (%s)" % project) as st:
                    st.status = run_project_command(
                        dep, store.root, path, args.sync_timeout, args.dry_run, "dependency sync"
                    )
            commands = env.get("session_start_commands") or []
            for i, spec in enumerate(commands, 1):
                with log.step("session start command %d of %d (%s)" % (i, len(commands), project)) as st:
                    st.status = run_project_command(
                        spec, store.root, path, args.sync_timeout, args.dry_run, "session start command"
                    )
        projects = list(d.matched)
        parent = d.parent_link()
        if parent and agent == "claude":
            out(
                "the session runs above the clone of %s; also linking the memory folder of %s "
                "(Claude keys memory to the folder the session runs in)" % parent
            )
            projects.append(parent)
        with log.step("linking") as st:
            plan = Plan(ctx, [agent], projects)
            rc, _ = apply_plan(ctx, plan, args.dry_run, "install")
            for project, path in d.matched:
                link_companions(ctx.plat, project, path, store.load_env(project), args.dry_run)
            if rc != 0:
                st.status = "failed (exit %s)" % rc
    else:
        out("local runtime: only the store pull runs")
    names = d.names()
    if not names:
        out("memory index: none (no project matched; stores live under %s)" % store.path("memory"))
    for project in names:
        print_memory_index(store, project)
    if d.mode == "scan" and len(names) > 1:
        out(
            "note: the session runs above %d projects. Claude may key its memory to %s, which is linked "
            "to none of them (UNVERIFIED how Claude picks the folder); read the memory index files listed above"
            % (len(names), d.start)
        )
    if runtime in CLOUD_RUNTIMES:
        from agent_mirror.box.notes import session_start_notes

        session_start_notes(ctx, store, d.matched, runtime, args, log)
    return rc, False
