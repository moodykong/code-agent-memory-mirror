"""apply (and install): carry out one action per unit of the plan.

The action for a unit comes from the choices file ({"unit id": "action"}), else from a choice
recorded for this machine, else from the default when the case needs no decision. reconcile and
first-setup units without a choice wait (with --yes-defaults first-setup takes its default).
Everything a link replaces is backed up with a manifest, as install always did, and `uninstall`
puts it back. Before anything is adopted into the store (published), the secret scan runs on it.
"""

import filecmp
import json
import os
import shutil
import socket

from agent_mirror import machine
from agent_mirror.inbox import union_merge
from agent_mirror.install import Installer, remember_project_quiet
from agent_mirror.config import load_user_config
from agent_mirror.links import describe, is_linklike, link_target
from agent_mirror.plan import VENDOR_SKILLS, Plan, build_plan, content_files, project_pairs
from agent_mirror.runtime import CLOUD_RUNTIMES
from agent_mirror.util import err, out, tilde, warn

NOOP = ("none",)
RECORD_ONLY = ("keep-local", "keep-machine", "skip")
CLOUD_NO_ADOPT = (
    "a cloud session does not publish machine content to the store's main line; adopt it on a developer machine"
)


def load_choices(path):
    """{unit id: action} from a JSON file; exits with a clear message on a bad file."""
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except OSError as exc:
        raise SystemExit("error: cannot read the choices file %s: %s" % (path, exc))
    except ValueError as exc:
        raise SystemExit("error: the choices file %s is not valid JSON: %s" % (path, exc))
    if not isinstance(data, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in data.items()):
        raise SystemExit('error: the choices file %s must hold one JSON object: {"unit id": "action", ...}' % path)
    return data


def choice_problems(choices, plan, only=None):
    problems = []
    for uid, action in sorted(choices.items()):
        u = plan.unit(uid)
        if u is None:
            problems.append("%s: no such unit in the plan (agent-mirror plan --json lists them)" % uid)
        elif action not in u["choices"]:
            problems.append(
                "%s (%s): %r is not allowed; choose one of %s" % (uid, u["case"], action, ", ".join(u["choices"]))
            )
    for uid in only or []:
        if plan.unit(uid) is None:
            problems.append("--only %s: no such unit in the plan" % uid)
    return problems


def scan_paths(paths):
    """[(file, [pattern names])] for the files about to be copied into the store."""
    from agent_mirror.sync import scan_file

    hits = []
    for path in paths:
        found = scan_file(path)
        if found:
            hits.append((path, found))
    return hits


def tree_files(root):
    return [full for _, full, kind in content_files(root) if kind == "F"]


def copy_clean(src, dst):
    """Copy a folder, leaving out caches, editor leftovers and links (never followed or copied)."""
    skipped = []
    for rel, full, kind in content_files(src):
        if kind == "L":
            skipped.append(rel)
            continue
        dest = os.path.join(dst, *rel.split("/"))
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.copy2(full, dest)
    os.makedirs(dst, exist_ok=True)
    for rel in skipped:
        warn("left out the link %s/%s (links are never copied into the store)" % (tilde(src), rel))


