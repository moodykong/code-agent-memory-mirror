"""install and uninstall: links from the agents' home folders into the store.

Links only ever point from the home folder INTO the store. Anything a link
replaces is moved to ~/.agent-mirror-backups/<timestamp>/ and recorded in a
manifest so `uninstall` can put it back.
"""

import filecmp
import glob
import json
import os
import shutil

from agent_mirror.config import config_agents, config_projects, remember_project, resolve_project
from agent_mirror.links import (
    describe,
    is_linklike,
    link_location,
    link_target,
    make_link,
    remove_link,
    resolve_problem,
)
from agent_mirror.platforms import claude_slug, find_claude_project_dir
from agent_mirror.runtime import parse_agents
from agent_mirror.util import err, expand, iso_now, norm, now_stamp, out, tilde, warn, within

MANIFEST = "manifest.json"


# manifests ------------------------------------------------------------------------


def load_manifests(plat):
    found = []
    pattern = os.path.join(plat.backup_root(), "*", MANIFEST)
    for path in sorted(glob.glob(pattern), reverse=True):
        try:
            with open(path, encoding="utf-8") as fh:
                found.append((path, json.load(fh)))
        except (OSError, ValueError) as exc:
            warn("cannot read manifest %s: %s" % (tilde(path), exc))
    return found


def active_copies(plat, manifests=None):
    """Managed copies made by install and not yet uninstalled: {normalised path: entry}."""
    copies = {}
    for _, data in manifests if manifests is not None else load_manifests(plat):
        if data.get("restored_at"):
            continue
        for e in data.get("entries", []):
            if e.get("method") == "copy" and e.get("path"):
                copies.setdefault(norm(e["path"]), e)
    return copies


def known_stores(ctx, manifests):
    stores = []
    store = ctx.store_or_none(quiet=True)
    if store:
        stores.append(store.root)
    for _, data in manifests:
        s = data.get("store")
        if s and s not in stores:
            stores.append(s)
    return stores


# the Claude memory folder for a project ------------------------------------------------


def memory_link_path(ctx, project_path, override=None):
    """(path of Claude's memory folder for this project, a note or None)."""
    if override:
        return expand(override), "memory folder given with --memory-dir"
    claude_home = ctx.plat.claude_home()
    paths = [project_path, os.path.abspath(project_path)]
    found = find_claude_project_dir(claude_home, paths, windows=ctx.plat.windows)
    if found:
        return os.path.join(found, "memory"), None
    slug = claude_slug(project_path)
    note = "Claude has no project folder for %s yet; using %s (computed from the path)" % (project_path, slug)
    return os.path.join(claude_home, "projects", slug, "memory"), note


def merge_tree(src, dst, dry_run):
    """Copy files from src that dst lacks. Never overwrite. Returns (copied, conflicts, same)."""
    copied, conflicts, same = [], [], []
    for root, dirs, files in os.walk(src):
        dirs[:] = sorted(d for d in dirs if not is_linklike(os.path.join(root, d)))
        for f in sorted(files):
            s = os.path.join(root, f)
            rel = os.path.relpath(s, src)
            d = os.path.join(dst, rel)
            if os.path.islink(s):
                warn("skipped link %s (memory files must be real files)" % tilde(s))
                continue
            if not os.path.lexists(d):
                copied.append(rel)
                if not dry_run:
                    os.makedirs(os.path.dirname(d), exist_ok=True)
                    shutil.copy2(s, d)
            elif os.path.isfile(d) and filecmp.cmp(s, d, shallow=False):
                same.append(rel)
            else:
                conflicts.append(rel)
    return copied, conflicts, same


# install ------------------------------------------------------------------------------


