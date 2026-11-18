"""The box: bulk untracked files moved between developer machines and agents.

shared    one tree of durable files: developer machines upload (box push), each the folders it owns
          (owners.py); every machine and session reads it (box index, box ls, box fetch).
sessions  one namespace per session (sessions.py): box send writes into its own session; every machine
          and session can list and fetch them (box sessions, box fetch --session); a developer machine
          promotes what is worth keeping into the shared tree (box promote) and prunes the rest.

Backends: azure-blob (Azure CLI), rclone, or none. The older names of the two sides were inbound and
outbound; the config keys with those names still work.
"""

import os
import shlex
import shutil

from agent_mirror.box import archive, azure, browse, extras, owners, rclone, sessions
from agent_mirror.box.common import box_context, old_side_keys, side_setting
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
    by_session = action == "fetch" and getattr(args, "session", None)
    if action == "fetch" and not by_session and not args.path:
        err("box fetch takes a PATH (the shared tree), or --session ID [PATH] (a session's files)")
        return 2
    if action == "fetch" and not by_session and getattr(args, "into", None):
        err("--into goes with --session (a fetch from the shared tree writes into the project folder)")
        return 2
    if action in ("fetch", "send") and not by_session:
        rel = clean_rel(args.path)
        if rel is None:
            err("box %s takes a path relative to the project folder, without '..' (got %r)" % (action, args.path))
            return 2
        if action == "send" and is_env_file(rel.rsplit("/", 1)[-1]):
            err("refusing to upload %s: env files hold secrets" % rel)
            return 2
        if action == "send" and rel == sessions.MANIFEST:
            err("refusing to upload %s: that name is the session manifest" % rel)
            return 2
    if action == "send":
        problem = sessions.label_problem(getattr(args, "label", None))
        if problem:
            err(problem)
            return 2
    s, rc = box_context(args, ctx)
    if s is None:
        return rc
    backend = BACKEND_MODULES[s["backend"]]
    runtime = detect_runtime()
    reads_box = action in ("owners", "sessions", "promote", "prune", "release") or by_session
    reads_box = reads_box or (action == "send" and getattr(args, "archive", False))
    reads_box = reads_box or (action == "fetch" and not by_session and archive_unit_of(rel, s["archives"]))
    if reads_box and (shutil.which(sessions.TOOL_FOR_BACKEND[s["backend"]]) or action not in ("owners", "sessions")):
        if not backend.prepare(args, runtime):
            return 1  # (box owners and box sessions use the committed index when the program is missing)
    if action == "push":
        return owners.push(args, s, runtime, backend, ctx.plat)
    if action == "release":
        return owners.release(args, s, runtime, backend, ctx.plat)
    if action == "owners":
        return owners.cmd_owners(args, s, runtime, backend)
    if action == "sessions":
        return sessions.cmd_sessions(args, s, runtime)
    if action == "promote":
        return sessions.cmd_promote(args, s, runtime, backend, ctx.plat)
    if action == "prune":
        return sessions.cmd_prune(args, s, runtime)
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
    if action == "fetch" and by_session:
        return sessions.cmd_fetch(args, s, runtime)
    if action == "fetch":
        top = archive_unit_of(rel, s["archives"])
        if top:
            rc = archive.fetch_archive(args, s, runtime, side_for(s, "shared"), rel, top)
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
    return send(args, s, runtime, backend, rel)


def send(args, s, runtime, backend, rel):
    """box send: upload into this session's namespace, then record the files in its manifest."""
    side = side_for(s, "sessions")
    if not args.dry_run and not backend.ready(False):
        return 1  # before a session is started or announced
    session, created = sessions.current_session(
        s["project_path"], runtime, new=getattr(args, "new_session", False), label=getattr(args, "label", None), dry_run=args.dry_run
    )
    sid = session["id"]
    out(sessions.describe_send(side, sid, created, session.get("label")))
    info = None
    if getattr(args, "archive", False):
        rc, info = archive.send_archive(args, s, runtime, side, rel, sid)
        if rc == 0 and info is None and not args.dry_run:
            return 0  # nothing to upload
    else:
        rc = backend.send(args, s, runtime, rel, sid)
    if rc != 0:
        return rc
    if args.dry_run:
        out("would record the sent files in %s" % side.where("%s/%s" % (sid, sessions.MANIFEST)))
        return 0
    entries = sessions.sent_entries(s, rel, info)
    if not entries:
        return 0
    ok, problem, doc = sessions.update_manifest(
        side,
        sid,
        sessions.new_manifest(s, session, runtime),
        args.timeout,
        files=entries,
        label=session.get("label"),
        new=created,
    )
    if not ok:
        err("the files were uploaded, but the session manifest could not be updated: %s" % problem)
        return 1
    out(
        "session %s now lists %d file(s); other sessions list it with `agent-mirror box sessions` and fetch it "
        "with `%s`" % (sid, len(doc.get("files") or []), sessions.fetch_command(s, sid))
    )
    return 0


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
            "shared": side_setting(box, backend, "shared"),
            "sessions": side_setting(box, backend, "sessions"),
            "prefix": prefix,
        }
        lines = azure.describe(s)
        problems = azure_rule_problems(box)
    elif backend == "rclone":
        lines = rclone.describe(
            {"shared": side_setting(box, backend, "shared"), "sessions": side_setting(box, backend, "sessions")}
        )
        problems = generic_rule_problems(box)
    else:
        return ["unknown backend %r" % backend], ["box.backend must be azure-blob, rclone or none"], []
    lines.append("include  %s" % ", ".join(box.get("include") or []))
    lines.append("exclude  %s" % ", ".join(box.get("exclude") or []))
    if box.get("archives"):
        lines.append("archives %s (one bundle per matching folder; large files on their own)" % ", ".join(box.get("archives")))
    old = old_side_keys(box, backend)
    if old:
        lines.append(
            "note     the config uses the older key names %s; the new names are %s (both work)"
            % (", ".join(old), ", ".join(k.replace("inbound", "shared").replace("outbound", "sessions") for k in old))
        )
    problems = problems + archive.archive_problems(box)
    warnings = []
    if not problems:
        globs = [str(g).strip().strip("/") for g in box.get("archives") or []]
        for path in paths:
            if os.path.isdir(path):
                warnings += [w for w in archive_units(path, globs)[1] if w not in warnings]
    return lines, problems, warnings
