"""doctor: report runtime, config, store state, links, hooks, tools and box settings."""

import os
import shutil

from agent_mirror import PLUGIN_ROOT, __version__, machine, recipes
from agent_mirror.box import describe_box
from agent_mirror.config import config_projects
from agent_mirror.hooks import status_line
from agent_mirror.inbox import held_summary, upstream_parts
from agent_mirror.install import Installer, link_state, memory_link_path
from agent_mirror.links import describe
from agent_mirror.projects import discover_projects
from agent_mirror.runtime import AGENT_NAMES, CLOUD_RUNTIMES, detect_runtime
from agent_mirror.util import expand, git, is_git_toplevel, out, read_text, tilde, upstream


def claude_md_state(store):
    path = store.path("CLAUDE.md")
    if os.path.islink(path):
        return "ok (link)" if os.readlink(path) == "AGENTS.md" else "BROKEN (%s)" % describe(path)
    if os.path.isfile(path):
        lines = [ln.strip() for ln in read_text(path).splitlines()]
        return "ok (imports @AGENTS.md)" if "@AGENTS.md" in lines else "OTHER (a separate file, not AGENTS.md)"
    return "MISSING (optional; init-store writes one)"


BOX_TOOLS = {"az": "azure-blob", "rclone": "rclone"}


def present_projects(ctx, d, runtime):
    """Projects whose box tools this machine must have: on a cloud runtime the projects of this session,
    on a developer machine also every configured project whose folder exists here."""
    names = set(n for n, _ in d.matched)
    if runtime not in CLOUD_RUNTIMES:
        for name, paths in config_projects(ctx.config):
            if any(os.path.isdir(expand(p)) for p in paths):
                names.add(name)
    return names


def tool_lines(tool, path, users, here, runtime):
    """doctor's lines for one tool. users: projects whose box uses it (None: not a box tool)."""
    if path:
        return ["  %-7s %s" % ("ok", "%s (%s)" % (tool, path))]
    if users is None:
        return ["  %-7s %s" % ("MISSING", tool)]
    needed = [n for n in users if n in here]
    if needed:
        return [
            "  %-7s %s (needed by the box of %s)" % ("MISSING", tool, ", ".join(needed)),
            "    PROBLEM  %s is not installed, and the %s box of %s needs it"
            % (tool, BOX_TOOLS[tool], ", ".join(needed)),
        ]
    if users:
        where = "in this session" if runtime in CLOUD_RUNTIMES else "checked out on this machine"
        return [
            "  %-7s %s not installed (only the box of %s uses %s, and %s not %s)"
            % ("-", tool, ", ".join(users), BOX_TOOLS[tool], "it is" if len(users) == 1 else "they are", where)
        ]
    return ["  %-7s %s" % ("-", tool + " not needed (no project's box uses %s)" % BOX_TOOLS[tool])]


def projects_by_store(store, ctx, d):
    found = {}
    for name in [n for n, _ in d.matched] + [n for n, _ in config_projects(ctx.config)]:
        found[os.path.normcase(os.path.realpath(store.memory_store(name)))] = name
    return found


def unit_for_target(store, target, memory_names):
    """The plan unit id a link target belongs to (for recorded choices), or None."""
    t = os.path.normcase(os.path.realpath(target))
    if t == os.path.normcase(os.path.realpath(store.path("AGENTS.md"))):
        return None  # two units share this target; doctor shows the link state only
    skills = os.path.normcase(os.path.realpath(store.path("skills")))
    if os.path.dirname(t) == skills:
        return "skill:%s" % os.path.basename(target)
    if t in memory_names:
        return "memory:%s" % memory_names[t]
    return None


def plan_counts(plan):
    from agent_mirror.plan import CASE_ORDER

    counts = {}
    for u in plan.units:
        counts[u["case"]] = counts.get(u["case"], 0) + 1
    return [(c, counts[c]) for c in CASE_ORDER if c in counts]


