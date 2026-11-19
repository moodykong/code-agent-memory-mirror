"""The plugin link: <claude home>/skills/agent-mirror -> the plugin/ folder of the agent-mirror checkout that runs.

Claude Code loads a folder in its skills folder that holds .claude-plugin/plugin.json (or a link to one) as a
"skills-directory plugin": `claude plugin list` shows it as <manifest name>@skills-dir, and its skills are
invoked as /<manifest name>:<skill>, for example /agent-mirror:wrap-up. The namespace comes from the manifest's
name, not from the link's; the link carries the same name so that a person looking at the folder sees what it is.

The link belongs to the tool, not to the store. It points at the very checkout whose CLI runs, so the plugin
skills always match the CLI's version, and there is one copy of the tool. `install` and `apply` (for Claude)
create or repair it, a cloud session start repairs it on a machine set up before it existed (step "plugin link
check"), `doctor` and `plan` report it, and `uninstall` removes it. `plan` never lists it as a skill.

What is at the link's path decides what happens:

    linked           a link to this checkout's plugin folder           nothing to do
    missing          nothing there                                     created
    other-checkout   a link to the plugin folder of another checkout   relinked to this one
    stale            a broken link that agent-mirror made              relinked to this one
    store-skill      a store skill named agent-mirror holds the name   left alone: rename that skill
    elsewhere        a link to something else                          left alone
    broken           a broken link agent-mirror did not make           left alone
    folder, file     a real folder or file                             left alone
    folder-link      the skills folder itself is a link                left alone (apply converts a legacy one)
    unavailable      this checkout has no Claude plugin manifest       nothing

Something agent-mirror did not make is never replaced: the message says what is there and what to do. A copy of
the plugin folder would go stale, so where no link can be made (a symlink fails and so does a junction on
Windows) nothing is made and the error says why. Nothing here is for Codex: its manifest only carries the
same name.
"""

import json
import os

from agent_mirror import PLUGIN_ROOT
from agent_mirror.links import (
    describe,
    is_linklike,
    link_location,
    link_target,
    make_link,
    remove_link,
    resolve_problem,
)
from agent_mirror.util import err, norm, out, short, tilde, warn, within

NAME = "agent-mirror"  # the plugin's manifest name, and the link's name in <claude home>/skills
MANIFEST_NAMES = (NAME, "code-agent-memory-mirror")  # the manifest name since 0.5.9, and before it
ENTRY_KIND = "plugin-link"  # the kind of its entry in an install manifest
ENABLES = "/agent-mirror:wrap-up and the other agent-mirror plugin skills"

LINKED = "linked"
MISSING = "missing"
OTHER = "other-checkout"
STALE = "stale"
REPAIRABLE = (MISSING, OTHER, STALE)
TOOL_LINK = (LINKED, OTHER, STALE)  # what plan never lists as a skill and uninstall removes


def slot(plat):
    """Where the link lives: <claude home>/skills/agent-mirror."""
    return plat.join(plat.claude_skills(), NAME)


