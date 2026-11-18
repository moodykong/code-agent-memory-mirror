"""plan: compare this machine with the store, one unit at a time. Read-only.

Units: the instruction file per agent (instructions:claude, instructions:codex), each skill by name
(skill:NAME), each project's Claude memory folder (memory:PROJECT) and each project's bulk-file box
(box:PROJECT), plus skills-folder when an older version linked the whole shared skills folder.

For every unit the machine's side and the store's side give one case:

    machine          store                 case          default action
    has content      no record             first-setup   adopt (box: writer)
    empty            has content           join          link (box: reader)
    has content      has content, differs  reconcile     none: needs a choice
    has content      identical             identical     link, silently
    empty            no record             nothing       nothing (memory: link the empty folder)
    a correct link   any                   linked        nothing (drift is reported)

A unit is "waiting" when it is reconcile or first-setup and nothing decides it (a choices file,
--yes-defaults for first-setup, or a choice recorded for this machine). The machine is "fresh" when
no unit is waiting.
"""

import filecmp
import hashlib
import json
import os
import time

from agent_mirror import PLUGIN_ROOT, __version__, machine
from agent_mirror.config import config_agents, config_projects, load_user_config
from agent_mirror.install import active_copies, memory_link_path
from agent_mirror.links import is_junction, is_linklike, link_target, resolve_problem
from agent_mirror.runtime import detect_runtime, parse_agents
from agent_mirror.box.rules import include_prefix  # noqa: F401 (kept importable from here)
from agent_mirror.sizes import iso_utc
from agent_mirror.util import err, expand, norm, out, tilde, warn, within

SCHEMA = 1
CASE_ORDER = ("reconcile", "first-setup", "legacy-folder-link", "join", "identical", "nothing", "linked", "external")
NEEDS_CHOICE = ("reconcile", "first-setup")
KIND_ORDER = ("skills-folder", "instructions", "skill", "memory", "box")
LEFTOVER_NAMES = {".DS_Store", "Thumbs.db", "desktop.ini"}
LEFTOVER_SUFFIXES = ("~", ".swp", ".swo", ".swx", ".pyc", ".pyo")
SKIP_DIRS = {"__pycache__", ".git"}
VENDOR_SKILLS = "synced"
TEMPLATE_AGENTS = os.path.join(PLUGIN_ROOT, "templates", "store", "AGENTS.md")
# sha256 of the template AGENTS.md of earlier releases (LF line endings): a store still holding one of
# them has no instructions of its own yet
OLD_TEMPLATE_DIGESTS = {
    "dade76da6e37c86c10ae6a5fcb51e1c518296f99b3c91333d4b50edf87caeba6",  # 0.1.0
    "472f1f3d157fed70fd6da53ba5bcd15f6262c0bc2b7dc4c86ed089e6b3629331",  # 0.2.0 and 0.2.1
    "d9d6715bd6dff5b802070b9b359dd546cbf5868e58fe59c6db8c5acc900e9b43",  # 0.3.0 to 0.4.0
}
BOX_WALK_SECONDS = 10.0

CHOICES = {
    "skill": {
        "first-setup": ["adopt", "keep-local"],
        "join": ["link", "skip"],
        "reconcile": ["take-store", "keep-machine", "adopt"],
        "identical": ["link", "keep-machine"],
        "nothing": ["none"],
        "linked": ["none"],
        "external": ["none"],
    },
    "instructions": {
        "first-setup": ["adopt", "keep-local"],
        "join": ["link", "keep-local"],
        "reconcile": ["take-store", "keep-local", "adopt"],
        "identical": ["link", "keep-local"],
        "nothing": ["none"],
        "linked": ["none"],
    },
    "memory": {
        "first-setup": ["adopt", "skip"],
        "join": ["link", "skip"],
        "reconcile": ["merge", "link", "skip"],
        "identical": ["link", "skip"],
        "nothing": ["link", "skip"],
        "linked": ["none"],
    },
    "box": dict((c, list(machine.BOX_ROLES)) for c in ("first-setup", "join", "reconcile", "identical", "nothing")),
    "skills-folder": {"legacy-folder-link": ["convert"], "external": ["none"]},
}
DEFAULTS = {
    "skill": {
        "first-setup": "adopt",
        "join": "link",
        "identical": "link",
        "nothing": "none",
        "linked": "none",
        "external": "none",
    },
    "instructions": {"first-setup": "adopt", "join": "link", "identical": "link", "nothing": "none", "linked": "none"},
    "memory": {"first-setup": "adopt", "join": "link", "identical": "link", "nothing": "link", "linked": "none"},
    "box": {"first-setup": "writer", "join": "reader"},
    "skills-folder": {"legacy-folder-link": "convert", "external": "none"},
}


