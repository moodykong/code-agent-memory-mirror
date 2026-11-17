"""The platform layer for linux, darwin and win32, and the link fallbacks (symlink, junction, copy)."""

import contextlib
import io
import json
import os
import tempfile
import unittest

from helpers import setUpModule  # noqa: F401 (the sandbox guard runs first)
from helpers import make_home, make_store, reactivate_sandbox, read, remove_tree, write

from agent_mirror.config import Context
from agent_mirror.install import Installer, cmd_uninstall, link_state
from agent_mirror.links import make_link
from agent_mirror.platforms import Platform, claude_slug, find_claude_project_dir, slug_candidates
from agent_mirror.runtime import detect_runtime


class PlatformMappingTest(unittest.TestCase):
    def test_linux(self):
        p = Platform("linux", {"HOME": "/home/u"})
        self.assertEqual(p.claude_home(), "/home/u/.claude")
        self.assertEqual(p.codex_home(), "/home/u/.codex")
        self.assertEqual(p.agents_skills(), "/home/u/.agents/skills")
        self.assertEqual(p.config_file(), "/home/u/.config/agent-mirror/config.json")
        self.assertEqual(p.cache_dir(), "/home/u/.cache/agent-mirror")
        self.assertEqual(p.backup_root(), "/home/u/.agent-mirror-backups")
        self.assertEqual(p.launcher("/opt/plugin"), "/opt/plugin/bin/agent-mirror")
        self.assertEqual(
            p.command_line(["/opt/my plugin/bin/agent-mirror", "sync"]), "'/opt/my plugin/bin/agent-mirror' sync"
        )
        p = Platform(
            "linux", {"HOME": "/home/u", "XDG_CONFIG_HOME": "/xdg", "CLAUDE_CONFIG_DIR": "/c", "CODEX_HOME": "/x"}
        )
        self.assertEqual(p.config_file(), "/xdg/agent-mirror/config.json")
        self.assertEqual(p.claude_home(), "/c")
        self.assertEqual(p.codex_home(), "/x")

    def test_darwin(self):
        p = Platform("darwin", {"HOME": "/Users/u"})
        self.assertEqual(p.claude_home(), "/Users/u/.claude")
        self.assertEqual(p.config_file(), "/Users/u/.config/agent-mirror/config.json")
        self.assertEqual(p.cache_dir(), "/Users/u/Library/Caches/agent-mirror")
        self.assertEqual(p.launcher("/p"), "/p/bin/agent-mirror")
        self.assertFalse(p.windows)

    def test_win32(self):
        env = {"USERPROFILE": "C:\\Users\\u", "APPDATA": "C:\\Users\\u\\AppData\\Roaming", "LOCALAPPDATA": "D:\\L"}
        p = Platform("win32", env)
        self.assertTrue(p.windows)
        self.assertEqual(p.home(), "C:\\Users\\u")
        self.assertEqual(p.claude_home(), "C:\\Users\\u\\.claude")
        self.assertEqual(p.codex_home(), "C:\\Users\\u\\.codex")
        self.assertEqual(p.agents_skills(), "C:\\Users\\u\\.agents\\skills")
        self.assertEqual(p.config_file(), "C:\\Users\\u\\AppData\\Roaming\\agent-mirror\\config.json")
        self.assertEqual(p.cache_dir(), "D:\\L\\agent-mirror")
        self.assertEqual(p.launcher("C:\\tools\\plugin"), "C:\\tools\\plugin\\bin\\agent-mirror.cmd")
        self.assertEqual(
            p.command_line(["C:\\tools\\plugin\\bin\\agent-mirror.cmd", "session-start"]),
            '"C:/tools/plugin/bin/agent-mirror.cmd" session-start',
        )
        # without APPDATA, and with only HOMEDRIVE/HOMEPATH
        p = Platform("win32", {"HOMEDRIVE": "E:", "HOMEPATH": "\\Users\\v"})
        self.assertEqual(p.home(), "E:\\Users\\v")
        self.assertEqual(p.config_file(), "E:\\Users\\v\\AppData\\Roaming\\agent-mirror\\config.json")

    def test_slugs_and_existing_claude_folder(self):
        self.assertEqual(claude_slug("/home/u/my.project"), "-home-u-my-project")
        # verified on a real Windows 11 machine: colon, backslashes and spaces all become '-',
        # " - " becomes "---", and the drive letter keeps the case it has in the path
        self.assertEqual(claude_slug("C:\\Users\\Name\\my-project"), "C--Users-Name-my-project")
        self.assertEqual(claude_slug("C:\\Users\\Name\\My Project"), "C--Users-Name-My-Project")
        self.assertEqual(claude_slug("C:\\Users\\Name\\work - copy"), "C--Users-Name-work---copy")
        self.assertEqual(claude_slug("c:\\Users\\Name\\x"), "c--Users-Name-x")
        self.assertEqual(slug_candidates(["C:\\Users\\Name\\my-project"])[0], "C--Users-Name-my-project")
        self.assertEqual(slug_candidates(["C:\\Users\\u\\proj"]), ["C--Users-u-proj", "c--Users-u-proj"])
        base = os.path.realpath(tempfile.mkdtemp(prefix="agent-mirror-slug-"))
        self.addCleanup(remove_tree, base)
        os.makedirs(os.path.join(base, "projects", "c--Users-u-proj"))
        found = find_claude_project_dir(base, ["C:\\Users\\u\\proj"], windows=True)
        self.assertEqual(found, os.path.join(base, "projects", "c--Users-u-proj"))
        self.assertIsNone(find_claude_project_dir(base, ["/elsewhere"], windows=False))

    def test_detect_runtime_in_process(self):
        self.assertEqual(detect_runtime({}), "unknown")
        self.assertEqual(detect_runtime({"CLAUDE_CODE_REMOTE": "TRUE"}), "claude-cloud")
        self.assertEqual(detect_runtime({"CODEX_CI": "1"}), "codex-local")
        self.assertEqual(detect_runtime({"AGENT_RUNTIME": "codex-cloud"}), "codex-cloud")


