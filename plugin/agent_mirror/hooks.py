"""hooks install|uninstall|status, and the `hook memory-write` handler.

Three hooks go into Claude Code's user settings (<claude home>/settings.json):
  SessionStart              agent-mirror session-start
  PostToolUse (Write|Edit)  agent-mirror hook memory-write   (async)
  SessionEnd                agent-mirror sync

Every other setting and hook in the file is kept. Our entries are recognised
by their command (the agent-mirror launcher followed by one of the three
subcommands), so uninstall removes exactly those and nothing else.
"""

import contextlib
import json
import os
import re
import shutil
import sys
import traceback

from agent_mirror import PLUGIN_ROOT
from agent_mirror.util import err, iso_now, now_stamp, out, tilde, within, write_text

OURS_RX = re.compile(
    r"""agent-mirror(?:\.cmd)?['"]?"""
    r"""(?:\s+--store\s+(?:"[^"]*"|'[^']*'|\S+))?"""
    r"""\s+(?P<sub>session-start|hook\s+memory-write|sync)\s*$"""
)

# (event, matcher, subcommand, extra keys)
HOOKS = (
    ("SessionStart", None, ["session-start"], {"timeout": 1800}),
    ("PostToolUse", "Write|Edit", ["hook", "memory-write"], {"async": True, "timeout": 300}),
    ("SessionEnd", None, ["sync"], {"timeout": 120}),
)


def classify(command):
    """'session-start', 'hook memory-write' or 'sync' when the command is one of ours, else None."""
    if not isinstance(command, str):
        return None
    m = OURS_RX.search(command.strip())
    return " ".join(m.group("sub").split()) if m else None


def wanted_hooks(ctx, launcher=None):
    """[(event, matcher, key, hook entry)] for the current launcher."""
    plat = ctx.plat
    launcher = launcher or plat.launcher(PLUGIN_ROOT)
    prefix = [launcher]
    if ctx.store_source_for_commands() == "flag":
        store = ctx.require_store()
        prefix += ["--store", store.root]
    found = []
    for event, matcher, sub, extra in HOOKS:
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


def merge_install(settings, wanted):
    """Add our hooks to a settings dict in place. Returns a list of changes (empty if none)."""
    if "hooks" in settings and not isinstance(settings["hooks"], dict):
        raise ValueError('"hooks" in the settings file is not an object')
    hooks = settings.setdefault("hooks", {})
    changes = []
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
    for event, _, sub, _ in HOOKS:
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
    try:
        changes = merge_install(settings, wanted_hooks(ctx)) if args.action == "install" else merge_uninstall(settings)
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
            out("would %s %s" % (verb, c))
        return 0
    if json.dumps(settings, indent=2) == before:
        out("hooks %s: nothing to change in %s" % (args.action, tilde(path)))
        return 0
    if existed:
        dest = backup_settings(ctx, path)
        out("backed up %s to %s" % (tilde(path), tilde(dest)))
    write_text(path, json.dumps(settings, indent=2) + "\n")
    for c in changes:
        out("%s %s" % ("added" if args.action == "install" else "removed", c))
    out("hooks %s: %d change(s) written to %s" % (args.action, len(changes), tilde(path)))
    return 0


def hooks_status(ctx, settings, path):
    hooks = settings.get("hooks") if isinstance(settings.get("hooks"), dict) else {}
    out("settings: %s (%s)" % (tilde(path), "exists" if os.path.isfile(path) else "missing"))
    installed = 0
    for event, matcher, key, entry in wanted_hooks(ctx):
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
    out("hooks: %d of %d installed for this launcher" % (installed, len(HOOKS)))
    return 0


def status_line(ctx):
    """One line for doctor."""
    path = ctx.plat.claude_settings()
    try:
        settings, _ = load_settings(path)
    except (OSError, ValueError):
        return "cannot read %s" % tilde(path)
    hooks = settings.get("hooks") if isinstance(settings.get("hooks"), dict) else {}
    n = sum(1 for event, _, sub, _ in HOOKS if our_positions(hooks, event, " ".join(sub)))
    return "%d of %d agent-mirror hooks in %s" % (n, len(HOOKS), tilde(path))


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
