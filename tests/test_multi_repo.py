"""Sessions that start in a folder above several clones (the environment is per user, not per repo)."""

import json
import os
import unittest

from helpers import ToolTest, memory_dir, write

from agent_mirror.projects import remote_repo_path, repo_matches


class RemoteMatchTest(unittest.TestCase):
    def test_url_forms(self):
        for url in (
            "https://github.com/Example-Org/Other.git",
            "https://github.com/example-org/other",
            "https://x-access-token:" + "abc" + "@github.com/example-org/other.git/",
            "git@github.com:Example-Org/Other.git",
            "ssh://git@github.com:22/example-org/other",
            "git@github.com:example-org/other",
        ):
            with self.subTest(url=url):
                self.assertEqual(remote_repo_path(url), "example-org/other")
                self.assertTrue(repo_matches(url, "Example-Org/Other"))
                self.assertTrue(repo_matches(url, "example-org/other.git"))
                self.assertFalse(repo_matches(url, "example-org/oth"))
        self.assertTrue(repo_matches("https://gitlab.example.com/group/sub/app.git", "sub/app"))
        for local in ("/srv/git/app.git", "C:\\repos\\app", "C:/repos/app", ""):
            self.assertIsNone(remote_repo_path(local), local)


class MultiRepoSessionTest(ToolTest):
    def setUp(self):
        super().setUp()
        self.parent = os.path.join(self.base, "session")
        self.known = os.path.join(self.parent, "demo-clone")
        self.unknown = os.path.join(self.parent, "tools", "random-lib")
        self.other = os.path.join(self.parent, "other-clone")
        os.makedirs(os.path.join(self.parent, "node_modules", "pkg", ".git"))  # vendor folders are skipped
        self.clone(self.known, "https://github.com/Example/demo.git")
        self.clone(self.unknown, "git@github.com:someone/random-lib.git")
        marker = "open('other_sync_ran.marker', 'w').close()"
        write(
            self.s("projects", "other", "env.json"),
            json.dumps({"repo": "Example-Org/Other", "dependency_sync": ["python3", "-c", marker]}),
        )
        write(self.s("memory", "other", "MEMORY.md"), "- [O](o.md): other\n")

    def clone(self, path, origin):
        os.makedirs(path)
        self.git("init", "-q", cwd=path)
        self.git("remote", "add", "origin", origin, cwd=path)

    def session(self, *extra, **kw):
        env = {"AGENT_RUNTIME": "claude-cloud", "CLAUDE_PROJECT_DIR": self.parent}
        env.update(kw.get("env") or {})
        return self.run_tool("session-start", *extra, env=env)

    def test_one_known_one_unknown(self):
        rc, out = self.session()
        self.assertEqual(rc, 0, out)
        self.assertIn("project demo: %s" % self.known, out)
        self.assertIn("no project entry, nothing to carry: %s" % self.unknown, out)
        self.assertNotIn("node_modules", out)
        self.assertTrue(os.path.exists(os.path.join(self.known, "dep_sync_ran.marker")), out)
        self.assertFalse(os.path.exists(os.path.join(self.unknown, "dep_sync_ran.marker")))
        self.assertEqual(os.readlink(memory_dir(self.home, self.known)), self.s("memory", "demo"))
        # exactly one project matched: the session folder's own memory folder is linked too
        self.assertEqual(os.readlink(memory_dir(self.home, self.parent)), self.s("memory", "demo"))
        self.assertFalse(os.path.lexists(memory_dir(self.home, self.unknown)))
        self.assertIn("memory index: " + self.s("memory", "demo", "MEMORY.md"), out)

    def test_two_known_ssh_and_https(self):
        self.clone(self.other, "git@github.com:example-org/other.git")
        rc, out = self.session()
        self.assertEqual(rc, 0, out)
        self.assertEqual(os.readlink(memory_dir(self.home, self.known)), self.s("memory", "demo"))
        self.assertEqual(os.readlink(memory_dir(self.home, self.other)), self.s("memory", "other"))
        self.assertTrue(os.path.exists(os.path.join(self.other, "other_sync_ran.marker")), out)
        self.assertFalse(os.path.lexists(memory_dir(self.home, self.parent)))
        self.assertIn("memory index: " + self.s("memory", "demo", "MEMORY.md"), out)
        self.assertIn("memory index: " + self.s("memory", "other", "MEMORY.md"), out)
        self.assertIn("UNVERIFIED", out)
        rc, out = self.run_tool("doctor", "--project-path", self.parent)
        self.assertEqual(rc, 0, out)
        self.assertIn("project demo: %s" % self.known, out)
        self.assertIn("project other: %s" % self.other, out)
        rc, out = self.run_tool("box", "index", "--dry-run", "--project-path", self.parent)
        self.assertEqual(rc, 2, out)
        self.assertIn("several projects found", out)

    def test_no_matches_is_not_an_error(self):
        empty = os.path.join(self.base, "empty-session")
        self.clone(os.path.join(empty, "lib"), "https://github.com/someone/lib.git")
        rc, out = self.session(env={"CLAUDE_PROJECT_DIR": empty})
        self.assertEqual(rc, 0, out)
        self.assertIn("no project entry, nothing to carry", out)
        self.assertIn("memory index: none (no project matched", out)
        rc, out = self.session(env={"CLAUDE_PROJECT_DIR": os.path.join(self.base, "work")})
        self.assertEqual(rc, 0, out)

    def test_inside_one_clone_uses_only_that_clone(self):
        rc, out = self.session(env={"CLAUDE_PROJECT_DIR": self.known})
        self.assertEqual(rc, 0, out)
        self.assertIn("session repo: %s" % self.known, out)
        self.assertNotIn(self.unknown, out)

    def test_forced_project(self):
        rc, out = self.session("--project", "other", "--project-path", self.unknown)
        self.assertEqual(rc, 0, out)
        self.assertIn("project: other at %s (forced with --project)" % self.unknown, out)
        self.assertEqual(os.readlink(memory_dir(self.home, self.unknown)), self.s("memory", "other"))
        self.assertFalse(os.path.lexists(memory_dir(self.home, self.known)))
        self.assertIn("memory index: " + self.s("memory", "other", "MEMORY.md"), out)


if __name__ == "__main__":
    unittest.main()