def cmd_doctor(args, ctx):
    plat = ctx.plat
    runtime = detect_runtime()
    out("agent-mirror %s (plugin %s)" % (__version__, PLUGIN_ROOT))
    out("runtime: %s" % runtime)
    cfg_path = plat.config_file()
    out("config: %s (%s)" % (tilde(cfg_path), "found" if os.path.isfile(cfg_path) else "missing"))
    store = ctx.store_or_none()
    if store is None:
        out("store: none. Run `agent-mirror init-store PATH` (or --adopt an existing store), or pass --store")
        return 0
    mid, mname = machine.identity(ctx.config)
    out(
        "machine: %s"
        % ("%s (id %s)" % (mname, mid[:12]) if mid else "no id yet (init-store, install or apply assigns one)")
    )
    out("store: %s (from %s)" % (store.root, store.source))
    meta = store.meta()
    if meta:
        out("  store.json: schema %s, tool ref %s" % (meta.get("schema"), store.tool_ref()))
    else:
        out("  store.json: MISSING (init-store --adopt writes one)")
    if is_git_toplevel(store.root, args.timeout):
        rc_b, branch, _ = git(store.root, ["symbolic-ref", "--short", "-q", "HEAD"], args.timeout)
        if rc_b != 0:
            branch = "(detached)"
        up = upstream(store.root, args.timeout)
        _, status, _ = git(store.root, ["status", "--porcelain"], args.timeout)
        n = len([ln for ln in status.splitlines() if ln.strip()])
        state = "clean" if n == 0 else "%d changed or untracked paths" % n
        ahead = ""
        if up:
            rc, counts, _ = git(store.root, ["rev-list", "--left-right", "--count", "HEAD...@{u}"], args.timeout)
            if rc == 0 and len(counts.split()) == 2:
                ahead = ", %s ahead / %s behind (as of the last fetch)" % tuple(counts.split())
        out("  git: branch %s, upstream %s, %s%s" % (branch.strip() or "?", up or "none", state, ahead))
    else:
        out("  git: not a git clone (sync needs one)")
    out("  CLAUDE.md: %s" % claude_md_state(store))
    agents = [args.agent] if args.agent else list(AGENT_NAMES)
    d = discover_projects(ctx, args.project, args.project_path)
    d.report()
    out("links for %s:" % ",".join(agents))
    inst = Installer(ctx, dry_run=True)
    items = [(link, target) for link, target, _, _ in inst.planned_links(agents)]
    if "claude" in agents:
        projects = list(d.matched)
        parent = d.parent_link()
        if parent:
            projects.append(parent)
        if not args.project:
            for name, paths in config_projects(ctx.config):
                for p in paths:
                    real = os.path.realpath(expand(p))
                    if os.path.isdir(real) and (name, real) not in projects:
                        projects.append((name, real))
        forced = d.matched[0][0] if d.mode == "forced" else None
        for name, path in projects:
            link, _ = memory_link_path(ctx, path, args.memory_dir if name == forced else None)
            items.append((link, store.memory_store(name)))
    counts = {}
    recorded = machine.choices(ctx.config)
    for link, target in items:
        label, note = link_state(inst, link, target)
        uid = unit_for_target(store, target, projects_by_store(store, ctx, d))
        choice = recorded.get(uid) if uid else None
        if label != "OK" and choice in machine.STICKY:
            label, note = "DECIDED", "%s: %s (recorded for this machine)" % (uid, choice)
        counts[label] = counts.get(label, 0) + 1
        out("  %-10s %s (%s)" % (label, tilde(link), note))
    out("  summary: " + ", ".join("%d %s" % (v, k) for k, v in sorted(counts.items())))
    checkouts = list(d.matched)  # (project, local path) pairs, for the box overlap warnings
    try:
        from agent_mirror.plan import Plan, gather_projects

        plan = Plan(ctx, agents, gather_projects(ctx, [], discover=True, quiet=True))
        checkouts += [pair for pair in plan.projects if pair not in checkouts]
        waiting = plan.waiting()
        out(
            "plan: %s; %s"
            % (
                ", ".join("%d %s" % (n, c) for c, n in plan_counts(plan)) or "no units",
                (
                    "fresh (nothing waits for a choice)"
                    if not waiting
                    else "%d unit(s) wait for a choice: %s (agent-mirror plan)"
                    % (len(waiting), ", ".join(u["id"] for u in waiting))
                ),
            )
        )
    except SystemExit:
        out("plan: could not be built")
    out("hooks (claude): %s" % status_line(ctx))
    out("tools:")
    names = store.project_names()
    backends = {}
    for name in names:
        backend = str((store.load_env(name).get("box") or {}).get("backend") or "none").strip().lower()
        backends.setdefault(backend, []).append(name)
    here = present_projects(ctx, d, runtime)
    for tool in ("git", "uv", "az", "rclone"):
        users = backends.get(BOX_TOOLS.get(tool), []) if tool in BOX_TOOLS else None
        for line in tool_lines(tool, shutil.which(tool), users, here, runtime):
            out(line)
    out("box:")
    if not names:
        out("  (no projects/<name>/env.json in the store)")
    for name in names:
        lines, problems, warnings = describe_box(store.load_env(name), [p for n, p in checkouts if n == name])
        out("  %s: %s" % (name, lines[0]))
        for line in lines[1:]:
            out("    " + line)
        for p in problems:
            out("    PROBLEM  %s" % p)
        for w in warnings:
            out("    warning  %s" % w)
    out("secrets (recipes in env.json):")
    for name in names:
        env = store.load_env(name)
        probs = recipes.problems(env)
        warns = [] if probs else sorted(set(w for v in ("claude", "codex") for w in recipes.coverage_warnings(env, v)))
        count = len(recipes.entries(env))
        out("  %s: %d recipe(s)%s" % (name, count, "" if (probs or warns) else ", every variable covered"))
        for p in probs:
            out("    PROBLEM  %s" % p)
        for w in warns:
            out("    warning  %s" % w)
    out("inbox (cloud pushes, as of the last fetch; store.json cloud_push: %s):" % store.cloud_push())
    remote, _ = upstream_parts(store.root, args.timeout)
    waiting = held_summary(store.root, remote, args.timeout) if remote else []
    if not waiting:
        out("  no inbox branches")
    for branch, held, memory in waiting:
        if held:
            out(
                "  HELD       %s: %d memory change(s); held for review: %s (agent-mirror review show %s)"
                % (branch, memory, ", ".join(held), branch)
            )
        else:
            out("  WAITING    %s: %d memory change(s); the next sync applies them" % (branch, memory))
    return 0
