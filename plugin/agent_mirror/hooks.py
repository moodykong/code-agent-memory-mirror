"""hooks install|uninstall|status, and the `hook memory-write` and `hook stop-check` handlers.

Three hooks go into Claude Code's user settings (<claude home>/settings.json) on every machine:
  SessionStart              agent-mirror session-start
  PostToolUse (Write|Edit)  agent-mirror hook memory-write   (async)
  SessionEnd                agent-mirror sync
and one more on a cloud runtime only (developer machines never get it: no added time at the end of a turn):
  Stop                      agent-mirror hook stop-check

Every other setting and hook in the file is kept. Our entries are recognised
by their command (the agent-mirror launcher followed by one of the four
subcommands), so uninstall removes exactly those and nothing else, and
install on a developer machine removes a Stop entry of ours.

The stop check (Claude Code's Stop event): on a cloud runtime, when the session's project clones hold
files for the box (box/unsent.py: in no git commit, not in the box, and large, binary, many small files or
ignored by git), it prints {"decision": "block", "reason": "..."} so that the agent goes on with the reason as
its instruction: send each file, or declare it disposable with `box skip`. Small untracked files are left to
git: a vendor's own stop hook asks to commit those, and this check stays out of its way. It never blocks twice
for the same set of files for the box (it keeps short digests of the files it last blocked on, never their
names, in <cache>/stop-check.json, and blocks again only when the set gained a file), at most STOP_BLOCKS times
a session, and never when its check was cut short or failed. Every run on a cloud runtime adds one line to
<cache>/stop-check.log (no file names; `probe` shows the last ones). Anywhere else, or on any error, it exits
0 and prints nothing.
"""

import contextlib
import io
import json
import os
import re
import shutil
import sys
import threading
import time
import traceback

from agent_mirror import PLUGIN_ROOT, __version__
from agent_mirror.runtime import CLOUD_RUNTIMES, detect_runtime
from agent_mirror.util import err, iso_now, now_stamp, out, short, tilde, within, write_text

OURS_RX = re.compile(
    r"""agent-mirror(?:\.cmd)?['"]?"""
    r"""(?:\s+--store\s+(?:"[^"]*"|'[^']*'|\S+))?"""
    r"""\s+(?P<sub>session-start|hook\s+memory-write|hook\s+stop-check|sync)\s*$"""
)

# (event, matcher, subcommand, extra keys)
HOOKS = (
    ("SessionStart", None, ["session-start"], {"timeout": 1800}),
    ("PostToolUse", "Write|Edit", ["hook", "memory-write"], {"async": True, "timeout": 300}),
    ("SessionEnd", None, ["sync"], {"timeout": 120}),
)
# hooks for a cloud runtime only: Claude Code stops the command after `timeout` seconds; the check itself stops
# much earlier (--time-limit, and a hard stop at STOP_HARD_EXTRA seconds after it)
CLOUD_HOOKS = (("Stop", None, ["hook", "stop-check"], {"timeout": 30}),)
ALL_HOOKS = HOOKS + CLOUD_HOOKS
STOP_STATE = "stop-check.json"
STOP_BLOCKS = 3  # blocks per session at most
STOP_HARD_EXTRA = 12  # seconds after --time-limit when the stop check exits 0 whatever it is doing
STOP_SHOW = 15  # lines of the listing in the reason, per project
STOP_LOG = "stop-check.log"
STOP_LOG_KEEP = 200  # lines of the log kept, the newest
STOP_FIRST_LINE = "agent-mirror %s stop check (Claude Code Stop hook: agent-mirror hook stop-check)"


def hook_set(runtime):
    """The hooks a runtime gets: three everywhere, plus the Stop hook on a cloud runtime."""
    return HOOKS + (CLOUD_HOOKS if runtime in CLOUD_RUNTIMES else ())


def classify(command):
    """'session-start', 'hook memory-write', 'hook stop-check' or 'sync' when the command is one of ours, else None."""
    if not isinstance(command, str):
        return None
    m = OURS_RX.search(command.strip())
    return " ".join(m.group("sub").split()) if m else None


