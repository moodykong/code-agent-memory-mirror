"""probe: reachability and machine checks, mostly for a fresh cloud machine.

`probe` is quick: HTTPS and TCP checks plus machine facts.
`probe --full` is the long first-session report. Optional sections run only
when the project config has the settings they need. It only looks, except for
two small writes when a box is configured: `box fetch` of one small indexed
file into the project, and `box send` of one small note file.
"""

import getpass
import glob
import json
import os
import platform as pyplatform
import re
import shlex
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

from agent_mirror import PLUGIN_ROOT, __version__
from agent_mirror.box import describe_box
from agent_mirror.box.extras import fuse_status
from agent_mirror.links import is_linklike, link_target
from agent_mirror.projects import discover_projects
from agent_mirror.runtime import detect_runtime
from agent_mirror.session import command_argv
from agent_mirror.util import git, out, short, tilde

RUNTIME_VARS = ("CLAUDE_CODE_REMOTE", "CODEX_THREAD_ID", "CODEX_CI", "HTTPS_PROXY", "https_proxy", "ALL_PROXY")
TOOLS = ("python3", "python", "uv", "git", "az", "rclone", "gh", "node")


def http_probe(url, timeout):
    req = urllib.request.Request(url, headers={"User-Agent": "agent-mirror-probe"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return True, "HTTP %s" % resp.status
    except urllib.error.HTTPError as exc:
        return True, "HTTP %s (reachable)" % exc.code
    except Exception as exc:  # noqa: BLE001 (report any failure as FAIL)
        reason = getattr(exc, "reason", exc)
        return False, short("%s: %s" % (type(exc).__name__, reason), 160)


def tcp_probe(host, port, timeout):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, "connected"
    except Exception as exc:  # noqa: BLE001
        return False, short("%s: %s" % (type(exc).__name__, exc), 160)


def tool_version(cmd):
    exe = shutil.which(cmd[0])
    if not exe:
        return "MISSING"
    try:
        proc = subprocess.run(
            [exe] + list(cmd[1:]),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            encoding="utf-8",
            errors="replace",
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return "not available (%s)" % short(exc, 80)
    text = (proc.stdout or "").strip() or (proc.stderr or "").strip()
    return short(text.splitlines()[0], 70) if text else "rc %s" % proc.returncode


def memory_summary():
    try:
        if sys.platform.startswith("linux"):
            vals = {}
            with open("/proc/meminfo") as fh:
                for line in fh:
                    k, v = line.split(":", 1)
                    vals[k] = int(v.split()[0])
            return "%.1f GiB total, %.1f GiB available" % (
                vals["MemTotal"] / 1048576.0,
                vals.get("MemAvailable", 0) / 1048576.0,
            )
        if sys.platform == "darwin":
            proc = subprocess.run(["sysctl", "-n", "hw.memsize"], stdout=subprocess.PIPE, timeout=5)
            return "%.1f GiB total" % (int(proc.stdout.strip()) / 1073741824.0)
        if os.name == "nt":
            import ctypes

            class MemStatus(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            st = MemStatus()
            st.dwLength = ctypes.sizeof(MemStatus)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st))
            return "%.1f GiB total, %.1f GiB available" % (
                st.ullTotalPhys / 1073741824.0,
                st.ullAvailPhys / 1073741824.0,
            )
    except Exception:  # noqa: BLE001
        pass
    return "unknown"


def disk_free(path):
    try:
        du = shutil.disk_usage(path)
        return "%.1f GiB of %.1f GiB" % (du.free / 1073741824.0, du.total / 1073741824.0)
    except OSError as exc:
        return "unknown (%s)" % exc


def store_host_url(store):
    """https://<host> of the store's remote, for the default reachability check."""
    if store is None:
        return None
    rc, url, _ = git(store.root, ["remote", "get-url", "origin"], 5)
    if rc != 0:
        return None
    url = url.strip()
    m = re.match(r"^https?://(?:[^@/]*@)?([^/:]+)", url) or re.match(r"^(?:ssh://)?[^@]+@([^:/]+)", url)
    return "https://%s" % m.group(1) if m else None


def project_settings(ctx, args):
    """(store, discovery, [(project, clone path, env)]) for every project this session touches."""
    store = ctx.store_or_none(quiet=True)
    d = discover_projects(ctx, args.project, args.project_path)
    found = [(name, path, store.load_env(name) if store else {}) for name, path in d.matched]
    return store, d, found


def merged(found, key, sub=None):
    """Unique values of a list setting across projects (sub: a key inside a dict setting)."""
    values = []
    for _, _, env in found:
        source = (env.get(key) or {}).get(sub) if sub else env.get(key)
        for v in source or []:
            if v not in values:
                values.append(v)
    return values


def env_var_report(found):
    names = [str(n) for n in merged(found, "env_var_names")]
    return [n for n in names if os.environ.get(n)], [n for n in names if not os.environ.get(n)]


def cmd_probe(args, ctx):
    if args.full:
        return full_probe(args, ctx)
    return quick_probe(args, ctx)


def quick_probe(args, ctx):
    t = args.timeout
    plat = ctx.plat
    out("runtime: %s" % detect_runtime())
    store, d, found = project_settings(ctx, args)
    urls = []
    default = store_host_url(store)
    if default:
        urls.append(default)
    for u in merged(found, "probe", "https_urls") + list(args.url or []):
        if u not in urls:
            urls.append(u)
    tcp_hosts = merged(found, "probe", "tcp_hosts") + list(args.tcp or [])
    passed = failed = 0
    if not urls and not tcp_hosts:
        out("network: nothing to check (no store remote, no probe.https_urls or probe.tcp_hosts, no --url or --tcp)")
    for url in urls:
        ok, note = http_probe(url, t)
        passed, failed = passed + ok, failed + (not ok)
        out("%s  https %s  %s" % ("PASS" if ok else "FAIL", url, note))
    for spec in tcp_hosts:
        host, _, port = str(spec).rpartition(":")
        if not host or not port.isdigit():
            out("FAIL  tcp %s  expected host:port" % spec)
            failed += 1
            continue
        ok, note = tcp_probe(host, int(port), t)
        passed, failed = passed + ok, failed + (not ok)
        out("%s  tcp %s  %s" % ("PASS" if ok else "FAIL", spec, note))
    out("python: %s (%s)" % (sys.version.split()[0], sys.executable))
    out("uv --version: %s" % tool_version(["uv", "--version"]))
    out("disk free (home): %s" % disk_free(plat.home()))
    out("cpu count: %s" % os.cpu_count())
    out("memory: %s" % memory_summary())
    out("%s exists: %s" % (tilde(plat.claude_settings()), "yes" if os.path.isfile(plat.claude_settings()) else "no"))
    out("%s exists: %s" % (tilde(plat.codex_home()), "yes" if os.path.isdir(plat.codex_home()) else "no"))
    is_set, not_set = env_var_report(found)
    out("projects: %s" % (", ".join(d.names()) or "none matched"))
    out("env vars set (names only): %s" % (", ".join(is_set) or "none"))
    out("env vars not set: %s" % (", ".join(not_set) or "none"))
    out("probe: %d pass, %d fail" % (passed, failed))
    return 0


# probe --full ----------------------------------------------------------------------------------


def self_argv(ctx, *args):
    argv = [sys.executable, os.path.join(PLUGIN_ROOT, "bin", "agent-mirror")]
    if ctx.store_flag:
        argv += ["--store", ctx.store_flag]
    return argv + list(args)


def section(title):
    out("")
    out("===== %s =====" % title)


def tail_run(argv, limit, tail=12, cwd=None):
    """Run, print the last lines of the combined output and the exit code. Never raises."""
    try:
        proc = subprocess.run(
            argv,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            encoding="utf-8",
            errors="replace",
            timeout=limit,
        )
        text, rc = proc.stdout or "", proc.returncode
    except subprocess.TimeoutExpired as exc:
        text, rc = (exc.output or "") if isinstance(exc.output, str) else "", "timeout after %s s" % limit
    except OSError as exc:
        text, rc = str(exc), "could not start"
    for line in text.rstrip().splitlines()[-tail:]:
        out(line)
    out("(exit %s)" % rc)
    return rc


def full_probe(args, ctx):
    plat = ctx.plat
    store, d, found = project_settings(ctx, args)
    cwd = d.start if os.path.isdir(d.start) else os.getcwd()
    out("agent-mirror %s probe --full, projects: %s" % (__version__, ", ".join(d.names()) or "none matched"))

    section("1 where am I")
    out("runtime: %s" % detect_runtime())
    try:
        user = getpass.getuser()
    except Exception:  # noqa: BLE001
        user = "?"
    out("user: %s   home: %s   session folder: %s" % (user, plat.home(), cwd))
    d.report()
    for path in [p for _, p in d.matched] + d.unmatched:
        rc, origin, _ = git(path, ["remote", "get-url", "origin"], 5)
        origin = re.sub(r"https://[^@/]*@", "https://", origin.strip()) if rc == 0 else "none"
        rc, branch, _ = git(path, ["branch", "--show-current"], 5)
        out("git %s: %s @ %s" % (tilde(path), origin, branch.strip() if rc == 0 else "?"))
    out("cpu: %s   memory: %s   disk free here: %s" % (os.cpu_count(), memory_summary(), disk_free(cwd)))
    out("os: %s" % pyplatform.platform())

    section("2 what the setup script left behind")
    seen = False
    for f in sorted(set([os.path.join(plat.home(), "agent_setup_probe.txt"), "/root/agent_setup_probe.txt"])):
        if os.path.isfile(f):
            seen = True
            out("--- %s" % f)
            try:
                with open(f, encoding="utf-8", errors="replace") as fh:
                    out(fh.read().rstrip())
            except OSError as exc:
                out("cannot read: %s" % exc)
    if not seen:
        out("no agent_setup_probe.txt (no cloud setup script ran here)")
    if store:
        rc, log, _ = git(store.root, ["log", "--oneline", "-1"], 10)
        out("store: %s  %s" % (store.root, log.strip() if rc == 0 else "(not a git clone)"))
    else:
        out("store: none configured")
    out(
        "claude settings file: %s   codex home: %s"
        % (
            "yes" if os.path.isfile(plat.claude_settings()) else "no",
            "yes" if os.path.isdir(plat.codex_home()) else "no",
        )
    )

    section("3 tools")
    out("%-8s %s (%s)" % ("this", sys.version.split()[0], sys.executable))
    for tool in TOOLS:
        out("%-8s %s" % (tool, tool_version([tool, "--version"])))
    ok, reason = fuse_status()
    out("%-8s %s (%s)" % ("mount", "looks possible" if ok else "not possible", reason))
    for name, path, env in found:
        if env.get("python"):
            out(
                "%s: python %s via uv: %s"
                % (name, env["python"], tool_version(["uv", "python", "find", str(env["python"])]))
            )
        out("%s: .venv %s" % (name, "present" if os.path.isdir(os.path.join(path, ".venv")) else "absent"))

    section("4 environment variable names (set or missing, never values)")
    names = [str(n) for n in merged(found, "env_var_names")]
    if not names:
        out("no env_var_names in the project config")
    for n in names:
        out("%-36s %s" % (n, "set" if os.environ.get(n) else "MISSING"))
    for n in RUNTIME_VARS:
        out("%-36s %s" % (n, "set" if os.environ.get(n) else "-"))

    section("5 links and config (agent-mirror doctor)")
    if d.mode == "forced":
        where = ["--project", d.matched[0][0], "--project-path", d.matched[0][1]]
    else:
        where = ["--project-path", d.start]
    tail_run(self_argv(ctx, "doctor", *where), 60, tail=40)

    section("6 network")
    tail_run(self_argv(ctx, "probe", *where), 120, tail=25)
    for host in [str(h).rpartition(":")[0] for h in merged(found, "probe", "tcp_hosts")]:
        try:
            ok = bool(socket.getaddrinfo(host, None))
            out("  resolve %s: %s" % (host, "ok" if ok else "empty"))
        except Exception as exc:  # noqa: BLE001
            out("  resolve %s: FAIL %s" % (host, short(exc, 120)))

    section("7 memory")
    mems = sorted(glob.glob(os.path.join(plat.claude_home(), "projects", "*", "memory")))
    if not mems:
        out("claude memory folders: none")
    for m in mems[:8]:
        kind = "-> %s" % link_target(m) if is_linklike(m) else "real folder"
        try:
            n = len(os.listdir(m))
        except OSError:
            n = "?"
        out("  %s %s (%s files)" % (tilde(m), kind, n))
    if store:
        for name in d.names():
            idx = store.path("memory", store.memory_project(name), "MEMORY.md")
            if os.path.isfile(idx):
                with open(idx, encoding="utf-8", errors="replace") as fh:
                    out("%s index lines: %d (%s)" % (name, len(fh.read().splitlines()), idx))
            else:
                out("%s index: none yet" % name)
    tail_run(self_argv(ctx, "sync", "--dry-run"), 90, tail=6)

    section("8 box")
    if not found:
        out("skipped: no project matched")
    for name, path, env in found:
        lines, problems = describe_box(env)
        out("--- %s: %s" % (name, lines[0]))
        if lines[0].startswith("not configured") or "not configured yet" in lines[0]:
            continue
        for line in lines[1:]:
            out("  " + line)
        for p in problems:
            out("  PROBLEM " + p)
        box_args = ["--project", name, "--project-path", path]
        tail_run(self_argv(ctx, "box", "index", *box_args), 300, tail=4)
        tail_run(self_argv(ctx, "box", "status", *box_args), 60, tail=6)
        index_path = store.path("projects", name, "box_index.jsonl")
        small = small_indexed_file(index_path)
        if small:
            out("fetch %s" % small)
            tail_run(self_argv(ctx, "box", "fetch", small, *box_args), 180, tail=3)
        else:
            member = smallest_archived_file(index_path)
            if member:
                local = os.path.join(path, *member.split("/"))
                existed = os.path.lexists(local)
                out("fetch %s (the smallest file of an archived folder: a sub-path fetch)" % member)
                tail_run(self_argv(ctx, "box", "fetch", member, *box_args), 300, tail=3)
                if existed:
                    out("not evicted: %s was here before the fetch" % member)
                else:
                    tail_run(self_argv(ctx, "box", "evict", member, "--force", *box_args), 60, tail=2)
            else:
                out("no small file found in the index to fetch")
        note = ".agent_probe_note.txt"
        note_path = os.path.join(path, note)
        try:
            with open(note_path, "w", encoding="utf-8") as fh:
                fh.write(
                    "probe written %s from %s\n"
                    % (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), detect_runtime())
                )
            tail_run(self_argv(ctx, "box", "send", note, *box_args), 180, tail=3)
        except OSError as exc:
            out("could not write the probe note: %s" % exc)
        finally:
            try:
                os.remove(note_path)
            except OSError:
                pass
        tail_run(self_argv(ctx, "box", "push", "--dry-run", *box_args), 60, tail=3)

    section("9 project probe commands")
    if not any((env.get("probe") or {}).get("commands") for _, _, env in found):
        out("skipped: no probe.commands in the project config")
    for name, path, env in found:
        for item in (env.get("probe") or {}).get("commands") or []:
            label = item.get("name") if isinstance(item, dict) else None
            spec = item.get("run") if isinstance(item, dict) else item
            argv = command_argv(spec, store.root if store else "", path)
            out("--- %s, %s: %s" % (name, label or "command", " ".join(shlex.quote(a) for a in argv)))
            if argv and shutil.which(argv[0]) is None and not os.path.isfile(argv[0]):
                out("%s not found" % argv[0])
                continue
            tail_run(argv, 180, tail=6, cwd=path)

    section("10 dependency sync timing")
    if not any(env.get("dependency_sync") for _, _, env in found):
        out("skipped: no dependency_sync in the project config")
    for name, path, env in found:
        dep = env.get("dependency_sync")
        if not dep:
            continue
        argv = command_argv(dep, store.root if store else "", path)
        out("--- %s" % name)
        start = time.time()
        tail_run(argv, 1700, tail=3, cwd=path)
        out("dependency sync took %d s" % int(time.time() - start))

    section("done")
    return 0


def smallest_archived_file(index_path):
    """'<archive>/<path>' of the smallest bundled file in an archived folder's table of contents, or None."""
    from agent_mirror.box.archive import read_local_toc
    from agent_mirror.box.browse import load_index

    try:
        _, archives = load_index(index_path)
    except (OSError, ValueError):
        return None
    best = None
    for a in sorted(archives, key=lambda x: x.get("archive_bytes") or 0):
        toc = read_local_toc(os.path.dirname(index_path), a) or []
        for e in toc:
            if not isinstance(e, dict) or not e.get("path"):
                continue
            size = e.get("size") or 0
            rank = (0 if e.get("in") == "bundle" else 1, size)
            if best is None or rank < best[0]:
                best = (rank, "%s/%s" % (a["path"], e["path"]))
        if best:
            break
    return best[1] if best else None


def small_indexed_file(index_path):
    try:
        with open(index_path, encoding="utf-8") as fh:
            for i, line in enumerate(fh):
                if i > 20000:
                    break
                r = json.loads(line)
                if not isinstance(r, dict) or r.get("archive") or "writer" in r:
                    continue
                size = r.get("size") or 0
                if 0 < size < 20000 and str(r.get("path", "")).endswith((".md", ".txt", ".json")):
                    return r["path"]
    except (OSError, ValueError):
        pass
    return None
