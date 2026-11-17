"""agent-mirror clip: put text on the clipboard, check it, clear it, without printing secrets.

An agent can hand a long or secret text to the user this way: the text goes to the
clipboard, and the terminal only shows a summary (line count and first line, or with
--secret only each NAME and its value length).

Backends, first match wins:
  Windows and WSL   clip.exe to copy, PowerShell Get-Clipboard to read
  macOS             pbcopy and pbpaste
  Linux             wl-copy and wl-paste (Wayland), then xclip, then xsel (X11)
"""

import os
import shutil
import subprocess
import sys

from agent_mirror.envfile import VALID_NAME
from agent_mirror.platforms import normalize_platform
from agent_mirror.util import err, out, short

PS_READ = "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; Get-Clipboard -Raw"


def is_wsl(read_version=None):
    try:
        text = read_version() if read_version else open("/proc/version", encoding="utf-8", errors="replace").read()
    except OSError:
        return False
    return "microsoft" in text.lower()


def backend(platform_name=None, env=None, which=shutil.which, read_version=None):
    """{name, copy, paste, clear, encoding} for this machine, or None when there is no clipboard tool."""
    name = normalize_platform(platform_name or sys.platform)
    env = os.environ if env is None else env
    if name == "win32" or (name == "linux" and is_wsl(read_version)):
        clip = which("clip.exe") or which("clip")
        ps = which("powershell.exe") or which("pwsh.exe") or which("powershell") or which("pwsh")
        if clip and ps:
            return {
                "name": "clip.exe and PowerShell",
                "copy": [clip],
                "paste": [ps, "-NoProfile", "-Command", PS_READ],
                "clear": None,
                "encoding": "utf-16",
            }
        if name == "win32":
            return None
    if name == "darwin":
        if which("pbcopy") and which("pbpaste"):
            return {
                "name": "pbcopy and pbpaste",
                "copy": ["pbcopy"],
                "paste": ["pbpaste"],
                "clear": None,
                "encoding": "utf-8",
            }
        return None
    if env.get("WAYLAND_DISPLAY") and which("wl-copy") and which("wl-paste"):
        return {
            "name": "wl-copy and wl-paste",
            "copy": ["wl-copy"],
            "paste": ["wl-paste", "--no-newline"],
            "clear": ["wl-copy", "--clear"],
            "encoding": "utf-8",
        }
    if env.get("DISPLAY") and which("xclip"):
        return {
            "name": "xclip",
            "copy": ["xclip", "-selection", "clipboard", "-in"],
            "paste": ["xclip", "-selection", "clipboard", "-out"],
            "clear": None,
            "encoding": "utf-8",
        }
    if env.get("DISPLAY") and which("xsel"):
        return {
            "name": "xsel",
            "copy": ["xsel", "--clipboard", "--input"],
            "paste": ["xsel", "--clipboard", "--output"],
            "clear": ["xsel", "--clipboard", "--clear"],
            "encoding": "utf-8",
        }
    return None


NO_BACKEND = (
    "no clipboard tool found: on Windows and WSL agent-mirror needs clip.exe and PowerShell, on macOS pbcopy and "
    "pbpaste, on Linux wl-clipboard (Wayland) or xclip or xsel (X11, with DISPLAY set)"
)


def summarize(text, secret):
    """Lines describing clipboard text without showing it (secret) or showing only the first line."""
    lines = text.splitlines()
    if not secret:
        first = short(lines[0], 80) if lines else ""
        return ["%d line(s), %d characters; first line: %s" % (len(lines), len(text), first)]
    found = ["%d line(s); names and value lengths only:" % len(lines)]
    for n, line in enumerate(lines, 1):
        name, sep, value = line.partition("=")
        name = name.strip()
        if name.startswith("export "):
            name = name[len("export ") :].strip()
        if sep and VALID_NAME.match(name):
            found.append("  %s (%d characters)" % (name, len(value.strip())))
        elif line.strip():
            found.append("  line %d: %d characters" % (n, len(line)))
    return found


def run_copy(b, text):
    data = text.encode(b["encoding"])
    proc = subprocess.run(b["copy"], input=data, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=30)
    return proc.returncode, proc.stderr.decode("utf-8", "replace")


def run_paste(b):
    proc = subprocess.run(
        b["paste"], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30
    )
    text = proc.stdout.decode("utf-8", "replace").replace("\r\n", "\n")
    if b["name"].startswith("clip.exe") and text.endswith("\n"):
        text = text[:-1]
    return proc.returncode, text, proc.stderr.decode("utf-8", "replace")


def cmd_clip(args, ctx):
    b = backend()
    if b is None:
        err(NO_BACKEND)
        return 3
    action = args.action
    try:
        if action == "copy":
            if not args.file:
                err("clip copy needs a FILE")
                return 2
            with open(os.path.expanduser(args.file), encoding="utf-8") as fh:
                text = fh.read()
            rc, stderr = run_copy(b, text)
            if rc != 0:
                err("copy failed (%s): %s" % (b["name"], short(stderr, 200)))
                return 1
            out("copied %s to the clipboard (%s)" % (args.file, b["name"]))
            for line in (
                summarize(text, args.secret)
                if args.secret
                else ["%d line(s), %d characters" % (len(text.splitlines()), len(text))]
            ):
                out(line)
            return 0
        if action == "check":
            rc, text, stderr = run_paste(b)
            if rc != 0:
                err("reading the clipboard failed (%s): %s" % (b["name"], short(stderr, 200)))
                return 1
            for line in summarize(text, args.secret):
                out(line)
            return 0
        if b["clear"]:
            proc = subprocess.run(b["clear"], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=30)
            rc, stderr = proc.returncode, proc.stderr.decode("utf-8", "replace")
        else:
            rc, stderr = run_copy(b, "")
        if rc != 0:
            err("clearing the clipboard failed (%s): %s" % (b["name"], short(stderr, 200)))
            return 1
        out("cleared the clipboard (%s)" % b["name"])
        return 0
    except (OSError, subprocess.SubprocessError) as exc:
        err("clipboard tool failed (%s): %s" % (b["name"], exc))
        return 1
