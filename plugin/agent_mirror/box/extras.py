"""box receive, box mount and unmount, and box rclone-env."""

import json
import os
import re
import shutil
import sys
import tempfile

from agent_mirror.box.local import disk_guard, ensure_git_exclude, human
from agent_mirror.box.rules import clean_rel
from agent_mirror.box.sides import side_for
from agent_mirror.platforms import normalize_platform
from agent_mirror.runtime import CLOUD_RUNTIMES
from agent_mirror.util import err, expand, out, run, scrub, short, show_cmd, warn, within


def project_folder(s, value, default):
    value = str(value or default)
    path = os.path.expanduser(value)
    return path if os.path.isabs(path) else os.path.join(s["project_path"], *value.replace("\\", "/").split("/"))


# receive --------------------------------------------------------------------------------------


def receive_dir(s, into=None):
    if into:
        return expand(into)
    return project_folder(s, s["box"].get("receive_dir"), "_agent_outbound")


def cmd_receive(args, s, runtime):
    if runtime in CLOUD_RUNTIMES:
        err("refusing box receive on %s: results are received on the developer machine" % runtime)
        return 2
    into = receive_dir(s, args.into)
    side = side_for(s, "outbound")
    if args.dry_run:
        out("dry run: nothing will change")
        out("would run: %s" % show_cmd(side.list_cmd("")))
        if hasattr(side, "copy_down_cmd"):
            out("then: %s" % show_cmd(side.copy_down_cmd(into)))
        else:
            out(
                "then download each file that is not in %s yet: %s" % (into, show_cmd(side.get_cmd("<path>", "<file>")))
            )
        out("(never overwriting existing local files)")
        return 0
    items = side.list("", args.transfer_timeout)
    if items is None:
        err("cannot list %s" % side.where())
        return 1
    safe = [it for it in items if clean_rel(it["path"] or "") == it["path"]]
    if len(safe) < len(items):
        warn("left out %d remote names that are not plain relative paths" % (len(items) - len(safe)))
    missing = [it for it in safe if not os.path.lexists(os.path.join(into, *it["path"].split("/")))]
    if not missing:
        out("nothing new in %s (%d file(s), all already in %s)" % (side.where(), len(safe), into))
        return 0
    need = sum(int(it.get("size") or 0) for it in missing)
    if not disk_guard(os.path.dirname(into) if not os.path.exists(into) else into, need, s["min_free"], args.force):
        return 1
    os.makedirs(into, exist_ok=True)
    if within(os.path.realpath(into), os.path.realpath(s["project_path"])):
        ensure_git_exclude(s["project_path"], into)
    result = side.copy_down(into, args.transfer_timeout, keep_existing=True)
    if result is not None:
        ok, detail = result
        if not ok:
            err("download failed: %s" % detail)
            return 1
    else:
        stage = tempfile.mkdtemp(prefix=".agent-mirror-receive-", dir=into)
        try:
            for it in missing:
                tmp = os.path.join(stage, "f")
                got = side.get(it["path"], tmp, args.transfer_timeout)
                if got != "ok":
                    err("cannot download %s: %s" % (it["path"], got))
                    return 1
                dest = os.path.join(into, *it["path"].split("/"))
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                if os.path.lexists(dest):
                    continue
                os.replace(tmp, dest)
        finally:
            shutil.rmtree(stage, ignore_errors=True)
    out(
        "received %d new file(s) (%s) into %s; kept %d already there"
        % (len(missing), human(need), into, len(safe) - len(missing))
    )
    return 0


# mount ------------------------------------------------------------------------------------------


def fuse_status(platform_name=None, exists=os.path.exists, which=shutil.which):
    """(looks possible, reason) for an rclone FUSE mount on this machine."""
    name = normalize_platform(platform_name or sys.platform)
    if name == "linux":
        if not exists("/dev/fuse"):
            return False, "/dev/fuse is missing (no FUSE on this machine or container)"
        prog = which("fusermount3") or which("fusermount")
        if not prog:
            return False, "no fusermount3 or fusermount program (install the fuse3 package)"
        return True, "/dev/fuse and %s found" % os.path.basename(prog)
    if name == "darwin":
        if exists("/Library/Filesystems/macfuse.fs"):
            return True, "macFUSE found (UNVERIFIED)"
        return False, "macFUSE is not installed"
    if name == "win32":
        for d in (r"C:\Program Files (x86)\WinFsp", r"C:\Program Files\WinFsp"):
            if exists(d):
                return True, "WinFsp found (UNVERIFIED)"
        return False, "WinFsp is not installed"
    return False, "mounting is not supported on %s" % name


def mount_dir(s):
    return project_folder(s, s["box"].get("mount_dir"), "_box")


def mount_cmd(remote, folder, cache_max_size, windows=False):
    cmd = ["rclone", "mount", remote, folder, "--read-only", "--vfs-cache-mode", "full"]
    cmd += ["--vfs-cache-max-size", cache_max_size]
    return cmd + ([] if windows else ["--daemon"])


def unmount_cmd(platform_name, folder, which=shutil.which):
    name = normalize_platform(platform_name)
    if name == "linux":
        prog = which("fusermount3") or which("fusermount") or "fusermount3"
        return [os.path.basename(prog), "-u", folder]
    if name == "darwin":
        return ["umount", folder]
    return None


