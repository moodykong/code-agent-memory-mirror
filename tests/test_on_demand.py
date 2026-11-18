"""Archive mode, on-demand retrieval, receive, mount and rclone-env.

The azure-blob tests use the fake az with a folder standing in for the containers.
The rclone tests use the real rclone with local folders as remotes, and are
skipped when rclone is not installed.
"""

import contextlib
import gzip
import io
import json
import os
import shlex
import shutil
import tarfile
import unittest

from helpers import setUpModule  # noqa: F401 (the sandbox guard runs first)
from helpers import FAKE_RCLONE, ToolTest, install_fake, read, remove_tree, write

from agent_mirror.box.archive import safe_extract
from agent_mirror.box.extras import fuse_status, mount_cmd, unmount_cmd
from agent_mirror.box.local import disk_guard

THRESHOLD = 1000


class ArchiveTestBase(ToolTest):
    def setUp(self):
        super().setUp()
        self.git("init", "-q", cwd=self.project)
        ws = self.p("ws-1")
        for i in range(12):
            write(os.path.join(ws, "src", "a", "f%02d.txt" % i), "small %d\n" % i)
        write(os.path.join(ws, "src", "b", "g.txt"), "g\n")
        os.makedirs(os.path.join(ws, "big"))
        with open(os.path.join(ws, "big", "blob1.bin"), "wb") as fh:
            fh.write(os.urandom(5000))
        with open(os.path.join(ws, "src", "a", "huge.dat"), "wb") as fh:
            fh.write(os.urandom(3000))
        write(os.path.join(ws, ".env"), "SECRET=1\n")
        write(os.path.join(ws, "src", "__pycache__", "m.pyc"), "x")
        try:
            os.symlink(os.path.join(os.sep, "etc", "hostname"), os.path.join(ws, "link-out"))
        except (OSError, NotImplementedError):
            pass  # no symlinks here; the archive must leave links out either way
        self.originals = {}
        for root, _, files in os.walk(ws):
            for f in files:
                full = os.path.join(root, f)
                if not os.path.islink(full):
                    with open(full, "rb") as fh:
                        self.originals[os.path.relpath(full, self.project).replace(os.sep, "/")] = fh.read()

    def configure(self, **box):
        env = json.loads(read(self.s("projects", "demo", "env.json")))
        env["box"].update({"archives": ["ws-*"], "archive_large_file_bytes": THRESHOLD, "min_free_bytes": 0})
        env["box"].update(box)
        write(self.s("projects", "demo", "env.json"), json.dumps(env))

    def wipe_local(self):
        shutil.rmtree(self.p("ws-1"))


