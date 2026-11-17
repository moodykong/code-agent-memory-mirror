"""Everything that differs between Linux, macOS and Windows, in one place.

A Platform is built from a platform name ('linux', 'darwin', 'win32') and an
environment mapping, so the mapping can be unit-tested on any OS by passing
both. Paths are joined with the target OS's rules (ntpath for Windows).

This is the ONLY module that turns the environment into home-like locations
(the user home, agent homes, config, cache, backups, scheduler folders, temp).
Nothing else may call os.path.expanduser, Path.home or read HOME, USERPROFILE,
APPDATA, LOCALAPPDATA or XDG_* directly.

AGENT_MIRROR_HOME, when set, replaces the user home on every platform: every
location is derived from it (the config is then <AGENT_MIRROR_HOME>/.config/
agent-mirror on all platforms), and HOME, USERPROFILE, APPDATA, XDG_CONFIG_HOME,
CLAUDE_CONFIG_DIR, CODEX_HOME and the temp variables are ignored unless they
point inside it.

AGENT_MIRROR_TEST_SANDBOX=<dir> is a tripwire for test runs: every command
exits with code 97 when any home-like location is outside <dir>, and real
schedulers and clipboard tools are never started unless the program found on
PATH is itself inside <dir>.
"""

import ntpath
import os
import posixpath
import re
import shlex
import shutil
import subprocess
import sys
import tempfile

SANDBOX_EXIT = 97
GUARDED_TOOLS = (
    "systemctl",
    "launchctl",
    "schtasks",
    "clip",
    "powershell",
    "pwsh",
    "pbcopy",
    "pbpaste",
    "wl-copy",
    "wl-paste",
    "xclip",
    "xsel",
)


def normalize_platform(name):
    name = (name or "").lower()
    if name.startswith("win"):
        return "win32"
    if name.startswith("darwin") or name == "macos":
        return "darwin"
    if name.startswith("linux"):
        return "linux"
    return name or "unknown"


def real_inside(path, root):
    """True when path (resolved) is root or lies under it; case-insensitive on Windows."""
    p = os.path.normcase(os.path.realpath(str(path)))
    r = os.path.normcase(os.path.realpath(str(root)))
    return p == r or p.startswith(r.rstrip(os.sep) + os.sep)


