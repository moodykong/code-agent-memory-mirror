"""NAME=value files that hold secrets: quoting, reading back, and owner-only writing.

Values are quoted so that both a shell (`. file`) and a dotenv parser read them back
unchanged. Files are created with mode 0600 and are never written into the store or
into any git work tree.
"""

import os
import re
import stat

from agent_mirror.util import git_toplevel, within

VALID_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_PLAIN_ENV_VALUE = re.compile(r"^[A-Za-z0-9_./:@+,-]*$")


def quote_env_value(value):
    """Quote a value for a NAME=value line so that both a shell (`. file`) and a dotenv
    parser read it back unchanged. An OAuth token is JSON, full of double quotes; written
    bare, a shell strips those quotes and the token no longer parses."""
    value = str(value)
    if _PLAIN_ENV_VALUE.match(value):
        return value
    if "'" not in value and "\n" not in value:
        return "'%s'" % value
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$").replace("`", "\\`")
    return '"%s"' % escaped.replace("\n", "\\n")


def unquote_env_value(text):
    """The value of a NAME=value line, undoing quote_env_value (and plain shell quoting)."""
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] == "'":
        return text[1:-1]
    if len(text) >= 2 and text[0] == text[-1] == '"':
        body, outp, i = text[1:-1], [], 0
        while i < len(body):
            c = body[i]
            if c == "\\" and i + 1 < len(body):
                nxt = body[i + 1]
                outp.append("\n" if nxt == "n" else nxt)
                i += 2
                continue
            outp.append(c)
            i += 1
        return "".join(outp)
    return text


def parse_env_text(text):
    """{NAME: value} from NAME=value lines; blank lines, # comments and a leading `export ` are allowed."""
    found = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        name, sep, value = line.partition("=")
        name = name.strip()
        if sep and VALID_NAME.match(name):
            found[name] = unquote_env_value(value)
    return found


def owner_only(path):
    """True when no one but the owner can read the file (always True on Windows, UNVERIFIED there)."""
    if os.name == "nt":
        return True
    try:
        return not (os.stat(path).st_mode & (stat.S_IRWXG | stat.S_IRWXO))
    except OSError:
        return False


def placement_problem(path, store_root=None, refuse_git=True):
    """A reason a secret file may not be written at path, or None."""
    real = os.path.realpath(path)
    if store_root and within(real, os.path.realpath(store_root)):
        return "refusing to write secrets into the store (%s); pick a file outside it" % store_root
    if refuse_git:
        parent = os.path.dirname(real)
        while parent and not os.path.isdir(parent):
            nxt = os.path.dirname(parent)
            if nxt == parent:
                break
            parent = nxt
        top = git_toplevel(parent, timeout=10) if os.path.isdir(parent) else None
        if top:
            return "refusing to write secrets inside the git work tree %s; pick a file outside any repo" % top
    return None


def write_secret_file(path, pairs, replace=False):
    """Write NAME=value lines to a new 0600 file (or, with replace, over an existing owner-only file).

    Returns None, or the reason nothing was written.
    """
    if os.path.lexists(path):
        if not replace:
            return "%s already exists; remove it first, or pass --replace to overwrite it" % path
        if os.path.islink(path) or not os.path.isfile(path) or not owner_only(path):
            return "%s is not an owner-only regular file; it is not overwritten" % path
        os.remove(path)
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        for name, value in pairs:
            fh.write("%s=%s\n" % (name, quote_env_value(value)))
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return None
