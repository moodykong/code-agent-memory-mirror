"""Command line for agent-mirror. Run `agent-mirror -h` or `agent-mirror <command> -h`."""

import argparse
import os
import sys

from agent_mirror import __version__
from agent_mirror.box.common import DEFAULT_TRANSFER_TIMEOUT
from agent_mirror.config import Context
from agent_mirror.runtime import AGENT_NAMES, detect_runtime
from agent_mirror.util import DEFAULT_TIMEOUT, out

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

    s = sub.add_parser("detect", parents=[common], help="print the runtime name")
    s.set_defaults(func=cmd_detect)

    s = sub.add_parser("init-store", parents=[common, dry], help="make a new store, or --adopt an existing one")
    s.add_argument("path", metavar="PATH", help="the store folder")
    s.add_argument("--remote", metavar="URL", help="git remote for the store (origin)")
    s.add_argument("--adopt", action="store_true", help="use an existing store folder as it is")
    s.add_argument("--commit", action="store_true", help="commit the new skeleton")
    s.set_defaults(func=lazy("storeinit", "cmd_init_store"))

    s = sub.add_parser(
        "install", parents=[common, timeout, proj, dry], help="link the agents' home folders into the store"
    )
    s.add_argument(
        "--agents", help="comma list of %s (default: the config's agents, else both)" % ",".join(AGENT_NAMES)
    )
    s.add_argument(
        "--memory-dir", metavar="PATH", help="Claude's memory folder for --project (default: found or computed)"
    )
    s.set_defaults(func=lazy("install", "cmd_install"))

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

    s = sub.add_parser("hook", parents=[common], help="handlers called by agent hooks (reads the hook JSON on stdin)")
    s.add_argument("name", choices=["memory-write"])
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

    s = sub.add_parser("box", parents=[common], help="move bulk files: push, index, ls, fetch, send, receive and more")
    box = s.add_subparsers(dest="box_action", metavar="action")
    box.required = True
    every = [common, timeout, proj, dry, transfer]
    force = argparse.ArgumentParser(add_help=False)
    force.add_argument("--force", action="store_true", help="go ahead even when the disk guard says no")
    b = box.add_parser("push", parents=every, help="upload the include folders and archives (developer machine only)")
    b.add_argument("--delete", action="store_true", help="also delete remote files whose local file is gone")
    b = box.add_parser(
        "index", parents=every, help="list the inbound side into box_index.jsonl (with archive contents)"
    )
    b.add_argument("--page-size", type=int, default=5000, help="azure-blob: blobs per listing call (default 5000)")
    b = box.add_parser("ls", parents=[common, proj, transfer], help="browse the box from the index, offline")
    b.add_argument("path", nargs="?", help="folder or file relative to the project folder (default: the top)")
    b = box.add_parser("fetch", parents=every + [force], help="download a file or folder from the inbound side")
    b.add_argument("path", help="path relative to the project folder")
    b.add_argument("--all", action="store_true", help="archived folders: also fetch every large file")
    b = box.add_parser("send", parents=every + [force], help="upload a file or folder to the outbound side")
    b.add_argument("path", help="path relative to the project folder")
    b.add_argument("--archive", action="store_true", help="send a folder as one .tar.gz")
    b = box.add_parser("receive", parents=every + [force], help="download the outbound side (developer machine only)")
    b.add_argument(
        "--into", metavar="DIR", help="target folder (default: box.receive_dir, else <project>/_agent_outbound)"
    )
    b = box.add_parser("status", parents=[common, proj], help="what was fetched on this machine, and free disk")
    b = box.add_parser("evict", parents=[common, proj, dry], help="delete fetched copies to free disk (cloud only)")
    b.add_argument("path", help="path relative to the project folder")
    b.add_argument("--force", action="store_true", help="allow it on a local runtime")
    b = box.add_parser("mount", parents=every, help="rclone: mount the inbound side read-only (needs FUSE)")
    b.add_argument("--cache-max-size", default="10G", help="cap for the local file cache (default 10G)")
    b = box.add_parser("unmount", parents=every, help="rclone: unmount what box mount mounted")
    b = box.add_parser("rclone-env", parents=[common], help="turn a local rclone remote into RCLONE_CONFIG_* variables")
    b.add_argument("--remote", required=True, metavar="NAME", help="the rclone remote name")
    b.add_argument("--out", metavar="FILE", help="write NAME=value lines to FILE (owner-only); otherwise names only")
    for box_parser in box.choices.values():
        box_parser.set_defaults(func=lazy("box", "cmd_box"))

    s = sub.add_parser("cloud", parents=[common], help="render cloud environment settings into the store")
    cloud = s.add_subparsers(dest="cloud_action", metavar="action")
    cloud.required = True
    c = cloud.add_parser("render", parents=[common, dry], help="write projects/NAME/cloud/ in the store")
    c.add_argument("--project", required=True, help="project name under projects/ in the store")
    c.add_argument("--vendor", choices=["claude", "codex", "all"], default="all", help="which vendor (default all)")
    c.set_defaults(func=lazy("cloud", "cmd_cloud"))
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    ctx = Context(store_flag=getattr(args, "store", None))
    try:
        return args.func(args, ctx) or 0
    except KeyboardInterrupt:
        return 130
    except BrokenPipeError:  # output piped into `head` and closed early
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
        return 0