class Platform:
    def __init__(self, name=None, env=None):
        self.name = normalize_platform(name or sys.platform)
        self.env = os.environ if env is None else env
        self.windows = self.name == "win32"
        self.path = ntpath if self.windows else posixpath
        override = (self.env.get("AGENT_MIRROR_HOME") or "").strip()
        self.mirror_home = self.path.normpath(override) if override else None

    def join(self, *parts):
        return self.path.join(*parts)

    def lexically_inside(self, path, root):
        n = self.path
        p, r = n.normcase(n.normpath(str(path))), n.normcase(n.normpath(str(root)))
        return p == r or p.startswith(r.rstrip(n.sep) + n.sep)

    def _var(self, name):
        """An environment value; in AGENT_MIRROR_HOME mode only when it points inside that folder."""
        value = (self.env.get(name) or "").strip()
        if not value:
            return None
        if self.mirror_home and not self.lexically_inside(value, self.mirror_home):
            return None
        return value

    # homes ------------------------------------------------------------------

    def home(self):
        if self.mirror_home:
            return self.mirror_home
        e = self.env
        if self.windows:
            h = e.get("USERPROFILE") or ((e.get("HOMEDRIVE") or "") + (e.get("HOMEPATH") or ""))
        else:
            h = e.get("HOME")
        return h or os.path.expanduser("~")

    def claude_home(self):
        return self._var("CLAUDE_CONFIG_DIR") or self.join(self.home(), ".claude")

    def codex_home(self):
        return self._var("CODEX_HOME") or self.join(self.home(), ".codex")

    def agents_dir(self):
        return self.join(self.home(), ".agents")

    def agents_skills(self):
        return self.join(self.agents_dir(), "skills")

    def claude_settings(self):
        return self.join(self.claude_home(), "settings.json")

    # agent-mirror's own folders -------------------------------------------------

    def config_dir(self):
        if self.mirror_home:
            return self.join(self.mirror_home, ".config", "agent-mirror")
        if self.windows:
            base = self.env.get("APPDATA") or self.join(self.home(), "AppData", "Roaming")
        else:
            base = self.env.get("XDG_CONFIG_HOME") or self.join(self.home(), ".config")
        return self.join(base, "agent-mirror")

    def config_file(self):
        return self.join(self.config_dir(), "config.json")

    def cache_dir(self):
        if self.mirror_home:
            return self.join(self.mirror_home, ".cache", "agent-mirror")
        if self.windows:
            base = self.env.get("LOCALAPPDATA") or self.join(self.home(), "AppData", "Local")
        elif self.name == "darwin":
            base = self.join(self.home(), "Library", "Caches")
        else:
            base = self.env.get("XDG_CACHE_HOME") or self.join(self.home(), ".cache")
        return self.join(base, "agent-mirror")

    def backup_root(self):
        return self.join(self.home(), ".agent-mirror-backups")

    def systemd_user_dir(self):
        base = self._var("XDG_CONFIG_HOME") if self.mirror_home else self.env.get("XDG_CONFIG_HOME")
        return self.join(base or self.join(self.home(), ".config"), "systemd", "user")

    def launch_agents_dir(self):
        return self.join(self.home(), "Library", "LaunchAgents")

    def temp_dir(self):
        names = ("TEMP", "TMP", "TMPDIR") if self.windows else ("TMPDIR", "TEMP", "TMP")
        for name in names:
            value = self._var(name)
            if value:
                return value
        if self.mirror_home:
            return self.join(self.mirror_home, ".tmp")
        return tempfile.gettempdir()

    def locations(self):
        """Every home-like location agent-mirror may read or write, by name."""
        return {
            "home": self.home(),
            "claude_home": self.claude_home(),
            "claude_settings": self.claude_settings(),
            "codex_home": self.codex_home(),
            "agents_skills": self.agents_skills(),
            "config_dir": self.config_dir(),
            "config_file": self.config_file(),
            "cache_dir": self.cache_dir(),
            "backup_root": self.backup_root(),
            "systemd_user_dir": self.systemd_user_dir(),
            "launch_agents_dir": self.launch_agents_dir(),
            "temp_dir": self.temp_dir(),
        }

    # paths given by the user ----------------------------------------------------------

    def expand_user(self, path):
        """Replace a leading ~ with the home this Platform resolves (never the process's own home)."""
        p = str(path)
        if p == "~" or p.startswith("~/") or p.startswith("~\\"):
            return self.home() + p[1:]
        return p

    def tilde(self, path):
        h = str(self.home()).rstrip("/\\")
        p = str(path)
        if p == h:
            return "~"
        for sep in ("/", "\\"):
            if h and p.startswith(h + sep):
                return "~" + p[len(h) :]
        return p

    # the test sandbox tripwire ------------------------------------------------------------

    def sandbox(self):
        value = (self.env.get("AGENT_MIRROR_TEST_SANDBOX") or "").strip()
        return value or None

    def sandbox_violations(self):
        """[(name, path)] of home-like locations outside the test sandbox (empty when not in a sandbox)."""
        root = self.sandbox()
        if not root:
            return []
        return [(k, v) for k, v in sorted(self.locations().items()) if not real_inside(v, root)]

    def tool_blocked(self, program):
        """A reason not to start a scheduler or clipboard program in a test sandbox, or None."""
        root = self.sandbox()
        if not root:
            return None
        base = os.path.basename(str(program)).lower()
        for ext in (".exe", ".cmd", ".bat"):
            if base.endswith(ext):
                base = base[: -len(ext)]
        if base not in GUARDED_TOOLS:
            return None
        found = program if os.path.isabs(str(program)) else shutil.which(str(program))
        if found and real_inside(found, root):
            return None
        return "test sandbox: refusing to start the real %s (%s), which is outside %s" % (
            base,
            found or "not found",
            root,
        )

    def mkdtemp(self, prefix):
        base = self.temp_dir()
        os.makedirs(base, exist_ok=True)
        return tempfile.mkdtemp(prefix=prefix, dir=base)

    def mkstemp(self, prefix, suffix=""):
        base = self.temp_dir()
        os.makedirs(base, exist_ok=True)
        return tempfile.mkstemp(prefix=prefix, suffix=suffix, dir=base)

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


_ACTIVE = []


def activate(plat):
    """Make plat the Platform every helper uses (the command's own, or a test's injected one)."""
    del _ACTIVE[:]
    _ACTIVE.append(plat)


def current():
    return _ACTIVE[0] if _ACTIVE else Platform()
