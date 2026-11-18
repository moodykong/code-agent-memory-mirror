"""Shared fixtures for the agent-mirror tests (standard library only).

SAFETY: every subprocess gets AGENT_MIRROR_HOME (every home-like location is derived
from it) and AGENT_MIRROR_TEST_SANDBOX (the program exits with code 97 when any location
is outside it), plus HOME, USERPROFILE, APPDATA, LOCALAPPDATA, XDG_* and TEMP/TMP pointing
inside the sandbox; CLAUDE_CONFIG_DIR and CODEX_HOME are removed. Before any test runs,
importing this module starts `agent-mirror debug paths` in a sandbox and aborts the whole
test process when a single location is outside it. In-process tests use a Platform
activated on a sandboxed environment, never the real one.

Fake `az`, `rclone` and clipboard programs stand in for the real ones (a Python script,
plus a .cmd shim on Windows); nothing reaches a network or a cloud account.
"""

import atexit

import glob
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.realpath(os.path.abspath(__file__))))
PLUGIN = os.path.join(REPO, "plugin")
TOOL = os.path.join(PLUGIN, "bin", "agent-mirror")
TEMPLATE_STORE = os.path.join(PLUGIN, "templates", "store")

if PLUGIN not in sys.path:
    sys.path.insert(0, PLUGIN)

DROP_PREFIXES = ("GIT_", "AZURE_", "FAKE_", "RCLONE_", "AGENT_MIRROR_", "XDG_")
DROP_NAMES = (
    "AGENT_RUNTIME",
    "CLAUDE_CODE_REMOTE",
    "CLAUDECODE",
    "CODEX_THREAD_ID",
    "CODEX_SESSION_ID",
    "CODEX_CI",
    "CLAUDE_PROJECT_DIR",
    "CLAUDE_CONFIG_DIR",
    "CODEX_HOME",
    "APPDATA",
    "LOCALAPPDATA",
    "HOME",
    "USERPROFILE",
    "HOMEDRIVE",
    "HOMEPATH",
    "TEMP",
    "TMP",
    "TMPDIR",
)
SECRETISH = re.compile(r"(SECRET|TOKEN|PASSWORD|PASS|KEY)$|SECRET", re.I)

STORE_INDEX = "- [Shared](shared.md): shared memory\n"
SHARED_BODY = "---\nname: shared\ndescription: shared\nmetadata:\n  type: project\n---\nshared body\n"
LOCAL_INDEX = "- [Local](local_only.md): local memory\n"

DEMO_BOX = {
    "backend": "azure-blob",
    "storage_account": "testacct",
    "inbound_container": "in-box",
    "outbound_container": "out-box",
    "prefix": "demo",
    "include": ["temporary/**", "docs/slides/**", "figures/**"],
    "exclude": ["temporary/downloads/**", "temporary/scratch/**", "**/__pycache__/**", "**/.env", "**/*.env"],
}