# content ------------------------------------------------------------------------------------


def leftover(name):
    """Editor and OS leftovers that never count as content."""
    return (
        name in LEFTOVER_NAMES
        or name.endswith(LEFTOVER_SUFFIXES)
        or name.startswith(".#")
        or (len(name) > 1 and name.startswith("#") and name.endswith("#"))
    )


TEXT_PROBE = 8192
TEXT_MAX = 16 << 20


def file_digest(path):
    """sha256 of a file's content. Text files are compared with CRLF read as LF: a skill checked out on
    Windows (CRLF) and the same skill in the store (LF, as git normalizes it) are the same content.
    A file counts as text when it has no NUL byte in its first 8 KiB and is at most 16 MiB."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        head = fh.read(TEXT_PROBE)
        if b"\0" not in head and os.path.getsize(path) <= TEXT_MAX:
            data = head + fh.read()
            h.update(data.replace(b"\r\n", b"\n"))
            return h.hexdigest()
        h.update(head)
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def content_files(root):
    """[(relative path, full path, 'F' or 'L')] under a folder: links are listed, never followed."""
    found = []
    for folder, dirs, files in os.walk(root):
        keep = []
        for d in sorted(dirs):
            full = os.path.join(folder, d)
            if d in SKIP_DIRS or leftover(d):
                continue
            if os.path.islink(full) or is_junction(full):
                found.append((os.path.relpath(full, root).replace(os.sep, "/"), full, "L"))
                continue
            keep.append(d)
        dirs[:] = keep
        for f in sorted(files):
            if leftover(f):
                continue
            full = os.path.join(folder, f)
            found.append((os.path.relpath(full, root).replace(os.sep, "/"), full, "L" if os.path.islink(full) else "F"))
    return sorted(found)


def summarize(path):
    """{"files", "bytes", "newest", "fingerprint", "empty"} of a file or folder (links inside are not followed)."""
    h = hashlib.sha256()
    if os.path.isfile(path):
        with open(path, "rb") as fh:
            data = fh.read()
        if b"\0" not in data[:TEXT_PROBE]:
            data_for_digest = data.replace(b"\r\n", b"\n")
        else:
            data_for_digest = data
        h.update(b"F\0" + data_for_digest)
        return {
            "files": 1,
            "bytes": len(data),
            "newest": iso_utc(os.path.getmtime(path)),
            "fingerprint": h.hexdigest(),
            "empty": not data.strip(),
        }
    files = size = 0
    newest = None
    for rel, full, kind in content_files(path):
        if kind == "L":
            h.update(("%s\0L\0%s\n" % (rel, os.readlink(full) if os.path.islink(full) else "")).encode("utf-8"))
            continue
        try:
            st = os.stat(full)
            digest = file_digest(full)
        except OSError:
            continue
        files += 1
        size += st.st_size
        newest = st.st_mtime if newest is None else max(newest, st.st_mtime)
        h.update(("%s\0F\0%s\n" % (rel, digest)).encode("utf-8", "surrogateescape"))
    return {"files": files, "bytes": size, "newest": iso_utc(newest), "fingerprint": h.hexdigest(), "empty": files == 0}


def git_repo_of(path):
    """The work tree root that holds path (a .git file or folder above it), or None."""
    p = os.path.abspath(path)
    if not os.path.isdir(p):
        p = os.path.dirname(p)
    while True:
        if os.path.lexists(os.path.join(p, ".git")):
            return p
        parent = os.path.dirname(p)
        if parent == p:
            return None
        p = parent


def short_summary(s):
    if not s:
        return {}
    return {"files": s.get("files", 0), "bytes": s.get("bytes", 0), "newest": s.get("newest")}


class Place:
    """One place on this machine where a unit lives (a skill can live in several skills folders)."""

    def __init__(self, label, path, expected):
        self.label = label
        self.path = path
        self.expected = expected
        self.state = "absent"  # absent, empty, content, linked, drift, external
        self.summary = None
        self.target = None
        self.drift = None  # broken, elsewhere
        self.note = None
        self.stale = False  # a managed copy (Windows without symlinks) older than the store's file

    def as_dict(self):
        d = {"where": self.label, "path": tilde(self.path), "state": self.state}
        if self.summary and self.state == "content":
            d.update(short_summary(self.summary))
        if self.target:
            d["target"] = tilde(self.target)
        if self.drift:
            d["drift"] = self.drift
        if self.note:
            d["note"] = self.note
        return d


# the plan ---------------------------------------------------------------------------------------


class Plan:
    def __init__(self, ctx, agents, projects, memory_dirs=None):
        self.ctx = ctx
        self.plat = ctx.plat
        self.store = ctx.require_store()
        self.store_root = os.path.realpath(self.store.root)
        self.agents = list(agents)
        self.projects = list(projects)
        self.memory_dirs = dict(memory_dirs or {})
        self.cfg = load_user_config(self.plat)
        self.recorded = machine.choices(self.cfg)
        self.mid, self.mname = machine.identity(self.cfg)
        self.runtime = detect_runtime()
        self.copies = active_copies(self.plat)
        self.units = []
        self.ignored = []
        self.places = {}  # unit id -> [Place]
        self.build()

    # places ---------------------------------------------------------------------------

    def in_store(self, path):
        return within(os.path.realpath(path), self.store_root)

    def probe(self, label, path, expected, kind):
        """A Place for path, the expected link target being `expected`."""
        p = Place(label, path, expected)
        if not os.path.lexists(path):
            return p
        if is_linklike(path):
            p.target = link_target(path)
            exists = os.path.exists(path)
            if (p.target and within(p.target, self.store_root)) or (exists and self.in_store(path)):
                if exists and norm(os.path.realpath(path)) == norm(os.path.realpath(expected)):
                    p.state = "linked"
                else:
                    p.state = "drift"
                    p.drift = "elsewhere" if exists else "broken"
                return p
            if not exists:
                p.state = "empty"
                p.note = "a link to %s, which does not exist" % tilde(p.target or "?")
                return p
            real = os.path.realpath(path)
            repo = git_repo_of(real)
            if kind == "skill" and repo and norm(repo) != norm(self.store_root):
                p.state = "external"
                p.note = "a link into the repo %s" % tilde(repo)
                return p
            p.summary = summarize(real)
            p.state = "empty" if p.summary["empty"] else "content"
            p.note = "followed the link to %s" % tilde(real)
            return p
        entry = self.copies.get(norm(os.path.abspath(path)))
        if entry is not None and os.path.isfile(path) and norm(entry.get("target", "")) == norm(expected):
            p.state = "linked"
            p.note = "managed copy (symlinks are not available here)"
            if os.path.isfile(expected) and not filecmp.cmp(path, expected, shallow=False):
                p.stale = True
                p.note += "; out of date, link refreshes it"
            return p
        p.summary = summarize(path)
        p.state = "empty" if p.summary["empty"] else "content"
        return p

    def store_side(self, path, kind):
        """{"state": "none" | "content" | "template", ...summary} for the store's copy."""
        if not os.path.lexists(path):
            return {"state": "none"}, None
        problem = resolve_problem(path) if is_linklike(path) else None
        if problem is not None:
            # a broken link or a link loop in the store: linking to it is refused, and the refusal says why
            words = "a link loop" if problem == "loop" else "a broken link"
            return {"state": "content", "problem": "the store's copy is %s" % words}, None
        s = summarize(path)
        if s["empty"]:
            return {"state": "none"}, s
        side = {"state": "content"}
        side.update(short_summary(s))
        if kind == "instructions" and os.path.isfile(path):
            with open(path, "rb") as fh:
                mine = fh.read().replace(b"\r\n", b"\n")
            known = set(OLD_TEMPLATE_DIGESTS)
            if os.path.isfile(TEMPLATE_AGENTS):
                with open(TEMPLATE_AGENTS, "rb") as fh:
                    known.add(hashlib.sha256(fh.read().replace(b"\r\n", b"\n")).hexdigest())
            if hashlib.sha256(mine).hexdigest() in known:
                side["state"] = "template"
        return side, s

    # units --------------------------------------------------------------------------------

    def add(self, uid, kind, name, case, machine_side, store_side, note=None, places=None, extra=None):
        choices = list(CHOICES[kind].get(case, ["none"]))
        default = DEFAULTS[kind].get(case)
        u = {
            "id": uid,
            "kind": kind,
            "name": name,
            "machine": machine_side,
            "store": store_side,
            "case": case,
            "default_action": default,
            "choices": choices,
            "recorded": self.recorded.get(uid),
            "decision": None,
            "decided_by": None,
            "note": note or "",
        }
        if extra:
            u.update(extra)
        rec = u["recorded"]
        if rec is not None:
            sticky = rec in machine.STICKY or (kind == "box" and rec in machine.BOX_ROLES)
            if sticky:
                if rec in ("keep-local", "keep-machine"):
                    rec = "keep-machine" if "keep-machine" in choices else "keep-local"
                if rec in choices:
                    u["decision"] = rec
                    u["decided_by"] = "recorded"
                else:
                    u["note"] = (u["note"] + "; " if u["note"] else "") + (
                        "the recorded choice %s does not fit this case any more" % u["recorded"]
                    )
        if u["decision"] is None and case not in NEEDS_CHOICE:
            u["decision"] = default
            u["decided_by"] = "default" if default else None
        u["waiting"] = case in NEEDS_CHOICE and u["decision"] is None
        self.units.append(u)
        if places is not None:
            self.places[uid] = places
        return u

    def build(self):
        self.skills_folder_units()
        self.instruction_units()
        self.skill_units()
        if "claude" in self.agents:
            for project in self.project_names():
                self.memory_unit(project)
        for project in self.project_names():
            self.box_unit(project)
        rank = dict((k, i) for i, k in enumerate(KIND_ORDER))
        self.units.sort(key=lambda u: (rank.get(u["kind"], 9), u["id"]))

    def project_names(self):
        names = []
        for n, _ in self.projects:
            if n not in names:
                names.append(n)
        return names

    def paths_of(self, project):
        return [p for n, p in self.projects if n == project]

    # skills ------------------------------------------------------------------------------

    def skill_folders(self):
        """The skills folders agent-mirror may write into, after legacy and external folder links."""
        usable = []
        for label, folder in self.plat.skill_folders(self.agents):
            uid = "skills-folder" if label == "shared" else "skills-folder:%s" % label
            if not is_linklike(folder):
                usable.append((label, folder))
                continue
            target = link_target(folder)
            side = {"state": "linked", "path": tilde(folder), "target": tilde(target or "?")}
            if (target and within(target, self.store_root)) or self.in_store(folder):
                self.add(
                    uid,
                    "skills-folder",
                    label,
                    "legacy-folder-link",
                    side,
                    {"state": "content", "path": "skills/"},
                    note=(
                        "the whole folder is one link into the store (older versions did this); convert makes it a "
                        "real folder with one link per store skill"
                    ),
                    extra={"path": folder},
                )
            else:
                self.add(
                    uid,
                    "skills-folder",
                    label,
                    "external",
                    side,
                    {"state": "none"},
                    note="the folder is a link to %s; agent-mirror never writes into it" % tilde(target or "?"),
                )
        return usable

    def skills_folder_units(self):
        self._folders = self.skill_folders()

    def skill_units(self):
        folders = self._folders
        names = set(n for n in self.store.skill_names() if n != VENDOR_SKILLS)
        for label, folder in folders:
            try:
                with os.scandir(folder) as it:
                    entries = sorted(it, key=lambda e: e.name)
            except OSError:
                continue
            for e in entries:
                reason = None
                if e.name == VENDOR_SKILLS:
                    reason = "vendor-synced folder"
                elif e.name.startswith("."):
                    reason = "dot folder (trash or hidden)"
                elif e.name == "__pycache__":
                    reason = "cache folder"
                elif not (e.is_symlink() or is_junction(e.path)) and not e.is_dir():
                    reason = "not a folder"
                elif (
                    e.is_dir()
                    and not (e.is_symlink() or is_junction(e.path))
                    and not os.path.isfile(os.path.join(e.path, "SKILL.md"))
                    and any(True for _ in os.scandir(e.path))
                ):
                    reason = "has files but no SKILL.md (not a skill)"
                if reason:
                    self.ignored.append({"path": tilde(e.path), "reason": reason})
                else:
                    names.add(e.name)
        for name in sorted(names):
            self.skill_unit(name, folders)

    def skill_unit(self, name, folders):
        uid = "skill:%s" % name
        expected = self.store.path("skills", name)
        places = [self.probe(label, os.path.join(folder, name), expected, "skill") for label, folder in folders]
        store_side, store_sum = self.store_side(expected, "skill")
        store_side["path"] = "skills/%s" % name
        self.combine(uid, "skill", name, places, store_side, store_sum)

    def combine(self, uid, kind, name, places, store_side, store_sum, extra=None):
        """Turn the places and the store side into one unit with its case."""
        content = [p for p in places if p.state == "content"]
        external = [p for p in places if p.state == "external"]
        linked = [p for p in places if p.state == "linked"]
        drift = [p for p in places if p.state == "drift"]
        present = [p for p in places if p.state != "absent"]
        has_store = store_side["state"] == "content"
        notes = []
        if external:
            case = "external"
            notes.append("%s; left alone" % external[0].note)
            if has_store:
                notes.append("the store also has a %s of this name; it is not linked here" % kind)
        elif content:
            fps = set(p.summary["fingerprint"] for p in content)
            if len(fps) > 1:
                notes.append("%d different copies on this machine" % len(fps))
            if store_side["state"] == "template":
                case = "first-setup"
                notes.append("the store holds only the template AGENTS.md")
            elif not has_store:
                case = "first-setup"
            elif store_sum and fps == {store_sum["fingerprint"]}:
                case = "identical"
            else:
                case = "reconcile"
            if case in ("first-setup", "reconcile") and len(content) > 1:
                pick = self.newest(content)
                notes.append("adopt takes the newest copy (%s)" % tilde(pick.path))
        else:
            missing = [p for p in places if p.state in ("absent", "empty")]
            if not missing:
                case = "linked"  # every place is a link into the store (drift is reported below)
            elif has_store or store_side["state"] == "template":
                case = "join"
                if linked or drift:
                    notes.append("linked in %d of %d places" % (len(linked), len(places)))
            else:
                case = "linked" if (linked or drift) else "nothing"
        for p in drift:
            notes.append(
                "drift: %s %s"
                % (
                    tilde(p.path),
                    "is a broken link into the store" if p.drift == "broken" else "points elsewhere in the store",
                )
            )
        for p in places:
            if p.note and p.state in ("content", "empty") and "followed" in p.note:
                notes.append("%s: %s" % (tilde(p.path), p.note))
        if content:
            pick = self.newest(content)
            machine_side = {"state": "content", "copies": len(content)}
            machine_side.update(short_summary(pick.summary))
        elif external:
            machine_side = {"state": "external"}
        elif linked and len(linked) == len(present):
            machine_side = {"state": "linked"}
        elif drift:
            machine_side = {"state": "drift"}
        else:
            machine_side = {"state": "empty"}
        machine_side["places"] = [p.as_dict() for p in places]
        if store_side.get("problem"):
            notes.append(store_side["problem"])
        stale = [p for p in places if getattr(p, "stale", False)]
        for p in stale:
            notes.append("%s: %s" % (tilde(p.path), p.note))
        u = self.add(uid, kind, name, case, machine_side, store_side, note="; ".join(notes), places=places, extra=extra)
        if case == "linked" and (drift or stale) and has_store:
            u["choices"] = ["none", "link"]
            if stale and not drift:
                u["default_action"] = "link"
                if u["decided_by"] != "recorded":
                    u["decision"], u["decided_by"] = "link", "default"
        return u

    @staticmethod
    def newest(places):
        return max(places, key=lambda p: ((p.summary or {}).get("newest") or "", p.label == "shared"))

    # instructions -------------------------------------------------------------------------

    def instruction_units(self):
        p = self.plat
        target = self.store.path("AGENTS.md")
        store_side, store_sum = self.store_side(target, "instructions")
        store_side["path"] = "AGENTS.md"
        files = []
        if "claude" in self.agents:
            files.append(("claude", p.join(p.claude_home(), "CLAUDE.md")))
        if "codex" in self.agents:
            files.append(("codex", p.join(p.codex_home(), "AGENTS.md")))
        for agent, path in files:
            place = self.probe(agent, path, target, "instructions")
            self.combine("instructions:%s" % agent, "instructions", agent, [place], dict(store_side), store_sum)

    # memory --------------------------------------------------------------------------------

    def memory_unit(self, project):
        expected = self.store.memory_store(project)
        places, seen = [], set()
        for path in self.paths_of(project):
            link, _ = memory_link_path(self.ctx, path, self.memory_dirs.get((project, path)))
            if norm(link) in seen:
                continue
            seen.add(norm(link))
            places.append(self.probe(tilde(path), link, expected, "memory"))
        store_side, store_sum = self.store_side(expected, "memory")
        store_side["path"] = "memory/%s" % os.path.basename(expected)
        self.combine("memory:%s" % project, "memory", project, places, store_side, store_sum)

    # box ------------------------------------------------------------------------------------

    def box_unit(self, project):
        env = self.store.load_env(project)
        box = env.get("box") or {}
        backend = str(box.get("backend") or "none").strip().lower()
        if backend == "none":
            return
        from agent_mirror.box import units as bunits
        from agent_mirror.box.owners import legacy_entry

        index = read_box_index(self.store.path("projects", project, "box_index.jsonl"), bunits.settings(box, None))
        writer = (index or {}).get("writer")
        in_index = (index or {}).get("folders") or {}
        owners = (index or {}).get("owners")
        if owners is None and writer:
            owners = dict((n, legacy_entry(writer, d.get("fingerprint"))) for n, d in in_index.items())
        owners = owners or {}

        def walk_first(unit):
            # folders owned by another machine need a full walk for the comparison: walk them first
            o = owners.get(unit["name"])
            return (0 if o and o.get("machine_id") != self.mid else 1, unit["name"])

        deadline = time.monotonic() + BOX_WALK_SECONDS
        totals = {"files": 0, "bytes": 0, "newest": None, "lower_bound": False}
        here = {}  # folder unit -> its summary on this machine (the first checkout that has it)
        for path in self.paths_of(project):
            layout = bunits.Layout(bunits.settings(box, path))
            for unit in sorted(layout.units, key=walk_first):
                m = layout.summary(unit, deadline)
                folder = os.path.join(path, *bunits.parts_of(unit["name"]))
                present = m["files"] > 0 or (m["lower_bound"] and unit["name"] != bunits.TOP and os.path.isdir(folder))
                if present and unit["name"] not in here:
                    here[unit["name"]] = m
                    m["_compare"] = (layout, unit)
                totals["files"] += m["files"]
                totals["bytes"] += m["bytes"]
                totals["lower_bound"] = totals["lower_bound"] or m["lower_bound"]
                if m["newest"] and (totals["newest"] is None or m["newest"] > totals["newest"]):
                    totals["newest"] = m["newest"]
        machine_side = {"state": "content" if here else "empty", "folders": sorted(here)}
        machine_side.update(totals)
        if index is None:
            store_side = {"state": "none", "path": "projects/%s/box_index.jsonl" % project}
        else:
            store_side = {
                "state": "content" if index["files"] else "none",
                "path": "projects/%s/box_index.jsonl" % project,
                "files": index["files"],
                "bytes": index["bytes"],
                "newest": index["newest"],
                "writer": index["writer"],
                "owners": index["owners"],
            }
        groups = {
            "owned_here": [],
            "conflicts": [],
            "same": [],
            "fetched": [],
            "not_compared": [],
            "elsewhere": [],
            "unclaimed": [],
        }
        for name in sorted(set(here) | set(owners)):
            owner = owners.get(name)
            if owner is None:
                groups["unclaimed"].append(name)
                continue
            if self.mid and owner.get("machine_id") == self.mid:
                groups["owned_here"].append(name)
                continue
            if name not in here:
                groups["elsewhere"].append(name)
                continue
            m = here[name]
            layout, unit = m["_compare"]
            how = layout.compare(unit, m, owner.get("fingerprint"))
            if how == "unknown" and m["fingerprint"] and not owner.get("fingerprint") and name in in_index:
                seen = in_index[name]
                if (seen.get("files"), seen.get("bytes")) != (m["files"], m["bytes"]):
                    # no fingerprint recorded, but the index shows other counts or sizes
                    how = "fetched" if layout.from_box(unit, m["entries"]) else "different"
            if how == "different":
                groups["conflicts"].append(name)
            elif how in ("same", "fetched"):
                groups[how].append(name)
            else:
                groups["not_compared"].append(name)

        def names(items, limit=8):
            shown = ", ".join(items[:limit])
            return shown + (" and %d more" % (len(items) - limit) if len(items) > limit else "")

        def who(name):
            o = owners.get(name) or {}
            return o.get("machine_name") or str(o.get("machine_id") or "?")[:12]

        notes = []
        if writer and index and index.get("owners") is None:
            notes.append(
                "the current writer is %s (machine id %s, since %s; old single-writer marker: it owns every "
                "folder in the box until a push writes _owners.json)"
                % (writer.get("machine_name"), str(writer.get("machine_id"))[:12], writer.get("date"))
                + (" (this machine)" if self.mid and writer.get("machine_id") == self.mid else "")
            )
        elif store_side["state"] == "content" and not owners:
            notes.append("the index names no owners (run `agent-mirror box index` after a push)")
        if groups["owned_here"]:
            notes.append("owned by this machine: %s" % names(groups["owned_here"]))
        if groups["conflicts"]:
            notes.append(
                "conflicts, owned elsewhere and different here: %s"
                % ", ".join("%s (owner %s)" % (n, who(n)) for n in groups["conflicts"])
            )
        if groups["same"]:
            notes.append("owned elsewhere, the same here: %s" % names(groups["same"]))
        if groups["fetched"]:
            notes.append("owned elsewhere, here only as fetched from the box: %s" % names(groups["fetched"]))
        if groups["not_compared"]:
            notes.append("owned elsewhere, here but not compared (no fingerprint or the walk ran out of time): %s" % names(groups["not_compared"]))
        if groups["elsewhere"]:
            notes.append("owned elsewhere, not here: %s" % names(groups["elsewhere"]))
        if groups["unclaimed"]:
            notes.append("not in the box or owned by no machine yet (the next push from here claims them): %s" % names(groups["unclaimed"]))
        if totals["lower_bound"]:
            notes.append("local sizes are a lower bound (the walk stopped early)")
        if machine_side["state"] == "content" and store_side["state"] == "content":
            case = "reconcile" if groups["conflicts"] else "identical"
        elif machine_side["state"] == "content":
            case = "first-setup"
        elif store_side["state"] == "content":
            case = "join"
        else:
            case = "nothing"
        u = self.add(
            "box:%s" % project,
            "box",
            project,
            case,
            machine_side,
            store_side,
            note="; ".join(notes),
            extra={"box_folders": groups},
        )
        if case == "identical":
            u["default_action"] = "writer" if (groups["owned_here"] or groups["unclaimed"]) else "reader"
            if u["decided_by"] != "recorded":
                u["decision"] = u["default_action"]
                u["decided_by"] = "default"
        elif case == "nothing" and u["decided_by"] == "default":
            u["decision"] = None
            u["decided_by"] = None

    # output -------------------------------------------------------------------------------------

    def waiting(self):
        return [u for u in self.units if u["waiting"]]

    def fresh(self):
        return not self.waiting()

    def data(self):
        return {
            "schema": SCHEMA,
            "tool": "agent-mirror %s" % __version__,
            "machine": {"id": self.mid, "name": self.mname},
            "runtime": self.runtime,
            "store": self.store.root,
            "agents": self.agents,
            "projects": [{"project": n, "path": p} for n, p in self.projects],
            "units": [dict((k, v) for k, v in u.items() if not k.startswith("_")) for u in self.units],
            "ignored": self.ignored,
            "waiting": [u["id"] for u in self.waiting()],
            "fresh": self.fresh(),
        }

    def unit(self, uid):
        for u in self.units:
            if u["id"] == uid:
                return u
        return None


