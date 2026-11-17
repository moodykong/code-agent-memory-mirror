"""The box: bulk untracked files moved between the developer machine and agents.

inbound   the developer machine uploads (box push); agents read (box index, box fetch).
outbound  agents upload results (box send); the developer pulls them by hand.

Backends: azure-blob (Azure CLI), rclone, or none.
"""

import os

from agent_mirror.box import archive, azure, browse, extras, rclone
from agent_mirror.box.common import box_context
from agent_mirror.box.rules import azure_rule_problems, clean_rel, generic_rule_problems, is_env_file
from agent_mirror.box.sides import side_for
from agent_mirror.runtime import detect_runtime
from agent_mirror.util import err

BACKEND_MODULES = {"azure-blob": azure, "rclone": rclone}


def cmd_box(args, ctx):
    action = args.box_action
    if action == "status":
        return browse.cmd_status(args, ctx)
    if action == "evict":
        return browse.cmd_evict(args, ctx)
    if action == "rclone-env":
        return extras.cmd_rclone_env(args, ctx)
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
        return backend.push(args, s, runtime)
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
        top = rel.split("/")[0]
        if archive.matches(top, s["archives"]):
            rc = archive.fetch_archive(args, s, runtime, side_for(s, "inbound"), rel, top)
            if rc is not None:
                return rc
        return backend.fetch(args, s, runtime, rel)
    src = os.path.join(s["project_path"], rel)
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


def describe_box(env):
    """Lines describing a project's box settings for doctor, plus problems."""
    box = env.get("box") or {}
    backend = str(box.get("backend") or "none").strip().lower()
    if backend == "none":
        return ["not configured (no box.backend)"], []
    if backend == "azure-blob":
        if not str(box.get("storage_account") or "").strip():
            return ["azure-blob, not configured yet (box.storage_account is empty)"], []
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
        return ["unknown backend %r" % backend], ["box.backend must be azure-blob, rclone or none"]
    lines.append("include  %s" % ", ".join(box.get("include") or []))
    lines.append("exclude  %s" % ", ".join(box.get("exclude") or []))
    if box.get("archives"):
        lines.append("archives %s (one bundle each; large files on their own)" % ", ".join(box.get("archives")))
    return lines, problems + archive.archive_problems(box)
