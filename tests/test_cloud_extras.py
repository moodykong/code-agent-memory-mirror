"""Items from real cloud use: companion repos, probe --full with only archives, doctor's tool lines,
and the cloud self-update of agent-mirror's own clone (a local bare repo stands in for its origin)."""

import json
import os
import shutil
import unittest

from helpers import setUpModule  # noqa: F401 (the sandbox guard runs first)
from helpers import PLUGIN, ToolTest, is_link, read, readlink, write

from agent_mirror.selfupdate import same_repo

COMPANIONS = [
    {"repo": "https://github.com/example-org/helper-lib", "ref": "v1.2.0", "dir": "helper-lib"},
    {"repo": "https://gitlab.example.com/group/tools.git", "ref": "main", "dir": "tools"},
]


class CompanionTest(ToolTest):
    def setUp(self):
        super().setUp()
        self.set_env_json(companion_repos=COMPANIONS)

    def test_render_adds_a_clone_step_and_hosts(self):
        rc, out = self.run_tool("cloud", "render", "--project", "demo")
        self.assertEqual(rc, 0, out)
        for script in ("claude/setup.sh", "codex/vm-install.sh", "codex/container-setup.sh"):
            text = read(self.s("projects", "demo", "cloud", *script.split("/")))
            self.assertIn('d="$HOME/companions/helper-lib"', text, script)
            self.assertIn('remote add origin "https://github.com/example-org/helper-lib"', text)
            self.assertIn('fetch -q --depth 1 origin "v1.2.0"', text)
            self.assertIn("checkout -q --force FETCH_HEAD ) || true", text)
            self.assertIn('d="$HOME/companions/tools"', text)
            step = [ln for ln in text.splitlines() if "companions/" in ln]
            self.assertTrue(step and not any("x-access-token" in ln or "STORE_REPO_TOKEN" in ln for ln in step), step)
        hosts = read(self.s("projects", "demo", "cloud", "claude", "allowed-hosts.txt")).split()
        self.assertIn("gitlab.example.com", hosts)

    def test_render_refuses_unsafe_entries(self):
        for bad in (
            {"repo": "http://github.com/a/b", "ref": "main", "dir": "b"},
            {"repo": "https://user:pw" + "@github.com/a/b", "ref": "main", "dir": "b"},
            {"repo": "git@github.com:a/b.git", "ref": "main", "dir": "b"},
            {"repo": "https://github.com/a/b", "ref": "main; rm -rf /", "dir": "b"},
            {"repo": "https://github.com/a/b", "ref": "main", "dir": "../b"},
            {"repo": "https://github.com/a/b", "ref": "main", "dir": "x/y"},
            {"repo": "https://github.com/a/b", "ref": "main", "dir": ".."},
            {"repo": "https://github.com/a/b", "ref": "main", "dir": "..."},
            {"repo": "https://github.com/a/../b", "ref": "main", "dir": "b"},
        ):
            with self.subTest(bad=bad):
                self.set_env_json(companion_repos=[bad])
                rc, out = self.run_tool("cloud", "render", "--project", "demo", "--dry-run")
                self.assertEqual(rc, 2, out)
                self.assertIn("compan", out)

    def test_cloud_session_start_links_companions_next_to_the_clone(self):
        self.git("init", "-q", cwd=self.project)
        self.git("remote", "add", "origin", "https://github.com/Example/demo.git", cwd=self.project)
        write(self.h("companions", "helper-lib", "README.md"), "helper\n")
        write(self.h("companions", "tools", "README.md"), "tools\n")
        os.makedirs(os.path.join(self.base, "work", "tools"))  # something is already there: left alone
        env = {"AGENT_RUNTIME": "claude-cloud", "CLAUDE_PROJECT_DIR": self.project}
        rc, out = self.run_tool("session-start", env=env)
        self.assertEqual(rc, 0, out)
        link = os.path.join(self.base, "work", "helper-lib")
        self.assertTrue(is_link(link), out)
        self.assertEqual(os.path.normcase(readlink(link)), os.path.normcase(self.h("companions", "helper-lib")))
        self.assertEqual(read(os.path.join(link, "README.md")), "helper\n")
        self.assertIn("companion tools: something is already at", out)
        self.assertFalse(is_link(os.path.join(self.base, "work", "tools")))
        rc, out = self.run_tool("session-start", env=env)
        self.assertIn("already links to", out)
        # on a developer machine session-start never links companions
        shutil.rmtree(self.h("companions"))
        rc, out = self.run_tool("session-start", env={"CLAUDECODE": "1", "CLAUDE_PROJECT_DIR": self.project})
        self.assertNotIn("companion", out)