def read_box_index(path, s=None):
    """{"files", "bytes", "newest", "writer", "owners", "folders"} from a committed box index, or None.

    owners is the _owners.json folders map (None when the index has no such line); folders gives, per
    folder unit in the index, {"files", "bytes", "fingerprint"} (archives always; include folders when
    the box settings s are given).
    """
    if not os.path.isfile(path):
        return None
    from agent_mirror.box import units as bunits
    from agent_mirror.box.owners import clean_folders

    found = {"files": 0, "bytes": 0, "newest": None, "writer": None, "owners": None, "folders": {}}
    globs = (s or {}).get("archives") or []
    layout_folders = bunits.folder_units((s or {}).get("include") or [], globs) if s else []
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(r, dict):
                    continue
                if isinstance(r.get("writer"), dict):
                    found["writer"] = r["writer"]
                    continue
                if isinstance(r.get("owners"), dict):
                    found["owners"] = clean_folders({"folders": r["owners"]})
                    continue
                if "sessions" in r or not r.get("path"):
                    continue  # the session summary, or anything that is not an entry
                if r.get("archive"):
                    files, size = int(r.get("files") or 0), int(r.get("bytes") or 0)
                    if r.get("path"):
                        found["folders"][r["path"]] = {"files": files, "bytes": size, "fingerprint": r.get("fingerprint")}
                else:
                    files, size = 1, int(r.get("size") or 0)
                    unit = bunits.unit_of_file(str(r.get("path") or ""), layout_folders, globs, s["exclude"]) if s else None
                    if unit:
                        d = found["folders"].setdefault(unit, {"files": 0, "bytes": 0, "fingerprint": None})
                        d["files"] += 1
                        d["bytes"] += size
                found["files"] += files
                found["bytes"] += size
                m = r.get("modtime")
                if isinstance(m, str) and m[:1].isdigit() and (found["newest"] is None or m > found["newest"]):
                    found["newest"] = m
    except OSError:
        return None
    return found