def wanted_hooks(ctx, launcher=None, runtime=None):
    """[(event, matcher, key, hook entry)] for the current launcher and runtime (default: the detected one)."""
    plat = ctx.plat
    launcher = launcher or plat.launcher(PLUGIN_ROOT)
    runtime = detect_runtime() if runtime is None else runtime
    prefix = [launcher]
    if ctx.store_source_for_commands() == "flag":
        store = ctx.require_store()
        prefix += ["--store", store.root]
    found = []
    for event, matcher, sub, extra in hook_set(runtime):
        entry = {"type": "command", "command": plat.command_line(prefix + sub)}
        entry.update(extra)
        found.append((event, matcher, " ".join(sub), entry))
    return found


def load_settings(path):
    if not os.path.isfile(path):
        return {}, False
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    if not text.strip():
        return {}, True
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("the file does not hold a JSON object")
    return data, True


def our_positions(hooks, event, key):
    """[(group index, hook index)] of our entries for one event and subcommand."""
    found = []
    for gi, group in enumerate(hooks.get(event) or []):
        if not isinstance(group, dict):
            continue
        for hi, h in enumerate(group.get("hooks") or []):
            if isinstance(h, dict) and classify(h.get("command")) == key:
                found.append((gi, hi))
    return found


def remove_ours(hooks, event, key):
    """Remove our entries for one event and subcommand. Returns how many were removed."""
    groups = hooks.get(event)
    if not isinstance(groups, list):
        return 0
    removed = 0
    kept_groups = []
    for group in groups:
        if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
            kept_groups.append(group)
            continue
        before = len(group["hooks"])
        group["hooks"] = [h for h in group["hooks"] if not (isinstance(h, dict) and classify(h.get("command")) == key)]
        n = before - len(group["hooks"])
        removed += n
        if n and not group["hooks"]:
            continue  # the group held only our hook
        kept_groups.append(group)
    if removed:
        if kept_groups:
            hooks[event] = kept_groups
        else:
            del hooks[event]
    return removed


def unwanted_hooks(runtime):
    """[(event, key)] of our hooks that a runtime must not have (the Stop hook on a developer machine)."""
    keep = set((e, " ".join(sub)) for e, _, sub, _ in hook_set(runtime))
    return [(e, " ".join(sub)) for e, _, sub, _ in ALL_HOOKS if (e, " ".join(sub)) not in keep]


def merge_install(settings, wanted, unwanted=()):
    """Add our hooks to a settings dict in place, and remove our `unwanted` [(event, key)] entries. Returns a list
    of changes (empty if none); a removal reads "removed <event>: ..."."""
    if "hooks" in settings and not isinstance(settings["hooks"], dict):
        raise ValueError('"hooks" in the settings file is not an object')
    hooks = settings.setdefault("hooks", {})
    changes = []
    for event, key in unwanted:
        if event in hooks and not isinstance(hooks[event], list):
            raise ValueError('"hooks.%s" in the settings file is not a list' % event)
        if remove_ours(hooks, event, key):
            changes.append("removed %s: agent-mirror %s (only a cloud machine runs it)" % (event, key))
    for event, matcher, key, entry in wanted:
        if event in hooks and not isinstance(hooks[event], list):
            raise ValueError('"hooks.%s" in the settings file is not a list' % event)
        pos = our_positions(hooks, event, key)
        if len(pos) == 1:
            gi, hi = pos[0]
            group = hooks[event][gi]
            if group["hooks"][hi] == entry and group.get("matcher") == matcher and len(group["hooks"]) == 1:
                continue
        remove_ours(hooks, event, key)
        group = {"hooks": [entry]}
        if matcher:
            group = {"matcher": matcher, "hooks": [entry]}
        hooks.setdefault(event, []).append(group)
        changes.append("%s: %s" % (event, entry["command"]))
    if not hooks and not changes:
        settings.pop("hooks", None)
    return changes


def merge_uninstall(settings):
    """Remove our hooks from a settings dict in place. Returns a list of removed items."""
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return []
    removed = []
    for event, _, sub, _ in ALL_HOOKS:
        key = " ".join(sub)
        n = remove_ours(hooks, event, key)
        removed += ["%s: agent-mirror %s" % (event, key)] * n
    if removed and not hooks:
        del settings["hooks"]
    return removed


def backup_settings(ctx, path):
    plat = ctx.plat
    rel = os.path.relpath(os.path.abspath(path), plat.home())
    if rel.startswith(".."):
        rel = os.path.basename(path)
    dest = os.path.join(plat.backup_root(), now_stamp() + "-hooks", rel)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    shutil.copy2(path, dest)
    return dest


