"""How many transfer-program calls the box commands make: the same with 3 or 30 sessions (or folders), and few.

On some remotes every rclone or az start plus its round trip costs seconds, so a command that makes one call
per session is slow once a box holds a few dozen. The fake tools log every call; these tests count them with
a small box and a large one and require the same count, under an explicit ceiling. Also here: prune never
deletes outside the namespaces it selected, even next to a look-alike name.
"""

import io
import json
import os
import shutil
import tarfile
import unittest

from helpers import setUpModule  # noqa: F401 (the sandbox guard runs first)
from helpers import FAKE_RCLONE, ToolTest, install_fake, read, remove_tree, write

# ceilings per command (the counts measured in 0.5.1 are in the comments)
CEILING = {
    "sessions": 2,  # one listing, one batched manifest read
    "fetch --session LABEL": 3,  # listing, batched manifest read, one download
    "fetch --session FULL-ID": 3,  # that namespace's listing, its manifest, one download
    "fetch --session PREFIX": 3,  # listing, the target's manifest, one download
    "send (new session)": 3,  # upload, manifest write, read back
    "send (same session)": 4,  # upload, manifest read, write, read back
    "index": 4,  # shared listing, sessions listing, batched manifest read (azure: plus the owners line)
}
PRUNE_CEILING = {"rclone": 5, "azure": 8}  # listing, manifests, delete (azure: one per namespace), (rmdirs), check


