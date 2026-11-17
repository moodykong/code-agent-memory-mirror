"""Config and store resolution order, init-store (create and adopt) and its refusals."""

import json
import os
import shutil
import subprocess
import tempfile
import unittest

from helpers import ToolTest, clean_env, read, write

from agent_mirror.config import Context, resolve_store_path
from agent_mirror.platforms import Platform


class ResolutionOrderTest(unittest.TestCase):
    def setUp(self):
        self.base = os.path.realpath(tempfile.mkdtemp(prefix="agent-mirror-cfg-"))
        self.addCleanup(shutil.rmtree, self.base, True)
        self.home = os.path.join(self.base, "home")
        for n in ("flag", "env", "cfg"):
            os.makedirs(os.path.join(self.base, n))
        write(
            os.path.join(self.home, ".config", "agent-mirror", "config.json"),
            json.dumps({"store_path": os.path.join(self.base, "cfg")}),
        )

    def plat(self, **env):
        e = {"HOME": self.home}
        e.update(env)
        return Platform("linux", e)

    def test_flag_then_env_then_config(self):
        flag, env, cfg = (os.path.join(self.base, n) for n in ("flag", "env", "cfg"))
        p = self.plat(AGENT_MIRROR_STORE=env)
        self.assertEqual(resolve_store_path(flag, p), (flag, "--store"))
        self.assertEqual(resolve_store_path(None, p), (env, "AGENT_MIRROR_STORE"))
        path, source = resolve_store_path(None, self.plat())
        self.assertEqual(path, cfg)
        self.assertTrue(source.startswith("config "))
        os.remove(os.path.join(self.home, ".config", "agent-mirror", "config.json"))
        self.assertEqual(resolve_store_path(None, self.plat()), (None, None))

    def test_xdg_config_home_moves_the_config(self):
        xdg = os.path.join(self.base, "xdg")
        write(
            os.path.join(xdg, "agent-mirror", "config.json"), json.dumps({"store_path": os.path.join(self.base, "env")})
        )
        path, _ = resolve_store_path(None, self.plat(XDG_CONFIG_HOME=xdg))
        self.assertEqual(path, os.path.join(self.base, "env"))

    def test_context_store_and_commands_source(self):
        ctx = Context(store_flag=os.path.join(self.base, "flag"), plat=self.plat())
        self.assertEqual(ctx.require_store().root, os.path.join(self.base, "flag"))
        self.assertEqual(ctx.store_source_for_commands(), "flag")
        ctx = Context(plat=self.plat(AGENT_MIRROR_STORE=os.path.join(self.base, "env")))
        self.assertEqual(ctx.store_source_for_commands(), "env")
        ctx = Context(plat=self.plat())
        self.assertIsNone(ctx.store_source_for_commands())
        self.assertEqual(ctx.require_store().root, os.path.join(self.base, "cfg"))


class StoreCommandsTest(ToolTest):
    def test_commands_explain_init_store_when_no_store(self):
        for args in (["install"], ["sync"], ["session-start"], ["cloud", "render", "--project", "demo"]):
            with self.subTest(args=args):
                rc, out = self.run_tool(*args, with_store=False)
                self.assertEqual(rc, 2, out)
                self.assertIn("agent-mirror init-store PATH", out)
        rc, out = self.run_tool("doctor", with_store=False)
        self.assertEqual(rc, 0, out)
        self.assertIn("store: none", out)

    def test_newer_store_schema_warns(self):
        write(self.s("store.json"), json.dumps({"schema": 99, "tool": {"repo": "x", "ref": "main"}}))
        rc, out = self.run_tool("doctor")
        self.assertEqual(rc, 0, out)
        self.assertIn("schema 99 is newer than this agent-mirror understands (1)", out)

    def test_init_store_create_commit_and_config(self):
        origin = os.path.join(self.base, "store-origin.git")
        self.git("init", "--bare", "-q", origin, cwd=self.base)
        target = os.path.join(self.base, "newstore")
        rc, out = self.run_tool("init-store", target, "--remote", origin, "--commit", with_store=False)
        self.assertEqual(rc, 0, out)
        for rel in (
            "AGENTS.md",
            "CLAUDE.md",
            "store.json",
            ".gitattributes",
            ".gitignore",
            "projects/_example/env.json",
        ):
            self.assertTrue(os.path.isfile(os.path.join(target, rel)), rel)
        self.assertTrue(os.path.isdir(os.path.join(target, "skills")))
        self.assertTrue(os.path.isdir(os.path.join(target, "memory")))
        self.assertIn("merge=union", read(os.path.join(target, ".gitattributes")))
        meta = json.loads(read(os.path.join(target, "store.json")))
        self.assertEqual(meta["schema"], 1)
        self.assertEqual(meta["tool"]["ref"], "main")
        self.assertEqual(self.git("rev-parse", "--abbrev-ref", "HEAD", cwd=target), "main")
        self.assertEqual(self.git("rev-list", "--count", "HEAD", cwd=target), "1")
        self.assertEqual(self.git("remote", "get-url", "origin", cwd=target), origin)
        cfg = json.loads(read(self.h(".config", "agent-mirror", "config.json")))
        self.assertEqual(cfg["store_path"], target)
        self.assertEqual(cfg["store_remote"], origin)
        # every command now finds the store from the config alone
        rc, out = self.run_tool("doctor", with_store=False)
        self.assertEqual(rc, 0, out)
        self.assertIn("store: %s (from config" % target, out)
        # a second create into the same folder is refused
        rc, out = self.run_tool("init-store", target, with_store=False)
        self.assertEqual(rc, 2, out)
        self.assertIn("--adopt", out)

    def test_init_store_dry_run_writes_nothing(self):
        target = os.path.join(self.base, "drystore")
        rc, out = self.run_tool("init-store", target, "--dry-run", with_store=False)
        self.assertEqual(rc, 0, out)
        self.assertFalse(os.path.exists(target))
        self.assertFalse(os.path.exists(self.h(".config", "agent-mirror", "config.json")))

    def test_init_store_adopt(self):
        os.remove(self.s("store.json"))
        rc, out = self.run_tool("init-store", self.store, "--adopt", with_store=False)
        self.assertEqual(rc, 0, out)
        self.assertTrue(os.path.isfile(self.s("store.json")))
        cfg = json.loads(read(self.h(".config", "agent-mirror", "config.json")))
        self.assertEqual(cfg["store_path"], self.store)
        self.assertIn("not a git clone", out)
        # adopting a folder that lacks the store parts is refused and says what is missing
        half = os.path.join(self.base, "half")
        write(os.path.join(half, "AGENTS.md"), "x\n")
        rc, out = self.run_tool("init-store", half, "--adopt", with_store=False)
        self.assertEqual(rc, 2, out)
        self.assertIn("skills (folder)", out)
        self.assertIn("memory (folder)", out)

    def test_refuses_store_inside_a_project_repo(self):
        repo = os.path.join(self.base, "someproject")
        os.makedirs(repo)
        subprocess.run(["git", "init", "-q", repo], env=clean_env(self.home), check=True)
        for extra in ([], ["--adopt"]):
            with self.subTest(extra=extra):
                rc, out = self.run_tool("init-store", os.path.join(repo, "store"), *extra, with_store=False)
                self.assertEqual(rc, 2, out)
                self.assertIn("is inside the git repo", out)
        self.assertFalse(os.path.exists(os.path.join(repo, "store")))
        rc, out = self.run_tool("init-store", self.h(".claude", "store"), with_store=False)
        self.assertEqual(rc, 2, out)
        self.assertIn("inside the agent home folder", out)


if __name__ == "__main__":
    unittest.main()