class DoctorToolsTest(ToolTest):
    def test_az_and_rclone_are_problems_only_when_a_box_needs_them(self):
        empty = os.path.join(self.base, "emptybin")
        os.makedirs(empty)
        rc, out = self.run_tool("doctor", env={"PATH": empty})
        self.assertEqual(rc, 0, out)
        self.assertIn("MISSING az (needed by the box of demo)", out)
        self.assertIn("PROBLEM  az is not installed, and the azure-blob box of demo needs it", out)
        self.assertIn("rclone not needed (no project's box uses rclone)", out)
        self.set_box(backend="rclone", inbound_remote="fakeremote:in", outbound_remote="fakeremote:out")
        rc, out = self.run_tool("doctor", env={"PATH": empty})
        self.assertIn("MISSING rclone (needed by the box of demo)", out)
        self.assertIn("az not needed (no project's box uses azure-blob)", out)
        self.assertNotIn("PROBLEM  az", out)
        self.assertIn("machine: ", out)
        self.assertIn("plan: ", out)


class ProbeArchiveTest(ToolTest):
    def test_probe_full_fetches_and_evicts_the_smallest_archived_file(self):
        ws = self.p("ws-1")
        write(os.path.join(ws, "a", "big.txt"), "b" * 400)
        write(os.path.join(ws, "a", "tiny.txt"), "t\n")
        self.set_box(archives=["ws-*"], archive_large_file_bytes=1000, min_free_bytes=0)
        root = os.path.join(self.base, "containers")
        os.makedirs(root)
        az = self.fake_az()
        az["FAKE_AZ_ROOT"] = root
        rc, out = self.box_run("box", "push", az=dict(az, CLAUDECODE="1"))
        self.assertEqual(rc, 0, out)
        rc, out = self.box_run("box", "index", az=dict(az, CLAUDECODE="1"))
        self.assertEqual(rc, 0, out)
        shutil.rmtree(ws)
        env = dict(az, AGENT_RUNTIME="claude-cloud")
        rc, out = self.run_tool(
            "probe", "--full", "--project", "demo", "--project-path", self.project, env=env, timeout=300
        )
        self.assertEqual(rc, 0, out)
        self.assertIn("fetch ws-1/a/tiny.txt (the smallest file of an archived folder", out)
        self.assertIn("evict: deleted 1 file(s)", out)
        self.assertFalse(os.path.exists(os.path.join(ws, "a", "tiny.txt")))
        # after its send test, probe lists the newest sessions and finds the one it just wrote
        self.assertRegex(out, r"the session just written \(claude-cloud-\d{8}T\d{6}Z-[0-9a-f]{6}\) is listed: yes")


