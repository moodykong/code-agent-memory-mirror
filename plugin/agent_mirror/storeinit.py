"""init-store: make a new store from templates/store, or adopt an existing store folder.

A store is its own git repo that the user owns. It must not sit inside a
project repo, inside this tool, or inside an agent's home folder.
"""

import os
import shutil

from agent_mirror import DEFAULT_REF, PLUGIN_ROOT, STORE_SCHEMA, TOOL_REPO
from agent_mirror.config import dump_json, load_json_file, load_user_config, save_user_config
from agent_mirror.util import (
    err,
    expand,
    git,
    git_toplevel,
    norm,
    out,
    read_text,
    short,
    tilde,
    warn,
    within,
    write_text,
)

TEMPLATE = os.path.join(PLUGIN_ROOT, "templates", "store")
REQUIRED = (("AGENTS.md", "file"), ("skills", "folder"), ("memory", "folder"))


def nearest_existing(path):
    p = path
    while not os.path.exists(p):
        parent = os.path.dirname(p)
        if parent == p:
            break
        p = parent
    return p


def placement_problem(ctx, path):
    """A reason the store may not live at path, or None."""
    real = os.path.realpath(path)
    tool_repo = os.path.dirname(PLUGIN_ROOT)
    if within(real, os.path.realpath(tool_repo)):
        return "%s is inside the agent-mirror program folder %s" % (path, tool_repo)
    p = ctx.plat
    for home in (p.claude_home(), p.codex_home(), p.agents_dir()):
        if within(real, os.path.realpath(home)):
            return "%s is inside the agent home folder %s; links from there into the store would loop" % (path, home)
    top = git_toplevel(nearest_existing(real), timeout=10)
    if top and norm(top) != norm(real):
        return "%s is inside the git repo %s; a store must be its own repo, " "never part of a project repo" % (
            path,
            top,
        )
    return None


def default_meta():
    return {"schema": STORE_SCHEMA, "tool": {"repo": TOOL_REPO, "ref": DEFAULT_REF}, "cloud_push": "inbox"}


def record_in_config(ctx, path, remote, dry_run):
    cfg = load_user_config(ctx.plat)
    old = cfg.get("store_path")
    cfg["store_path"] = path
    if remote:
        cfg["store_remote"] = remote
    cfg.setdefault("agents", ["claude", "codex"])
    cfg.setdefault("projects", {})
    where = tilde(ctx.plat.config_file())
    if dry_run:
        out("would write store_path %s to %s" % (path, where))
        return
    save_user_config(ctx.plat, cfg)
    if old and norm(expand(old)) != norm(path):
        out("config: store_path changed from %s to %s in %s" % (old, path, where))
    else:
        out("config: store_path %s in %s" % (path, where))


def git_ident():
    return ["-c", "user.name=agent-mirror", "-c", "user.email=agent-mirror@localhost"]


def create(ctx, path, args):
    if os.path.exists(path) and not os.path.isdir(path):
        err("%s exists and is not a folder" % path)
        return 2
    if os.path.isdir(path) and os.listdir(path):
        if all(os.path.exists(os.path.join(path, n)) for n, _ in REQUIRED):
            err("%s already looks like a store; use --adopt to use it as it is" % path)
        else:
            err("%s is not empty; pick an empty or new folder (or --adopt an existing store)" % path)
        return 2
    if args.dry_run:
        out("dry run: nothing will change")
        out("would copy the store skeleton from %s to %s" % (TEMPLATE, path))
        out(
            "would run: git init, then set the branch to main%s"
            % (" and add origin " + args.remote if args.remote else "")
        )
        if args.commit:
            out("would commit the skeleton")
        record_in_config(ctx, path, args.remote, True)
        return 0
    shutil.copytree(TEMPLATE, path, dirs_exist_ok=True)
    meta_path = os.path.join(path, "store.json")
    if not os.path.isfile(meta_path):
        write_text(meta_path, dump_json(default_meta()))
    out("created the store skeleton in %s" % path)
    for args_ in (["init", "-q"], ["symbolic-ref", "HEAD", "refs/heads/main"]):
        rc, _, stderr = git(path, args_, 30)
        if rc != 0:
            err("git %s failed: %s" % (" ".join(args_), short(stderr)))
            return 1
    out("git: initialised %s on branch main" % path)
    if args.remote:
        rc, _, stderr = git(path, ["remote", "add", "origin", args.remote], 30)
        if rc != 0:
            err("git remote add failed: %s" % short(stderr))
            return 1
        out("git: origin is %s" % args.remote)
    if args.commit:
        git(path, ["add", "-A"], 30)
        rc_e, email, _ = git(path, ["config", "user.email"], 10)
        ident = git_ident() if rc_e != 0 or not email.strip() else []
        rc, _, stderr = git(path, ident + ["commit", "-q", "-m", "store: initial layout from agent-mirror"], 30)
        if rc != 0:
            err("git commit failed: %s" % short(stderr))
            return 1
        out("git: committed the skeleton")
    record_in_config(ctx, path, args.remote, False)
    out("next: edit %s, then run `agent-mirror install --dry-run`" % os.path.join(path, "AGENTS.md"))
    if args.remote:
        out("      and push once by hand: git -C %s push -u origin main" % path)
    return 0