def cmd_mount(args, s, plat):
    if s["backend"] != "rclone":
        err("box mount needs the rclone backend (box.backend is %s)" % s["backend"])
        return 2
    if not re.match(r"^[0-9]+(\.[0-9]+)?[KMGTP]?$", args.cache_max_size):
        err("--cache-max-size takes a size like 10G or 500M (got %r)" % args.cache_max_size)
        return 2
    folder = mount_dir(s)
    cmd = mount_cmd(s["inbound"], folder, args.cache_max_size, plat.windows)
    ok, reason = fuse_status(plat.name)
    if args.dry_run:
        out("dry run: nothing will change")
        out("FUSE: %s (%s)" % ("looks possible" if ok else "not available", reason))
        out("would run: %s" % show_cmd(cmd))
        out("(read-only; files download when opened and the cache stays under %s)" % args.cache_max_size)
        return 0
    if not ok:
        err("cannot mount: %s" % reason)
        return 1
    if plat.windows:
        err("on Windows run this yourself in its own window (UNVERIFIED): %s" % show_cmd(cmd))
        return 1
    if not shutil.which("rclone"):
        err("rclone is not installed or not on PATH")
        return 1
    os.makedirs(folder, exist_ok=True)
    if os.listdir(folder):
        err("%s is not empty (already mounted? run `agent-mirror box unmount`)" % folder)
        return 1
    out("running: %s" % show_cmd(cmd))
    rc, stdout, stderr = run(cmd, timeout=60)
    if rc != 0:
        err("rclone mount failed: %s" % short(stderr or stdout or "rc %s" % rc, 600))
        return 1
    if within(os.path.realpath(folder), os.path.realpath(s["project_path"])):
        ensure_git_exclude(s["project_path"], folder)
    out(
        "mounted %s read-only at %s (VFS cache up to %s; files download when opened)"
        % (s["inbound"], folder, args.cache_max_size)
    )
    return 0


def cmd_unmount(args, s, plat):
    folder = mount_dir(s)
    cmd = unmount_cmd(plat.name, folder)
    if cmd is None:
        err("on %s, stop the rclone mount process yourself" % plat.name)
        return 1
    if args.dry_run:
        out("dry run: nothing will change")
        out("would run: %s" % show_cmd(cmd))
        return 0
    rc, stdout, stderr = run(cmd, timeout=60)
    if rc != 0:
        err("unmount failed: %s" % short(stderr or stdout or "rc %s" % rc, 400))
        return 1
    out("unmounted %s" % folder)
    return 0


# rclone-env -------------------------------------------------------------------------------------

_PLAIN_ENV_VALUE = re.compile(r"^[A-Za-z0-9_./:@+,-]*$")


def quote_env_value(value):
    """Quote a value for a NAME=value line so that both a shell (`. file`) and a dotenv
    parser read it back unchanged. An OAuth token is JSON, full of double quotes; written
    bare, a shell strips those quotes and the token no longer parses."""
    value = str(value)
    if _PLAIN_ENV_VALUE.match(value):
        return value
    if "'" not in value and "\n" not in value:
        return "'%s'" % value
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$").replace("`", "\\`")
    return '"%s"' % escaped.replace("\n", "\\n")


def cmd_rclone_env(args, ctx):
    name = args.remote
    if not re.match(r"^[A-Za-z0-9_]+$", name):
        err("remote name %r cannot be written as environment variables: use letters, digits and _ only" % name)
        return 2
    if not shutil.which("rclone"):
        err("rclone is not installed or not on PATH")
        return 1
    rc, stdout, stderr = run(["rclone", "config", "dump"], timeout=60)
    if rc != 0:
        err("rclone config dump failed: %s" % short(scrub(stderr) or "rc %s" % rc, 300))
        return 1
    try:
        remotes = json.loads(stdout or "{}")
    except ValueError:
        err("rclone config dump did not print JSON")
        return 1
    remote = remotes.get(name)
    if remote is None:
        lowered = dict((k.lower(), k) for k in remotes)
        remote = remotes.get(lowered.get(name.lower(), ""))
    if not isinstance(remote, dict):
        err("no rclone remote named %s in the local rclone config (`rclone listremotes` shows them)" % name)
        return 1
    lines, bad = [], []
    for key in sorted(remote):
        value = str(remote[key])
        if "\n" in value or "\r" in value or not re.match(r"^[A-Za-z0-9_]+$", key):
            bad.append(key)
            continue
        lines.append(("RCLONE_CONFIG_%s_%s" % (name.upper(), key.upper()), value))
    for key in bad:
        warn("left out the setting %s: it cannot be one environment variable line" % key)
    if name != name.lower():
        warn(
            "a remote defined by these variables is named %s (lower case) on the other machine; use %s: "
            "in box.inbound_remote and box.outbound_remote there" % (name.lower(), name.lower())
        )
    if "token" in remote:
        expiry = ""
        try:
            expiry = json.loads(remote["token"]).get("expiry") or ""
        except (ValueError, AttributeError, TypeError):
            pass
        warn(
            "this remote uses an OAuth token. Stored in an environment variable it cannot be refreshed on a "
            "throwaway machine, so replace it before it expires%s" % (" (expiry %s)" % expiry if expiry else "")
        )
    if not args.out:
        out("variable names for remote %s (values not shown; add the names to env_var_names):" % name)
        for var, _ in lines:
            out("  " + var)
        out("write the values to an owner-only file with: agent-mirror box rclone-env --remote %s --out FILE" % name)
        return 0
    path = expand(args.out)
    store = ctx.store_or_none(quiet=True)
    if store and within(os.path.realpath(path), store.root):
        err("refusing to write secrets into the store (%s); pick a file outside it" % store.root)
        return 2
    if os.path.lexists(path):
        err("%s already exists; remove it first (it is never overwritten)" % path)
        return 2
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        for var, value in lines:
            fh.write("%s=%s\n" % (var, quote_env_value(value)))
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    out("wrote %d variable(s) for remote %s to %s (owner-only; values not shown):" % (len(lines), name, path))
    for var, _ in lines:
        out("  " + var)
    return 0
