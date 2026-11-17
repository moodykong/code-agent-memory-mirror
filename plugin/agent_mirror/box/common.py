"""What every box backend shares: reading the settings and running the transfer program."""

import os
import subprocess

from agent_mirror.box.archive import DEFAULT_LARGE, DEFAULT_TOC_INLINE, archive_problems
from agent_mirror.box.local import min_free
from agent_mirror.box.rules import Rules, azure_rule_problems, clean_rel, generic_rule_problems
from agent_mirror.config import load_env_file, resolve_project
from agent_mirror.platforms import new_group_kwargs, stop_tree
from agent_mirror.util import err, exe_path, expand, out, scrub, tilde

DEFAULT_TRANSFER_TIMEOUT = 21600
INDEX_NAME = "box_index.jsonl"
AZURE_KEYS = ("storage_account", "inbound_container", "outbound_container", "prefix")
BACKENDS = ("azure-blob", "rclone", "none")


def box_context(args, ctx):
    """Return (settings, None), or (None, exit code) when there is nothing to do."""
    project, project_path = resolve_project(ctx, args.project, args.project_path)
    if getattr(args, "env_file", None):
        path = os.path.realpath(expand(args.env_file))
        env = load_env_file(path)
        project = project or os.path.basename(os.path.dirname(path))
    else:
        store = ctx.require_store()
        path = store.env_path(project) if project else None
        env = store.load_env(project) if project else {}
    if not env:
        err("no project env.json matched %s; pass --project (or --env-file)" % project_path)
        return None, 1
    box = env.get("box") or {}
    backend = str(box.get("backend") or "none").strip().lower()
    if backend == "none":
        out("box not configured: no box.backend in %s; nothing to do" % tilde(path))
        return None, 0
    if backend not in BACKENDS:
        err("box.backend %r in %s is not supported (use azure-blob, rclone or none)" % (backend, tilde(path)))
        return None, 2
    s = {
        "backend": backend,
        "project": project,
        "project_path": project_path,
        "env_path": path,
        "index_path": os.path.join(os.path.dirname(path), INDEX_NAME),
        "include": list(box.get("include") or []),
        "exclude": list(box.get("exclude") or []),
        "box": box,
    }
    if backend == "azure-blob":
        if not str(box.get("storage_account") or "").strip():
            out(
                "box not configured yet: box.storage_account is empty in %s "
                "(fill it once the storage account exists); nothing to do" % tilde(path)
            )
            return None, 0
        missing = [k for k in AZURE_KEYS if not str(box.get(k) or "").strip()]
        if missing:
            err("box settings in %s lack %s" % (tilde(path), ", ".join("box." + k for k in missing)))
            return None, 2
        prefix = clean_rel(str(box["prefix"]))
        if prefix is None:
            err("box.prefix in %s must be a relative path without '..'" % tilde(path))
            return None, 2
        problems = azure_rule_problems(box)
        s.update(
            {
                "account": str(box["storage_account"]).strip(),
                "inbound": str(box["inbound_container"]).strip(),
                "outbound": str(box["outbound_container"]).strip(),
                "prefix": prefix,
            }
        )
    else:
        inbound = str(box.get("inbound_remote") or "").strip()
        outbound = str(box.get("outbound_remote") or "").strip()
        if not inbound and not outbound:
            out(
                "box not configured yet: box.inbound_remote and box.outbound_remote are empty in %s; nothing to do"
                % tilde(path)
            )
            return None, 0
        missing = [k for k, v in (("inbound_remote", inbound), ("outbound_remote", outbound)) if not v]
        if missing:
            err("box settings in %s lack %s" % (tilde(path), ", ".join("box." + k for k in missing)))
            return None, 2
        problems = generic_rule_problems(box)
        s.update({"inbound": inbound, "outbound": outbound})
    problems = problems + archive_problems(box)
    if problems:
        for p in problems:
            err("%s (in %s)" % (p, tilde(path)))
        return None, 2
    large = box.get("archive_large_file_bytes")
    s.update(
        {
            "archives": [str(g).strip().strip("/") for g in box.get("archives") or []],
            "large_bytes": DEFAULT_LARGE if large is None else int(large),
            "toc_inline": int(box.get("toc_inline_bytes") or DEFAULT_TOC_INLINE),
            "min_free": min_free(box),
        }
    )
    s["rules"] = Rules(s["include"], s["exclude"])
    return s, None


def run_transfer(argv, timeout, capture=True, input_text=None):
    """Run az or rclone and never raise. Returns (returncode, stdout, stderr), secrets scrubbed.

    returncode is None when the program timed out or could not start. It runs
    in its own process group, so a timeout or Ctrl-C also stops its children
    (azcopy under az). This function never prints the command line.
    """
    pipe = subprocess.PIPE if capture else None
    cmd = [str(a) for a in argv]
    cmd[0] = exe_path(cmd[0])
    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
            stdout=pipe,
            stderr=pipe,
            encoding="utf-8",
            errors="replace",
            **new_group_kwargs(),
        )
    except OSError as exc:
        return None, "", scrub(exc)
    try:
        stdout, stderr = proc.communicate(input=input_text, timeout=timeout)
    except subprocess.TimeoutExpired:
        stop_tree(proc)
        return None, "", "timed out after %s s" % timeout
    except KeyboardInterrupt:
        stop_tree(proc)
        raise
    return proc.returncode, scrub(stdout), scrub(stderr)
