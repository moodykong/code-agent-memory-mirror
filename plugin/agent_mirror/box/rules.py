"""Include and exclude rules for the box, shared by every backend.

Patterns are relative to the project folder. '**' crosses folders, '*' and
'?' do not, and '**/' also matches no folder at all. A pattern without '/'
matches a file name at any depth.
"""

import os
import re


def clean_rel(path):
    p = str(path).replace("\\", "/").strip()
    if not p or p.startswith("/") or os.path.isabs(p) or re.match(r"^[A-Za-z]:", p):
        return None
    parts = [x for x in p.split("/") if x not in ("", ".")]
    if not parts or ".." in parts:
        return None
    return "/".join(parts)


def is_env_file(name):
    return name == ".env" or name.endswith(".env")


def glob_regex(pattern):
    p = pattern.strip().lstrip("/")
    if "/" not in p:
        p = "**/" + p
    rx = ""
    i = 0
    while i < len(p):
        if p.startswith("**/", i):
            rx += "(?:.*/)?"
            i += 3
        elif p.startswith("**", i):
            rx += ".*"
            i += 2
        elif p[i] == "*":
            rx += "[^/]*"
            i += 1
        elif p[i] == "?":
            rx += "[^/]"
            i += 1
        else:
            rx += re.escape(p[i])
            i += 1
    return re.compile("^" + rx + "$")


def literal_folder(text):
    """True for a plain relative folder path with no wildcards."""
    return not any(c in text for c in "*?[;") and clean_rel(text) == text


def simple_name(text):
    """True for one file or folder name; '*' and '?' are allowed."""
    return bool(text) and not any(c in text for c in "/[;") and "**" not in text


def include_prefix(pattern):
    """The literal folder an include pattern starts with ('docs/slides/**' -> 'docs/slides'), or None."""
    parts = []
    for part in str(pattern).strip().lstrip("/").split("/"):
        if not part or any(c in part for c in "*?[]{}") or part in (".", ".."):
            break
        parts.append(part)
    if parts and parts == str(pattern).strip().lstrip("/").split("/"):
        parts = parts[:-1]  # a plain file pattern: its folder
    return "/".join(parts) or None


def include_root(pattern):
    """'results/**' -> 'results'; None for any other shape."""
    p = pattern.strip().lstrip("/")
    if p.endswith("/**") and literal_folder(p[:-3]):
        return p[:-3]
    return None


def exclude_rule(pattern):
    """Sort an exclude glob into a shape that az storage blob sync can express.

    ('path', 'a/b')   from 'a/b/**'                everything under one folder
    ('name', '*.env') from '**/*.env' or '*.env'   files with that name, any depth
    ('dir', 'x')      from '**/x/**'               everything under any folder named x
    None for any other shape.
    """
    p = pattern.strip().lstrip("/")
    if p.startswith("**/"):
        rest = p[3:]
        if rest.endswith("/**"):
            return ("dir", rest[:-3]) if simple_name(rest[:-3]) else None
        return ("name", rest) if simple_name(rest) else None
    if "/" not in p:
        return ("name", p) if simple_name(p) else None
    if p.endswith("/**") and literal_folder(p[:-3]):
        return ("path", p[:-3])
    return None


def azure_rule_problems(box):
    problems = []
    for pat in box.get("include") or []:
        if include_root(pat) is None:
            problems.append("include pattern %r is not supported: use folder/** with no wildcards" % pat)
    for pat in box.get("exclude") or []:
        if exclude_rule(pat) is None:
            problems.append(
                "exclude pattern %r is not supported: use folder/**, **/name, name or **/folder-name/** "
                "(only * and ? as wildcards, and only inside a name)" % pat
            )
    return problems


def generic_rule_problems(box):
    problems = []
    for key in ("include", "exclude"):
        value = box.get(key) or []
        if not isinstance(value, list):
            problems.append("box.%s must be a list of patterns" % key)
            continue
        for pat in value:
            if not isinstance(pat, str) or not pat.strip():
                problems.append("box.%s holds an empty or non-text pattern" % key)
            elif ".." in pat.replace("\\", "/").split("/") or any(c in pat for c in "[]{}"):
                problems.append("%s pattern %r is not supported: no '..', brackets or braces" % (key, pat))
    return problems


class Rules:
    def __init__(self, include, exclude):
        self.include = [(p, glob_regex(p)) for p in include or []]
        self.exclude = [(p, glob_regex(p)) for p in exclude or []]

    def excluded(self, rel):
        """The exclude pattern that matches rel, or None."""
        for pat, rx in self.exclude:
            if rx.match(rel):
                return pat
        return None

    def included(self, rel):
        return any(rx.match(rel) for _, rx in self.include) and not self.excluded(rel)


def folder_files(src, rel, rules):
    """Files under the folder src to upload, and those skipped by the exclude rules or as env files."""
    keep, skipped = [], []
    for root, dirs, files in os.walk(src):
        dirs.sort()
        for f in sorted(files):
            sub = os.path.relpath(os.path.join(root, f), src).replace(os.sep, "/")
            if is_env_file(f) or rules.excluded(rel + "/" + sub):
                skipped.append(sub)
            else:
                keep.append(sub)
    return keep, skipped