class AzureArchiveTest(ArchiveTestBase):
    def setUp(self):
        super().setUp()
        self.configure()
        self.root = os.path.join(self.base, "containers")
        os.makedirs(self.root)

    def az(self, **extra):
        env = self.fake_az()
        env["FAKE_AZ_ROOT"] = self.root
        env.update(extra)
        return env

    def remote(self, *parts):
        return os.path.join(self.root, "in-box", "demo", "_archives", *parts)

    def test_pack_with_threshold_toc_index_and_partial_fetch(self):
        rc, out = self.box_run("box", "push", env={"CLAUDECODE": "1"}, az=self.az())
        self.assertEqual(rc, 0, out)
        self.assertIn("ws-1: pushed", out)
        sidecar = json.loads(read(self.remote("ws-1.json")))
        self.assertEqual(sidecar["files"], 15)
        self.assertEqual(sidecar["large"]["files"], 2)
        self.assertEqual(sidecar["bundle"]["files"], 13)
        self.assertTrue(sidecar["archive_bytes"] > 0)
        toc = dict((e["path"], e["in"]) for e in sidecar["toc"])
        self.assertEqual(toc["big/blob1.bin"], "large")
        self.assertEqual(toc["src/a/huge.dat"], "large")
        self.assertEqual(toc["src/a/f00.txt"], "bundle")
        self.assertNotIn(".env", toc)
        self.assertNotIn("link-out", toc)
        self.assertNotIn("src/__pycache__/m.pyc", toc)
        self.assertTrue(os.path.isfile(self.remote("ws-1.large", "big", "blob1.bin")))
        with tarfile.open(self.remote("ws-1.tar.gz")) as tf:
            names = sorted(tf.getnames())
        self.assertIn("ws-1/src/a/f00.txt", names)
        self.assertNotIn("ws-1/big/blob1.bin", names)
        self.assertFalse([n for n in names if n.endswith(".env")])
        # a second push with nothing changed uploads nothing
        rc, out = self.box_run("box", "push", env={"CLAUDECODE": "1"}, az=self.az())
        self.assertIn("unchanged since the last push", out)

        rc, out = self.box_run("box", "index", env={"CLAUDECODE": "1"}, az=self.az())
        self.assertEqual(rc, 0, out)
        lines = [json.loads(x) for x in read(self.s("projects", "demo", "box_index.jsonl")).splitlines()]
        arch = [x for x in lines if x.get("archive")]
        self.assertEqual(len(arch), 1)
        self.assertEqual((arch[0]["path"], arch[0]["files"], arch[0]["large_files"]), ("ws-1", 15, 2))
        self.assertTrue(os.path.isfile(self.s("projects", "demo", arch[0]["toc"])))
        self.assertFalse([x for x in lines if x["path"].startswith("_archives")])

        self.wipe_local()
        rc, out = self.box_run("box", "fetch", "ws-1", env={"AGENT_RUNTIME": "claude-cloud"}, az=self.az())
        self.assertEqual(rc, 0, out)
        self.assertEqual(read(self.p("ws-1", "src", "a", "f03.txt")), "small 3\n")
        self.assertFalse(os.path.exists(self.p("ws-1", "big", "blob1.bin")))
        self.assertIn("not fetched: 2 large file(s)", out)
        self.assertIn("agent-mirror box fetch ws-1/big/blob1.bin --project demo", out)

        rc, out = self.box_run(
            "box", "fetch", "ws-1/big/blob1.bin", env={"AGENT_RUNTIME": "claude-cloud"}, az=self.az()
        )
        self.assertEqual(rc, 0, out)
        with open(self.p("ws-1", "big", "blob1.bin"), "rb") as fh:
            self.assertEqual(fh.read(), self.originals["ws-1/big/blob1.bin"])

        shutil.rmtree(self.p("ws-1"))
        rc, out = self.box_run("box", "fetch", "ws-1/src/a", env={"AGENT_RUNTIME": "claude-cloud"}, az=self.az())
        self.assertEqual(rc, 0, out)
        self.assertTrue(os.path.isfile(self.p("ws-1", "src", "a", "f11.txt")))
        self.assertFalse(os.path.exists(self.p("ws-1", "src", "b", "g.txt")))
        self.assertIn("ws-1/src/a/huge.dat", out)
        self.assertNotIn("ws-1/big/blob1.bin", out)

        rc, out = self.box_run("box", "fetch", "ws-1", "--all", env={"AGENT_RUNTIME": "claude-cloud"}, az=self.az())
        self.assertEqual(rc, 0, out)
        for rel, data in self.originals.items():
            if rel.endswith((".env", ".pyc")):
                self.assertFalse(os.path.exists(self.p(*rel.split("/"))), rel)
                continue
            with open(self.p(*rel.split("/")), "rb") as fh:
                self.assertEqual(fh.read(), data, rel)

    def test_large_table_of_contents_goes_to_its_own_file(self):
        self.configure(toc_inline_bytes=10)
        rc, out = self.box_run("box", "push", env={"CLAUDECODE": "1"}, az=self.az())
        self.assertEqual(rc, 0, out)
        sidecar = json.loads(read(self.remote("ws-1.json")))
        self.assertIsNone(sidecar["toc"])
        self.assertEqual(sidecar["toc_file"], "_archives/ws-1.toc.json.gz")
        with gzip.open(self.remote("ws-1.toc.json.gz")) as fh:
            self.assertEqual(len(json.loads(fh.read().decode())), 15)
        rc, out = self.box_run("box", "index", env={"CLAUDECODE": "1"}, az=self.az())
        self.assertEqual(rc, 0, out)
        self.wipe_local()
        rc, out = self.box_run("box", "fetch", "ws-1/src/b/g.txt", env={"AGENT_RUNTIME": "claude-cloud"}, az=self.az())
        self.assertEqual(rc, 0, out)
        self.assertEqual(read(self.p("ws-1", "src", "b", "g.txt")), "g\n")

    def test_disk_guard_status_and_evict(self):
        self.box_run("box", "push", env={"CLAUDECODE": "1"}, az=self.az())
        self.wipe_local()
        self.configure(min_free_bytes=10**18)
        rc, out = self.box_run("box", "fetch", "ws-1", env={"AGENT_RUNTIME": "claude-cloud"}, az=self.az())
        self.assertEqual(rc, 1, out)
        self.assertIn("not enough free disk", out)
        self.assertFalse(os.path.exists(self.p("ws-1")))
        rc, out = self.box_run("box", "fetch", "ws-1", "--force", env={"AGENT_RUNTIME": "claude-cloud"}, az=self.az())
        self.assertEqual(rc, 0, out)
        self.configure(min_free_bytes=0)

        self.assertIn("/.agent-mirror/", read(self.p(".git", "info", "exclude")))
        self.assertFalse(os.path.exists(self.p(".gitignore")))
        rc, out = self.box_run("box", "status", env={"AGENT_RUNTIME": "claude-cloud"})
        self.assertEqual(rc, 0, out)
        self.assertIn("fetched from the box: 13 file(s)", out)
        self.assertIn("free disk:", out)

        write(self.p("ws-1", "mine.txt"), "made here\n")  # never came from the box
        write(self.p("ws-1", "src", "a", "f00.txt"), "edited after the fetch\n")
        rc, out = self.box_run("box", "evict", "ws-1", env={"CLAUDECODE": "1"})
        self.assertEqual(rc, 2, out)
        self.assertTrue(os.path.isfile(self.p("ws-1", "src", "a", "f01.txt")))
        rc, out = self.box_run("box", "evict", "ws-1", "--dry-run", env={"AGENT_RUNTIME": "claude-cloud"})
        self.assertIn("would delete 12 file(s)", out)
        self.assertTrue(os.path.isfile(self.p("ws-1", "src", "a", "f01.txt")))
        rc, out = self.box_run("box", "evict", "ws-1", env={"AGENT_RUNTIME": "claude-cloud"})
        self.assertEqual(rc, 0, out)
        self.assertIn("deleted 12 file(s)", out)
        self.assertIn("f00.txt changed since it was fetched; kept", out)
        self.assertFalse(os.path.exists(self.p("ws-1", "src", "a", "f01.txt")))
        self.assertFalse(os.path.exists(self.p("ws-1", "src", "b")))  # emptied folders go too
        self.assertEqual(read(self.p("ws-1", "mine.txt")), "made here\n")
        self.assertEqual(read(self.p("ws-1", "src", "a", "f00.txt")), "edited after the fetch\n")
        rc, out = self.box_run("box", "evict", "ws-1/mine.txt", env={"AGENT_RUNTIME": "claude-cloud"})
        self.assertEqual(rc, 1, out)
        self.assertTrue(os.path.isfile(self.p("ws-1", "mine.txt")))

    def test_send_archive_and_receive(self):
        write(self.p("temporary", "run", "a.csv"), "1\n")
        write(self.p("temporary", "run", ".env"), "S=1\n")
        rc, out = self.box_run(
            "box",
            "send",
            "temporary/run",
            "--archive",
            "--note",
            "One run.",
            env={"AGENT_RUNTIME": "codex-cloud"},
            az=self.az(),
        )
        self.assertEqual(rc, 0, out)
        outbound = os.path.join(self.root, "out-box", "demo")
        stamped = os.listdir(outbound)[0]
        with tarfile.open(os.path.join(outbound, stamped, "temporary", "run.tar.gz")) as tf:
            self.assertEqual(tf.getnames(), ["temporary/run/a.csv"])
        rc, out = self.box_run("box", "receive", env={"AGENT_RUNTIME": "claude-cloud"}, az=self.az())
        self.assertEqual(rc, 2, out)
        target = self.p("_agent_outbound", stamped, "temporary", "run.tar.gz")
        rc, out = self.box_run("box", "receive", env={"CLAUDECODE": "1"}, az=self.az())
        self.assertEqual(rc, 0, out)
        self.assertTrue(os.path.isfile(target))
        self.assertIn("/_agent_outbound/", read(self.p(".git", "info", "exclude")))
        write(target, "kept\n")
        rc, out = self.box_run("box", "receive", env={"CLAUDECODE": "1"}, az=self.az())
        self.assertIn("nothing new", out)
        self.assertEqual(read(target), "kept\n")
        into = os.path.join(self.base, "elsewhere")
        rc, out = self.box_run("box", "receive", "--into", into, env={"CLAUDECODE": "1"}, az=self.az())
        self.assertTrue(os.path.isfile(os.path.join(into, stamped, "temporary", "run.tar.gz")), out)