class Applier:
    def __init__(self, ctx, plan, dry_run, installer=None):
        self.ctx = ctx
        self.plat = ctx.plat
        self.plan = plan
        self.store = plan.store
        self.dry = dry_run
        self.inst = installer or Installer(ctx, dry_run)
        self.cloud = plan.runtime in CLOUD_RUNTIMES
        self.records = {}
        self.done = []  # (unit id, action)
        self.refused = []  # (unit id, reason)
        self.waiting = []  # units
        self.left_alone = []  # (unit, reason)
        self.agents_md_by = None
        _, name = machine.identity(load_user_config(self.plat))
        self.machine_name = name or plan.mname or machine.safe_name(socket.gethostname())

    # deciding ------------------------------------------------------------------------------

    def decide(self, choices=None, only=None, yes_defaults=False):
        choices = choices or {}
        todo = []
        for u in self.plan.units:
            if only and u["id"] not in only:
                continue
            if u["id"] in choices:
                action = choices[u["id"]]
            elif u["decided_by"] == "recorded":
                continue  # decided earlier on this machine: nothing to do again
            elif u["case"] == "first-setup" and self.cloud:
                self.left_alone.append((u, CLOUD_NO_ADOPT))
                continue
            elif u["case"] == "first-setup" and yes_defaults:
                action = u["default_action"]
            elif u["waiting"]:
                self.waiting.append(u)
                continue
            else:
                action = u["decision"]
            if action is None or (action in NOOP and u["kind"] != "box"):
                continue
            if self.cloud and action == "adopt":
                self.left_alone.append((u, CLOUD_NO_ADOPT))
                continue
            todo.append((u, action))
        return todo

    def run(self, todo):
        for u, action in todo:
            handler = getattr(self, "do_" + u["kind"].replace("-", "_"))
            try:
                ok = handler(u, action)
            except OSError as exc:
                self.refuse(u, "failed: %s" % exc)
                continue
            if ok:
                self.done.append((u["id"], action))
                if action != "convert":
                    self.records[u["id"]] = action

    def refuse(self, u, reason):
        self.refused.append((u["id"], reason))
        err("refused %s: %s" % (u["id"], reason))
        return False

    # shared steps ---------------------------------------------------------------------------

    def touch(self, u, p):
        """True when the link at this place must be (re)made for the unit's action."""
        if p.state == "external":
            return False
        if p.state == "linked":
            return bool(getattr(p, "stale", False))
        return True

    def link_places(self, u, target, is_dir, allow_missing=False):
        ok = True
        for p in self.plan.places.get(u["id"], []):
            if not self.touch(u, p):
                continue
            if is_dir and p.label == "shared":
                self.ensure_folder(os.path.dirname(p.path))
            ok = self.inst.link(p.path, target, allow_missing=allow_missing, is_dir=is_dir) and ok
        return ok

    def ensure_folder(self, folder):
        """Create the shared skills folder as a real folder (recorded so uninstall removes it when empty)."""
        if os.path.lexists(folder):
            return
        if self.dry:
            out("would create the folder %s" % tilde(folder))
            return
        os.makedirs(folder)
        self.inst.entries.append(
            {
                "path": os.path.abspath(folder),
                "target": None,
                "before": "absent",
                "backup": None,
                "method": "mkdir",
                "created_dir": True,
            }
        )
        out("created the folder %s" % tilde(folder))

    def secret_check(self, u, files):
        hits = scan_paths(files)
        if not hits:
            return True
        detail = "; ".join("%s: %s" % (tilde(f), ", ".join(names)) for f, names in hits)
        return self.refuse(
            u, "the secret scan found key-shaped text, so nothing was copied into the store (%s)" % detail
        )

    def into_store(self, u, src, dst, is_dir):
        """Copy src into the store at dst, moving any previous store copy to the backup folder."""
        rel = os.path.relpath(dst, self.store.root).replace(os.sep, "/")
        if is_linklike(dst):
            return self.refuse(u, "the store's %s is a link; agent-mirror does not replace it" % rel)
        if self.dry:
            out("would copy %s into the store as %s" % (tilde(src), rel))
            return True
        if os.path.lexists(dst):
            old = os.path.join(self.inst.backup_root, "_store", *rel.split("/"))
            os.makedirs(os.path.dirname(old), exist_ok=True)
            shutil.move(dst, old)
            self.inst.replaced.append({"store_path": rel, "old_copy": old})
            out("kept the store's previous %s at %s" % (rel, tilde(old)))
        if is_dir:
            copy_clean(src, dst)
        else:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copyfile(src, dst)
        out("copied %s into the store as %s" % (tilde(src), rel))
        return True

    # units -------------------------------------------------------------------------------------

    def do_skill(self, u, action):
        target = self.store.path("skills", u["name"])
        if action in RECORD_ONLY:
            out("%s: %s (recorded for this machine; nothing linked)" % (u["id"], action))
            return True
        if action == "adopt":
            content = [p for p in self.plan.places[u["id"]] if p.state == "content"]
            src = os.path.realpath(Plan.newest(content).path)
            if not self.secret_check(u, tree_files(src)):
                return False
            if not self.into_store(u, src, target, is_dir=True):
                return False
        return self.link_places(u, target, is_dir=True, allow_missing=self.dry and action == "adopt")

    def do_instructions(self, u, action):
        target = self.store.path("AGENTS.md")
        if action in RECORD_ONLY:
            out("%s: %s (recorded for this machine; nothing linked)" % (u["id"], action))
            return True
        if action == "adopt":
            src = os.path.realpath(self.plan.places[u["id"]][0].path)
            if not self.secret_check(u, [src]):
                return False
            if self.agents_md_by:
                if os.path.isfile(target) and filecmp.cmp(src, target, shallow=False):
                    out(
                        "%s: the same text as the AGENTS.md just adopted from %s; linking"
                        % (u["id"], self.agents_md_by)
                    )
                else:
                    return self.refuse(
                        u,
                        "the store's AGENTS.md was just replaced from %s; run `agent-mirror plan` again and choose"
                        % self.agents_md_by,
                    )
            elif not self.into_store(u, src, target, is_dir=False):
                return False
            self.agents_md_by = u["id"]
        return self.link_places(u, target, is_dir=False, allow_missing=self.dry and action == "adopt")

    def do_memory(self, u, action):
        store_dir = self.store.memory_store(u["name"])
        if action in RECORD_ONLY:
            out("%s: %s (recorded for this machine; nothing linked)" % (u["id"], action))
            return True
        places = self.plan.places[u["id"]]
        if action in ("adopt", "merge"):
            content = [p for p in places if p.state == "content"]
            files = []
            for p in content:
                files += tree_files(os.path.realpath(p.path))
            if not self.secret_check(u, files):
                return False
            for p in content:
                self.merge_memory(os.path.realpath(p.path), store_dir)
        elif not os.path.isdir(store_dir):
            out("%s memory store %s" % ("would create the" if self.dry else "created the", tilde(store_dir)))
            if not self.dry:
                os.makedirs(store_dir, exist_ok=True)
        return self.link_places(u, store_dir, is_dir=True, allow_missing=True)

    def merge_memory(self, src, dst):
        """Copy files the store lacks; union-merge index files; save both-sides conflicts next to the store's file."""
        copied, merged, sides, same = [], [], [], 0
        for rel, full, kind in content_files(src):
            if kind == "L":
                warn("skipped the link %s (memory files must be real files)" % tilde(full))
                continue
            dest = os.path.join(dst, *rel.split("/"))
            with open(full, "rb") as fh:
                theirs = fh.read()
            if not os.path.lexists(dest):
                copied.append(rel)
                if not self.dry:
                    os.makedirs(os.path.dirname(dest), exist_ok=True)
                    shutil.copy2(full, dest)
                continue
            with open(dest, "rb") as fh:
                ours = fh.read()
            if ours == theirs:
                same += 1
                continue
            if os.path.basename(rel) == "MEMORY.md":
                text = union_merge("", ours.decode("utf-8", "replace"), theirs.decode("utf-8", "replace"))
                if text.encode("utf-8") != ours:
                    merged.append(rel)
                    if not self.dry:
                        with open(dest, "w", encoding="utf-8", newline="") as fh:
                            fh.write(text)
                continue
            side = self.side_name(dest, theirs)
            if side:
                sides.append((rel, os.path.relpath(side, dst).replace(os.sep, "/")))
                if not self.dry:
                    with open(side, "wb") as fh:
                        fh.write(theirs)
        verb = "would copy" if self.dry else "copied"
        out(
            "memory from %s: %s %d file(s) into %s, merged %d index file(s), %d conflict(s) saved beside the "
            "store's file, %d already the same"
            % (tilde(src), verb, len(copied), tilde(dst), len(merged), len(sides), same)
        )
        for rel, side in sides:
            warn(
                "memory conflict: %s differs; the store's version is kept and this machine's is saved as %s "
                "(not in the index)" % (rel, side)
            )
        self.inst.merges.append({"from": src, "store": dst, "copied": copied, "merged": merged, "side_files": sides})

    def side_name(self, dest, data):
        """<stem>.from-<machine name><ext> beside dest (numbered when taken by other content), or None if saved."""
        head, tail = os.path.split(dest)
        stem, ext = os.path.splitext(tail)
        n = 1
        while True:
            suffix = "" if n == 1 else "-%d" % n
            side = os.path.join(head, "%s.from-%s%s%s" % (stem, self.machine_name, suffix, ext or ""))
            if not os.path.lexists(side):
                return side
            with open(side, "rb") as fh:
                if fh.read() == data:
                    return None
            n += 1

    def do_box(self, u, action):
        out("%s: this machine's role is %s (recorded; apply never uploads or downloads anything)" % (u["id"], action))
        return True

    def do_skills_folder(self, u, action):
        if action != "convert":
            return True
        folder = u["path"]
        names = [
            n
            for n in self.store.skill_names()
            if n != VENDOR_SKILLS and machine.recorded(self.plan.cfg, "skill:%s" % n) != "skip"
        ]
        if self.dry:
            out(
                "would replace the link %s (%s) with a real folder and link %d store skill(s) into it"
                % (tilde(folder), describe(folder), len(names))
            )
            return True
        entry = {
            "path": os.path.abspath(folder),
            "target": self.store.path("skills"),
            "before": describe(folder),
            "before_link": link_target(folder),
            "method": "mkdir",
            "created_dir": True,
            "converted": True,
        }
        entry["backup"] = self.inst.backup(folder)
        os.makedirs(folder)
        self.inst.entries.append(entry)
        out(
            "converted %s: the whole-folder link is now a real folder (the old link is kept at %s)"
            % (tilde(folder), tilde(entry["backup"]))
        )
        ok = True
        for n in names:
            ok = self.inst.link(os.path.join(folder, n), self.store.path("skills", n), is_dir=True) and ok
        return ok

    # the end -------------------------------------------------------------------------------------

    def finish(self, label):
        rc = self.inst.finish(self.plan.agents, self.plan.projects, label=label)
        if machine.record(self.plat, self.records, self.dry):
            out("recorded %d choice(s) for this machine in %s" % (len(self.records), tilde(self.plat.config_file())))
        if self.refused:
            rc = 1
        return rc

    def report(self, label):
        for u, reason in self.left_alone:
            out("left alone: %s (%s): %s" % (u["id"], u["case"], reason))
        if self.waiting:
            out("%s: %d unit(s) wait for a choice and were left as they are:" % (label, len(self.waiting)))
            for u in self.waiting:
                out("  %-12s %-28s choices: %s" % (u["case"], u["id"], ", ".join(u["choices"])))
            out(
                "see them with `agent-mirror plan`, then choose with `agent-mirror apply --choices FILE` "
                '(a JSON object {"unit id": "action"}); --yes-defaults takes the default for first-setup units'
            )