def cmd_hooks(args, ctx):
    if args.agent != "claude":
        err(
            "hooks for %s are not supported: its hook format is not verified. Codex reads the store directly; "
            "run `agent-mirror session-start` and `agent-mirror sync` from its start step, or use "
            "`agent-mirror timer install`" % args.agent
        )
        return 2
    path = ctx.plat.claude_settings()
    try:
        settings, existed = load_settings(path)
    except (OSError, ValueError) as exc:
        err("cannot read %s: %s; nothing changed" % (tilde(path), exc))
        return 1
    if args.action == "status":
        return hooks_status(ctx, settings, path)
    if args.action == "install":
        ctx.require_store()  # the hooks would fail at every session without one
    before = json.dumps(settings, indent=2)
    runtime = detect_runtime()
    try:
        if args.action == "install":
            changes = merge_install(settings, wanted_hooks(ctx, runtime=runtime), unwanted_hooks(runtime))
        else:
            changes = merge_uninstall(settings)
    except ValueError as exc:
        err("%s in %s; nothing changed" % (exc, tilde(path)))
        return 1
    verb = "add or update" if args.action == "install" else "remove"
    if not changes:
        out("hooks %s: nothing to change in %s" % (args.action, tilde(path)))
        return 0
    if args.dry_run:
        out("dry run: nothing will change")
        for c in changes:
            out("would %s" % ("remove " + c[len("removed ") :] if c.startswith("removed ") else "%s %s" % (verb, c)))
        return 0
    if json.dumps(settings, indent=2) == before:
        out("hooks %s: nothing to change in %s" % (args.action, tilde(path)))
        return 0
    if existed:
        dest = backup_settings(ctx, path)
        out("backed up %s to %s" % (tilde(path), tilde(dest)))
    write_text(path, json.dumps(settings, indent=2) + "\n")
    for c in changes:
        out(c if c.startswith("removed ") else "%s %s" % ("added" if args.action == "install" else "removed", c))
    out("hooks %s: %d change(s) written to %s" % (args.action, len(changes), tilde(path)))
    return 0


def hooks_status(ctx, settings, path):
    hooks = settings.get("hooks") if isinstance(settings.get("hooks"), dict) else {}
    runtime = detect_runtime()
    out("settings: %s (%s)" % (tilde(path), "exists" if os.path.isfile(path) else "missing"))
    installed = 0
    wanted = wanted_hooks(ctx, runtime=runtime)
    for event, matcher, key, entry in wanted:
        pos = our_positions(hooks, event, key)
        if not pos:
            out("  MISSING    %s agent-mirror %s" % (event, key))
            continue
        gi, hi = pos[0]
        current = hooks[event][gi]["hooks"][hi].get("command")
        if current == entry["command"]:
            installed += 1
            out("  OK         %s %s" % (event, current))
        else:
            out("  OTHER PATH %s %s (install rewrites it)" % (event, current))
    for event, key in unwanted_hooks(runtime):
        if our_positions(hooks, event, key):
            out("  EXTRA      %s agent-mirror %s (only a cloud machine runs it; install removes it)" % (event, key))
    out("hooks: %d of %d installed for this launcher (%s)" % (installed, len(wanted), runtime))
    return 0


def status_line(ctx):
    """One line for doctor: how many of the hooks this runtime expects are there (3, or 4 on a cloud runtime)."""
    path = ctx.plat.claude_settings()
    try:
        settings, _ = load_settings(path)
    except (OSError, ValueError):
        return "cannot read %s" % tilde(path)
    hooks = settings.get("hooks") if isinstance(settings.get("hooks"), dict) else {}
    expected = hook_set(detect_runtime())
    n = sum(1 for event, _, sub, _ in expected if our_positions(hooks, event, " ".join(sub)))
    return "%d of %d agent-mirror hooks in %s" % (n, len(expected), tilde(path))


def repair_missing(ctx, runtime, dry_run, log):
    """Cloud session start: add the hooks this runtime expects and Claude's settings lack, when some of ours are
    there (a machine set up by an older version, before the Stop hook). Never raises."""
    try:
        with log.step("hooks check") as st:
            st.status = _repair_missing(ctx, runtime, dry_run)
    except Exception as exc:  # noqa: BLE001 (never fail the session)
        out("warning: hooks check failed: %s; the session goes on" % short(exc, 120))


