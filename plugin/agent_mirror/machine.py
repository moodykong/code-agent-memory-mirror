"""This machine: its stable id and human name, the choices recorded for it, and its profile in the store.

All of it lives in the user config file (the machine config, never the store):

    "machine": {"id": "<32 hex characters>", "name": "<name>"}
    "choices": {"<unit id>": "<action>"}       for example "skill:alpha": "keep-local",
                                               "box:my-project": "writer"

`init-store`, `install`, `apply` and the first `box push` create the id and name when they are missing.
The name defaults to the host name (made safe for a file name). After a real apply on a developer
runtime, a non-secret profile goes to the store at machines/<name>.json.
"""

import json
import os
import re
import socket
import uuid

from agent_mirror import __version__
from agent_mirror.config import dump_json, load_user_config, save_user_config
from agent_mirror.util import err, iso_now, out, read_text, tilde, write_text

NAME_RX = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
BOX_ROLES = ("writer", "reader", "none")
# choices that stand as this machine's decision until the user changes them
STICKY = ("keep-local", "keep-machine", "skip")
MACHINES_DIR = "machines"


def safe_name(text):
    """A file-name-safe machine name made from text (a host name), or 'machine'."""
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", str(text or "")).strip("-.")[:64]
    return name if NAME_RX.match(name) else "machine"


def name_problem(name):
    if not NAME_RX.match(str(name or "")):
        return (
            "a machine name is 1 to 64 letters, digits, '.', '_' or '-', starting with a letter or digit (got %r)"
            % (name,)
        )
    return None


def identity(cfg):
    """(id, name) from the config, either may be None."""
    m = cfg.get("machine") if isinstance(cfg.get("machine"), dict) else {}
    mid = str(m.get("id") or "").strip() or None
    name = str(m.get("name") or "").strip() or None
    return mid, name


def ensure(plat, dry_run=False, quiet=False):
    """(id, name), creating and saving whatever is missing (never in a dry run)."""
    cfg = load_user_config(plat)
    mid, name = identity(cfg)
    if mid and name:
        return mid, name
    new_id = mid or uuid.uuid4().hex
    new_name = name or safe_name(socket.gethostname())
    if dry_run:
        return mid, name or new_name
    m = cfg.get("machine") if isinstance(cfg.get("machine"), dict) else {}
    m.update({"id": new_id, "name": new_name})
    cfg["machine"] = m
    save_user_config(plat, cfg)
    if not quiet:
        out("machine: id %s, name %s (recorded in %s)" % (new_id[:12], new_name, tilde(plat.config_file())))
    return new_id, new_name


def choices(cfg):
    value = cfg.get("choices")
    return dict(value) if isinstance(value, dict) else {}


def recorded(cfg, unit_id):
    return choices(cfg).get(unit_id)


def box_role(cfg, project):
    role = recorded(cfg, "box:%s" % project)
    return role if role in BOX_ROLES else None


def record(plat, updates, dry_run=False):
    """Merge {unit id: action} into the config's choices. Returns True when the file changed."""
    if not updates or dry_run:
        return False
    cfg = load_user_config(plat)
    current = choices(cfg)
    before = dict(current)
    current.update(updates)
    if current == before:
        return False
    cfg["choices"] = dict(sorted(current.items()))
    save_user_config(plat, cfg)
    return True


def cmd_machine(args, ctx):
    plat = ctx.plat
    if args.name is not None:
        problem = name_problem(args.name)
        if problem:
            err(problem)
            return 2
        cfg = load_user_config(plat)
        mid, old = identity(cfg)
        m = cfg.get("machine") if isinstance(cfg.get("machine"), dict) else {}
        m.update({"id": mid or uuid.uuid4().hex, "name": args.name})
        cfg["machine"] = m
        save_user_config(plat, cfg)
        if old and old != args.name:
            out("machine name changed from %s to %s" % (old, args.name))
            out(
                "note: the store's profile machines/%s.json stays until you remove it; "
                "the next apply writes machines/%s.json" % (old, args.name)
            )
        else:
            out("machine name: %s" % args.name)
        return 0
    cfg = load_user_config(plat)
    mid, name = identity(cfg)
    out("machine id:   %s" % (mid or "not assigned yet (init-store, install or apply assigns one)"))
    out("machine name: %s" % (name or "not set (default: %s)" % safe_name(socket.gethostname())))
    out("platform:     %s" % plat.name)
    out("config:       %s" % tilde(plat.config_file()))
    recorded_choices = choices(cfg)
    if recorded_choices:
        out("recorded choices:")
        for unit_id, action in sorted(recorded_choices.items()):
            out("  %-32s %s" % (unit_id, action))
    else:
        out("recorded choices: none")
    return 0


# the profile in the store -------------------------------------------------------------


def profile_path(store, name):
    return store.path(MACHINES_DIR, "%s.json" % name)


def profile_data(plat, plan_data, mid, name):
    """The non-secret profile: no home paths outside the profile-relative form, no user names."""
    projects = {}
    for p in plan_data.get("projects", []):
        entry = projects.setdefault(p["project"], {"paths": [], "memory": None, "box_role": None})
        shown = tilde(p["path"])
        if shown.startswith("~") and shown not in entry["paths"]:
            entry["paths"].append(shown.replace("\\", "/"))
    skills = {"linked": [], "kept_local": [], "skipped": []}
    for u in plan_data.get("units", []):
        kind, case, decided = u["kind"], u["case"], u.get("decision")
        if kind == "memory" and u["name"] in projects:
            projects[u["name"]]["memory"] = "linked" if case == "linked" else (decided or "not linked")
        elif kind == "box" and u["name"] in projects:
            projects[u["name"]]["box_role"] = u.get("recorded")
        elif kind == "skill":
            if u.get("recorded") in ("keep-local", "keep-machine"):
                skills["kept_local"].append(u["name"])
            elif u.get("recorded") == "skip":
                skills["skipped"].append(u["name"])
            elif case == "linked":
                skills["linked"].append(u["name"])
    return {
        "schema": 1,
        "machine_id": mid,
        "machine_name": name,
        "platform": plat.name,
        "tool": "agent-mirror %s" % __version__,
        "date": iso_now()[:10],
        "agents": list(plan_data.get("agents", [])),
        "projects": projects,
        "skills": dict((k, sorted(v)) for k, v in skills.items()),
    }


def write_profile(store, data, dry_run=False):
    """Write machines/<name>.json when something other than the date changed. Returns the path or None."""
    path = profile_path(store, data["machine_name"])
    if os.path.isfile(path):
        try:
            old = json.loads(read_text(path))
        except (OSError, ValueError):
            old = {}
        if dict(old, date=None) == dict(data, date=None):
            return None
    if dry_run:
        out("would write the machine profile %s" % os.path.relpath(path, store.root).replace(os.sep, "/"))
        return None
    write_text(path, dump_json(data))
    out("machine profile: %s (sync commits it)" % os.path.relpath(path, store.root).replace(os.sep, "/"))
    return path
