"""A cloud machine must never publish a machine profile.

The vendor runs the setup script before it sets its own session markers, so during setup the
runtime would look like a developer machine. The rendered scripts export AGENT_RUNTIME, and sync
on a cloud runtime leaves machines/ out even if a profile was written by mistake.
"""

import os
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from helpers import ToolTest, clean_env, setUpModule, write  # noqa: E402,F401


class CloudRuntimeMarkerTest(ToolTest):
    def test_rendered_setup_scripts_export_the_cloud_runtime(self):
        self.git("init", "-q", "-b", "main")
        self.git("remote", "add", "origin", "https://github.com/example-org/my-agent-store.git")
        rc, out = self.run_tool("cloud", "render", "--project", "demo")
        self.assertEqual(rc, 0, out)
        cloud = self.s("projects", "demo", "cloud")
        expected = {
            "claude/setup.sh": "claude-cloud",
            "codex/vm-install.sh": "codex-cloud",
            "codex/container-setup.sh": "codex-cloud",
            "codex/container-maintenance.sh": "codex-cloud",
        }
        for rel, runtime in expected.items():
            with open(os.path.join(cloud, *rel.split("/")), encoding="utf-8") as fh:
                text = fh.read()
            self.assertIn("export AGENT_RUNTIME=%s" % runtime, text, rel)
            # the marker comes before the first agent-mirror call
            self.assertLess(text.index("export AGENT_RUNTIME"), text.index('"$AM"'), rel)

    def test_install_as_cloud_writes_no_machine_profile(self):
        rc, out = self.run_tool(
            "install", "--agents", "claude", env={"AGENT_RUNTIME": "claude-cloud"}
        )
        self.assertEqual(rc, 0, out)
        self.assertFalse(os.path.exists(self.s("machines")), out)

    def test_cloud_sync_leaves_machine_profiles_out(self):
        self.init_git()
        write(self.s("machines", "stray.json"), '{"name": "stray"}\n')
        write(self.s("memory", "demo", "note.md"), "---\nname: note\ndescription: d\nmetadata:\n  type: project\n---\nx\n")
        rc, out = self.run_tool("sync", env={"AGENT_RUNTIME": "claude-cloud"})
        self.assertEqual(rc, 0, out)
        branches = self.git("for-each-ref", "--format=%(refname:short)", "refs/heads/inbox/", cwd=self.origin).split()
        self.assertEqual(len(branches), 1, out)
        files = self.git("diff", "--name-only", "main", branches[0], cwd=self.origin).split()
        self.assertTrue(files, out)
        self.assertTrue(all(f.startswith("memory/") for f in files), files)


if __name__ == "__main__":
    unittest.main()