def _repair_missing(ctx, runtime, dry_run):
    path = ctx.plat.claude_settings()
    try:
        settings, existed = load_settings(path)
    except (OSError, ValueError) as exc:
        return "skipped (cannot read the settings: %s)" % short(exc, 60)
    hooks = settings.get("hooks") if isinstance(settings.get("hooks"), dict) else {}
    wanted = wanted_hooks(ctx, runtime=runtime)
    missing = [w for w in wanted if not our_positions(hooks, w[0], w[2])]
    if not missing:
        return "ok"
    if len(missing) == len(wanted):
        return "skipped (no agent-mirror hooks here)"
    names = ", ".join("%s (agent-mirror %s)" % (w[0], w[2]) for w in missing)
    if dry_run:
        out("would add the missing hooks %s to %s" % (names, tilde(path)))
        return "skipped (dry run)"
    merge_install(settings, missing)
    if existed:
        backup_settings(ctx, path)
    write_text(path, json.dumps(settings, indent=2) + "\n")
    out(
        "hooks: added %s to %s (this machine was set up before it existed); Claude Code may only use it from a "
        "later session: raise the `# revision:` line of the setup script so the vendor rebuilds the machine with it"
        % (names, tilde(path))
    )
    return "ok (added %d)" % len(missing)


# hook memory-write ------------------------------------------------------------------


def is_store_write(path, store_root, claude_home):
    """True if a written file is a store memory, skill or instruction file, or a Claude memory file."""
    real = os.path.realpath(path)
    for sub in ("memory", "skills"):
        if within(real, os.path.join(store_root, sub)):
            return True
    for name in ("AGENTS.md", "CLAUDE.md"):
        if os.path.normcase(real) == os.path.normcase(os.path.join(store_root, name)):
            return True
    projects = os.path.join(claude_home, "projects")
    absolute = os.path.abspath(path)
    if within(absolute, projects):
        parts = os.path.relpath(absolute, projects).split(os.sep)
        return len(parts) >= 3 and parts[1] == "memory"
    return False


def cmd_hook(args, ctx):
    """Never fails the tool call: always exits 0."""
    if args.name == "stop-check":
        return cmd_stop_check(args, ctx)
    if args.name != "memory-write":
        return 0
    try:
        data = json.load(sys.stdin)
        path = str(((data or {}).get("tool_input") or {}).get("file_path") or "")
    except Exception:  # noqa: BLE001 (bad hook input: do nothing)
        return 0
    if not path:
        return 0
    store = ctx.store_or_none(quiet=True)
    if store is None or not is_store_write(path, store.root, ctx.plat.claude_home()):
        return 0
    log = os.path.join(ctx.plat.cache_dir(), "sync.log")
    try:
        os.makedirs(os.path.dirname(log), exist_ok=True)
        with open(log, "a", encoding="utf-8") as fh, contextlib.redirect_stdout(fh), contextlib.redirect_stderr(fh):
            print("--- %s memory-write: %s" % (iso_now(), path), flush=True)
            from agent_mirror.cli import sync_defaults
            from agent_mirror.sync import cmd_sync

            try:
                cmd_sync(sync_defaults(), ctx)
            except SystemExit as exc:
                print("sync stopped: %s" % exc, flush=True)
            except Exception:  # noqa: BLE001
                traceback.print_exc()
    except OSError:
        pass
    return 0


# hook stop-check ------------------------------------------------------------------------------------------------