def manifest_name(folder):
    """The name in folder/.claude-plugin/plugin.json, or None."""
    try:
        with open(os.path.join(folder, ".claude-plugin", "plugin.json"), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    name = data.get("name") if isinstance(data, dict) else None
    return name if isinstance(name, str) else None


def is_tool_plugin(folder):
    """True when folder is the plugin/ folder of an agent-mirror checkout (this one or another)."""
    return manifest_name(folder) in MANIFEST_NAMES and os.path.isfile(
        os.path.join(folder, "agent_mirror", "__init__.py")
    )


def recorded_targets(manifests, path):
    """Targets an install manifest records for a plugin link at path (normalised)."""
    found = set()
    for _, data in manifests or []:
        for e in data.get("entries", []):
            if e.get("kind") == ENTRY_KIND and e.get("target") and norm(e.get("path", "")) == norm(path):
                found.add(norm(e["target"]))
    return found


def store_has_skill(store):
    return store is not None and NAME in store.skill_names()


def inspect(plat, store_root=None, store_skill=False, manifests=None, target=None):
    """{"path", "target", "state", "points_to", "what", "advice"} for the link's path (read-only)."""
    target = target or PLUGIN_ROOT
    path = slot(plat)
    folder = plat.claude_skills()
    info = {"path": path, "target": target, "state": None, "points_to": None, "what": None, "advice": None}

    def done(state, what=None, advice=None):
        info.update(state=state, what=what, advice=advice)
        return info

    if not manifest_name(target):
        return done("unavailable", "this copy of agent-mirror (%s) has no Claude plugin manifest" % target)
    if is_linklike(folder):
        legacy = bool(store_root and link_target(folder) and within(link_target(folder), store_root))
        return done(
            "folder-link",
            "the skills folder %s is itself a link (%s)" % (tilde(folder), describe(folder)),
            (
                "run `agent-mirror apply`: it turns this older whole-folder link into a real folder, then makes the link"
                if legacy
                else "agent-mirror never writes into a linked skills folder"
            ),
        )
    rename = "rename the store's skill (skills/%s in the store), then run `agent-mirror install`" % NAME
    if not os.path.lexists(path):
        if store_skill:
            return done(
                "store-skill", "free, but the store has a skill named %s, which install links there" % NAME, rename
            )
        return done(MISSING)
    if is_linklike(path):
        lt = link_target(path)
        info["points_to"] = lt
        in_store = bool(store_root and lt and within(lt, store_root))
        if os.path.exists(path):
            real = os.path.realpath(path)
            if norm(real) == norm(os.path.realpath(target)):
                return done(LINKED)
            if in_store or (store_root and within(real, store_root)):
                return done(
                    "store-skill", "a link into the store (%s): the store's skill %s" % (describe(path), NAME), rename
                )
            if is_tool_plugin(real):
                return done(OTHER, "a link to another agent-mirror checkout (%s)" % tilde(real))
            return done(
                "elsewhere",
                "a link to %s, which is not the agent-mirror plugin" % tilde(lt or real),
                "move it away, then run `agent-mirror install`",
            )
        if lt and norm(lt) in recorded_targets(manifests, path):
            return done(STALE, "a link agent-mirror made to %s, which no longer exists" % tilde(lt))
        if in_store:
            return done(
                "store-skill",
                "a broken link into the store (%s)" % describe(path),
                "remove it (`agent-mirror plan` shows it as drift), then run `agent-mirror install`",
            )
        return done(
            "broken",
            "a link to %s, which does not exist (not made by agent-mirror)" % tilde(lt or "?"),
            "remove it, then run `agent-mirror install`",
        )
    if os.path.isdir(path):
        advice = "move it away, then run `agent-mirror install`"
        if is_tool_plugin(path):
            what = "a real folder holding a copy of the agent-mirror plugin (a copy goes stale)"
        elif os.path.isfile(os.path.join(path, "SKILL.md")):
            what = "a real folder with a SKILL.md: a skill of this machine named %s" % NAME
            advice = "rename that skill (or move it away), then run `agent-mirror install`"
        else:
            what = "a real folder"
        return done("folder", what, advice)
    return done("file", "a real file", "move it away, then run `agent-mirror install`")


def plan_info(plat, store, manifests=None):
    """inspect() for this machine and store: what plan, doctor and apply use."""
    root = os.path.realpath(store.root) if store is not None else None
    return inspect(plat, root, store_has_skill(store), manifests)


def doctor_state(info):
    """(label, note) for doctor's list of links."""
    state = info["state"]
    points = info.get("points_to")
    kind = "junction" if points and not os.path.islink(info["path"]) else "symlink"
    if state == LINKED:
        return "OK", "%s -> %s; the agent-mirror plugin for Claude Code: %s" % (kind, info["target"], ENABLES)
    if state == MISSING:
        return "MISSING", "install creates it; it gives Claude Code %s" % ENABLES
    if state in (OTHER, STALE):
        return "ELSEWHERE", "%s; install relinks it to this checkout, for %s" % (info["what"], ENABLES)
    if state == "unavailable" or not os.path.lexists(info["path"]):
        label = "-"
    elif state in ("folder", "file"):
        label = "NOT LINKED"
    else:
        label = "ELSEWHERE"
    if state == "unavailable":
        return label, info["what"]
    return label, "%s; left alone, so Claude Code has no /agent-mirror:wrap-up here. To get it: %s" % (
        info["what"],
        info["advice"],
    )


def plan_line(info):
    """One line for plan's human output."""
    head = "plugin link %s (Claude Code: %s): " % (tilde(info["path"]), ENABLES)
    state = info["state"]
    if state == LINKED:
        return head + "linked to %s" % tilde(info["target"])
    if state == MISSING:
        return head + "missing; install and apply create it"
    if state in (OTHER, STALE):
        return head + "%s; install and apply relink it to %s" % (info["what"], tilde(info["target"]))
    if state == "unavailable":
        return head + info["what"]
    return head + "%s; left alone. To get it: %s" % (info["what"], info["advice"])


def plan_data(info):
    """The plugin link for `plan --json`."""
    return {
        "path": tilde(info["path"]),
        "target": tilde(info["target"]),
        "state": info["state"],
        "repairable": info["state"] in REPAIRABLE,
        "points_to": tilde(info["points_to"]) if info.get("points_to") else None,
        "note": info.get("what"),
        "advice": info.get("advice"),
    }


def ensure(inst, dry_run=None, quiet=False):
    """Create or repair the link with an Installer (its link methods and manifest); returns a step status.

    Never replaces what agent-mirror did not make. A failure to link is recorded in inst.errors."""
    from agent_mirror.install import load_manifests

    plat = inst.plat
    dry = inst.dry_run if dry_run is None else dry_run
    store_root = os.path.realpath(inst.store.root)
    info = inspect(plat, store_root, store_has_skill(inst.store), load_manifests(plat))
    state, path, target = info["state"], info["path"], info["target"]
    if state == LINKED:
        inst.already += 1
        return "ok"
    if state == "unavailable":
        return "skipped (no plugin manifest)"
    if state not in REPAIRABLE:
        if not quiet:
            warn(
                "%s is %s; left alone, so Claude Code has no /agent-mirror:wrap-up here. To get it: %s"
                % (tilde(path), info["what"], info["advice"])
            )
        return "skipped (%s)" % state
    reason = None
    loc = link_location(path)
    if within(loc, store_root):
        reason = "the link %s would sit inside the store" % loc
    elif not any(within(loc, r) for r in inst.allowed_roots()):
        reason = "the link %s is outside the home folder" % loc
    elif within(os.path.realpath(target), store_root):
        reason = "the plugin folder %s is inside the store" % target
    if reason:
        inst.errors.append(reason)
        err("refused the plugin link: %s" % reason)
        return "failed (refused)"
    old = info.get("points_to")
    if dry:
        if state == MISSING:
            out("would link %s -> %s (the agent-mirror plugin for Claude Code: %s)" % (tilde(path), target, ENABLES))
        else:
            out("would relink %s from %s to %s (%s)" % (tilde(path), tilde(old or "?"), target, info["what"]))
        return "skipped (dry run)"
    entry = {
        "path": os.path.abspath(path),
        "target": target,
        "before": describe(path) if os.path.lexists(path) else "absent",
        "backup": None,
        "kind": ENTRY_KIND,
    }
    if state != MISSING:
        entry["before_link"] = old
        remove_link(path)  # a link agent-mirror made (to another checkout, or to one that is gone)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        method = make_link(path, target, True, inst.fallback, inst.symlink, inst.junction)
    except OSError as exc:
        if old and not os.path.lexists(path):
            try:
                make_link(path, old, True, inst.fallback, inst.symlink, inst.junction)  # put the old link back
            except OSError:
                pass
        reason = "could not link %s -> %s: %s (a copy would go stale, so none is made)" % (
            path,
            target,
            short(exc, 200),
        )
        inst.errors.append(reason)
        err(reason)
        return "failed (could not link)"
    if method == "symlink" and resolve_problem(path) == "loop":
        remove_link(path)
        reason = "the plugin link %s -> %s made a loop; undone" % (path, target)
        inst.errors.append(reason)
        err(reason)
        return "failed (loop)"
    entry["method"] = method
    inst.entries.append(entry)
    suffix = " (junction)" if method == "junction" else ""
    if state == MISSING:
        out(
            "linked %s -> %s%s: the agent-mirror plugin for Claude Code (%s, from the next session)"
            % (tilde(path), target, suffix, ENABLES)
        )
        return "ok (linked)"
    out("relinked %s from %s to %s%s (%s)" % (tilde(path), tilde(old or "?"), target, suffix, info["what"]))
    return "ok (relinked)"


def is_ours(plat, path, manifests=None):
    """True when the link at path is the plugin link (to this checkout, another one, or one agent-mirror made)."""
    if norm(os.path.abspath(path)) != norm(os.path.abspath(slot(plat))) or not is_linklike(path):
        return False
    return inspect(plat, manifests=manifests)["state"] in TOOL_LINK


def repair(ctx, runtime, dry_run, log):
    """Cloud session start: make the plugin link on a machine set up before it existed. Never raises."""
    try:
        with log.step("plugin link check") as st:
            st.status = _repair(ctx, dry_run)
    except Exception as exc:  # noqa: BLE001 (never fail the session)
        out("warning: plugin link check failed: %s; the session goes on" % short(exc, 120))


def _repair(ctx, dry_run):
    from agent_mirror.install import Installer

    inst = Installer(ctx, dry_run)
    status = ensure(inst, dry_run)
    if inst.entries and not dry_run:
        inst.write_manifest(["claude"], [])
    return status