def write_machine_profile(ctx, plan, dry_run):
    """After a real apply on a developer runtime: machines/<name>.json in the store."""
    if dry_run or plan.runtime in CLOUD_RUNTIMES:
        return None
    mid, name = machine.ensure(ctx.plat, quiet=True)
    after = Plan(ctx, plan.agents, plan.projects, plan.memory_dirs)
    return machine.write_profile(plan.store, machine.profile_data(ctx.plat, after.data(), mid, name))


def apply_plan(ctx, plan, dry_run, label, choices=None, only=None, yes_defaults=False, installer=None):
    """Apply a plan; returns (exit code, Applier)."""
    if not dry_run:
        machine.ensure(ctx.plat)
    ap = Applier(ctx, plan, dry_run, installer=installer)
    todo = ap.decide(choices, only, yes_defaults)
    ap.run(todo)
    rc = ap.finish(label)
    ap.report(label)
    return rc, ap


def cmd_apply(args, ctx):
    plan = build_plan(args, ctx)
    choices = load_choices(args.choices) if args.choices else {}
    problems = choice_problems(choices, plan, args.only)
    if problems:
        for p in problems:
            err(p)
        return 2
    if args.dry_run:
        out("dry run: nothing will change")
    rc, _ = apply_plan(ctx, plan, args.dry_run, "apply", choices, args.only, args.yes_defaults)
    for name, path in project_pairs(ctx, args.project, args.project_path):
        remember_project_quiet(ctx, name, path, args.dry_run)
    write_machine_profile(ctx, plan, args.dry_run)
    return rc
