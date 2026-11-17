"""timer install|uninstall|status: run `agent-mirror sync` every N minutes.

Linux:   a systemd user service and timer in ~/.config/systemd/user/
macOS:   a launchd agent in ~/Library/LaunchAgents/ (UNVERIFIED)
Windows: a scheduled task made with schtasks (UNVERIFIED)

The content for each OS is built by plain functions that take the platform
name, so it can be unit-tested on any OS.
"""

import os
import plistlib
import subprocess
import sys

from agent_mirror import PLUGIN_ROOT
from agent_mirror.platforms import Platform
from agent_mirror.util import err, out, run, short, show_cmd, tilde, warn

UNIT = "agent-mirror-sync"
LABEL = "agent-mirror.sync"
TASK = "agent-mirror-sync"


def sync_argv(ctx, plat=None, python=None):
    """The command the scheduler runs."""
    plat = plat or ctx.plat
    argv = [plat.python_for_scheduler(python), plat.join(PLUGIN_ROOT, "bin", "agent-mirror")]
    if ctx.store_source_for_commands():  # a flag or AGENT_MIRROR_STORE: schedulers do not see either
        argv += ["--store", ctx.require_store().root]
    argv.append("sync")
    if plat.windows:
        argv += ["--log", plat.join(plat.cache_dir(), "sync.log")]
    return argv


# systemd (Linux) ------------------------------------------------------------------------


def systemd_quote(arg):
    a = str(arg).replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$")
    return '"%s"' % a


def systemd_units(argv, minutes):
    service = (
        "[Unit]\n"
        "Description=agent-mirror: sync the agent context store with git\n"
        "\n"
        "[Service]\n"
        "Type=oneshot\n"
        "ExecStart=%s\n" % " ".join(systemd_quote(a) for a in argv)
    )
    timer = (
        "[Unit]\n"
        "Description=agent-mirror: run sync every %d minutes\n"
        "\n"
        "[Timer]\n"
        "OnBootSec=5min\n"
        "OnUnitActiveSec=%dmin\n"
        "Unit=%s.service\n"
        "\n"
        "[Install]\n"
        "WantedBy=timers.target\n" % (minutes, minutes, UNIT)
    )
    return service, timer


def systemd_dir(plat):
    base = plat.env.get("XDG_CONFIG_HOME") or plat.join(plat.home(), ".config")
    return plat.join(base, "systemd", "user")


# launchd (macOS) -----------------------------------------------------------------------------


def launchd_plist(argv, minutes, log_path, path_env=None):
    data = {
        "Label": LABEL,
        "ProgramArguments": list(argv),
        "StartInterval": int(minutes) * 60,
        "RunAtLoad": True,
        "ProcessType": "Background",
        "StandardOutPath": log_path,
        "StandardErrorPath": log_path,
    }
    if path_env:
        data["EnvironmentVariables"] = {"PATH": path_env}
    return plistlib.dumps(data).decode("utf-8")


def launchd_path(plat):
    return plat.join(plat.home(), "Library", "LaunchAgents", LABEL + ".plist")


# schtasks (Windows) -----------------------------------------------------------------------------


def schtasks_create(argv, minutes):
    return [
        "schtasks",
        "/Create",
        "/TN",
        TASK,
        "/SC",
        "MINUTE",
        "/MO",
        str(minutes),
        "/TR",
        subprocess.list2cmdline(argv),
        "/F",
    ]


# plans ----------------------------------------------------------------------------------------------