class SelfUpdateTest(ToolTest):
    def make_tool_origin(self):
        """A bare repo holding a copy of the plugin: main, plus a newer commit on branch next."""
        work = os.path.join(self.base, "toolwork")
        shutil.copytree(PLUGIN, os.path.join(work, "plugin"), ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        self.git("init", "-q", "-b", "main", cwd=work)
        self.git("add", "-A", cwd=work)
        self.git("commit", "-q", "-m", "tool", cwd=work)
        self.bare = os.path.join(self.base, "tool-origin.git")
        self.git("init", "--bare", "-q", self.bare, cwd=self.base)
        self.git("remote", "add", "origin", self.bare, cwd=work)
        self.git("push", "-q", "origin", "main", cwd=work)
        # the tool clone as the cloud setup scripts make it: a shallow fetch of one ref
        self.clone = os.path.join(self.base, "toolclone")
        os.makedirs(self.clone)
        self.git("init", "-q", cwd=self.clone)
        self.git("remote", "add", "origin", self.bare, cwd=self.clone)
        self.git("fetch", "-q", "--depth", "1", "origin", "main", cwd=self.clone)
        self.git("checkout", "-q", "--force", "FETCH_HEAD", cwd=self.clone)
        init = os.path.join(work, "plugin", "agent_mirror", "__init__.py")
        text = read(init)
        write(init, text.replace('__version__ = "', '__version__ = "9.9.9-selfupdate+', 1))
        self.git("commit", "-q", "-am", "newer tool", cwd=work)
        self.git("push", "-q", "origin", "HEAD:refs/heads/next", cwd=work)
        self.newer = self.git("rev-parse", "HEAD", cwd=work)
        self.tool = os.path.join(self.clone, "plugin", "bin", "agent-mirror")

    def pin(self, repo, ref):
        write(
            self.s("store.json"), json.dumps({"schema": 1, "tool": {"repo": repo, "ref": ref}, "cloud_push": "inbox"})
        )

    def session(self, **extra):
        env = {"AGENT_RUNTIME": "claude-cloud"}
        env.update(extra)
        return self.run_tool(
            "session-start", "--project", "demo", "--project-path", self.project, env=env, tool=self.tool, timeout=300
        )

    def test_cloud_session_start_updates_the_tool_and_runs_the_new_code_once(self):
        self.make_tool_origin()
        self.pin(self.bare, "next")
        before = self.git("rev-parse", "HEAD", cwd=self.clone)
        rc, out = self.session()
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=self.clone), self.newer)
        self.assertIn("tool updated from %s to %s (ref next)" % (before[:12], self.newer[:12]), out)
        self.assertIn("agent-mirror 9.9.9-selfupdate+", out)  # the new code ran
        self.assertIn("running the updated code", out)
        self.assertEqual(out.count("session-start: runtime claude-cloud"), 2)  # once old, once new; no loop
        self.assertEqual(self.git("rev-parse", "--is-shallow-repository", cwd=self.clone), "true")
        rc, out = self.session()
        self.assertEqual(rc, 0, out)
        self.assertIn("already current", out)
        self.assertEqual(out.count("session-start: runtime claude-cloud"), 1)

    def test_no_update_when_the_origin_differs_or_on_a_local_runtime(self):
        self.make_tool_origin()
        before = self.git("rev-parse", "HEAD", cwd=self.clone)
        self.pin("https://github.com/example-org/another-tool", "next")
        rc, out = self.session()
        self.assertEqual(rc, 0, out)
        self.assertIn("not store.json's tool.repo", out)
        self.pin(self.bare, "next")
        rc, out = self.session(AGENT_RUNTIME=None, CLAUDECODE="1")
        self.assertEqual(rc, 0, out)
        self.assertNotIn("tool updated", out)
        self.pin(self.bare, "no-such-ref")
        rc, out = self.session()
        self.assertEqual(rc, 0, out)  # never fails the session
        self.assertIn("fetch of no-such-ref failed", out)
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=self.clone), before)

    def test_a_test_run_never_updates_the_clone_it_runs_from(self):
        # the template store.json points at the public tool repo, which is also this clone's origin
        rc, out = self.run_tool(
            "session-start", "--project", "demo", "--project-path", self.project, env={"AGENT_RUNTIME": "claude-cloud"}
        )
        self.assertEqual(rc, 0, out)
        self.assertIn("test sandbox: no self-update of a tool clone outside", out)

    def test_same_repo(self):
        self.assertTrue(same_repo("https://github.com/Owner/Name.git", "https://github.com/owner/name"))
        self.assertTrue(same_repo("git@github.com:owner/name.git", "https://github.com/owner/name/"))
        self.assertTrue(
            same_repo("https://x-access-token:abc" + "@github.com/owner/name", "https://github.com/owner/name")
        )
        self.assertFalse(same_repo("https://github.com/owner/name", "https://github.com/owner/other"))
        self.assertFalse(same_repo("", "https://github.com/owner/name"))


if __name__ == "__main__":
    unittest.main()