class Installer:
    def __init__(self, ctx, dry_run, memory_dir=None, symlink=None, junction=None, fallback=None):
        self.ctx = ctx
        self.plat = ctx.plat
        self.store = ctx.require_store()
        self.dry_run = dry_run
        self.memory_dir = memory_dir
        self.stamp = now_stamp()
        self.backup_root = os.path.join(self.plat.backup_root(), self.stamp)
        self.entries = []
        self.merges = []
        self.refreshed = []
        self.errors = []
        self.already = 0
        self.symlink = symlink
        self.junction = junction
        self.fallback = self.plat.windows if fallback is None else fallback
        self.copies = active_copies(self.plat)

    def verb(self, done, would):
        return ("would " + would) if self.dry_run else done

    def planned_links(self, agents):
        """List of (link, target, allow_missing_target, is_dir) for the given agents."""
        s = self.store
        p = self.plat
        items = []
        if "claude" in agents:
            items.append((p.join(p.claude_home(), "CLAUDE.md"), s.path("AGENTS.md"), False, False))
        if "codex" in agents:
            items.append((p.join(p.codex_home(), "AGENTS.md"), s.path("AGENTS.md"), False, False))
        if agents:
            items.append((p.agents_skills(), s.path("skills"), False, True))
        if "claude" in agents:
            for name in s.skill_names():
                items.append((p.join(p.claude_home(), "skills", name), s.path("skills", name), False, True))
        return items

    # safety -------------------------------------------------------------------

    def allowed_roots(self):
        p = self.plat
        return [os.path.realpath(r) for r in (p.home(), p.claude_home(), p.codex_home())]

    def check_link(self, link, target, allow_missing=False):
        """Return a reason to refuse creating link -> target, or None if it is safe."""
        store = self.store.root
        loc = link_location(link)
        if within(loc, store):
            return "link location %s resolves inside the store (this would make a link loop)" % loc
        if within(store, loc):
            return "the store itself is inside %s; replacing it would move the store" % loc
        if not any(within(loc, r) for r in self.allowed_roots()):
            return "link location %s is outside the home folder" % loc
        if not within(target, store):
            return "target %s is outside the store %s" % (target, store)
        problem = resolve_problem(target)
        if problem == "loop":
            return "target %s is a link loop" % target
        if problem == "missing" and not allow_missing:
            return "target %s does not exist" % target
        if problem not in (None, "missing"):
            return "target %s cannot be resolved: %s" % (target, problem)
        real_target = os.path.realpath(target)
        if not within(real_target, store):
            return "target %s resolves to %s, outside the store" % (target, real_target)
        return None

    def is_ours(self, link, target):
        if is_linklike(link):
            lt = link_target(link)
            return (
                lt is not None
                and within(lt, self.store.root)
                and norm(os.path.realpath(lt)) == norm(os.path.realpath(target))
            )
        entry = self.copies.get(norm(os.path.abspath(link)))
        return entry is not None and norm(entry.get("target", "")) == norm(target) and os.path.isfile(link)

    # actions ------------------------------------------------------------------

    def backup(self, path):
        rel = os.path.relpath(os.path.abspath(path), self.plat.home())
        if rel.startswith(".."):
            rel = os.path.join("_outside_home", os.path.abspath(path).lstrip(os.sep).replace(":", ""))
        dest = os.path.join(self.backup_root, rel)
        if not self.dry_run:
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            shutil.move(path, dest)
        return dest

    def refresh_copy(self, link, target):
        if self.dry_run:
            out("would refresh the managed copy %s from %s" % (tilde(link), target))
            return
        old = self.backup(link)
        shutil.copy2(target, link)
        self.refreshed.append({"path": os.path.abspath(link), "target": target, "old_copy": old})
        out("refreshed the managed copy %s from %s (old copy kept at %s)" % (tilde(link), target, tilde(old)))

    def link(self, link, target, allow_missing=False, is_dir=False):
        reason = self.check_link(link, target, allow_missing=allow_missing)
        if reason:
            self.errors.append(reason)
            err("refused %s: %s" % (tilde(link), reason))
            return False
        if self.is_ours(link, target):
            if not is_linklike(link) and not filecmp.cmp(link, target, shallow=False):
                self.refresh_copy(link, target)
            else:
                self.already += 1
            return True
        entry = {"path": os.path.abspath(link), "target": target, "before": "absent", "backup": None}
        if os.path.lexists(link):
            entry["before"] = describe(link)
            if is_linklike(link):
                entry["before_link"] = link_target(link)
            dest = self.backup(link)
            entry["backup"] = dest
            out("%s %s (%s) to %s" % (self.verb("backed up", "back up"), tilde(link), entry["before"], tilde(dest)))
        method = "symlink"
        if not self.dry_run:
            os.makedirs(os.path.dirname(link), exist_ok=True)
            try:
                method = make_link(link, target, is_dir, self.fallback, self.symlink, self.junction)
            except OSError as exc:
                if entry["backup"]:
                    shutil.move(entry["backup"], link)
                reason = "could not link %s -> %s: %s" % (link, target, exc)
                self.errors.append(reason)
                err(reason)
                return False
            if method == "symlink" and resolve_problem(link) == "loop":
                remove_link(link)
                if entry["backup"]:
                    shutil.move(entry["backup"], link)
                reason = "link %s -> %s made a loop; undone" % (link, target)
                self.errors.append(reason)
                err(reason)
                return False
        entry["method"] = method
        if method == "copy":
            out(
                "copied %s to %s (managed copy: symlinks are not available here; "
                "edit the store copy and re-run install)" % (target, tilde(link))
            )
        else:
            suffix = " (junction)" if method == "junction" else ""
            out("%s %s -> %s%s" % (self.verb("linked", "link"), tilde(link), target, suffix))
        self.entries.append(entry)
        return True

    def memory(self, project, project_path):
        link, note = memory_link_path(self.ctx, project_path, self.memory_dir)
        if note:
            out(note)
        store = self.store.memory_store(project)
        reason = self.check_link(link, store, allow_missing=True)
        if reason:
            self.errors.append(reason)
            err("refused %s: %s" % (tilde(link), reason))
            return
        if not os.path.isdir(store):
            out("%s memory store %s" % (self.verb("created", "create"), store))
            if not self.dry_run:
                os.makedirs(store, exist_ok=True)
        if os.path.isdir(link) and not is_linklike(link):
            copied, conflicts, same = merge_tree(link, store, self.dry_run)
            out(
                "memory merge from %s: %d files %s the store, %d already identical, %d conflicts"
                % (
                    tilde(link),
                    len(copied),
                    "to copy into" if self.dry_run else "copied into",
                    len(same),
                    len(conflicts),
                )
            )
            for rel in copied:
                out("  %s %s" % (self.verb("copied", "copy"), rel))
            for rel in conflicts:
                warn(
                    "memory conflict: %s differs between %s and the store; the store copy is kept, "
                    "the local copy stays in the backup" % (rel, tilde(link))
                )
            self.merges.append({"from": link, "store": store, "copied": copied, "conflicts": conflicts})
        self.link(link, store, allow_missing=True, is_dir=True)

    def install(self, agents, projects=()):
        for link, target, allow_missing, is_dir in self.planned_links(agents):
            self.link(link, target, allow_missing, is_dir)
        for project, project_path in projects:
            if "claude" in agents:
                self.memory(project, project_path)
            else:
                out(
                    "project %s: no memory link needed for %s (it reads the store directly)"
                    % (project, ",".join(agents))
                )
        return self.finish(agents, projects)

    def finish(self, agents, projects):
        manifest = None
        if (self.entries or self.merges or self.refreshed) and not self.dry_run:
            os.makedirs(self.backup_root, exist_ok=True)
            manifest = os.path.join(self.backup_root, MANIFEST)
            data = {
                "version": 1,
                "tool": "agent-mirror",
                "created": iso_now(),
                "store": self.store.root,
                "home": self.plat.home(),
                "agents": list(agents),
                "projects": [{"project": n, "path": p} for n, p in projects],
                "entries": self.entries,
                "merges": self.merges,
                "refreshed": self.refreshed,
                "restored_at": None,
            }
            with open(manifest, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2)
                fh.write("\n")
        changed = len(self.entries) + len(self.refreshed)
        if not changed and not self.errors:
            out("install: nothing to change (%d links already correct)" % self.already)
        else:
            out(
                "install: %d %s, %d already correct, %d refused%s"
                % (
                    changed,
                    "to change" if self.dry_run else "changed",
                    self.already,
                    len(self.errors),
                    ("; manifest " + tilde(manifest)) if manifest else "",
                )
            )
        return 1 if self.errors else 0


