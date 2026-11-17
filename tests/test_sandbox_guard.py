"""The test-sandbox tripwire: nothing may touch a home folder outside the sandbox.

History: an early test run on Windows isolated HOME only, while the program used
USERPROFILE and APPDATA there, and it changed the real user profile. These tests
prove the guard that makes that impossible.
"""

import json
import os
import sys
import tempfile
import unittest

from helpers import setUpModule  # noqa: F401 (the sandbox guard runs first)
from helpers import ToolTest, install_fake, path_inside, remove_tree

from agent_mirror.platforms import SANDBOX_EXIT, Platform

MARKER_TOOL = r"""
import os, sys
open(os.environ["FAKE_MARKER"], "a").write("ran %s\n" % sys.argv[1:])
"""


def listing(root):
    found = []
    for r, dirs, files in os.walk(root):
        for n in dirs + files:
            found.append(os.path.relpath(os.path.join(r, n), root))
    return sorted(found)


class SandboxGuardTest(ToolTest):
    def outside(self):
        path = os.path.realpath(tempfile.mkdtemp(prefix="agent-mirror-outside-"))
        self.addCleanup(remove_tree, path)
        return path

    def test_debug_paths_resolves_everything_inside_the_sandbox(self):
        rc, out = self.run_tool("debug", "paths")
        self.assertEqual(rc, 0, out)
        data = json.loads(out)
        self.assertEqual(data["outside_sandbox"], [])
        for name, path in data["locations"].items():
            self.assertTrue(path_inside(path, self.home), "%s=%s" % (name, path))

    def test_tripwire_stops_every_command_when_home_points_outside(self):
        outside = self.outside()
        env = {
            "AGENT_MIRROR_HOME": None,
            "HOME": outside,
            "USERPROFILE": outside,
            "APPDATA": os.path.join(outside, "AppData", "Roaming"),
            "LOCALAPPDATA": os.path.join(outside, "AppData", "Local"),
            "XDG_CONFIG_HOME": os.path.join(outside, ".config"),
            "XDG_CACHE_HOME": os.path.join(outside, ".cache"),
        }
        before = listing(outside)
        for args in (
            ["detect"],
            ["doctor"],
            ["install"],
            ["hooks", "install"],
            ["init-store", self.s()],
            ["sync"],
            ["timer", "install", "--dry-run"],
        ):
            with self.subTest(args=args):
                rc, out = self.run_tool(*args, env=env)
                self.assertEqual(rc, SANDBOX_EXIT, out)
                self.assertIn("so nothing runs", out)
        rc, out = self.run_tool("debug", "paths", env=env)
        self.assertEqual(rc, SANDBOX_EXIT, out)
        self.assertIn("home", json.loads(out)["outside_sandbox"])
        self.assertEqual(listing(outside), before)
        self.assertEqual(listing(outside), [])

    def test_an_outside_override_is_ignored_in_override_mode(self):
        outside = self.outside()
        rc, out = self.run_tool(
            "debug",
            "paths",
            env={"CLAUDE_CONFIG_DIR": outside, "CODEX_HOME": outside, "APPDATA": outside, "XDG_CONFIG_HOME": outside},
        )
        self.assertEqual(rc, 0, out)
        for path in json.loads(out)["locations"].values():
            self.assertFalse(path_inside(path, outside), path)

    def test_real_schedulers_and_clipboard_tools_are_never_started(self):
        outside = self.outside()
        marker = os.path.join(self.base, "marker.txt")
        names = ("systemctl", "launchctl", "schtasks", "xclip", "clip", "powershell")
        for name in names:
            folder = install_fake(outside, name, MARKER_TOOL, folder="realbin")
        env = {"PATH": folder, "FAKE_MARKER": marker, "DISPLAY": ":0"}
        rc, out = self.run_tool("timer", "status", env=env)
        self.assertIn("refusing to start the real", out)
        rc, out = self.run_tool("clip", "check", env=env)
        self.assertIn(rc, (SANDBOX_EXIT, 3), out)
        self.assertFalse(os.path.exists(marker), "a real tool outside the sandbox was started")

    def test_tool_guard_in_process(self):
        root = os.path.realpath(tempfile.mkdtemp(prefix="agent-mirror-sb-"))
        self.addCleanup(remove_tree, root)
        plat = Platform(
            None, {"AGENT_MIRROR_HOME": os.path.join(root, "home"), "AGENT_MIRROR_TEST_SANDBOX": root, "PATH": ""}
        )
        self.assertIn("refusing", plat.tool_blocked("systemctl"))
        self.assertIsNone(plat.tool_blocked("git"))
        inside = install_fake(root, "schtasks", MARKER_TOOL)
        name = os.path.join(inside, "schtasks.cmd" if os.name == "nt" else "schtasks")
        self.assertIsNone(plat.tool_blocked(name))
        self.assertIsNone(Platform(None, {"AGENT_MIRROR_HOME": root}).tool_blocked("systemctl"))
        self.assertTrue(sys.executable)


if __name__ == "__main__":
    unittest.main()