def sid_of(i, runtime="codex-cloud"):
    return "%s-202611%02dT%02d%02d00Z-%06x" % (runtime, 1 + i // 600, (i // 60) % 10, i % 60, i)


class CallsMixin:
    def setUp(self):
        super().setUp()
        self.configure_box()
        self.set_box(min_free_bytes=0)

    def plant(self, n):
        """n sessions plus 5 more to prune; returns (all ids, ids to prune).

        The newest is the target: labelled, with a folder sent as an archive. Every fourth session is from
        before 0.5.0 and has no manifest, so their share grows with the box too.
        """
        remove_tree(self.sessions_path())
        ids = [sid_of(i) for i in range(n)]
        doomed = [sid_of(1000 + i, "claude-cloud") for i in range(5)]
        for i, sid in enumerate(ids + doomed):
            files = [{"path": "results/r%d.txt" % i, "bytes": 3, "mtime": "2026-11-01T00:00:00Z"}]
            write(self.sessions_path(sid, "results", "r%d.txt" % i), "r%d\n" % i)
            target = sid == ids[-1]
            if i % 4 == 1 and not target and sid in ids:
                continue  # no manifest
            if target:
                write(self.sessions_path(sid, "results", "extra.txt"), "extra\n")
                files.append({"path": "results/extra.txt", "bytes": 6, "mtime": "2026-11-01T00:00:00Z"})
                bundle = self.sessions_path(sid, "results", "arch.tar.gz")
                with tarfile.open(bundle, "w:gz") as tf:
                    info = tarfile.TarInfo("results/arch/a.txt")
                    info.size = 4
                    tf.addfile(info, io.BytesIO(b"arch"))
                files.append(
                    {"path": "results/arch", "object": "results/arch.tar.gz", "archive": True, "files": 1, "bytes": 4,
                     "archive_bytes": os.path.getsize(bundle), "mtime": "2026-11-01T00:00:00Z"}
                )
            manifest = {
                "schema": 1, "id": sid, "label": "target" if target else "lab-%d" % i, "runtime": sid.rsplit("-", 2)[0],
                "machine": None, "project": "demo", "repo": None, "git": {"branch": None, "commit": None},
                "started": "2026-11-01T00:00:00Z", "updated": "2026-11-01T00:00:00Z", "tool": "agent-mirror x",
                "files": files, "promoted": [],
            }
            write(self.sessions_path(sid, "_session.json"), json.dumps(manifest))
        for path in (os.path.join(self.project, ".agent-mirror"), os.path.join(self.project, "_sessions")):
            remove_tree(path)
        return ids, doomed

    def count(self, *args, **env):
        before = self.calls()
        e = dict(self.tool_env())
        e.update({"CLAUDECODE": "1"})
        e.update(env)
        rc, out = self.run_tool("box", *args, "--project", "demo", "--project-path", self.project, env=e)
        self.assertEqual(rc, 0, out)
        return self.calls() - before

    def measure(self, n):
        ids, doomed = self.plant(n)
        got = {}
        got["sessions"] = self.count("sessions")
        got["fetch --session LABEL"] = self.count("fetch", "--session", "target", "--into", "by-label")
        got["fetch --session FULL-ID"] = self.count("fetch", "--session", ids[-1], "--into", "by-id")
        got["fetch --session PREFIX"] = self.count("fetch", "--session", ids[-1][:-3], "--into", "by-prefix")
        self.assertEqual(read(self.p("by-id", "results", "extra.txt")), "extra\n")
        self.assertEqual(read(self.p("by-id", "results", "arch", "a.txt")), "arch")  # the archive was extracted
        self.assertEqual(read(self.p("by-label", "results", "arch", "a.txt")), "arch")
        write(self.p("results", "mine.txt"), "mine\n")
        got["send (new session)"] = self.count("send", "results/mine.txt", "--label", "new")
        got["send (same session)"] = self.count("send", "results/mine.txt")
        got["index"] = self.count("index")
        prune = []
        for sid in doomed:
            prune += ["--session", sid]
        got["prune 5 --yes"] = self.count("prune", *prune, "--yes")
        for sid in doomed:
            self.assertFalse(os.path.exists(self.sessions_path(sid)), sid)
        self.assertTrue(os.path.isfile(self.sessions_path(ids[0], "_session.json")))
        return got

    def test_sessions_commands_do_not_grow_with_the_box(self):
        small, large = self.measure(3), self.measure(30)
        self.assertEqual(small, large)
        for cmd, n in small.items():
            ceiling = PRUNE_CEILING[self.name] if cmd.startswith("prune") else CEILING[cmd]
            self.assertLessEqual(n, ceiling, "%s: %d calls" % (cmd, n))
        print("\n%s calls with 3 and 30 sessions: %s" % (self.name, json.dumps(small, sort_keys=True)))

    def folders(self, n):
        remove_tree(self.p("ws"))
        for i in range(n):
            write(self.p("ws", "run-%02d" % i, "f.txt"), "f%d\n" % i)

    def test_shared_side_reads_do_not_grow_with_the_folders(self):
        self.set_box(archives=["ws/run-*"])
        got = {}
        for n in (3, 30):
            self.folders(n)
            self.count("push")  # the first push uploads every folder (data, so it grows)
            got[n] = {"push (unchanged)": self.count("push"), "owners": self.count("owners"), "index": self.count("index")}
        self.assertEqual(got[3], got[30])
        self.assertLessEqual(got[30]["push (unchanged)"], 3)
        self.assertLessEqual(got[30]["owners"], 2)
        self.assertLessEqual(got[30]["index"], 6)
        print("\n%s shared-side calls with 3 and 30 folders: %s" % (self.name, json.dumps(got[30], sort_keys=True)))

    def test_prune_never_touches_a_look_alike(self):
        old = "claude-cloud-20261101T000000Z"  # an id from before 0.5.0 ...
        newer = old + "-0a0b0c"  # ... that is the start of another namespace's name
        for sid in (old, newer):
            write(self.sessions_path(sid, "a.txt"), sid + "\n")
        write(self.sessions_path(old + "x", "keep.txt"), "not a session\n")
        rc, out = self.run_tool(
            "box", "prune", "--session", old, "--yes", "--project", "demo", "--project-path", self.project,
            env=dict(self.tool_env(), CLAUDECODE="1"),
        )
        self.assertEqual(rc, 0, out)
        self.assertFalse(os.path.exists(self.sessions_path(old, "a.txt")))
        self.assertEqual(read(self.sessions_path(newer, "a.txt")), newer + "\n")
        self.assertEqual(read(self.sessions_path(old + "x", "keep.txt")), "not a session\n")


class AzureCallsTest(CallsMixin, ToolTest):
    name = "azure"

    def configure_box(self):
        self.root = os.path.join(self.base, "containers")

    def tool_env(self):
        env = self.fake_az()
        env["FAKE_AZ_ROOT"] = self.root
        return env

    def sessions_path(self, *parts):
        return os.path.join(self.root, "out-box", "demo", *[x for p in parts for x in p.split("/")])

    def calls(self):
        log = os.path.join(self.base, "az_log.jsonl")
        return len(read(log).splitlines()) if os.path.isfile(log) else 0


class RcloneCallsTest(CallsMixin, ToolTest):
    name = "rclone"

    def configure_box(self):
        self.root = os.path.join(self.base, "remotes")
        self.log = os.path.join(self.base, "rclone_log.jsonl")
        env = json.loads(read(self.s("projects", "demo", "env.json")))
        env["box"] = {
            "backend": "rclone",
            "shared_remote": "fakeremote:box/shared",
            "sessions_remote": "fakeremote:box/sessions",
            "include": ["temporary/**"],
        }
        write(self.s("projects", "demo", "env.json"), json.dumps(env))

    def tool_env(self):
        fakebin = install_fake(self.base, "rclone", FAKE_RCLONE)
        return {
            "PATH": fakebin + os.pathsep + os.environ.get("PATH", ""),
            "FAKE_RCLONE_LOG": self.log,
            "FAKE_RCLONE_INBOUND": "fakeremote:box/shared",
            "FAKE_RCLONE_ROOT": self.root,
        }

    def sessions_path(self, *parts):
        return os.path.join(self.root, "fakeremote", "box", "sessions", *[x for p in parts for x in p.split("/")])

    def calls(self):
        return len(read(self.log).splitlines()) if os.path.isfile(self.log) else 0


@unittest.skipUnless(shutil.which("rclone"), "rclone is not installed")
class RcloneRealPruneTest(ToolTest):
    """Real rclone: one delete with anchored filters removes only the selected namespaces."""

    def test_prune_with_real_rclone_never_touches_a_look_alike(self):
        sessions = os.path.join(self.base, "remote", "sessions")
        env = json.loads(read(self.s("projects", "demo", "env.json")))
        env["box"] = {"backend": "rclone", "shared_remote": os.path.join(self.base, "remote", "shared"), "sessions_remote": sessions}
        write(self.s("projects", "demo", "env.json"), json.dumps(env))
        old = "claude-cloud-20261101T000000Z"
        doomed = [old, sid_of(1, "claude-cloud")]
        kept = [old + "-0a0b0c", sid_of(2)]
        for sid in doomed + kept:
            write(os.path.join(sessions, sid, "sub", "a.txt"), sid + "\n")
        write(os.path.join(sessions, old + "x", "keep.txt"), "not a session\n")
        rc, out = self.run_tool(
            "box", "prune", "--session", doomed[0], "--session", doomed[1], "--yes", "--project", "demo",
            "--project-path", self.project, env={"RCLONE_CONFIG": os.path.join(self.base, "rclone.conf"), "CLAUDECODE": "1"},
        )
        self.assertEqual(rc, 0, out)
        self.assertEqual(sorted(os.listdir(sessions)), sorted(kept + [old + "x"]))  # the emptied folders are gone too
        for sid in kept:
            self.assertEqual(read(os.path.join(sessions, sid, "sub", "a.txt")), sid + "\n")


if __name__ == "__main__":
    unittest.main()