class LsOfflineTest(ToolTest):
    def test_ls_from_index_and_toc_without_any_tool(self):
        idx = self.s("projects", "demo", "box_index.jsonl")
        write(
            idx,
            json.dumps({"path": "temporary/a.txt", "size": 3, "modtime": "t"})
            + "\n"
            + json.dumps(
                {
                    "path": "ws-1",
                    "archive": True,
                    "files": 3,
                    "bytes": 5006,
                    "large_files": 1,
                    "toc": "box_toc/ws-1.json",
                }
            )
            + "\n",
        )
        write(
            self.s("projects", "demo", "box_toc", "ws-1.json"),
            json.dumps(
                [
                    {"path": "a/x.txt", "size": 3, "mtime": 1, "in": "bundle"},
                    {"path": "a/y.txt", "size": 3, "mtime": 1, "in": "bundle"},
                    {"path": "big.bin", "size": 5000, "mtime": 1, "in": "large"},
                ]
            ),
        )
        write(self.p("ws-1", "a", "x.txt"), "abc")
        empty = os.path.join(self.base, "emptybin")
        os.makedirs(empty)
        env = {"PATH": empty}
        rc, out = self.run_tool("box", "ls", "--project", "demo", "--project-path", self.project, env=env)
        self.assertEqual(rc, 0, out)
        self.assertRegex(out, r"0/1 .*temporary/")
        self.assertRegex(out, r"1/3 .*ws-1/  archived folder: 3 files, 1 large")
        rc, out = self.run_tool("box", "ls", "ws-1", "--project", "demo", "--project-path", self.project, env=env)
        self.assertEqual(rc, 0, out)
        self.assertRegex(out, r"1/2 .*ws-1/a/")
        self.assertIn("ws-1/big.bin  large: agent-mirror box fetch ws-1/big.bin", out)
        rc, out = self.run_tool(
            "box", "ls", "ws-1/a/x.txt", "--project", "demo", "--project-path", self.project, env=env
        )
        self.assertRegex(out, r"here .*ws-1/a/x.txt")
        rc, out = self.run_tool("box", "ls", "nope", "--project", "demo", "--project-path", self.project, env=env)
        self.assertEqual(rc, 1, out)


