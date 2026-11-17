"""Everything that differs between Linux, macOS and Windows, in one place.

A Platform is built from a platform name ('linux', 'darwin', 'win32') and an
environment mapping, so the mapping can be unit-tested on any OS by passing
both. Paths are joined with the target OS's rules (ntpath for Windows).

Only Linux (including WSL) is verified. See docs/platforms.md.
"""

import ntpath
import os
import posixpath
import re
import shlex
import subprocess
import sys


def normalize_platform(name):
    name = (name or "").lower()
    if name.startswith("win"):
        return "win32"
    if name.startswith("darwin") or name == "macos":
        return "darwin"
    if name.startswith("linux"):
        return "linux"
    return name or "unknown"


class Platform:
    def __init__(self, name=None, env=None):
        self.name = normalize_platform(name or sys.platform)
        self.env = os.environ if env is None else env
        self.windows = self.name == "win32"
        self.path = ntpath if self.windows else posixpath

    def join(self, *parts):
        return self.path.join(*parts)

    # homes ------------------------------------------------------------------

    def home(self):
        e = self.env
        if self.windows:
            h = e.get("USERPROFILE") or ((e.get("HOMEDRIVE") or "") + (e.get("HOMEPATH") or ""))
        else:
            h = e.get("HOME")
        return h or os.path.expanduser("~")

    def claude_home(self):
        return self.env.get("CLAUDE_CONFIG_DIR") or self.join(self.home(), ".claude")

    def codex_home(self):
        return self.env.get("CODEX_HOME") or self.join(self.home(), ".codex")

    def agents_dir(self):
        return self.join(self.home(), ".agents")

    def agents_skills(self):
        return self.join(self.agents_dir(), "skills")

    def claude_settings(self):
        return self.join(self.claude_home(), "settings.json")

    # agent-mirror's own folders -------------------------------------------------

    def config_dir(self):
        if self.windows:
            base = self.env.get("APPDATA") or self.join(self.home(), "AppData", "Roaming")
        else:
            base = self.env.get("XDG_CONFIG_HOME") or self.join(self.home(), ".config")
        return self.join(base, "agent-mirror")

    def config_file(self):
        return self.join(self.config_dir(), "config.json")

    def cache_dir(self):
        if self.windows:
            base = self.env.get("LOCALAPPDATA") or self.join(self.home(), "AppData", "Local")
        elif self.name == "darwin":
            base = self.join(self.home(), "Library", "Caches")
        else:
            base = self.env.get("XDG_CACHE_HOME") or self.join(self.home(), ".cache")
        return self.join(base, "agent-mirror")

    def backup_root(self):
        return self.join(self.home(), ".agent-mirror-backups")

    # launcher and commands written into other programs' settings ----------------------

    def launcher(self, plugin_root):
        return self.join(plugin_root, "bin", "agent-mirror.cmd" if self.windows else "agent-mirror")

    def quote(self, arg):
        """Quote one argument for a hook command line.

        POSIX: shell quoting. Windows: double quotes and forward slashes, which
        both cmd.exe and Git Bash accept (UNVERIFIED, see docs/platforms.md).
        """
        arg = str(arg)
        if self.windows:
            if re.match(r"^[A-Za-z0-9_.=:-]+$", arg):
                return arg
            return '"%s"' % arg.replace("\\", "/")
        return shlex.quote(arg)

    def command_line(self, argv):
        return " ".join(self.quote(a) for a in argv)

    def python_for_scheduler(self, executable=None):
        """Python to run from a scheduler. On Windows prefer pythonw.exe (no console window)."""
        exe = executable or sys.executable
        if self.windows:
            d, base = ntpath.split(exe)
            if base.lower() == "python.exe":
                w = ntpath.join(d, "pythonw.exe")
                if os.path.exists(w):
                    return w
        return exe


# Claude Code's project folder names ----------------------------------------------


def claude_slug(path):
    """Claude Code's folder name for a project: every non letter/digit becomes '-'."""
    return re.sub(r"[^A-Za-z0-9]", "-", str(path))


def slug_candidates(paths):
    """Possible slugs for a project path: as given, and both drive-letter cases (C: and c:)."""
    found = []
    for p in paths:
        p = str(p)
        variants = [p]
        if re.match(r"^[A-Za-z]:", p):
            variants += [p[0].upper() + p[1:], p[0].lower() + p[1:]]
        for v in variants:
            s = claude_slug(v)
            if s not in found:
                found.append(s)
    return found


def find_claude_project_dir(claude_home, paths, windows=False, listdir=os.listdir):
    """The existing folder under <claude home>/projects/ for one of these paths, or None."""
    root = os.path.join(claude_home, "projects")
    try:
        names = listdir(root)
    except OSError:
        return None
    wanted = slug_candidates(paths)
    for slug in wanted:
        for name in names:
            same = name.lower() == slug.lower() if windows else name == slug
            if same and os.path.isdir(os.path.join(root, name)):
                return os.path.join(root, name)
    return None


# processes ---------------------------------------------------------------------------


def pid_alive(pid, windows=None):
    """True if a process with this id is running. Never sends a signal on Windows."""
    if pid <= 0:
        return False
    if windows is None:
        windows = os.name == "nt"
    if windows:
        try:
            import ctypes

            k = ctypes.windll.kernel32
            handle = k.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
            if not handle:
                return False
            code = ctypes.c_ulong()
            ok = k.GetExitCodeProcess(handle, ctypes.byref(code))
            k.CloseHandle(handle)
            return bool(ok) and code.value == 259  # STILL_ACTIVE
        except Exception:  # noqa: BLE001 (unknown: let the lock age decide)
            return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def new_group_kwargs():
    """Popen arguments that start a program in its own process group."""
    if os.name == "nt":
        return {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
    return {"start_new_session": True}


def stop_tree(proc):
    """Stop a program and everything it started (azcopy runs as a child of az)."""
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=15,
            )
        except (OSError, subprocess.SubprocessError):
            pass
        try:
            proc.kill()
            proc.communicate(timeout=5)
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
        return
    import signal

    for sig, wait in ((signal.SIGTERM, 10), (signal.SIGKILL, 5)):
        try:
            os.killpg(proc.pid, sig)
        except OSError:
            pass
        try:
            proc.communicate(timeout=wait)
            return
        except (subprocess.TimeoutExpired, ValueError, OSError):
            continue


def current():
    return Platform()