def link_state(installer, link, target):
    """(label, note) for doctor."""
    if installer.is_ours(link, target):
        if not is_linklike(link):
            same = filecmp.cmp(link, target, shallow=False) if os.path.isfile(target) else False
            return "COPY", "copy, re-run install after editing" + ("" if same else "; out of date")
        lt = link_target(link)
        kind = "junction" if not os.path.islink(link) else "symlink"
        return "OK", "%s -> %s" % (kind, lt)
    if is_linklike(link):
        return "ELSEWHERE", describe(link) + "; install backs it up and relinks it"
    if os.path.lexists(link):
        return "NOT LINKED", describe(link) + "; install backs it up and links it"
    return "MISSING", "install creates it"


def cmd_install(args, ctx):
    agents = parse_agents(args.agents) if args.agents else config_agents(ctx.config)
    ctx.require_store()
    projects = []
    explicit = bool(args.project or args.project_path)
    if explicit:
        if args.project and not args.project_path:
            err("--project needs --project-path (the folder where the project is checked out)")
            return 2
        name, path = resolve_project(ctx, args.project, args.project_path)
        if not name:
            err("could not tell which project %s is; pass --project" % path)
            return 2
        projects.append((name, path))
    else:
        if args.memory_dir:
            err("--memory-dir needs --project and --project-path")
            return 2
        for name, paths in config_projects(ctx.config):
            for p in paths:
                real = os.path.realpath(expand(p))
                if os.path.isdir(real):
                    projects.append((name, real))
                else:
                    warn("configured project %s: %s does not exist; skipped" % (name, p))
    if args.dry_run:
        out("dry run: nothing will change")
    inst = Installer(ctx, args.dry_run, memory_dir=args.memory_dir)
    rc = inst.install(agents, projects)
    if explicit and args.project_path and not args.dry_run and rc == 0:
        name, path = projects[0]
        if remember_project(ctx.plat, name, path):
            out("config: recorded project %s at %s in %s" % (name, path, tilde(ctx.plat.config_file())))
    return rc