class GuardAndExtractUnitTest(unittest.TestCase):
    def test_disk_guard_with_injected_free_space(self):
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf), contextlib.redirect_stdout(buf):
            self.assertTrue(disk_guard("/x", 100, 50, False, free_fn=lambda p: 1000))
            self.assertFalse(disk_guard("/x", 900, 200, False, free_fn=lambda p: 1000))
            self.assertTrue(disk_guard("/x", 900, 200, True, free_fn=lambda p: 1000))
        text = buf.getvalue()
        self.assertIn("this needs 900 B plus a reserve of 200 B (box.min_free_bytes), and 1000 B is free", text)
        self.assertIn("going ahead because of --force", text)

    def test_safe_extract_rejects_unsafe_members(self):
        base = os.path.realpath(__import__("tempfile").mkdtemp(prefix="agent-mirror-tar-"))
        self.addCleanup(remove_tree, base)
        bundle = os.path.join(base, "b.tar.gz")
        with tarfile.open(bundle, "w:gz") as tf:
            for name, data in (
                ("ws-1/ok.txt", b"ok"),
                ("../evil.txt", b"x"),
                ("/abs.txt", b"x"),
                ("other/x.txt", b"x"),
            ):
                info = tarfile.TarInfo(name)
                info.size = len(data)
                tf.addfile(info, io.BytesIO(data))
            for name, target in (("ws-1/out-link", "../../etc/hostname"), ("ws-1/in-link", "ok.txt")):
                info = tarfile.TarInfo(name)
                info.type = tarfile.SYMTYPE
                info.linkname = target
                tf.addfile(info)
        project = os.path.join(base, "proj")
        os.makedirs(project)
        written, kept, rejected = safe_extract(bundle, project, "ws-1", None, overwrite=False)
        self.assertEqual(sorted(written), ["ws-1/in-link", "ws-1/ok.txt"])
        self.assertEqual(rejected, 4)
        self.assertFalse(os.path.exists(os.path.join(base, "evil.txt")))
        self.assertFalse(os.path.lexists(os.path.join(project, "ws-1", "out-link")))
        written, kept, rejected = safe_extract(bundle, project, "ws-1", {"ws-1/ok.txt"}, overwrite=False)
        self.assertEqual((written, kept), ([], 1))


