"""The box: bulk untracked files moved between developer machines and agents.

inbound   developer machines upload (box push), each the folders it owns (owners.py);
          agents read (box index, box fetch).
outbound  agents upload results (box send); the developer pulls them by hand.

Backends: azure-blob (Azure CLI), rclone, or none.
"""

import os
import shlex

from agent_mirror.box import archive, azure, browse, extras, owners, rclone
from agent_mirror.box.common import box_context
from agent_mirror.box.rules import azure_rule_problems, clean_rel, generic_rule_problems, is_env_file
from agent_mirror.box.sides import side_for
from agent_mirror.box.units import archive_unit_of, archive_units
from agent_mirror.runtime import detect_runtime
from agent_mirror.util import err, local_path, out

BACKEND_MODULES = {"azure-blob": azure, "rclone": rclone}


def cmd_box(args, ctx):
    action = args.box_action
    if action == "status":
        return browse.cmd_status(args, ctx)
    if action == "evict":
        return browse.cmd_evict(args, ctx)
    if action == "rclone-env":
        return extras.cmd_rclone_env(args, ctx)
    if action == "candidates":
        from agent_mirror.box.candidates import cmd_candidates

        return cmd_candidates(args, ctx)
    rel = None
    if action in ("fetch", "send"):
        rel = clean_rel(args.path)
        if rel is None:
            err("box %s takes a path relative to the project folder, without '..' (got %r)" % (action, args.path))
            return 2
        if action == "send" and is_env_file(rel.rsplit("/", 1)[-1]):
            err("refusing to upload %s: env files hold secrets" % rel)
            return 2
    s, rc = box_context(args, ctx)
    if s is None:
        return rc
    backend = BACKEND_MODULES[s["backend"]]
    runtime = detect_runtime()
    if action == "push":
        return owners.push(args, s, runtime, backend, ctx.plat)
    if action == "release":
        return owners.release(args, s, runtime, backend, ctx.plat)
    if action == "owners":
        return owners.cmd_owners(args, s, runtime, backend)
    if action == "index":
        return backend.index(args, s, runtime)
    if action == "ls":
        return browse.cmd_ls(args, s)
    if action == "receive":
        return extras.cmd_receive(args, s, runtime)
    if action == "mount":
        return extras.cmd_mount(args, s, ctx.plat)
    if action == "unmount":
        return extras.cmd_unmount(args, s, ctx.plat)
    if action == "fetch":
        top = archive_unit_of(rel, s["archives"])
        if top:
            rc = archive.fetch_archive(args, s, runtime, side_for(s, "inbound"), rel, top)
            if rc is not None:
                return rc
        else:
            inside = archived_under(s, rel)
            if inside:
                project_flag = " --project %s" % shlex.quote(s["project"]) if s.get("project") else ""
                out("%s holds %d archived folder(s); each is fetched on its own:" % (rel, len(inside)))
                for name in inside[:50]:
                    out("  agent-mirror box fetch %s%s" % (shlex.quote(name), project_flag))
        return backend.fetch(args, s, runtime, rel)
    src = local_path(s["project_path"], rel)
    hit = s["rules"].excluded(rel) or (os.path.isdir(src) and s["rules"].excluded(rel + "/"))
    if hit:
        err("refusing to upload %s: it matches the exclude pattern %s" % (rel, hit))
        return 2
    if not os.path.lexists(src) and not args.dry_run:
        err("%s does not exist" % src)
        return 1
    if getattr(args, "archive", False):
        return archive.send_archive(args, s, runtime, side_for(s, "outbound"), rel)
    return backend.send(args, s, runtime, rel)


def archived_under(s, rel):
    """Archived folders below rel, from the committed index (offline)."""
    if not os.path.isfile(s["index_path"]):
        return []
    try:
        _, archives = browse.load_index(s["index_path"])
    except (OSError, ValueError):
        return []
    return sorted(a["path"] for a in archives if a["path"].startswith(rel + "/"))


def describe_box(env, paths=()):
    """Lines describing a project's box settings for doctor, plus problems and warnings.

    paths are local checkouts of the project; archive entries that overlap on their folders are warnings.
    """
    box = env.get("box") or {}
    backend = str(box.get("backend") or "none").strip().lower()
    if backend == "none":
        return ["not configured (no box.backend)"], [], []
    if backend == "azure-blob":
        if not str(box.get("storage_account") or "").strip():
            return ["azure-blob, not configured yet (box.storage_account is empty)"], [], []
        prefix = str(box.get("prefix") or "").strip("/")
        s = {
            "account": box.get("storage_account"),
            "inbound": box.get("inbound_container"),
            "outbound": box.get("outbound_container"),
            "prefix": prefix,
        }
        lines = azure.describe(s)
        problems = azure_rule_problems(box)
    elif backend == "rclone":
        lines = rclone.describe({"inbound": box.get("inbound_remote"), "outbound": box.get("outbound_remote")})
        problems = generic_rule_problems(box)
    else:
        return ["unknown backend %r" % backend], ["box.backend must be azure-blob, rclone or none"], []
    lines.append("include  %s" % ", ".join(box.get("include") or []))
    lines.append("exclude  %s" % ", ".join(box.get("exclude") or []))
    if box.get("archives"):
        lines.append("archives %s (one bundle per matching folder; large files on their own)" % ", ".join(box.get("archives")))
    problems = problems + archive.archive_problems(box)
    warnings = []
    if not problems:
        globs = [str(g).strip().strip("/") for g in box.get("archives") or []]
        for path in paths:
            if os.path.isdir(path):
                warnings += [w for w in archive_units(path, globs)[1] if w not in warnings]
    return lines, problems, warnings
