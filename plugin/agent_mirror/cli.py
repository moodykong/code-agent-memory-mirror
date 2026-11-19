"""Command line for agent-mirror. Run `agent-mirror -h` or `agent-mirror <command> -h`."""

import argparse
import json
import os
import sys

from agent_mirror import __version__
from agent_mirror.box.common import DEFAULT_TRANSFER_TIMEOUT
from agent_mirror.box.owners import TAKE_ALL
from agent_mirror.config import Context
from agent_mirror.platforms import SANDBOX_EXIT
from agent_mirror.runtime import AGENT_NAMES, detect_runtime
from agent_mirror.util import DEFAULT_TIMEOUT, err, out

DESCRIPTION = (
    "Give Claude Code and Codex the same instructions, skills and memory on every machine and in "
    "cloud sessions, from a git store you own."
)
EPILOG = (
    "The store is found from --store, then AGENT_MIRROR_STORE, then the user config file "
    "(made by init-store). Commands that change things take --dry-run."
)


def cmd_detect(args, ctx):
    out(detect_runtime())
    return 0


def cmd_debug(args, ctx):
    """Print every home-like location as JSON; exit 97 when one is outside the test sandbox."""
    plat = ctx.plat
    violations = plat.sandbox_violations()
    data = {
        "platform": plat.name,
        "agent_mirror_home": plat.mirror_home,
        "sandbox": plat.sandbox(),
        "locations": plat.locations(),
        "outside_sandbox": [name for name, _ in violations],
    }
    out(json.dumps(data, indent=2))
    return SANDBOX_EXIT if violations else 0


def lazy(module, func):
    """Import a command only when it runs, so `detect` and `--help` stay fast."""

    def call(args, ctx):
        mod = __import__("agent_mirror." + module, fromlist=[func])
        return getattr(mod, func)(args, ctx)

    return call


def sync_defaults():
    return argparse.Namespace(timeout=DEFAULT_TIMEOUT, dry_run=False, message=None, log=None)