class MountTest(ToolTest):
    def test_fuse_detection_and_commands(self):
        yes = lambda p: True  # noqa: E731
        no = lambda p: False  # noqa: E731
        self.assertEqual(fuse_status("linux", exists=yes, which=lambda n: "/usr/bin/" + n)[0], True)
        ok, reason = fuse_status("linux", exists=no, which=lambda n: None)
        self.assertFalse(ok)
        self.assertIn("/dev/fuse is missing", reason)
        ok, reason = fuse_status("linux", exists=yes, which=lambda n: None)
        self.assertIn("fusermount", reason)
        self.assertFalse(fuse_status("darwin", exists=no)[0])
        self.assertTrue(fuse_status("win32", exists=yes)[0])
        self.assertEqual(
            mount_cmd("od:box/in", "/p/_box", "5G"),
            [
                "rclone",
                "mount",
                "od:box/in",
                "/p/_box",
                "--read-only",
                "--vfs-cache-mode",
                "full",
                "--vfs-cache-max-size",
                "5G",
                "--daemon",
            ],
        )
        self.assertNotIn("--daemon", mount_cmd("od:x", "X:", "5G", windows=True))
        self.assertEqual(unmount_cmd("linux", "/p/_box", which=lambda n: None), ["fusermount3", "-u", "/p/_box"])
        self.assertEqual(unmount_cmd("darwin", "/p/_box"), ["umount", "/p/_box"])
        self.assertIsNone(unmount_cmd("win32", "X:"))

    def test_mount_dry_run_needs_rclone_backend(self):
        rc, out = self.run_tool("box", "mount", "--dry-run", "--project", "demo", "--project-path", self.project)
        self.assertEqual(rc, 2, out)
        self.assertIn("needs the rclone backend", out)
        env = json.loads(read(self.s("projects", "demo", "env.json")))
        env["box"] = {
            "backend": "rclone",
            "inbound_remote": "od:box/in",
            "outbound_remote": "od:box/out",
            "mount_dir": "mnt/box",
        }
        write(self.s("projects", "demo", "env.json"), json.dumps(env))
        rc, out = self.run_tool(
            "box", "mount", "--dry-run", "--cache-max-size", "3G", "--project", "demo", "--project-path", self.project
        )
        self.assertEqual(rc, 0, out)
        self.assertIn(
            "would run: rclone mount od:box/in %s --read-only --vfs-cache-mode full --vfs-cache-max-size 3G%s"
            % (shlex.quote(self.p("mnt", "box")), "" if os.name == "nt" else " --daemon"),
            out,
        )
        self.assertIn("FUSE:", out)
        rc, out = self.run_tool(
            "box", "mount", "--dry-run", "--cache-max-size", "lots", "--project", "demo", "--project-path", self.project
        )
        self.assertEqual(rc, 2, out)


class RcloneEnvTest(ToolTest):
    def test_names_only_then_owner_only_file(self):
        token = '{"access_token":"' + "tok" + "en-value-" + "x" * 12 + '","expiry":"2030-01-01T00:00:00Z"}'
        cfg = {"mydrive": {"type": "onedrive", "token": token, "drive_id": "d1", "drive_type": "personal"}}
        fakebin = install_fake(self.base, "rclone", FAKE_RCLONE)
        env = {
            "PATH": fakebin + os.pathsep + os.environ.get("PATH", ""),
            "FAKE_RCLONE_LOG": os.path.join(self.base, "rlog"),
            "FAKE_RCLONE_CONFIG": json.dumps(cfg),
        }
        rc, out = self.run_tool("box", "rclone-env", "--remote", "mydrive", env=env)
        self.assertEqual(rc, 0, out)
        for name in ("TYPE", "TOKEN", "DRIVE_ID", "DRIVE_TYPE"):
            self.assertIn("RCLONE_CONFIG_MYDRIVE_" + name, out)
        self.assertNotIn("token-value", out)
        self.assertNotIn("personal", out)
        self.assertIn("cannot be refreshed on a throwaway machine", out)
        self.assertIn("expiry 2030-01-01", out)
        target = os.path.join(self.base, "secrets", "rclone.env")
        rc, out = self.run_tool("box", "rclone-env", "--remote", "mydrive", "--out", target, env=env)
        self.assertEqual(rc, 0, out)
        self.assertNotIn("token-value", out)
        if os.name == "nt":
            self.assertIn("OUTSIDE your user profile", out)  # Windows ignores 0600; base/ is outside the home
        else:
            self.assertEqual(os.stat(target).st_mode & 0o777, 0o600)
        lines = dict(line.split("=", 1) for line in read(target).splitlines())
        self.assertEqual(lines["RCLONE_CONFIG_MYDRIVE_DRIVE_ID"], "d1")
        self.assertEqual(lines["RCLONE_CONFIG_MYDRIVE_TOKEN"], "'%s'" % token)  # JSON is single-quoted for shells and dotenv readers
        rc, out = self.run_tool("box", "rclone-env", "--remote", "mydrive", "--out", target, env=env)
        self.assertEqual(rc, 2, out)
        rc, out = self.run_tool("box", "rclone-env", "--remote", "mydrive", "--out", self.s("x.env"), env=env)
        self.assertEqual(rc, 2, out)
        self.assertFalse(os.path.exists(self.s("x.env")))
        rc, out = self.run_tool("box", "rclone-env", "--remote", "nope", env=env)
        self.assertEqual(rc, 1, out)
        rc, out = self.run_tool("box", "rclone-env", "--remote", "bad-name", env=env)
        self.assertEqual(rc, 2, out)