# the projects a plan considers ------------------------------------------------------------------


def project_pairs(ctx, names, paths):
    """[(project, path)] from repeated --project and --project-path flags; exits on a mismatch."""
    from agent_mirror.config import resolve_project

    names, paths = list(names or []), list(paths or [])
    if names and len(names) != len(paths):
        err("give each --project a --project-path (the folder where it is checked out), in the same order")
        raise SystemExit(2)
    pairs = []
    if names:
        for n, p in zip(names, paths):
            pairs.append((n, os.path.realpath(expand(p))))
        return pairs
    for p in paths:
        name, path = resolve_project(ctx, None, p)
        if not name:
            err("could not tell which project %s is; pass --project" % p)
            raise SystemExit(2)
        pairs.append((name, os.path.realpath(path)))
    return pairs


def gather_projects(ctx, pairs=(), discover=True, quiet=False):
    """Projects to consider: the config's, the ones given, and (discover) the one found from the current folder."""
    found, seen = [], set()

    def add(name, path):
        real = os.path.realpath(expand(path))
        key = (name, norm(real))
        if key not in seen:
            seen.add(key)
            found.append((name, real))

    for name, paths in config_projects(ctx.config):
        for p in paths:
            real = os.path.realpath(expand(p))
            if os.path.isdir(real):
                add(name, real)
            elif not quiet:
                warn("configured project %s: %s does not exist; skipped" % (name, p))
    for name, path in pairs:
        add(name, path)
    if discover:
        from agent_mirror.projects import discover_projects

        for name, path in discover_projects(ctx).matched:
            add(name, path)
    return found