def cmd_stop_check(args, ctx, stdin=None):
    """Claude Code's Stop hook. Prints {"decision": "block", "reason": ...} or nothing; always exits 0.

    Off a cloud runtime it returns at once without reading anything. On a cloud runtime every step runs with its
    output kept back, a timer ends the process with exit 0 at --time-limit + STOP_HARD_EXTRA seconds whatever
    it is doing (a hung git, a stdin that never closes), and every run adds one line to <cache>/stop-check.log."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        runtime = detect_runtime()
    if runtime not in CLOUD_RUNTIMES:
        return 0
    limit = max(0.0, float(getattr(args, "time_limit", 8.0) or 0.0))
    info = {"session": None, "decision": "pass", "box": None, "git": None, "outcome": None}

    def hard_stop():
        try:
            info.update(decision="error", outcome="hard stop after %g s" % (limit + STOP_HARD_EXTRA))
            log_stop(ctx.plat, info)
        finally:
            os._exit(0)

    guard = threading.Timer(limit + STOP_HARD_EXTRA, hard_stop)
    guard.daemon = True
    guard.start()
    decision = None
    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            text = (stdin if stdin is not None else sys.stdin).read()
            decision = stop_decision(ctx, text, limit, info)
    except BaseException as exc:  # noqa: BLE001 (a hook never fails the turn: no block)
        decision = None
        info.update(decision="error", outcome=type(exc).__name__)  # the type only: a message may name a file
    finally:
        guard.cancel()
    if decision:
        info["decision"] = "block"
    log_stop(ctx.plat, info)
    if decision:
        sys.stdout.write(json.dumps(decision) + "\n")
        sys.stdout.flush()
    return 0


def stop_log_path(plat):
    return os.path.join(plat.cache_dir(), STOP_LOG)


def log_stop(plat, info):
    """Add one line for a run of the stop check to <cache>/stop-check.log: the time, the tool version, the session
    id, the decision (block, pass or error), how many files were for the box and left to git, and the outcome;
    never a file name. Keeps the newest STOP_LOG_KEEP lines. Never raises."""
    try:

        def count(key):
            return "-" if info.get(key) is None else str(info[key])

        session = re.sub(r"[^A-Za-z0-9._-]", "", str(info.get("session") or ""))[:64] or "-"
        line = "%s agent-mirror %s session=%s decision=%s for_the_box=%s left_to_git=%s (%s)" % (
            time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            __version__,
            session,
            info.get("decision") or "pass",
            count("box"),
            count("git"),
            " ".join(str(info.get("outcome") or "").split())[:160],
        )
        path = stop_log_path(plat)
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                lines = [ln for ln in fh.read().splitlines() if ln.strip()]
        except OSError:
            lines = []
        write_text(path, "\n".join((lines + [line])[-STOP_LOG_KEEP:]) + "\n")
    except Exception:  # noqa: BLE001 (the log never fails the hook)
        pass


def stop_check_lines(ctx, runtime=None, last=5):
    """probe: whether the Stop hook is in Claude's settings (and whether this runtime should have it), and the last
    lines of the stop check's log. On a developer runtime nothing, unless the hook is there or a log is."""
    plat = ctx.plat
    runtime = detect_runtime() if runtime is None else runtime
    path = plat.claude_settings()
    wanted = runtime in CLOUD_RUNTIMES
    there = False
    try:
        settings, _ = load_settings(path)
        hooks = settings.get("hooks") if isinstance(settings.get("hooks"), dict) else {}
        there = bool(our_positions(hooks, "Stop", "hook stop-check"))
        if there and wanted:
            state = "installed"
        elif wanted:
            state = "MISSING (a cloud runtime should have it: `agent-mirror hooks install` adds it)"
        elif there:
            state = "installed, but only a cloud machine runs it (`agent-mirror hooks install` removes it)"
        else:
            state = "not installed (only a cloud machine runs it)"
    except (OSError, ValueError) as exc:
        state = "unknown (cannot read %s: %s)" % (tilde(path), short(exc, 80))
    log = stop_log_path(plat)
    try:
        with open(log, encoding="utf-8", errors="replace") as fh:
            runs = [ln for ln in fh.read().splitlines() if ln.strip()]
    except OSError:
        runs = []
    if not wanted and not there and not runs:
        return []  # a developer machine as it should be: nothing to say
    lines = ["stop check hook (Stop: agent-mirror hook stop-check, %s): %s" % (runtime, state)]
    if not runs:
        lines.append("stop check log: no runs recorded (%s)" % tilde(log))
        return lines
    lines.append("stop check log (%s), the last %d of %d runs kept:" % (tilde(log), min(last, len(runs)), len(runs)))
    lines += ["  " + ln for ln in runs[-last:]]
    return lines


def stop_state_path(plat):
    return os.path.join(plat.cache_dir(), STOP_STATE)