def remove_tree(path):
    """Delete a test folder completely. On Windows, git object files are read-only and shutil.rmtree with
    ignore_errors would silently leave them (and the folder) behind, so clear the attribute and retry."""

    def retry(func, target, _exc):
        try:
            os.chmod(target, stat.S_IWRITE)
            func(target)
        except OSError:
            pass

    if not os.path.lexists(path):
        return
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=retry)
    else:
        shutil.rmtree(path, onerror=retry)


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def sandbox_env(home, extra=None, sandbox=None):
    """The environment for one program run: everything home-like points inside the sandbox.

    extra overrides values; a value of None removes the variable.
    """
    sandbox = sandbox or os.path.dirname(home)
    tmp = os.path.join(sandbox, "tmp")
    os.makedirs(tmp, exist_ok=True)
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in DROP_NAMES and not k.startswith(DROP_PREFIXES) and not SECRETISH.search(k)
    }
    env.update(
        {
            "AGENT_MIRROR_HOME": home,
            "AGENT_MIRROR_TEST_SANDBOX": sandbox,
            "HOME": home,
            "USERPROFILE": home,
            "APPDATA": os.path.join(home, "AppData", "Roaming"),
            "LOCALAPPDATA": os.path.join(home, "AppData", "Local"),
            "XDG_CONFIG_HOME": os.path.join(home, ".config"),
            "XDG_CACHE_HOME": os.path.join(home, ".cache"),
            "TEMP": tmp,
            "TMP": tmp,
            "TMPDIR": tmp,
            "GIT_CONFIG_NOSYSTEM": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    for k, v in (extra or {}).items():
        if v is None:
            env.pop(k, None)
        else:
            env[k] = v
    return env


def clean_env(home, extra=None):
    return sandbox_env(home, extra)


def path_inside(path, root):
    """The test suite's own check, independent of the program's."""
    p = os.path.normcase(os.path.realpath(path))
    r = os.path.normcase(os.path.realpath(root))
    return p == r or p.startswith(r.rstrip(os.sep) + os.sep)


_GUARD_DONE = []


def require_sandbox():
    """Abort the whole test process unless the program resolves every location inside a sandbox."""
    if _GUARD_DONE:
        return
    base = os.path.realpath(tempfile.mkdtemp(prefix="agent-mirror-guard-"))
    problem = None
    try:
        home = os.path.join(base, "home")
        os.makedirs(home)
        try:
            proc = subprocess.run(
                [sys.executable, TOOL, "debug", "paths"],
                cwd=base,
                env=sandbox_env(home, sandbox=base),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                universal_newlines=True,
                timeout=120,
            )
            data = json.loads(proc.stdout)
            outside = [k for k, v in data["locations"].items() if not path_inside(v, base)]
            if proc.returncode != 0 or outside or data.get("outside_sandbox"):
                problem = "locations outside the sandbox: %s (rc %s)" % (
                    ", ".join(outside or data.get("outside_sandbox") or ["?"]),
                    proc.returncode,
                )
        except Exception as exc:  # noqa: BLE001 (any doubt aborts)
            problem = "the sandbox check could not run: %s" % exc
    finally:
        remove_tree(base)
    if problem:
        sys.stderr.write("\nREFUSING TO RUN ANY TEST: %s\n" % problem)
        sys.stderr.flush()
        os._exit(97)
    _GUARD_DONE.append(True)


def setUpModule():
    require_sandbox()


def can_symlink():
    probe = tempfile.mkdtemp(prefix="agent-mirror-symlink-probe-")
    try:
        os.symlink(probe, os.path.join(probe, "link"), target_is_directory=True)
        return True
    except (OSError, NotImplementedError):
        return False
    finally:
        remove_tree(probe)


SYMLINKS = can_symlink()  # Windows needs Developer Mode or admin rights for symlinks; never assume either way


def dir_link(target, link):
    """A directory link for a test fixture: a symlink, else (Windows) a junction."""
    try:
        os.symlink(target, link, target_is_directory=True)
    except (OSError, NotImplementedError):
        from agent_mirror.links import make_junction

        make_junction(link, target)


def readlink(path):
    """Where a symlink or junction points, absolute, without the Windows \\\\?\\ prefix."""
    from agent_mirror.links import link_target

    return link_target(path)


def is_link(path):
    from agent_mirror.links import is_linklike

    return is_linklike(path)


def memory_dir(home, project_path):
    return os.path.join(home, ".claude", "projects", re.sub(r"[^A-Za-z0-9]", "-", project_path), "memory")


def make_store(base, name="store"):
    """A small store: template files, two skills, a demo memory and a demo project with a box."""
    store = os.path.join(base, name)
    os.makedirs(store)
    for n in ("AGENTS.md", "CLAUDE.md", ".gitignore", ".gitattributes", "store.json"):
        shutil.copy2(os.path.join(TEMPLATE_STORE, n), os.path.join(store, n))
    for skill in ("alpha", "beta"):
        write(os.path.join(store, "skills", skill, "SKILL.md"), "---\nname: %s\ndescription: test\n---\nbody\n" % skill)
    write(os.path.join(store, "memory", "demo", "MEMORY.md"), STORE_INDEX)
    write(os.path.join(store, "memory", "demo", "shared.md"), SHARED_BODY)
    marker_cmd = [sys.executable, "-c", "open('dep_sync_ran.marker', 'w').close()"]
    env = {
        "repo": "Example/demo",
        "memory_project": "demo",
        "dependency_sync": " ".join(shlex.quote(c) for c in marker_cmd),
        "env_var_names": ["DEMO_API_URL"],
        "box": dict(DEMO_BOX),
    }
    write(os.path.join(store, "projects", "demo", "env.json"), json.dumps(env, indent=2))
    return store


def make_home(base, project_path):
    """A fake home with pre-existing user files, skills and a real Claude memory folder."""
    h = os.path.join(base, "home")
    write(os.path.join(h, ".claude", "CLAUDE.md"), "old claude user file\n")
    write(os.path.join(h, ".claude", "skills", "alpha", "SKILL.md"), "old claude alpha\n")
    write(os.path.join(h, ".claude", "skills", "synced", "vendor", "SKILL.md"), "vendor skill\n")
    plugin = os.path.join(base, "otherproject", "plugin")
    os.makedirs(plugin)
    dir_link(plugin, os.path.join(h, ".claude", "skills", "projlink"))
    write(os.path.join(h, ".codex", "AGENTS.md"), "")
    write(os.path.join(h, ".agents", "skills", "alpha", "SKILL.md"), "old agents alpha\n")
    mem = memory_dir(h, project_path)
    write(os.path.join(mem, "MEMORY.md"), LOCAL_INDEX)
    write(os.path.join(mem, "shared.md"), SHARED_BODY)
    write(os.path.join(mem, "local_only.md"), "local only memory\n")
    write(
        os.path.join(h, ".gitconfig"),
        "[user]\n\tname = Test User\n\temail = tester@example.com\n"
        "[commit]\n\tgpgsign = false\n[init]\n\tdefaultBranch = main\n",
    )
    return h


# A stand-in for the Azure CLI. It logs each call (never stdin) to FAKE_AZ_LOG
# and acts out the few storage commands agent-mirror uses, against the blob
# list in FAKE_AZ_BLOBS.
FAKE_AZ = r"""
import fnmatch, json, os, shutil, sys

argv = sys.argv[1:]
entry = {"argv": argv}


def run_hooks(dest):
    # FAKE_HOOKS (a JSON list) plays another machine: after an upload whose stored path ends with
    # rule["after_put"], copy rule["copy_from"] to rule["copy_to"], at most rule["times"] times (default 1).
    spec = os.environ.get("FAKE_HOOKS")
    if not spec:
        return
    with open(spec) as fh:
        rules = json.load(fh)
    target = dest.replace(os.sep, "/")
    for i, r in enumerate(rules):
        if not target.endswith(r["after_put"]):
            continue
        counter = "%s.%d.count" % (spec, i)
        n = 0
        if os.path.exists(counter):
            with open(counter) as fh:
                n = int(fh.read() or 0)
        if n >= r.get("times", 1):
            continue
        with open(counter, "w") as fh:
            fh.write(str(n + 1))
        os.makedirs(os.path.dirname(r["copy_to"]), exist_ok=True)
        shutil.copyfile(r["copy_from"], r["copy_to"])


def opt(name, default=None):
    for i, a in enumerate(argv):
        if a == name and i + 1 < len(argv):
            return argv[i + 1]
    return default


def finish(code=0, stdout=None):
    with open(os.environ["FAKE_AZ_LOG"], "a") as fh:
        fh.write(json.dumps(entry) + "\n")
    if stdout is not None:
        sys.stdout.write(stdout + "\n")
    sys.exit(code)


blobs = []
if os.environ.get("FAKE_AZ_BLOBS"):
    with open(os.environ["FAKE_AZ_BLOBS"]) as fh:
        blobs = sorted(json.load(fh), key=lambda b: b["name"])
# FAKE_AZ_ROOT, when set, is a folder standing in for the containers: uploads land there
root = os.environ.get("FAKE_AZ_ROOT")
container = opt("--container-name") or opt("--source") or opt("--destination")


def stored_path(name):
    return os.path.join(root, container, *name.split("/")) if root else None


if root and container:
    base = os.path.join(root, container)
    for r, _, fs in os.walk(base):
        for f in fs:
            full = os.path.join(r, f)
            name = os.path.relpath(full, base).replace(os.sep, "/")
            blobs.append({"name": name, "size": os.path.getsize(full), "modtime": "t", "stored": True})
    blobs.sort(key=lambda b: b["name"])

if argv[:3] == ["keyvault", "secret", "show"]:
    vault = json.loads(os.environ.get("FAKE_AZ_KEYVAULT", "{}"))
    value = vault.get(opt("--name"))
    if value is None:
        sys.stderr.write("ERROR: SecretNotFound\n")
        finish(3)
    finish(0, value)
if argv[:2] == ["account", "show"]:
    finish(int(os.environ.get("FAKE_AZ_ACCOUNT_SHOW_RC", "0")))
if argv[:1] == ["login"]:
    if "--password=@-" in argv:
        entry["stdin_is_secret"] = sys.stdin.read() == os.environ.get("FAKE_AZ_SECRET")
    if os.environ.get("FAKE_AZ_LOGIN_FAIL"):
        sys.stderr.write("AADSTS7000215: invalid secret " + os.environ.get("AZURE_CLIENT_SECRET", "") + "\n")
        finish(1)
    finish(0)
if argv[:3] == ["storage", "blob", "list"]:
    hits = [b for b in blobs if b["name"].startswith(opt("--prefix", ""))]
    if opt("--query") == "[].name":
        finish(0, json.dumps([b["name"] for b in hits]))
    if opt("--num-results") == "*":
        finish(0, json.dumps([{"name": b["name"], "size": b["size"], "modtime": b["modtime"]} for b in hits]))
    start, size = int(opt("--marker", "0")), int(opt("--num-results"))
    items = [dict(b, next=None) for b in hits[start : start + size]]
    nxt = str(start + size) if start + size < len(hits) else None
    items.append({"name": None, "size": None, "modtime": None, "next": nxt})
    finish(0, json.dumps(items))
if argv[:3] == ["storage", "blob", "download-batch"]:
    for b in blobs:
        if fnmatch.fnmatchcase(b["name"], opt("--pattern")):
            path = os.path.join(opt("--destination"), b["name"])
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as fh:
                fh.write("remote " + b["name"])
    finish(0)
if argv[:3] == ["storage", "blob", "download"]:
    name = opt("--name")
    src = stored_path(name)
    if src and os.path.isfile(src):
        import shutil
        shutil.copyfile(src, opt("--file"))
        finish(0)
    if any(b["name"] == name for b in blobs):
        with open(opt("--file"), "w") as fh:
            fh.write("remote " + name)
        finish(0)
    sys.stderr.write("ErrorCode:BlobNotFound The specified blob does not exist.\n")
    finish(3)
if argv[:3] == ["storage", "blob", "upload"] and root:
    dest = stored_path(opt("--name"))
    if os.path.exists(dest) and opt("--overwrite") == "false":
        sys.stderr.write("ErrorCode:BlobAlreadyExists\n")
        finish(1)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    shutil.copyfile(opt("--file"), dest)
    run_hooks(dest)
    finish(0)
if argv[:3] == ["storage", "blob", "upload-batch"]:
    src = opt("--source")
    entry["files"] = sorted(
        os.path.relpath(os.path.join(r, f), src).replace(os.sep, "/") for r, _, fs in os.walk(src) for f in fs
    )
    finish(0, str(len(entry["files"])))
finish(0)
"""

# A stand-in for rclone: logs argv to FAKE_RCLONE_LOG and acts out lsjson,
# lsjson --stat, cat, copy and copyto against FAKE_RCLONE_FILES (paths under the
# inbound remote) and, when set, FAKE_RCLONE_ROOT: a folder standing in for every
# remote ("name:path" is <root>/name/path), where uploads land and downloads come from.
FAKE_RCLONE = r"""
import json, os, re, shutil, sys

argv = sys.argv[1:]
entry = {"argv": argv}


def run_hooks(dest):
    # FAKE_HOOKS (a JSON list) plays another machine: after an upload whose stored path ends with
    # rule["after_put"], copy rule["copy_from"] to rule["copy_to"], at most rule["times"] times (default 1).
    spec = os.environ.get("FAKE_HOOKS")
    if not spec:
        return
    with open(spec) as fh:
        rules = json.load(fh)
    target = dest.replace(os.sep, "/")
    for i, r in enumerate(rules):
        if not target.endswith(r["after_put"]):
            continue
        counter = "%s.%d.count" % (spec, i)
        n = 0
        if os.path.exists(counter):
            with open(counter) as fh:
                n = int(fh.read() or 0)
        if n >= r.get("times", 1):
            continue
        with open(counter, "w") as fh:
            fh.write(str(n + 1))
        os.makedirs(os.path.dirname(r["copy_to"]), exist_ok=True)
        shutil.copyfile(r["copy_from"], r["copy_to"])


def finish(code=0, stdout=None, stderr=None):
    with open(os.environ["FAKE_RCLONE_LOG"], "a") as fh:
        fh.write(json.dumps(entry) + "\n")
    if stdout is not None:
        sys.stdout.write(stdout + "\n")
    if stderr is not None:
        sys.stderr.write(stderr + "\n")
    sys.exit(code)


files = []
if os.environ.get("FAKE_RCLONE_FILES"):
    with open(os.environ["FAKE_RCLONE_FILES"]) as fh:
        files = json.load(fh)
inbound = os.environ.get("FAKE_RCLONE_INBOUND", "")
root = os.environ.get("FAKE_RCLONE_ROOT")


def is_remote(text):
    return bool(re.match(r"^[A-Za-z0-9_-]{2,}:", text or ""))


def stored(remote):
    if not root or not is_remote(remote):
        return None
    name, _, path = remote.partition(":")
    return os.path.join(root, name, *[p for p in path.split("/") if p])


def rel_of(remote):
    return remote[len(inbound):].lstrip("/") if remote.startswith(inbound) else None


def stored_files():
    base = stored(inbound) if inbound else None
    found = []
    if base and os.path.isdir(base):
        for r, _, fs in os.walk(base):
            for f in fs:
                found.append(os.path.relpath(os.path.join(r, f), base).replace(os.sep, "/"))
    return found


all_files = sorted(set(files) | set(stored_files()))

if argv[:1] == ["lsjson"] and "--stat" in argv:
    rel = rel_of(argv[-1])
    if rel in all_files:
        finish(0, json.dumps({"Path": rel, "IsDir": False, "Size": 3}))
    if any(f.startswith(rel + "/") for f in all_files):
        finish(0, json.dumps({"Path": rel, "IsDir": True, "Size": -1}))
    finish(3, stderr="error: object not found")
if argv[:1] == ["lsjson"]:
    rel = rel_of(argv[-1]) or ""
    under = [f[len(rel) + 1:] if rel else f for f in all_files if not rel or f.startswith(rel + "/")]
    finish(0, json.dumps([{"Path": f, "Name": f.rsplit("/", 1)[-1], "Size": 1, "ModTime": "2026-01-01T00:00:00Z", "IsDir": False} for f in under]))
if argv[:2] == ["config", "dump"]:
    finish(0, os.environ.get("FAKE_RCLONE_CONFIG", "{}"))
if argv[:1] == ["cat"]:
    src = stored(argv[1])
    if src and os.path.isfile(src):
        with open(src) as fh:
            finish(0, fh.read())
    finish(3, stderr="error: directory not found")
if argv[:1] in (["copy"], ["copyto"]) and "--files-from" in argv:
    with open(argv[argv.index("--files-from") + 1]) as fh:
        entry["files_from"] = fh.read().split()
if argv[:1] == ["copyto"] and not is_remote(argv[1]) and stored(argv[2]):
    dest = stored(argv[2])
    if not ("--ignore-existing" in argv and os.path.exists(dest)):
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.copyfile(argv[1], dest)
        run_hooks(dest)
if argv[:1] == ["copyto"] and inbound and argv[1].startswith(inbound):
    dest = argv[2]
    src = stored(argv[1])
    rel = rel_of(argv[1])
    if not (src and os.path.isfile(src)) and rel not in files:
        finish(3, stderr="error: object not found")
    if not ("--ignore-existing" in argv and os.path.exists(dest)):
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        if src and os.path.isfile(src):
            shutil.copyfile(src, dest)
        else:
            with open(dest, "w") as fh:
                fh.write("remote " + rel)
if argv[:1] == ["copy"] and inbound and argv[1].startswith(inbound):
    rel = rel_of(argv[1])
    for f in files:
        if f.startswith(rel + "/"):
            dest = os.path.join(argv[2], f[len(rel) + 1:])
            if "--ignore-existing" in argv and os.path.exists(dest):
                continue
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest, "w") as fh:
                fh.write("remote " + f)
finish(0)
"""


def install_fake(base, name, source, folder="fakebin"):
    """A fake program `name` in base/<folder>: a Python script, run directly on POSIX, through a .cmd shim on Windows."""
    fakebin = os.path.join(base, folder)
    script = os.path.join(fakebin, name + ".py")
    if not os.path.isfile(script):
        write(script, source)
        if os.name == "nt":
            with open(os.path.join(fakebin, name + ".cmd"), "w", newline="\r\n") as fh:
                fh.write('@"%s" "%%~dp0%s.py" %%*\n' % (sys.executable, name))
        else:
            write(os.path.join(fakebin, name), "#!%s\n%s" % (sys.executable, source))
            os.chmod(os.path.join(fakebin, name), 0o755)
    return fakebin


class ToolTest(unittest.TestCase):
    """Base class: a fake home, a demo project folder and a store, all under one temp folder."""

    def setUp(self):
        self.base = os.path.realpath(tempfile.mkdtemp(prefix="agent-mirror-test-"))
        self.addCleanup(remove_tree, self.base)
        self.project = os.path.join(self.base, "work", "demo")
        os.makedirs(self.project)
        self.store = make_store(self.base)
        self.home = make_home(self.base, self.project)

    # helpers -------------------------------------------------------------------

    def assertLinked(self, link, target):
        """link points at target: a symlink or junction, or (only where symlinks are unavailable) a managed copy."""
        if is_link(link):
            self.assertEqual(os.path.normcase(readlink(link)), os.path.normcase(target), link)
            return
        self.assertFalse(SYMLINKS, "%s is not a link although symlinks work on this machine" % link)
        self.assertTrue(os.path.isfile(link) and os.path.isfile(target), "%s is neither a link nor a copy" % link)
        self.assertEqual(read(link), read(target))

    def run_tool(self, *args, **kw):
        extra = {"AGENT_MIRROR_STORE": self.store} if kw.get("with_store", True) else {}
        extra.update(kw.get("env") or {})
        proc = subprocess.run(
            [sys.executable, kw.get("tool", TOOL)] + list(args),
            cwd=kw.get("cwd", self.base),
            env=clean_env(self.home, extra),
            input=kw.get("stdin"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            timeout=kw.get("timeout", 180),
        )
        return proc.returncode, proc.stdout + proc.stderr

    def install(self, *extra):
        return self.run_tool(
            "install", "--agents", "claude,codex", "--project", "demo", "--project-path", self.project, *extra
        )

    def h(self, *parts):
        return os.path.join(self.home, *parts)

    def s(self, *parts):
        return os.path.join(self.store, *parts)

    def p(self, *parts):
        return os.path.join(self.project, *parts)

    def manifests(self):
        return sorted(glob.glob(self.h(".agent-mirror-backups", "*", "manifest.json")))

    def git(self, *args, **kw):
        proc = subprocess.run(
            ["git"] + list(args),
            cwd=kw.get("cwd", self.store),
            env=clean_env(self.home),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            check=True,
            timeout=60,
        )
        return proc.stdout.strip()

    def init_git(self):
        self.origin = os.path.join(self.base, "origin.git")
        self.git("init", "--bare", "-q", self.origin, cwd=self.base)
        self.git("init", "-q", "-b", "main")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "initial")
        self.git("remote", "add", "origin", self.origin)
        self.git("push", "-q", "-u", "origin", "main")

    def set_env_json(self, path=None, **fields):
        path = path or self.s("projects", "demo", "env.json")
        env = json.loads(read(path))
        env.update(fields)
        write(path, json.dumps(env))

    def set_box(self, path=None, **fields):
        path = path or self.s("projects", "demo", "env.json")
        env = json.loads(read(path))
        env["box"].update(fields)
        write(path, json.dumps(env))

    def fake_az(self, blobs=None):
        """Put the fake az first on PATH; returns the env for run_tool."""
        fakebin = install_fake(self.base, "az", FAKE_AZ)
        self.az_log = os.path.join(self.base, "az_log.jsonl")
        env = {"PATH": fakebin + os.pathsep + os.environ.get("PATH", ""), "FAKE_AZ_LOG": self.az_log}
        if blobs is not None:
            env["FAKE_AZ_BLOBS"] = os.path.join(self.base, "blobs.json")
            write(env["FAKE_AZ_BLOBS"], json.dumps(blobs))
        return env

    def az_calls(self, *argv_start):
        if not os.path.isfile(self.az_log):
            return []
        calls = [json.loads(line) for line in read(self.az_log).splitlines()]
        return [c for c in calls if c["argv"][: len(argv_start)] == list(argv_start)]

    def box_run(self, *args, env=None, az=None):
        e = dict(az if az is not None else self.fake_az())
        e.update(env or {})
        return self.run_tool(*args, "--project", "demo", "--project-path", self.project, env=e)


# In-process code (tests that call package functions directly) resolves every location through a
# Platform built on a sandboxed environment, never on the real one.
_INPROC = os.path.realpath(tempfile.mkdtemp(prefix="agent-mirror-inproc-"))
atexit.register(remove_tree, _INPROC)
os.makedirs(os.path.join(_INPROC, "home"), exist_ok=True)
from agent_mirror import platforms as _platforms  # noqa: E402

SANDBOX_PLATFORM = _platforms.Platform(env=sandbox_env(os.path.join(_INPROC, "home"), sandbox=_INPROC))


def reactivate_sandbox():
    """Put the sandboxed Platform back after an in-process test activated an injected one."""
    _platforms.activate(SANDBOX_PLATFORM)


reactivate_sandbox()
require_sandbox()