def agents_from(args, ctx):
    return parse_agents(args.agents) if getattr(args, "agents", None) else config_agents(ctx.config)


def build_plan(args, ctx, discover=True):
    agents = agents_from(args, ctx)
    pairs = project_pairs(ctx, getattr(args, "project", None), getattr(args, "project_path", None))
    projects = gather_projects(ctx, pairs, discover=discover, quiet=getattr(args, "json", False))
    return Plan(ctx, agents, projects)


# human output --------------------------------------------------------------------------------------


def size_text(side):
    if side.get("state") not in ("content", "template"):
        return side.get("state", "?")
    from agent_mirror.box.local import human

    text = "%d file%s, %s" % (side.get("files", 0), "" if side.get("files") == 1 else "s", human(side.get("bytes")))
    if side.get("newest"):
        text += ", newest %s" % side["newest"].replace("T", " ")[:16]
    if side.get("copies", 1) > 1:
        text += " (%d copies)" % side["copies"]
    if side.get("state") == "template":
        text += " (the template)"
    return text


def print_unit(u, verbose=True):
    out("  %s" % u["id"])
    out("      machine: %s | store: %s" % (size_text(u["machine"]), size_text(u["store"])))
    if verbose:
        for place in u["machine"].get("places", []):
            out("      at %s: %s" % (place["path"], place["state"]))
    if u.get("decided_by") == "recorded":
        out("      decided: %s (recorded for this machine)" % u["decision"])
    elif u["default_action"]:
        out("      default: %s; choices: %s" % (u["default_action"], ", ".join(u["choices"])))
    else:
        out("      choices: %s" % ", ".join(u["choices"]))
    if u["note"]:
        out("      note: %s" % u["note"])


