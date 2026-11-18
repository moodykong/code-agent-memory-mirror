"""Folder units: the pieces of a box that one machine owns and pushes.

A folder unit is one of:

  an archived folder   a folder matched by a box.archives entry ("ws-*", "decompile/project-*"),
                       named by its path relative to the project folder. A folder inside an
                       archived folder is never a unit of its own.
  an include folder    the literal folder an include pattern starts with ("results/**" -> results).
                       An include folder inside another one belongs to the outer one; one inside
                       (or equal to) an archived folder is covered by that archive.
  "."                  rclone only: include patterns that do not start with a folder ("*.pdf").
                       It holds the matching files that are not inside another unit's folder.

Matching is by name, so every machine that shares the project config sees the same units for the
same folders. Ownership (owners.py) is recorded per unit.
"""

import datetime
import fnmatch
import os

from agent_mirror.box import archive
from agent_mirror.box.rules import Rules, include_prefix

TOP = "."


def parts_of(rel):
    return [p for p in str(rel).replace("\\", "/").split("/") if p and p != "."]


def entry_parts(entry):
    return parts_of(str(entry).strip().strip("/"))


def matches_entry(parts, entry):
    ep = entry_parts(entry)
    return len(parts) == len(ep) and all(fnmatch.fnmatchcase(a, b) for a, b in zip(parts, ep))


def entries_matching(rel, globs):
    """The box.archives entries that match the folder rel exactly, in config order."""
    parts = parts_of(rel)
    return [g for g in globs if matches_entry(parts, g)]


def archive_unit_of(rel, globs):
    """The archived folder that holds rel (rel itself or the shortest matching parent), or None.

    Decided by name only, so it works on a machine that does not have the folder.
    """
    parts = parts_of(rel)
    if not parts or parts[0] in archive.RESERVED:
        return None
    for k in range(1, len(parts) + 1):
        if any(matches_entry(parts[:k], g) for g in globs):
            return "/".join(parts[:k])
    return None


def real_dir(entry):
    try:
        if entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
            return False
    except OSError:
        return False
    if os.name == "nt":
        from agent_mirror.links import is_junction

        return not is_junction(entry.path)
    return True


def folders_matching(project_path, entry):
    """Local folders (relative paths) that match one box.archives entry; links are never followed."""
    level = [""]
    for depth, pat in enumerate(entry_parts(entry)):
        found = []
        for rel in level:
            folder = os.path.join(project_path, *parts_of(rel)) if rel else project_path
            try:
                with os.scandir(folder) as it:
                    entries = sorted(it, key=lambda e: e.name)
            except OSError:
                continue
            for e in entries:
                if depth == 0 and e.name in archive.RESERVED:
                    continue
                if real_dir(e) and fnmatch.fnmatchcase(e.name, pat):
                    found.append(rel + "/" + e.name if rel else e.name)
        level = found
    return level


def archive_units(project_path, globs):
    """(archived folders here, sorted; warnings about overlapping entries)."""
    hits = {}
    for g in globs:
        for rel in folders_matching(project_path, g):
            hits.setdefault(rel, []).append(g)
    units, warnings = [], []
    for rel in sorted(hits):
        outer = archive_unit_of(rel, globs)
        if outer != rel:
            warnings.append(
                "%s matches the box.archives entry %r but is inside the archived folder %s, "
                "so it travels in that bundle, not as a unit of its own" % (rel, hits[rel][0], outer)
            )
            continue
        if len(hits[rel]) > 1:
            warnings.append(
                "%s matches %d box.archives entries (%s); a folder should match one entry, and the first (%r) wins"
                % (rel, len(hits[rel]), ", ".join(repr(g) for g in hits[rel]), hits[rel][0])
            )
        units.append(rel)
    return units, warnings


def folder_units(include, globs):
    """[{"name", "kind": "folder", "patterns", "roots"}] from the include patterns, sorted by name."""
    by_root = {}
    for pat in include or []:
        root = include_prefix(pat) or TOP
        if root != TOP and archive_unit_of(root, globs):
            continue  # inside an archived folder: the archive carries it
        by_root.setdefault(root, []).append(pat)
    merged = {}
    for root in sorted(by_root, key=lambda r: (r != TOP, r.count("/"), r)):
        outer = None
        if root != TOP:
            outer = next((m for m in merged if m != TOP and root.startswith(m + "/")), None)
        if outer:
            merged[outer]["patterns"] += by_root[root]
            merged[outer]["roots"].append(root)
        else:
            merged[root] = {"name": root, "kind": "folder", "patterns": list(by_root[root]), "roots": [root]}
    return [merged[n] for n in sorted(merged)]