def adopt(ctx, path, args):
    if not os.path.isdir(path):
        err("%s is not a folder" % path)
        return 2
    missing = []
    for name, kind in REQUIRED:
        p = os.path.join(path, name)
        ok = os.path.isfile(p) if kind == "file" else os.path.isdir(p)
        if not ok:
            missing.append("%s (%s)" % (name, kind))
    if missing:
        err("%s is not a store yet: missing %s" % (path, ", ".join(missing)))
        return 2
    if args.dry_run:
        out("dry run: nothing will change")
    meta_path = os.path.join(path, "store.json")
    if os.path.isfile(meta_path):
        meta = load_json_file(meta_path, "store file")
        out("store.json: schema %s, tool ref %s" % (meta.get("schema"), (meta.get("tool") or {}).get("ref")))
        if isinstance(meta.get("schema"), int) and meta["schema"] > STORE_SCHEMA:
            warn(
                "store.json schema %d is newer than this agent-mirror understands (%d)" % (meta["schema"], STORE_SCHEMA)
            )
        if "cloud_push" not in meta:
            if args.dry_run:
                out('would add "cloud_push": "inbox" to %s' % meta_path)
            else:
                meta["cloud_push"] = "inbox"
                write_text(meta_path, dump_json(meta))
                out('added "cloud_push": "inbox" to %s: cloud sessions push to inbox branches (commit it)' % meta_path)
    elif args.dry_run:
        out("would write %s" % meta_path)
    else:
        write_text(meta_path, dump_json(default_meta()))
        out("wrote %s (commit it)" % meta_path)
    attrs = os.path.join(path, ".gitattributes")
    if not os.path.isfile(attrs) or "merge=union" not in read_text(attrs):
        warn(
            "%s lacks the line `memory/**/MEMORY.md merge=union`; without it two machines adding "
            "index lines at once conflict" % attrs
        )
    top = git_toplevel(path, 10)
    if not top:
        warn("%s is not a git clone; sync needs git (git init, add a remote, push)" % path)
    else:
        rc, origin, _ = git(path, ["remote", "get-url", "origin"], 10)
        if args.remote:
            if rc != 0:
                if args.dry_run:
                    out("would add origin %s" % args.remote)
                else:
                    git(path, ["remote", "add", "origin", args.remote], 10)
                    out("git: origin is %s" % args.remote)
            elif origin.strip() != args.remote:
                warn("origin is %s, not %s; left as it is" % (origin.strip(), args.remote))
        elif rc != 0:
            warn("the store has no origin remote; sync will commit locally only")
    record_in_config(ctx, path, args.remote, args.dry_run)
    return 0


def cmd_init_store(args, ctx):
    path = os.path.realpath(expand(args.path))
    problem = placement_problem(ctx, path)
    if problem:
        err("refusing: " + problem)
        return 2
    if args.adopt:
        return adopt(ctx, path, args)
    return create(ctx, path, args)


def describe_template():
    """Files a new store starts with (for docs and tests)."""
    found = []
    for root, _, files in os.walk(TEMPLATE):
        for f in files:
            found.append(os.path.relpath(os.path.join(root, f), TEMPLATE).replace(os.sep, "/"))
    return sorted(found)