@unittest.skipUnless(shutil.which("rclone"), "rclone is not installed")
class RcloneLocalArchiveTest(ArchiveTestBase):
    """Real rclone, with local folders standing in for the remotes."""

    def setUp(self):
        super().setUp()
        self.inbound = os.path.join(self.base, "remote", "in")
        self.outbound = os.path.join(self.base, "remote", "out")
        os.makedirs(self.inbound)
        os.makedirs(self.outbound)
        env = json.loads(read(self.s("projects", "demo", "env.json")))
        env["box"] = {
            "backend": "rclone",
            "inbound_remote": self.inbound,
            "outbound_remote": self.outbound,
            "include": ["temporary/**"],
            "exclude": ["**/__pycache__/**"],
        }
        write(self.s("projects", "demo", "env.json"), json.dumps(env))
        self.configure()
        self.renv = {"RCLONE_CONFIG": os.path.join(self.base, "rclone.conf")}

    def run_box(self, *args, runtime="CLAUDECODE"):
        env = dict(self.renv)
        env.update({"CLAUDECODE": "1"} if runtime == "CLAUDECODE" else {"AGENT_RUNTIME": runtime})
        return self.run_tool(*args, "--project", "demo", "--project-path", self.project, env=env)

    def test_push_index_fetch_with_real_rclone(self):
        write(self.p("temporary", "keep.txt"), "k\n")
        rc, out = self.run_box("box", "push")
        self.assertEqual(rc, 0, out)
        self.assertTrue(os.path.isfile(os.path.join(self.inbound, "temporary", "keep.txt")))
        self.assertFalse(os.path.exists(os.path.join(self.inbound, "ws-1")))  # never file by file
        self.assertTrue(os.path.isfile(os.path.join(self.inbound, "_archives", "ws-1.tar.gz")))
        self.assertTrue(os.path.isfile(os.path.join(self.inbound, "_archives", "ws-1.large", "big", "blob1.bin")))
        rc, out = self.run_box("box", "index")
        self.assertEqual(rc, 0, out)
        index = read(self.s("projects", "demo", "box_index.jsonl"))
        self.assertIn('"archive": true', index)
        self.wipe_local()
        rc, out = self.run_box("box", "fetch", "ws-1", "--all", runtime="claude-cloud")
        self.assertEqual(rc, 0, out)
        with open(self.p("ws-1", "src", "a", "huge.dat"), "rb") as fh:
            self.assertEqual(fh.read(), self.originals["ws-1/src/a/huge.dat"])
        self.assertEqual(read(self.p("ws-1", "src", "b", "g.txt")), "g\n")
        write(self.p("ws-1", "src", "b", "g.txt"), "local edit\n")
        rc, out = self.run_box("box", "fetch", "ws-1")
        self.assertEqual(read(self.p("ws-1", "src", "b", "g.txt")), "local edit\n")  # never overwrites locally
        os.makedirs(os.path.join(self.outbound, "codex-cloud-x"))
        write(os.path.join(self.outbound, "codex-cloud-x", "r.txt"), "result\n")
        rc, out = self.run_box("box", "receive")
        self.assertEqual(rc, 0, out)
        self.assertEqual(read(self.p("_agent_outbound", "codex-cloud-x", "r.txt")), "result\n")


if __name__ == "__main__":
    unittest.main()