def build_parser():
    parser = argparse.ArgumentParser(prog="agent-mirror", description=DESCRIPTION, epilog=EPILOG)
    parser.add_argument("--store", metavar="PATH", help="the store folder (default: AGENT_MIRROR_STORE or the config)")
    parser.add_argument("--version", action="version", version="agent-mirror %s" % __version__)
    sub = parser.add_subparsers(dest="cmd", metavar="command")
    sub.required = True

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--store", metavar="PATH", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    timeout = argparse.ArgumentParser(add_help=False)
    timeout.add_argument(
        "--timeout", type=int, default=DEFAULT_TIMEOUT, help="seconds for each git or network call (default 20)"
    )
    proj = argparse.ArgumentParser(add_help=False)
    proj.add_argument("--project", help="project name (default: matched from the project path)")
    proj.add_argument("--project-path", help="project checkout folder (default: CLAUDE_PROJECT_DIR or the cwd)")
    dry = argparse.ArgumentParser(add_help=False)
    dry.add_argument("--dry-run", action="store_true", help="print what would happen, change nothing")
    transfer = argparse.ArgumentParser(add_help=False)
    transfer.add_argument(
        "--transfer-timeout",
        type=int,
        default=DEFAULT_TRANSFER_TIMEOUT,
        help="seconds for one whole transfer call (default %d, 6 hours)" % DEFAULT_TRANSFER_TIMEOUT,
    )
    transfer.add_argument(
        "--env-file",
        metavar="PATH",
        help="read the box settings from this env.json instead of projects/<project>/env.json; "
        "box index then writes box_index.jsonl next to it",
    )

    s = sub.add_parser("debug", parents=[common], help="diagnostics: debug paths prints every resolved location")
    s.add_argument("what", choices=["paths"])
    s.set_defaults(func=cmd_debug)

    s = sub.add_parser("detect", parents=[common], help="print the runtime name")
    s.set_defaults(func=cmd_detect)

    s = sub.add_parser("init-store", parents=[common, dry], help="make a new store, or --adopt an existing one")
    s.add_argument("path", metavar="PATH", help="the store folder")
    s.add_argument("--remote", metavar="URL", help="git remote for the store (origin)")
    s.add_argument("--adopt", action="store_true", help="use an existing store folder as it is")
    s.add_argument("--commit", action="store_true", help="commit the new skeleton")
    s.set_defaults(func=lazy("storeinit", "cmd_init_store"))

    agents_arg = argparse.ArgumentParser(add_help=False)
    agents_arg.add_argument(
        "--agents", help="comma list of %s (default: the config's agents, else both)" % ",".join(AGENT_NAMES)
    )
    many = argparse.ArgumentParser(add_help=False)
    many.add_argument(
        "--project",
        action="append",
        metavar="NAME",
        help="a project to consider, with a --project-path in the same order (repeatable)",
    )
    many.add_argument(
        "--project-path", action="append", metavar="PATH", help="where that project is checked out (repeatable)"
    )

    s = sub.add_parser(
        "plan",
        parents=[common, timeout, agents_arg, many],
        help="compare this machine with the store, unit by unit (read-only)",
        description="Compare this machine with the store for every unit (instruction files, skills, each "
        "project's Claude memory and box) and say what apply would do. Projects: the config's, the ones "
        "given, and the one found from the current folder. Changes nothing.",
    )
    s.add_argument("--json", action="store_true", help="print the plan as JSON")
    s.set_defaults(func=lazy("plan", "cmd_plan"))

    s = sub.add_parser(
        "apply",
        parents=[common, timeout, agents_arg, many, dry],
        help="apply the plan: one action per unit, from a choices file or the defaults",
        description="Recompute the plan, then apply one action per unit: from --choices, else a choice "
        "recorded for this machine, else the default when the case needs no decision. reconcile and "
        "first-setup units without a choice wait. Everything replaced is backed up; uninstall restores it.",
    )
    s.add_argument("--choices", metavar="FILE", help='a JSON file {"unit id": "action", ...}')
    s.add_argument("--only", nargs="+", metavar="UNIT_ID", help="apply only these units")
    s.add_argument(
        "--yes-defaults", action="store_true", help="first-setup units take their default (reconcile units still wait)"
    )
    s.set_defaults(func=lazy("apply", "cmd_apply"))

    s = sub.add_parser(
        "install",
        parents=[common, timeout, proj, dry, agents_arg],
        help="fresh machines: link the agents' home folders into the store",
        description="For a fresh machine. Computes the plan and applies every unit that needs no decision; "
        "units that need one (first-setup, reconcile) are listed and left alone: run `agent-mirror plan`.",
    )
    s.add_argument(
        "--memory-dir", metavar="PATH", help="Claude's memory folder for --project (default: found or computed)"
    )
    s.add_argument(
        "--strict", action="store_true", help="exit with code 4 and change nothing when the machine is not fresh"
    )
    s.set_defaults(func=lazy("install", "cmd_install"))

    s = sub.add_parser("machine", parents=[common], help="show this machine's id, name and recorded choices")
    s.add_argument("--name", help="set this machine's name (used for machines/<name>.json in the store)")
    s.set_defaults(func=lazy("machine", "cmd_machine"))

    s = sub.add_parser("uninstall", parents=[common, timeout, dry], help="remove links into the store, restore backups")
    s.set_defaults(func=lazy("install", "cmd_uninstall"))

    s = sub.add_parser(
        "doctor", parents=[common, timeout, proj], help="report runtime, store, links, hooks, tools, box"
    )
    s.add_argument("--agent", choices=AGENT_NAMES, help="check links for one agent only")
    s.add_argument("--memory-dir", metavar="PATH", help="Claude's memory folder for --project")
    s.set_defaults(func=lazy("doctor", "cmd_doctor"))

    s = sub.add_parser("session-start", parents=[common, timeout, proj, dry], help="run at the start of every session")
    s.add_argument("--agent", choices=AGENT_NAMES, help="override the agent taken from the runtime")
    s.add_argument("--sync-timeout", type=int, default=1800, help="seconds for each project command (default 1800)")
    s.add_argument(
        "--repair-timeout",
        type=int,
        default=900,
        help="cloud: seconds in all for running setup steps again when the store changed them (default 900)",
    )
    s.add_argument(
        "--box-timeout",
        type=int,
        default=20,
        help="cloud: seconds for listing each project's newest box sessions and their notes (default 20; 0 skips it)",
    )
    s.add_argument(
        "--push-check-timeout",
        type=float,
        default=5.0,
        metavar="SECONDS",
        help="developer machines: seconds in all for checking whether the box folders this machine owns changed "
        "since its last push, offline (default 5; 0 skips it)",
    )
    s.set_defaults(func=lazy("session", "cmd_session_start"))

    s = sub.add_parser("sync", parents=[common, timeout, dry], help="scan, commit, pull and push store changes")
    s.add_argument("--message", help="commit message (default: memory: sync from <runtime> <hostname>)")
    s.add_argument("--log", metavar="FILE", help="append all output to FILE (for schedulers)")
    s.set_defaults(func=lazy("sync", "cmd_sync"))

    s = sub.add_parser(
        "probe", parents=[common, proj], help="reachability and machine checks (--full: first-session report)"
    )
    s.add_argument("--full", action="store_true", help="the long report, driven by the project config")
    s.add_argument("--timeout", type=int, default=5, help="seconds per network check (default 5)")
    s.add_argument("--tcp", action="append", metavar="HOST:PORT", help="raw TCP connect check (repeatable)")
    s.add_argument("--url", action="append", metavar="URL", help="extra HTTPS GET check (repeatable)")
    s.set_defaults(func=lazy("probe", "cmd_probe"))

    s = sub.add_parser(
        "hook",
        parents=[common],
        help="handlers called by agent hooks (read the hook JSON on stdin, always exit 0)",
        description="memory-write: sync when the file just written is a store memory, skill or instruction file. "
        "stop-check (cloud runtimes only; silent elsewhere): when the session's project clones hold files for the "
        "box (large, binary, many small files or ignored by git; `box unsent`), print the Stop hook decision that "
        "asks the agent to send each one or declare it disposable; small untracked files are left to git; never "
        "the same set twice, at most 3 times a session; one line per run in <cache>/stop-check.log.",
    )
    s.add_argument("name", choices=["memory-write", "stop-check"])
    s.add_argument(
        "--time-limit",
        type=float,
        default=8.0,
        metavar="SECONDS",
        help="stop-check: seconds for checking every project clone (default 8; when it runs out, nothing is blocked)",
    )
    s.set_defaults(func=lazy("hooks", "cmd_hook"))

    s = sub.add_parser("hooks", parents=[common, dry], help="install, uninstall or show the agent hooks")
    s.add_argument("action", choices=["install", "uninstall", "status"])
    s.add_argument(
        "--agent", default="claude", choices=AGENT_NAMES, help="agent whose settings to change (default claude)"
    )
    s.set_defaults(func=lazy("hooks", "cmd_hooks"))

    s = sub.add_parser("timer", parents=[common, dry], help="install, uninstall or show a periodic sync")
    s.add_argument("action", choices=["install", "uninstall", "status"])
    s.add_argument("--minutes", type=int, default=30, help="minutes between syncs (default 30)")
    s.set_defaults(func=lazy("timer", "cmd_timer"))

    s = sub.add_parser(
        "box",
        parents=[common],
        help="bulk files: a shared tree (push, ls, fetch, note) and per-session namespaces (send, sessions, promote)",
    )
    box = s.add_subparsers(dest="box_action", metavar="action")
    box.required = True
    every = [common, timeout, proj, dry, transfer]
    force = argparse.ArgumentParser(add_help=False)
    force.add_argument("--force", action="store_true", help="go ahead even when the disk guard says no")
    b = box.add_parser(
        "push",
        parents=every,
        help="upload the folders this machine owns or claims (developer machine only)",
        description="Upload this machine's folders of the box: each archived folder and each include folder "
        "is owned by one machine (<shared>/_owners.json). A folder nobody owns is claimed (recorded after "
        "its upload), folders this machine owns are uploaded when changed, and folders another machine owns "
        "are skipped with a note saying whether the local copy is the same. A machine whose recorded role "
        "is reader or none is refused before any transfer.",
    )
    b.add_argument(
        "--delete", action="store_true", help="also delete remote files whose local file is gone (pushed folders only)"
    )
    b.add_argument(
        "--take-over",
        action="append",
        nargs="?",
        const=TAKE_ALL,
        metavar="FOLDER",
        help="move FOLDER to this machine and upload it (repeatable). Without FOLDER, only for a box that has no "
        "_owners.json yet: this machine takes over every folder, as with the single writer before 0.4.0",
    )
    b.add_argument("--only", action="append", metavar="FOLDER", help="push only these folders (repeatable)")
    b = box.add_parser(
        "owners",
        parents=[common, timeout, proj, transfer],
        help="list every folder of the box, its owner, and how the local copy compares (read-only)",
        description="Read <shared>/_owners.json (or the committed index when the transfer program is not "
        "available) and list every folder: its owning machine and since when, whether it is here, and the "
        "local copy against the owner's fingerprint: same, different, absent, fetched (only unchanged copies "
        "fetched from the box), unknown or unowned. Changes nothing.",
    )
    b.add_argument("--json", action="store_true", help="print JSON")
    b = box.add_parser(
        "release",
        parents=[common, timeout, proj, dry, transfer],
        help="give up ownership of folders this machine owns (deletes no data)",
        description="Remove this machine's ownership entries for these folders from <shared>/_owners.json, "
        "so another machine's next push claims them. Only the owning machine can release a folder; no box "
        "data is deleted. Developer machines only.",
    )
    b.add_argument("folders", nargs="+", metavar="FOLDER", help="a folder of the box, as `box owners` names it")
    b = box.add_parser(
        "note",
        parents=[common, timeout, proj, dry, transfer],
        help="set or clear a shared folder's one-line note (its owning machine only)",
        description="Record a one-line note on a folder of the shared tree in <shared>/_owners.json: what the "
        "folder holds and what it is for. Only the folder's owning machine sets or clears it, on a developer "
        "machine. `box push` keeps it; `box owners`, `box ls` and a cloud session start show it. The owners "
        "line of the local box index is updated too; commit the index so other machines see the note offline.",
    )
    b.add_argument("folder", metavar="FOLDER", help="a folder of the box, as `box owners` names it")
    b.add_argument("text", nargs="?", metavar="TEXT", help="the note: one line, up to 500 characters")
    b.add_argument("--clear", action="store_true", help="remove the folder's note")
    b = box.add_parser(
        "index",
        parents=every,
        help="list the shared side into box_index.jsonl (with archive contents, owners and a session summary)",
    )
    b.add_argument("--page-size", type=int, default=5000, help="azure-blob: blobs per listing call (default 5000)")
    b = box.add_parser("ls", parents=[common, proj, transfer], help="browse the box from the index, offline")
    b.add_argument("path", nargs="?", help="folder or file relative to the project folder (default: the top)")
    b = box.add_parser(
        "fetch",
        parents=every + [force],
        help="download from the shared tree, or a session's files with --session",
        description="Download a file or folder of the shared tree into the project folder, or with --session ID "
        "the files of one session (all of them, or PATH inside it) into <project>/_sessions/<session id>/, "
        "after printing the session's notes (--dry-run prints them and downloads nothing). "
        "Existing local files are never overwritten on a developer runtime, and the disk guard applies.",
    )
    b.add_argument("path", nargs="?", help="path relative to the project folder (with --session: inside the session)")
    b.add_argument("--all", action="store_true", help="archived folders: also fetch every large file")
    b.add_argument(
        "--session",
        metavar="ID",
        help="fetch from this session: its id, a unique start of the id, or its label (see `box sessions`)",
    )
    b.add_argument(
        "--into",
        metavar="DIR",
        help="with --session: the target folder, relative to the project folder (default _sessions/<session id>; "
        "`--into .` writes over the project tree, never overwriting files on a developer runtime)",
    )
    b = box.add_parser(
        "send",
        parents=every + [force],
        help="upload a file or folder into this session's namespace on the sessions side",
        description="Upload a file or folder into this session's namespace on the sessions side "
        "(<sessions>/<session id>/), and record it in the session manifest. Every send of one session goes "
        "to the same namespace; a second send of a path replaces it. Every machine and later session can "
        "list it with `box sessions` and fetch it with `box fetch --session`.",
    )
    b.add_argument(
        "path",
        help="a file or folder: relative to the project folder, else to the current folder (or absolute); "
        "one outside the project folder is sent under its base name",
    )
    b.add_argument("--archive", action="store_true", help="send a folder as one .tar.gz")
    b.add_argument(
        "--note",
        metavar="TEXT",
        help="what these files are, why they were made and what to do next (one to three sentences, up to 500 "
        "characters); kept in the session manifest for later sessions. Required on a cloud runtime",
    )
    b.add_argument(
        "--label",
        metavar="TEXT",
        help="a short label for this session, set on first use (letters, digits, '-', '_', '.'; up to 60)",
    )
    b.add_argument(
        "--new-session",
        action="store_true",
        help="start a new session (developer runtimes keep one for 24 hours; cloud runtimes one per machine)",
    )
    b = box.add_parser(
        "sessions",
        parents=[common, timeout, proj, transfer],
        help="list the sessions in the box, newest first (read-only, any runtime)",
        description="List the session namespaces on the sessions side, newest first: id, label, runtime, git "
        "branch and short commit, file count, total size and when it was last updated, with the session's note "
        "under it (up to 200 characters; --json has the whole note). Reads one listing of "
        "the sessions side and each listed session's manifest; older namespaces without a manifest are "
        "listed from their files. Uses the committed index when the transfer program is missing.",
    )
    b.add_argument("--json", action="store_true", help="print JSON")
    b.add_argument("--limit", type=int, default=20, metavar="N", help="show at most N sessions (default 20)")
    b = box.add_parser(
        "promote",
        parents=every,
        help="copy a session's file or folder into the shared tree, owned by this machine (developer machine only)",
        description="Copy a file or folder of a session into the shared tree at FOLDER (default: the same path). "
        "The files are downloaded into a temp folder, placed into this machine's copy of the folder (never over "
        "a different local file), and that folder is pushed with the normal ownership of box push: claimed "
        "when nobody owns it. Refused when the folder is owned by another machine or not covered by "
        "box.include or box.archives. The promotion is recorded in the session manifest.",
    )
    b.add_argument("path", metavar="PATH", help="a file or folder inside the session")
    b.add_argument("--session", required=True, metavar="ID", help="the session: id, unique start of the id, or label")
    b.add_argument("--as", dest="as_folder", metavar="FOLDER", help="where it goes in the shared tree (default: PATH)")
    b = box.add_parser(
        "prune",
        parents=[common, timeout, proj, dry, transfer],
        help="delete session namespaces from the box (developer machine only; needs --yes)",
        description="Delete session namespaces from the sessions side: those named with --session, and those "
        "last updated more than --older-than DAYS ago. It always lists what it would delete with sizes and "
        "deletes only with --yes. It never touches the shared tree and refuses names that do not look like "
        "session ids.",
    )
    b.add_argument("--older-than", type=float, metavar="DAYS", help="sessions last updated more than DAYS days ago")
    b.add_argument("--session", action="append", metavar="ID", help="a full session id (repeatable)")
    b.add_argument("--keep-promoted", action="store_true", help="keep sessions with anything promoted from them")
    b.add_argument("--yes", action="store_true", help="really delete (without it, only the list is shown)")
    b = box.add_parser(
        "receive",
        parents=every + [force],
        help="download every session not received yet (developer machine only)",
        description="Download every session's files that are not in the receive folder yet, one folder per "
        "session, never overwriting local files. To look first, use `agent-mirror box sessions`; to get one "
        "session (or part of it) anywhere, use `agent-mirror box fetch --session ID [PATH]`.",
    )
    b.add_argument(
        "--into", metavar="DIR", help="target folder (default: box.receive_dir, else <project>/_agent_outbound)"
    )
    b = box.add_parser("status", parents=[common, proj], help="what was fetched on this machine, and free disk")
    b = box.add_parser("evict", parents=[common, proj, dry], help="delete fetched copies to free disk (cloud only)")
    b.add_argument("path", help="path relative to the project folder (a fetched session: _sessions/<session id>)")
    b.add_argument("--force", action="store_true", help="allow it on a local runtime")
    b = box.add_parser("mount", parents=every, help="rclone: mount the shared side read-only (needs FUSE)")
    b.add_argument("--cache-max-size", default="10G", help="cap for the local file cache (default 10G)")
    b = box.add_parser("unmount", parents=every, help="rclone: unmount what box mount mounted")
    b = box.add_parser(
        "candidates",
        parents=[common, proj],
        help="list untracked and ignored project entries that could go in the box (read-only)",
    )
    b.add_argument("--json", action="store_true", help="print JSON")
    b.add_argument(
        "--min-bytes", type=int, default=0, metavar="N", help="leave out entries smaller than N bytes (default 0)"
    )
    b.add_argument(
        "--time-budget", type=float, default=30.0, metavar="SECONDS", help="time for all size walks (default 30)"
    )
    b = box.add_parser(
        "unsent",
        parents=[common, proj],
        help="list the files of the clone this session made or changed that are in no git commit and not in the box",
        description="List the files of the project clone that are untracked or ignored by git, changed at or after "
        "the session baseline (recorded by session-start), and persisted nowhere: not fetched from the box "
        "unchanged, not sent in this session unchanged, not declared disposable with `box skip`, and not left out "
        "by name (env files, links, .git, .agent-mirror, __pycache__, .venv, venv, node_modules, .cache, "
        ".pytest_cache, .mypy_cache, .ruff_cache, .tox, .nox, nested git clones), box.exclude or "
        "box.unsent_ignore. Each is for the box (ignored by git, or untracked and large: above "
        "box.git_text_max_bytes, binary above box.git_binary_max_bytes, or one of many small files of a top-level "
        "folder above box.git_total_max_bytes in all) or left to git (a small untracked file: commit it as usual). "
        "Lists the files for the box grouped by top-level folder, largest first, and one line for the rest. "
        "Read-only and offline. Exit 0; with --check, 3 when something is for the box or the check could not finish.",
    )
    b.add_argument("--all", action="store_true", help="also list the small untracked files left to git")
    b.add_argument("--json", action="store_true", help="print JSON")
    b.add_argument(
        "--check",
        action="store_true",
        help="exit 3 when files are for the box or the check could not finish, 0 otherwise",
    )
    b.add_argument(
        "--time-limit",
        type=float,
        default=5.0,
        metavar="SECONDS",
        help="time for the whole check (default 5); a check cut short says so",
    )
    b = box.add_parser(
        "skip",
        parents=[common, timeout, proj, dry, transfer],
        help="declare files or folders of the clone disposable for this session, with a reason",
        description="Record that this session leaves these files or folders behind on purpose: in the local "
        "session record (so `box unsent` and the stop check leave them out) and in the session manifest on the "
        "box, as an entry of its `skipped` list with the reason, so later sessions see what was left behind and "
        "why (`box sessions`, `box fetch --session`). When the box cannot be reached, it is recorded on this "
        "machine only and the command says so. Paths must be inside the project clone.",
    )
    b.add_argument("paths", nargs="+", metavar="PATH", help="a file or folder inside the clone (repeatable)")
    b.add_argument(
        "--reason",
        required=True,
        metavar="TEXT",
        help="why they can go (one line, up to 500 characters; text that looks like a secret is refused)",
    )
    b.add_argument("--label", metavar="TEXT", help="a short label for this session, set on first use (as box send)")
    b = box.add_parser("rclone-env", parents=[common], help="turn a local rclone remote into RCLONE_CONFIG_* variables")
    b.add_argument("--remote", required=True, metavar="NAME", help="the rclone remote name")
    b.add_argument("--out", metavar="FILE", help="write NAME=value lines to FILE (owner-only); otherwise names only")
    for box_parser in box.choices.values():
        box_parser.set_defaults(func=lazy("box", "cmd_box"))

    s = sub.add_parser("cloud", parents=[common], help="cloud environments: render, secrets, runbook, bootstrap")
    cloud = s.add_subparsers(dest="cloud_action", metavar="action")
    cloud.required = True
    c = cloud.add_parser(
        "render", parents=[common, dry], help="write projects/NAME/cloud/ (scripts, runbook) in the store"
    )
    c.add_argument("--project", required=True, help="project name under projects/ in the store")
    c.add_argument("--vendor", choices=["claude", "codex", "all"], default="all", help="which vendor (default all)")
    c = cloud.add_parser(
        "secrets", parents=[common], help="produce the environment values on this machine (--check or --out)"
    )
    c.add_argument("--project", required=True, help="project name under projects/ in the store")
    c.add_argument("--vendor", choices=["claude", "codex"], default="claude", help="which vendor (default claude)")
    how = c.add_mutually_exclusive_group(required=True)
    how.add_argument("--check", action="store_true", help="show what can be produced now; never prints values")
    how.add_argument("--out", metavar="FILE", help="write NAME=value lines to a new owner-only file outside any repo")
    c.add_argument("--replace", action="store_true", help="with --out: overwrite an existing owner-only file")
    c = cloud.add_parser(
        "bootstrap",
        parents=[common, timeout, dry],
        help="on a cloud machine: set it up from the store's project settings (run by the setup script)",
        description="Run by the fixed setup script on a cloud machine. Reads each project's env.json from the "
        "store's default branch and runs its setup steps (companion repos, python through uv, setup_commands, "
        "each timed into the setup note file), then init-store --adopt, install and (claude) hooks install. "
        "Records a stamp of the setup inputs on this machine, so that session-start can run the steps again "
        "when the store changes them. Refuses to run on a developer machine.",
    )
    c.add_argument("--project", action="append", required=True, metavar="NAME", help="a project (repeatable)")
    c.add_argument("--vendor", choices=["claude", "codex"], required=True, help="which vendor's machine this is")
    c.add_argument("--note", metavar="FILE", help="the setup note file (default ~/agent_setup_probe.txt)")
    c.add_argument("--step-timeout", type=int, default=1800, help="seconds for each setup command (default 1800)")
    c = cloud.add_parser("runbook", parents=[common], help="print the setup steps for a person (or --json)")
    c.add_argument("--project", required=True, help="project name under projects/ in the store")
    c.add_argument("--vendor", choices=["claude", "codex", "all"], default="all", help="which vendor (default all)")
    c.add_argument("--json", action="store_true", help="print runbook.json instead")
    for cloud_parser in cloud.choices.values():
        cloud_parser.set_defaults(func=lazy("cloud", "cmd_cloud"))

    s = sub.add_parser("review", parents=[common, timeout], help="review inbox branches pushed by cloud sessions")
    s.add_argument(
        "action",
        nargs="?",
        choices=["list", "show", "accept", "discard"],
        default="list",
        help="list (default), show BRANCH, accept BRANCH, discard BRANCH",
    )
    s.add_argument("branch", nargs="?", metavar="BRANCH", help="an inbox branch, with or without the inbox/ prefix")
    s.add_argument("--paths", nargs="+", metavar="PATH", help="accept: only these held paths")
    s.set_defaults(func=lazy("inbox", "cmd_review"))

    s = sub.add_parser("clip", parents=[common], help="clipboard: copy FILE, check, clear (never prints secrets)")
    s.add_argument("action", choices=["copy", "check", "clear"])
    s.add_argument("file", nargs="?", metavar="FILE", help="copy: the file whose text goes on the clipboard")
    s.add_argument("--secret", action="store_true", help="print only NAME and value length per line")
    s.set_defaults(func=lazy("clip", "cmd_clip"))
    return parser


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if "clip" in argv:
        # `clip copy --secret FILE` and `clip copy FILE --secret` must both work; argparse only
        # accepts the second form when an optional positional follows a flag.
        at = argv.index("clip") + 1
        rest = argv[at:]
        argv = argv[:at] + [a for a in rest if a != "--secret"] + [a for a in rest if a == "--secret"]
    args = build_parser().parse_args(argv)
    ctx = Context(store_flag=getattr(args, "store", None))
    if args.cmd != "debug":
        violations = ctx.plat.sandbox_violations()
        if violations:
            err(
                "AGENT_MIRROR_TEST_SANDBOX is set to %s, but these locations are outside it, so nothing runs: %s"
                % (ctx.plat.sandbox(), "; ".join("%s=%s" % v for v in violations))
            )
            return SANDBOX_EXIT
    try:
        return args.func(args, ctx) or 0
    except KeyboardInterrupt:
        return 130
    except BrokenPipeError:  # output piped into `head` and closed early
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
        return 0