def load_stop_state(plat, session):
    """{"session", "blocks", "keys", ...} for this session (a new session starts from nothing)."""
    try:
        with open(stop_state_path(plat), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        data = None
    if not isinstance(data, dict) or data.get("session") != session:
        return {"session": session, "blocks": 0, "keys": []}
    data["keys"] = [k for k in data.get("keys") or [] if isinstance(k, str)]
    data["blocks"] = int(data.get("blocks") or 0)
    return data


def save_stop_state(plat, state, outcome):
    state["last"] = {"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "outcome": outcome}
    try:
        write_text(stop_state_path(plat), json.dumps(state, sort_keys=True) + "\n")
    except OSError:
        pass


def stop_decision(ctx, text, limit, info=None):
    """The Stop hook's decision dict, or None to let the agent stop. Reads only local files and git. It blocks only
    on files for the box; files left to git (small untracked files) are a commit hook's business.

    info, a dict, receives the session id, the counts of files for the box and left to git (when every check
    finished) and the outcome, for the log."""
    from agent_mirror.box import unsent
    from agent_mirror.projects import discover_projects

    info = {} if info is None else info
    data = json.loads(text) if text and text.strip() else None
    if not isinstance(data, dict):
        info["outcome"] = "unreadable input"
        return None  # unreadable input: never block on it
    session = str(data.get("session_id") or "")
    info["session"] = session
    active = data.get("stop_hook_active") is True
    store = ctx.store_or_none(quiet=True)
    if store is None:
        info["outcome"] = "no store"
        return None
    cwd = data.get("cwd") if isinstance(data.get("cwd"), str) and os.path.isdir(data.get("cwd")) else None
    start = ctx.plat.env.get("CLAUDE_PROJECT_DIR") or cwd or os.getcwd()
    deadline = time.monotonic() + limit
    d = discover_projects(ctx, None, start)
    results = []
    state = load_stop_state(ctx.plat, session)

    def passed(outcome):
        save_stop_state(ctx.plat, state, outcome)
        info["outcome"] = outcome
        return None

    to_git = 0
    for project, path in d.matched:
        box = store.load_env(project).get("box") or {}
        if not os.path.isdir(path) or not unsent.box_configured(box):
            continue
        left = deadline - time.monotonic()
        if left <= 0:
            return passed("passed (out of time)")
        r = unsent.find_unsent(project, path, box, left, keys=True)
        if not r["complete"] or r.get("problem"):
            return passed("passed (check of %s cut short or failed)" % project)  # never blocks
        to_git += r[unsent.LEFT_TO_GIT]["files"]
        if r[unsent.FOR_BOX]["files"]:
            results.append(r)
    keys = set(k for r in results for k in r["keys"])
    info.update(box=len(keys), git=to_git)
    if not keys:
        return passed("passed (nothing for the box%s)" % (", %d left to git" % to_git if to_git else ""))
    gained = keys - set(state["keys"])
    if not gained:
        # the set it last blocked on, or part of it: the agent has seen it (stop_hook_active or not)
        return passed("passed (no new files for the box%s)" % (", stop_hook_active" if active else ""))
    if state["blocks"] >= STOP_BLOCKS:
        return passed("passed (already blocked %d times)" % state["blocks"])
    state["blocks"] += 1
    state["keys"] = sorted(keys)
    outcome = "blocked (%d file%s for the box)" % (len(keys), "" if len(keys) == 1 else "s")
    save_stop_state(ctx.plat, state, outcome)
    info["outcome"] = outcome
    return {"decision": "block", "reason": stop_reason(results, several=len(d.matched) > 1)}


def stop_reason(results, several=False):
    """What the agent is told to do: a fixed first line naming the tool, the compact listing of the files for the
    box, the rule for files left to git, the two commands and the keep/leave criteria."""
    from agent_mirror.box import unsent

    lines = [
        STOP_FIRST_LINE % __version__,
        "This cloud machine is thrown away after the session, and these files of the project clone (large, binary, "
        "many small files or ignored by git) are in no git commit and not in the box:",
    ]
    for r in results:
        n = r[unsent.FOR_BOX]["files"]
        lines.append("%s (%s): %d file%s for the box" % (r["project"], r["project_path"], n, "" if n == 1 else "s"))
        lines += unsent.listing_lines(r, cap=STOP_SHOW)
    lines.append(unsent.GIT_RULE)
    lines.append("For each one, either send it: %s" % unsent.SEND)
    lines.append("or declare it disposable: %s" % unsent.SKIP)
    if several:
        lines.append("(the session has several project clones: add --project NAME to both)")
    lines.append(unsent.CRITERIA)
    return "\n".join(lines)