def unit_of_file(rel, folders, globs, exclude):
    """The folder unit a loose box file belongs to (for the index and a box listing), or None."""
    if archive_unit_of(rel, globs):
        return None
    best = None
    top = None
    for u in folders:
        if u["name"] == TOP:
            top = u
        elif rel.startswith(u["name"] + "/") and (best is None or len(u["name"]) > len(best["name"])):
            best = u
    if best is not None:
        return best["name"] if Rules(best["patterns"], exclude).included(rel) else None
    if top is not None and Rules(top["patterns"], exclude).included(rel):
        return TOP
    return None


class Layout:
    """Every unit of one project checkout: archived folders found here plus the include folders."""

    def __init__(self, s):
        self.s = s
        self.project = s["project_path"]
        self.globs = list(s.get("archives") or [])
        self.archives, self.warnings = archive_units(self.project, self.globs) if os.path.isdir(self.project) else ([], [])
        self.folders = folder_units(s.get("include") or [], self.globs)
        self.units = [{"name": a, "kind": "archive"} for a in self.archives] + self.folders
        self.units.sort(key=lambda u: u["name"])
        self.by_name = dict((u["name"], u) for u in self.units)

    def managed(self):
        """Folders inside the project that agent-mirror itself manages (mount and receive folders)."""
        from agent_mirror.box.extras import mount_dir, receive_dir
        from agent_mirror.util import norm, within

        found = ["_sessions"]  # sessions fetched with box fetch --session
        root = os.path.realpath(self.project)
        for folder in (mount_dir(self.s), receive_dir(self.s)):
            real = os.path.realpath(folder)
            if within(real, root) and norm(real) != norm(root):
                found.append(os.path.relpath(real, root).replace(os.sep, "/"))
        return found

    def entries(self, unit, deadline=None):
        """(files the box carries for this unit [{path, size, mtime}], skip counts; "incomplete" on a timeout).

        Paths are relative to the unit's folder (to the project folder for ".").
        """
        name = unit["name"]
        if unit["kind"] == "archive":
            return archive.scan(self.project, name, self.s["rules"], deadline=deadline)
        rules = Rules(unit["patterns"], self.s.get("exclude") or [])
        if name == TOP:
            prune = set(u["name"] for u in self.units if u["name"] != TOP) | set(self.managed())
        else:
            prune = set(a for a in self.archives if a.startswith(name + "/"))
        if name != TOP and not os.path.isdir(os.path.join(self.project, *parts_of(name))):
            return [], {"env": 0, "excluded": 0, "links": 0}
        return archive.scan(self.project, name, rules, deadline=deadline, keep=rules.included, prune=prune)

    def from_box(self, unit, entries):
        """True when every file of the unit here was fetched from the box and is unchanged since."""
        from agent_mirror.box.local import FetchRecord

        if not entries:
            return False
        record = FetchRecord(self.project)
        if not record.files:
            return False
        lead = "" if unit["name"] == TOP else unit["name"] + "/"
        return all(lead + e["path"] in record.files and record.unchanged(lead + e["path"]) for e in entries)

    def compare(self, unit, summary, owner_fp):
        """'same', 'different', 'fetched' (here only as unchanged copies from the box) or 'unknown'."""
        how = compare(summary["fingerprint"], owner_fp)
        if how == "different" and self.from_box(unit, summary["entries"]):
            return "fetched"
        return how

    def summary(self, unit, deadline=None):
        """{"files", "bytes", "newest", "fingerprint" (None when the walk ran out of time), "lower_bound"}."""
        entries, skipped = self.entries(unit, deadline)
        incomplete = bool(skipped.get("incomplete"))
        newest = max([e["mtime"] for e in entries]) if entries else None
        return {
            "files": len(entries),
            "bytes": sum(e["size"] for e in entries),
            "newest": iso(newest),
            "fingerprint": None if incomplete else archive.fingerprint(entries),
            "lower_bound": incomplete,
            "entries": entries,
            "skipped": skipped,
        }


def iso(ts):
    if ts is None:
        return None
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def settings(box, project_path):
    """The parts of the box settings that units need, for callers without a full box context (plan)."""
    include = list(box.get("include") or [])
    exclude = list(box.get("exclude") or [])
    return {
        "project_path": project_path,
        "include": include,
        "exclude": exclude,
        "archives": [str(g).strip().strip("/") for g in box.get("archives") or [] if isinstance(g, str)],
        "rules": Rules(include, exclude),
        "box": box,
    }


def compare(local_fp, owner_fp):
    """'same', 'different' or 'unknown' for a local fingerprint against the owner's."""
    if not local_fp or not owner_fp:
        return "unknown"
    return "same" if local_fp == owner_fp else "different"
