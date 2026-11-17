"""session-start: run at the start of every agent session.

Every runtime: pull the store. On a cloud runtime also: dependency sync in the
project folder, the project's session_start_commands, and install for the
runtime's agent (including the memory link). Always prints the memory index path.
"""

import os
import shlex
import shutil
import time

from agent_mirror.projects import discover_projects
from agent_mirror.install import Installer
from agent_mirror.runtime import CLOUD_RUNTIMES, agent_for_runtime, detect_runtime
from agent_mirror.util import git, is_git_toplevel, out, run, short, upstream, warn


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
        if a.startswith("~"):
            a = os.path.expanduser(a)
        done.append(a)
    return done


def run_project_command(spec, store_root, path, timeout, dry_run, what):
    argv = command_argv(spec, store_root, path)
    if not argv:
        return
    shown = " ".join(shlex.quote(a) for a in argv)
    if dry_run:
        out("would run in %s: %s" % (path, shown))
        return
    if not os.path.isdir(path):
        warn("project folder %s does not exist; skipped %s" % (path, what))
        return
    if shutil.which(argv[0]) is None and not os.path.isfile(argv[0]):
        warn("%s not found; skipped %s (%s)" % (argv[0], what, shown))
        return
    start = time.time()
    rc, stdout, stderr = run(argv, cwd=path, timeout=timeout)
    took = int(time.time() - start)
    if rc == 0:
        out("%s ok in %d s: %s" % (what, took, shown))
    else:
        tail = "\n".join((stderr or stdout).strip().splitlines()[-15:])
        warn("%s failed after %d s (%s): %s\n%s" % (what, took, shown, rc, tail))


def pull_store(store, timeout, dry_run):
    if not is_git_toplevel(store.root, timeout):
        warn("store %s is not a git clone; skipped the pull" % store.root)
        return
    up = upstream(store.root, timeout)
    if not up:
        warn("the store has no upstream branch; skipped the pull")
        return
    cloud = detect_runtime() in CLOUD_RUNTIMES
    how = ["pull", "--rebase", "--autostash", "--quiet"] if cloud else ["pull", "--ff-only", "--quiet"]
    if dry_run:
        out("would run: git %s (store)" % " ".join(how[:-1]))
        return
    rc, _, stderr = git(store.root, how, timeout)
    if rc != 0:
        if cloud:
            rc_a, gitdir, _ = git(store.root, ["rev-parse", "--absolute-git-dir"], timeout)
            if rc_a == 0 and any(
                os.path.isdir(os.path.join(gitdir.strip(), d)) for d in ("rebase-merge", "rebase-apply")
            ):
                git(store.root, ["rebase", "--abort"], timeout)
        warn("store pull failed (%s); using the local copy" % short(stderr or "no output"))
    else:
        out("store: pulled from %s" % up)


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
    out("agent-mirror session-start: runtime %s, store %s" % (runtime, store.root))
    if args.dry_run:
        out("dry run: nothing will change")
    pull_store(store, args.timeout, args.dry_run)
    d = discover_projects(ctx, args.project, args.project_path)
    d.report()
    rc = 0
    if runtime in CLOUD_RUNTIMES:
        agent = args.agent or agent_for_runtime(runtime)
        for project, path in d.matched:
            env = store.load_env(project)
            dep = env.get("dependency_sync")
            if dep:
                run_project_command(dep, store.root, path, args.sync_timeout, args.dry_run, "dependency sync")
            for spec in env.get("session_start_commands") or []:
                run_project_command(spec, store.root, path, args.sync_timeout, args.dry_run, "session start command")
        projects = list(d.matched)
        parent = d.parent_link()
        if parent and agent == "claude":
            out(
                "the session runs above the clone of %s; also linking the memory folder of %s "
                "(Claude keys memory to the folder the session runs in)" % parent
            )
            projects.append(parent)
        inst = Installer(ctx, args.dry_run)
        rc = inst.install([agent], projects)
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
    return rc