# uninstall ------------------------------------------------------------------------------


def cmd_uninstall(args, ctx):
    dry = args.dry_run
    p = ctx.plat
    if dry:
        out("dry run: nothing will change")
    manifests = load_manifests(p)
    stores = known_stores(ctx, manifests)
    copies = active_copies(p, manifests)
    candidates = [p.join(p.claude_home(), "CLAUDE.md"), p.join(p.codex_home(), "AGENTS.md"), p.agents_skills()]
    skills_dir = p.join(p.claude_home(), "skills")
    if os.path.isdir(skills_dir):
        candidates += [os.path.join(skills_dir, n) for n in sorted(os.listdir(skills_dir))]
    candidates += sorted(glob.glob(os.path.join(p.claude_home(), "projects", "*", "memory")))
    for _, data in manifests:
        candidates += [e.get("path") for e in data.get("entries", []) if e.get("path")]
    removed = set()
    problems = 0
    for path in candidates:
        path = os.path.abspath(path)
        if path in removed:
            continue
        if is_linklike(path):
            lt = link_target(path)
            if lt and any(within(lt, s) for s in stores):
                removed.add(path)
                out("%s link %s" % ("would remove" if dry else "removed", tilde(path)))
                if not dry:
                    remove_link(path)
            continue
        entry = copies.get(norm(path))
        if entry and os.path.isfile(path):
            target = entry.get("target", "")
            if os.path.isfile(target) and not filecmp.cmp(path, target, shallow=False):
                warn("managed copy %s differs from %s (edited?); left in place" % (tilde(path), target))
                problems += 1
                continue
            removed.add(path)
            out("%s managed copy %s" % ("would remove" if dry else "removed", tilde(path)))
            if not dry:
                os.remove(path)
    restored = 0
    for mpath, data in manifests:
        if data.get("restored_at"):
            continue
        for entry in reversed(data.get("entries", [])):
            backup, path = entry.get("backup"), entry.get("path")
            if not backup:
                continue
            if not os.path.lexists(backup):
                warn("backup %s is gone; cannot restore %s" % (tilde(backup), tilde(path)))
                problems += 1
                continue
            occupied = os.path.lexists(path) and not (dry and path in removed)
            if occupied:
                warn("%s is in use (%s); left the backup at %s" % (tilde(path), describe(path), tilde(backup)))
                problems += 1
                continue
            out("%s %s from %s" % ("would restore" if dry else "restored", tilde(path), tilde(backup)))
            restored += 1
            if not dry:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                shutil.move(backup, path)
        if not dry:
            data["restored_at"] = iso_now()
            with open(mpath, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2)
                fh.write("\n")
    out(
        "uninstall: %d links %s, %d items %s, %d problems"
        % (
            len(removed),
            "to remove" if dry else "removed",
            restored,
            "to restore" if dry else "restored",
            problems,
        )
    )
    return 0
