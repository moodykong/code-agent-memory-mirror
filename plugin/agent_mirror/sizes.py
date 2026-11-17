"""Fast, bounded size walks: os.scandir, never following links, with a depth cap and a deadline.

When the walk stops early (too deep, out of time, or a folder it cannot read) the totals are a
lower bound, and the result says so.
"""

import datetime
import os
import time

from agent_mirror.links import is_junction

DEFAULT_MAX_DEPTH = 12


def iso_utc(ts):
    if ts is None:
        return None
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def measure(path, deadline=None, max_depth=DEFAULT_MAX_DEPTH, large=None, skip=None):
    """{"files", "bytes", "large_files", "newest", "lower_bound"} for a file or a folder.

    deadline is a time.monotonic() value; large is the large-file threshold in bytes (0 or None: none);
    skip(name, is_dir) -> True leaves an entry out.
    """
    found = {"files": 0, "bytes": 0, "large_files": 0, "newest": None, "lower_bound": False}
    newest = [None]

    def add(st):
        found["files"] += 1
        found["bytes"] += st.st_size
        if large and st.st_size >= large:
            found["large_files"] += 1
        if newest[0] is None or st.st_mtime > newest[0]:
            newest[0] = st.st_mtime

    try:
        st = os.lstat(path)
    except OSError:
        return found
    if not os.path.isdir(path) or os.path.islink(path):
        if os.path.isfile(path) and not os.path.islink(path):
            add(st)
        found["newest"] = iso_utc(newest[0])
        return found
    stack = [(path, 0)]
    while stack:
        folder, depth = stack.pop()
        if deadline is not None and time.monotonic() > deadline:
            found["lower_bound"] = True
            break
        try:
            with os.scandir(folder) as it:
                entries = list(it)
        except OSError:
            found["lower_bound"] = True
            continue
        for e in entries:
            try:
                if e.is_symlink() or (os.name == "nt" and is_junction(e.path)):
                    continue
                is_dir = e.is_dir(follow_symlinks=False)
                if skip and skip(e.name, is_dir):
                    continue
                if is_dir:
                    if depth + 1 >= max_depth:
                        found["lower_bound"] = True
                    else:
                        stack.append((e.path, depth + 1))
                elif e.is_file(follow_symlinks=False):
                    add(e.stat(follow_symlinks=False))
            except OSError:
                found["lower_bound"] = True
    found["newest"] = iso_utc(newest[0])
    return found
