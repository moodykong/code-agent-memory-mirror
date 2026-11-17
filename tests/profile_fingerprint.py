"""Read-only fingerprint of the places agent-mirror could change in the REAL user profile.

Run it before and after a test run on a machine whose profile must stay untouched, and compare:

    python tests/profile_fingerprint.py > before.json
    python -m unittest discover -s tests
    python tests/profile_fingerprint.py > after.json
    python tests/profile_fingerprint.py --compare before.json after.json

It only reads (names, kinds, link targets, sizes, modification times and SHA-256 of small files) and
prints JSON; it writes nothing and starts no program except a read-only `schtasks /Query` on Windows.
Unlike the program, it deliberately reads the real environment (HOME, USERPROFILE, APPDATA, ...):
that is what it checks. Standard library only; not a test module (unittest does not collect it).
"""

import hashlib
import json
import os
import subprocess
import sys

SMALL = 1 << 20
MAX_ENTRIES = 5000


def home():
    if os.name == "nt":
        return os.environ.get("USERPROFILE") or os.path.expanduser("~")
    return os.environ.get("HOME") or os.path.expanduser("~")


def locations():
    h = home()
    claude = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(h, ".claude")
    codex = os.environ.get("CODEX_HOME") or os.path.join(h, ".codex")
    if os.name == "nt":
        config = os.path.join(os.environ.get("APPDATA") or os.path.join(h, "AppData", "Roaming"), "agent-mirror")
        cache = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.join(h, "AppData", "Local"), "agent-mirror")
    else:
        config = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.join(h, ".config"), "agent-mirror")
        cache = os.path.join(os.environ.get("XDG_CACHE_HOME") or os.path.join(h, ".cache"), "agent-mirror")
    return {
        "claude_md": (os.path.join(claude, "CLAUDE.md"), 0),
        "claude_settings": (os.path.join(claude, "settings.json"), 0),
        "claude_skills": (os.path.join(claude, "skills"), 1),
        "claude_projects": (os.path.join(claude, "projects"), 0),
        "codex_agents_md": (os.path.join(codex, "AGENTS.md"), 0),
        "agents_skills": (os.path.join(h, ".agents", "skills"), 1),
        "config": (config, 3),
        "cache": (cache, 3),
        "backups": (os.path.join(h, ".agent-mirror-backups"), 1),
        "companions": (os.path.join(h, "companions"), 1),
        "systemd_user": (
            os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.join(h, ".config"), "systemd", "user"),
            1,
        ),
    }


def is_link(path):
    if os.path.islink(path):
        return True
    fn = getattr(os.path, "isjunction", None)
    if fn:
        return bool(fn(path))
    try:
        st = os.lstat(path)
    except OSError:
        return False
    return bool(getattr(st, "st_file_attributes", 0) & 0x400)


def describe(path):
    try:
        st = os.lstat(path)
    except OSError:
        return {"kind": "absent"}
    if is_link(path):
        try:
            target = os.readlink(path)
        except OSError:
            target = "?"
        return {"kind": "link", "target": target}
    if os.path.isdir(path):
        return {"kind": "dir", "mtime": int(st.st_mtime)}
    d = {"kind": "file", "size": st.st_size, "mtime": int(st.st_mtime)}
    if st.st_size <= SMALL:
        try:
            with open(path, "rb") as fh:
                d["sha256"] = hashlib.sha256(fh.read()).hexdigest()
        except OSError:
            d["sha256"] = "unreadable"
    return d


def walk(path, depth, found):
    found[path] = describe(path)
    if depth <= 0 or found[path]["kind"] != "dir" or len(found) > MAX_ENTRIES:
        return
    try:
        names = sorted(os.listdir(path))
    except OSError:
        return
    for n in names:
        walk(os.path.join(path, n), depth - 1, found)


def memory_folders(projects, found):
    """Each <claude home>/projects/*/memory: its kind and link target, and its files when it is real."""
    try:
        slugs = sorted(os.listdir(projects))
    except OSError:
        return
    for slug in slugs:
        mem = os.path.join(projects, slug, "memory")
        if os.path.lexists(mem):
            walk(mem, 0 if is_link(mem) else 2, found)


def scheduled_task():
    if os.name != "nt":
        return None
    try:
        proc = subprocess.run(
            ["schtasks", "/Query", "/TN", "agent-mirror-sync"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"error": str(exc)}
    return {"rc": proc.returncode, "sha256": hashlib.sha256(proc.stdout or b"").hexdigest()}


def fingerprint():
    found = {}
    for name, (path, depth) in sorted(locations().items()):
        if name == "claude_projects":
            memory_folders(path, found)
        else:
            walk(path, depth, found)
    return {"home": home(), "entries": found, "scheduled_task": scheduled_task()}


def compare(a_path, b_path):
    with open(a_path, encoding="utf-8") as fh:
        a = json.load(fh)
    with open(b_path, encoding="utf-8") as fh:
        b = json.load(fh)
    diffs = []
    for key in sorted(set(a["entries"]) | set(b["entries"])):
        if a["entries"].get(key) != b["entries"].get(key):
            diffs.append("%s: %s -> %s" % (key, a["entries"].get(key), b["entries"].get(key)))
    if a.get("scheduled_task") != b.get("scheduled_task"):
        diffs.append("scheduled task: %s -> %s" % (a.get("scheduled_task"), b.get("scheduled_task")))
    if diffs:
        print("DIFFERENT (%d):" % len(diffs))
        for d in diffs:
            print("  " + d)
        return 1
    print("identical: %d entries, scheduled task %s" % (len(a["entries"]), a.get("scheduled_task")))
    return 0


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "--compare":
        sys.exit(compare(sys.argv[2], sys.argv[3]))
    json.dump(fingerprint(), sys.stdout, indent=1, sort_keys=True)
    sys.stdout.write("\n")
