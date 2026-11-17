"""Small helpers shared by every command: output, paths and running programs."""

import datetime
import os
import re
import shlex
import shutil
import subprocess
import sys

DEFAULT_TIMEOUT = 20


def out(msg=""):
    print(msg, flush=True)


def warn(msg):
    print("warning: " + msg, flush=True)


def err(msg):
    print("error: " + msg, file=sys.stderr, flush=True)


def short(text, limit=300):
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def now_stamp():
    return datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f")


def utc_stamp():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def iso_now():
    return datetime.datetime.now().isoformat(timespec="seconds")


# paths ----------------------------------------------------------------------


def norm(path):
    """Normalised path for comparisons (case-folded on Windows)."""
    return os.path.normcase(os.path.normpath(str(path)))


def within(path, root):
    """True if path equals root or lies under it (plain string check)."""
    p, r = norm(path), norm(root)
    if p == r:
        return True
    return p.startswith(r.rstrip(os.sep) + os.sep)


def tilde(path):
    """path with the home this command resolves shown as ~ (see platforms.py)."""
    from agent_mirror.platforms import current

    return current().tilde(path)


def local_path(root, rel):
    """A local path for a forward-slash relative path (box and store paths), with native separators."""
    return os.path.join(root, *[p for p in str(rel).split("/") if p])


def expand(path):
    from agent_mirror.platforms import current

    return os.path.abspath(current().expand_user(str(path)))


def read_text(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def write_text(path, text, newline="\n"):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp-agent-mirror"
    with open(tmp, "w", encoding="utf-8", newline=newline) as fh:
        fh.write(text)
    os.replace(tmp, path)


# running programs -------------------------------------------------------------


def exe_path(name):
    """The program to start. On Windows, resolve .cmd and .bat wrappers (az.cmd) through PATH."""
    if os.name == "nt":
        found = shutil.which(name)
        if found:
            return found
    return name


def run(cmd, cwd=None, timeout=DEFAULT_TIMEOUT, capture=True, extra_env=None, merge=False):
    """Run a command and never raise. Returns (returncode, stdout, stderr).

    returncode is None when the command timed out or could not start. With
    merge=True, stderr is folded into stdout in the order it was written.
    """
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    if extra_env:
        env.update(extra_env)
    argv = [str(c) for c in cmd]
    from agent_mirror.platforms import current

    blocked = current().tool_blocked(argv[0])
    if blocked:
        return None, "", blocked
    argv[0] = exe_path(argv[0])
    kwargs = dict(
        cwd=cwd,
        timeout=timeout,
        env=env,
        stdin=subprocess.DEVNULL,
        encoding="utf-8",
        errors="replace",
    )
    if capture:
        kwargs["stdout"] = subprocess.PIPE
        kwargs["stderr"] = subprocess.STDOUT if merge else subprocess.PIPE
    try:
        proc = subprocess.run(argv, **kwargs)
        return proc.returncode, proc.stdout or "", proc.stderr or ""
    except subprocess.TimeoutExpired:
        return None, "", "timed out after %s s" % timeout
    except OSError as exc:
        return None, "", str(exc)


def git(root, args, timeout=DEFAULT_TIMEOUT):
    return run(["git", "-C", root] + list(args), timeout=timeout)


def git_toplevel(path, timeout=DEFAULT_TIMEOUT):
    rc, stdout, _ = run(["git", "-C", path, "rev-parse", "--show-toplevel"], timeout=timeout)
    if rc != 0 or not stdout.strip():
        return None
    return os.path.realpath(stdout.strip())


def is_git_toplevel(path, timeout=DEFAULT_TIMEOUT):
    top = git_toplevel(path, timeout)
    return bool(top) and norm(top) == norm(os.path.realpath(path))


def upstream(root, timeout=DEFAULT_TIMEOUT):
    rc, stdout, _ = git(root, ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"], timeout)
    return stdout.strip() if rc == 0 and stdout.strip() else None


# secrets in output --------------------------------------------------------------

SECRET_NAME_RX = re.compile(r"(SECRET|TOKEN|PASSWORD|PASS|KEY)$|SECRET", re.I)


def secret_values(env=None):
    """Values of environment variables whose names look secret (long enough to be real)."""
    env = os.environ if env is None else env
    vals = []
    for name, value in env.items():
        if value and len(value) >= 6 and SECRET_NAME_RX.search(name):
            vals.append(value)
    return sorted(set(vals), key=len, reverse=True)


def scrub(text, env=None):
    """Replace secret-looking environment variable values with ***."""
    text = "" if text is None else str(text)
    for value in secret_values(env):
        text = text.replace(value, "***")
    return text


def show_cmd(cmd, env=None):
    """A command line for display: POSIX quoting, secrets replaced with ***."""
    return " ".join(shlex.quote(scrub(c, env)) for c in cmd)