def print_plan(plan):
    data = plan.data()
    out(
        "agent-mirror plan: machine %s, store %s, agents %s, runtime %s"
        % (
            "%s (%s)" % (plan.mname, plan.mid[:12]) if plan.mid else "(no machine id yet)",
            plan.store.root,
            ",".join(plan.agents),
            plan.runtime,
        )
    )
    if plan.projects:
        out("projects: %s" % ", ".join("%s (%s)" % (n, tilde(p)) for n, p in plan.projects))
    else:
        out("projects: none (add them with --project NAME --project-path PATH)")
    by_case = {}
    for u in plan.units:
        by_case.setdefault(u["case"], []).append(u)
    titles = {
        "reconcile": "reconcile: this machine and the store differ; needs your choice",
        "first-setup": "first-setup: only this machine has it; adopt copies it into the store, then links it",
        "legacy-folder-link": "legacy-folder-link: an older version linked a whole folder; convert by default",
    }
    for case in ("reconcile", "first-setup", "legacy-folder-link"):
        units = by_case.get(case) or []
        if units:
            out("")
            out("%s (%d)" % (titles[case], len(units)))
            for u in units:
                print_unit(u)
    drift = [u for u in by_case.get("linked", []) if "drift" in u["note"]]
    if drift:
        out("")
        out("drift: linked units whose link is broken or points elsewhere (%d)" % len(drift))
        for u in drift:
            print_unit(u, verbose=False)
    rest = [(c, len(by_case.get(c) or [])) for c in CASE_ORDER[3:] if by_case.get(c)]
    if rest:
        out("")
        out("the rest: %s" % ", ".join("%d %s" % (n, c) for c, n in rest))
        for case in ("join", "identical", "nothing", "external"):
            units = by_case.get(case) or []
            if units:
                out("  %s: %s" % (case, ", ".join(u["id"] for u in units)))
    decided = [u for u in plan.units if u.get("decided_by") == "recorded"]
    if decided:
        out("decided earlier on this machine: %s" % ", ".join("%s=%s" % (u["id"], u["decision"]) for u in decided))
    if data["ignored"]:
        out("not skills (left alone): %s" % ", ".join("%s (%s)" % (i["path"], i["reason"]) for i in data["ignored"]))
    out("")
    waiting = plan.waiting()
    if not waiting:
        out(
            "fresh: yes. Nothing waits for a choice; `agent-mirror install` or `agent-mirror apply` applies the defaults."
        )
    else:
        out(
            'fresh: no. %d unit(s) wait for a choice: %s. Write a choices file ({"unit id": "action"}) and run '
            "`agent-mirror apply --choices FILE`." % (len(waiting), ", ".join(u["id"] for u in waiting))
        )


def cmd_plan(args, ctx):
    plan = build_plan(args, ctx)
    if args.json:
        out(json.dumps(plan.data(), indent=2))
    else:
        print_plan(plan)
    return 0