class RealJunctionTest(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "directory junctions exist only on Windows")
    def test_make_detect_and_remove_a_real_junction(self):
        from agent_mirror.links import is_junction, is_linklike, link_target, make_junction, remove_link

        base = os.path.realpath(tempfile.mkdtemp(prefix="agent-mirror-junction-"))
        self.addCleanup(remove_tree, base)
        target = os.path.join(base, "target")
        write(os.path.join(target, "f.txt"), "x")
        link = os.path.join(base, "link")
        make_junction(link, target)
        self.assertTrue(is_junction(link))
        self.assertTrue(is_linklike(link))
        self.assertEqual(os.path.normcase(link_target(link)), os.path.normcase(target))
        self.assertEqual(read(os.path.join(link, "f.txt")), "x")
        remove_link(link)
        self.assertFalse(os.path.lexists(link))
        self.assertEqual(read(os.path.join(target, "f.txt")), "x")


class LinkFallbackTest(unittest.TestCase):
    def setUp(self):
        self.base = os.path.realpath(tempfile.mkdtemp(prefix="agent-mirror-links-"))
        self.addCleanup(remove_tree, self.base)

    @staticmethod
    def failing_symlink(*args, **kwargs):
        raise OSError(1314, "A required privilege is not held by the client")

    def test_make_link_methods(self):
        target_dir = os.path.join(self.base, "t_dir")
        target_file = os.path.join(self.base, "t_file")
        os.makedirs(target_dir)
        write(target_file, "content\n")
        with self.assertRaises(OSError):
            make_link(os.path.join(self.base, "a"), target_file, False, fallback=False, symlink=self.failing_symlink)
        made = []
        junction = lambda link, target: (made.append((link, target)), os.symlink(target, link))  # noqa: E731
        m = make_link(os.path.join(self.base, "d"), target_dir, True, True, self.failing_symlink, junction)
        self.assertEqual(m, "junction")
        self.assertEqual(made, [(os.path.join(self.base, "d"), target_dir)])
        m = make_link(os.path.join(self.base, "f"), target_file, False, True, self.failing_symlink, junction)
        self.assertEqual(m, "copy")
        self.assertFalse(os.path.islink(os.path.join(self.base, "f")))
        self.assertEqual(read(os.path.join(self.base, "f")), "content\n")
        self.assertEqual(make_link(os.path.join(self.base, "s"), target_file, False, True), "symlink")

    def test_installer_falls_back_records_copies_and_refreshes(self):
        project = os.path.join(self.base, "work", "demo")
        os.makedirs(project)
        store = make_store(self.base)
        home = make_home(self.base, project)
        plat = Platform(None, {"AGENT_MIRROR_HOME": home})
        self.addCleanup(reactivate_sandbox)
        ctx = Context(store_flag=store, plat=plat)
        # on Windows the real junction code runs; elsewhere a symlink stands in for it
        junction = None if os.name == "nt" else (lambda link, target: os.symlink(target, link))  # noqa: E731

        def installer():
            return Installer(ctx, False, symlink=self.failing_symlink, junction=junction, fallback=True)

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(installer().install(["claude"], []), 0)
        out = buf.getvalue()
        claude_md = os.path.join(home, ".claude", "CLAUDE.md")
        self.assertFalse(os.path.islink(claude_md))
        self.assertEqual(read(claude_md), read(os.path.join(store, "AGENTS.md")))
        self.assertIn("managed copy", out)
        self.assertIn("(junction)", out)
        manifest = [os.path.join(r, f) for r, _, fs in os.walk(plat.backup_root()) for f in fs if f == "manifest.json"]
        data = json.loads(read(manifest[0]))
        methods = {e["path"]: e["method"] for e in data["entries"]}
        self.assertEqual(methods[claude_md], "copy")
        self.assertEqual(methods[os.path.join(home, ".agents", "skills")], "junction")

        inst = installer()
        self.assertEqual(link_state(inst, claude_md, os.path.join(store, "AGENTS.md"))[0], "COPY")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(inst.install(["claude"], []), 0)
        self.assertIn("nothing to change", buf.getvalue())

        write(os.path.join(store, "AGENTS.md"), "edited in the store\n")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(installer().install(["claude"], []), 0)
        self.assertIn("refreshed the managed copy", buf.getvalue())
        self.assertEqual(read(claude_md), "edited in the store\n")

        class Args:
            dry_run = False

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cmd_uninstall(Args(), ctx)
        self.assertIn("removed managed copy", buf.getvalue())
        self.assertEqual(read(claude_md), "old claude user file\n")


if __name__ == "__main__":
    unittest.main()