def plan(plat, action, minutes=30, argv=None, uid=None, path_env=None):
    """{'files': {path: text}, 'remove': [paths], 'run': [argv], 'check': [argv]} for one OS and action."""
    p = {"files": {}, "remove": [], "run": [], "check": []}
    if plat.name == "linux":
        d = systemd_dir(plat)
        service_path = plat.join(d, UNIT + ".service")
        timer_path = plat.join(d, UNIT + ".timer")
        if action == "install":
            service, timer = systemd_units(argv, minutes)
            p["files"] = {service_path: service, timer_path: timer}
            p["run"] = [
                ["systemctl", "--user", "daemon-reload"],
                ["systemctl", "--user", "enable", "--now", UNIT + ".timer"],
            ]
        elif action == "uninstall":
            p["check"] = [["systemctl", "--user", "disable", "--now", UNIT + ".timer"]]
            p["remove"] = [service_path, timer_path]
            p["run"] = [["systemctl", "--user", "daemon-reload"]]
        else:
            p["remove"] = []
            p["files"] = {}
            p["check"] = [
                ["systemctl", "--user", "is-enabled", UNIT + ".timer"],
                ["systemctl", "--user", "list-timers", UNIT + ".timer", "--no-pager"],
            ]
            p["paths"] = [service_path, timer_path]
    elif plat.name == "darwin":
        target = "gui/%s" % (uid if uid is not None else "<uid>")
        plist_path = launchd_path(plat)
        log = plat.join(plat.home(), "Library", "Logs", "agent-mirror-sync.log")
        if action == "install":
            p["files"] = {plist_path: launchd_plist(argv, minutes, log, path_env)}
            p["check"] = [["launchctl", "bootout", "%s/%s" % (target, LABEL)]]
            p["run"] = [["launchctl", "bootstrap", target, plist_path]]
        elif action == "uninstall":
            p["check"] = [["launchctl", "bootout", "%s/%s" % (target, LABEL)]]
            p["remove"] = [plist_path]
        else:
            p["check"] = [["launchctl", "print", "%s/%s" % (target, LABEL)]]
            p["paths"] = [plist_path]
    elif plat.name == "win32":
        if action == "install":
            p["run"] = [schtasks_create(argv, minutes)]
        elif action == "uninstall":
            p["run"] = [["schtasks", "/Delete", "/TN", TASK, "/F"]]
        else:
            p["check"] = [["schtasks", "/Query", "/TN", TASK]]
    else:
        raise ValueError("timer is not supported on %s" % plat.name)
    return p


def cmd_timer(args, ctx, plat=None):
    plat = plat or ctx.plat
    if not 1 <= args.minutes <= 1439:
        err("--minutes must be between 1 and 1439")
        return 2
    argv = sync_argv(ctx, plat) if args.action == "install" else None
    uid = os.getuid() if hasattr(os, "getuid") else None
    try:
        p = plan(plat, args.action, args.minutes, argv, uid, os.environ.get("PATH"))
    except ValueError as exc:
        err(str(exc))
        return 2
    if args.action == "status":
        for path in p.get("paths", []):
            out("%-8s %s" % ("exists" if os.path.isfile(path) else "missing", tilde(path)))
        for cmd in p["check"]:
            rc, stdout, stderr = run(cmd, timeout=20)
            text = (stdout or stderr).strip() or "(no output)"
            out("$ %s  (rc %s)" % (show_cmd(cmd), rc))
            for line in text.splitlines()[:12]:
                out("  " + line)
        return 0
    if args.action == "install" and plat.windows and len(subprocess.list2cmdline(argv)) > 261:
        warn("the task command is longer than the 261 characters schtasks accepts; move the plugin to a shorter path")
    if args.dry_run:
        out("dry run: nothing will change")
        for path, text in p["files"].items():
            out("would write %s:" % path)
            for line in text.splitlines():
                out("  | " + line)
        for path in p["remove"]:
            out("would remove %s" % path)
        for cmd in p["check"] + p["run"]:
            out("would run: %s" % show_cmd(cmd))
        return 0
    for cmd in p["check"]:
        rc, stdout, stderr = run(cmd, timeout=30)
        out("ran: %s (rc %s)" % (show_cmd(cmd), rc))
    for path, text in p["files"].items():
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        out("wrote %s" % tilde(path))
    for path in p["remove"]:
        if os.path.exists(path):
            os.remove(path)
            out("removed %s" % tilde(path))
    failures = 0
    for cmd in p["run"]:
        rc, stdout, stderr = run(cmd, timeout=60)
        if rc == 0:
            out("ran: %s" % show_cmd(cmd))
        else:
            failures += 1
            err("%s failed (rc %s): %s" % (show_cmd(cmd), rc, short(stderr or stdout or "no output")))
            if plat.name == "linux" and "systemd" in (stderr or stdout or "").lower():
                err("is systemd running for your user? On WSL, enable it with [boot] systemd=true in /etc/wsl.conf")
    out("timer %s: %s" % (args.action, "done" if not failures else "%d step(s) failed" % failures))
    return 1 if failures else 0


def preview(platform_name, minutes=30, argv=None, env=None, uid=501):
    """The install plan for another OS (used by tests and docs)."""
    plat = Platform(platform_name, env or {})
    argv = argv or [sys.executable, "agent-mirror", "sync"]
    return plan(plat, "install", minutes, argv, uid, "/usr/bin:/bin")
