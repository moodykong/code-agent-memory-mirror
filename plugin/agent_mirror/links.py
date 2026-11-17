"""Making, reading and removing links from the home folder into the store.

Order of methods: a symlink; on Windows, when that fails (no Developer Mode
or admin rights), a directory junction for folders and a managed copy for
single files. The manifest records which method was used.
"""

import errno
import os
import shutil
from pathlib import Path

from agent_mirror.util import run, short

FILE_ATTRIBUTE_REPARSE_POINT = 0x400
IO_REPARSE_TAG_MOUNT_POINT = 0xA0000003


def is_junction(path):
    fn = getattr(os.path, "isjunction", None)  # Python 3.12+
    if fn is not None:
        try:
            return bool(fn(path))
        except OSError:
            return False
    if os.name != "nt":
        return False
    try:
        st = os.lstat(path)
    except OSError:
        return False
    attrs = getattr(st, "st_file_attributes", 0)
    return bool(attrs & FILE_ATTRIBUTE_REPARSE_POINT) and getattr(st, "st_reparse_tag", 0) == IO_REPARSE_TAG_MOUNT_POINT


def is_linklike(path):
    return os.path.islink(path) or is_junction(path)


def raw_link(path):
    try:
        target = os.readlink(path)
    except (OSError, ValueError):
        return None
    if target.startswith("\\\\?\\"):  # Windows junction targets come back with this prefix
        target = target[4:]
    return target


def link_target(path):
    """Where a symlink or junction points, as an absolute normalised path, or None."""
    target = raw_link(path)
    if target is None:
        return None
    return os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(path)), target))


def link_location(link):
    """Where a link would really live: real parent folder plus its own name."""
    parent = os.path.realpath(os.path.dirname(os.path.abspath(link)))
    return os.path.join(parent, os.path.basename(link))


def resolve_problem(path):
    """None if path resolves cleanly, 'loop' for a link loop, 'missing', or a message."""
    try:
        Path(path).resolve(strict=True)
        return None
    except RuntimeError:  # Python 3.8 to 3.12 report loops this way
        return "loop"
    except OSError as exc:
        if exc.errno == errno.ELOOP or getattr(exc, "winerror", None) == 1921:
            return "loop"
        if exc.errno == errno.ENOENT:
            return "missing"
        return str(exc)


def describe(path):
    if os.path.islink(path):
        return "symlink -> %s" % raw_link(path)
    if is_junction(path):
        return "junction -> %s" % raw_link(path)
    if os.path.isdir(path):
        return "real folder"
    if os.path.exists(path):
        return "real file"
    return "absent"


def make_junction(link, target):
    rc, stdout, stderr = run(["cmd", "/c", "mklink", "/J", link, target], timeout=30)
    if rc != 0 or not os.path.isdir(link):
        raise OSError("mklink /J failed: %s" % short(stderr or stdout or "rc %s" % rc))


def make_link(link, target, is_dir, fallback, symlink=None, junction=None, copy=None):
    """Create link -> target and return the method used: 'symlink', 'junction' or 'copy'.

    fallback=True allows the Windows fallbacks when the symlink fails;
    otherwise the symlink error is raised. symlink, junction and copy can be
    replaced (tests simulate a failing symlink this way).
    """
    symlink = symlink or os.symlink
    try:
        symlink(target, link, target_is_directory=is_dir)
        return "symlink"
    except (OSError, NotImplementedError):
        if not fallback:
            raise
    if is_dir:
        (junction or make_junction)(link, target)
        return "junction"
    (copy or shutil.copy2)(target, link)
    return "copy"


def remove_link(path):
    """Remove a symlink or junction (never its target)."""
    try:
        os.unlink(path)
    except OSError:
        os.rmdir(path)  # Windows directory symlinks and junctions
