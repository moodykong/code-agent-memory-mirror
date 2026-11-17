"""agent-mirror clip: backend selection (injected platform, env and PATH) and summaries that hide secrets."""

import os
import unittest

from helpers import ToolTest, install_fake, read, write

from agent_mirror.clip import backend, summarize

FAKE_XCLIP = r"""
import os, sys
store = os.environ["FAKE_CLIP_STORE"]
if "-out" in sys.argv:
    sys.stdout.write(open(store).read() if os.path.exists(store) else "")
else:
    open(store, "w").write(sys.stdin.read())
"""


def which_from(names):
    return lambda n: ("/fake/" + n) if n in names else None


class BackendTest(unittest.TestCase):
    def test_selection_per_platform(self):
        wsl = lambda: "Linux version 5.15 (microsoft-standard-WSL2)"  # noqa: E731
        plain = lambda: "Linux version 6.1 (gcc)"  # noqa: E731
        b = backend("win32", {}, which_from(["clip.exe", "powershell.exe"]))
        self.assertEqual(b["name"], "clip.exe and PowerShell")
        self.assertIn("Get-Clipboard -Raw", b["paste"][-1])
        self.assertIsNone(backend("win32", {}, which_from([])))
        b = backend("linux", {}, which_from(["clip.exe", "powershell.exe", "xclip"]), read_version=wsl)
        self.assertEqual(b["name"], "clip.exe and PowerShell")
        b = backend("linux", {"DISPLAY": ":0"}, which_from(["xclip"]), read_version=wsl)
        self.assertEqual(b["name"], "xclip")  # WSL without the Windows tools on PATH falls back
        self.assertEqual(backend("darwin", {}, which_from(["pbcopy", "pbpaste"]))["copy"], ["pbcopy"])
        both = which_from(["wl-copy", "wl-paste", "xclip", "xsel"])
        self.assertEqual(
            backend("linux", {"WAYLAND_DISPLAY": "w", "DISPLAY": ":0"}, both, read_version=plain)["name"],
            "wl-copy and wl-paste",
        )
        self.assertEqual(backend("linux", {"DISPLAY": ":0"}, both, read_version=plain)["name"], "xclip")
        self.assertEqual(backend("linux", {"DISPLAY": ":0"}, which_from(["xsel"]), read_version=plain)["name"], "xsel")
        self.assertIsNone(backend("linux", {}, both, read_version=plain))

    def test_summaries(self):
        text = "API_TOKEN=abcdef123456\nexport OTHER='two words'\nplain line\n"
        secret = "\n".join(summarize(text, True))
        self.assertIn("API_TOKEN (12 characters)", secret)
        self.assertIn("OTHER (11 characters)", secret)
        self.assertIn("line 3: 10 characters", secret)
        self.assertNotIn("abcdef", secret)
        self.assertNotIn("two words", secret)
        self.assertEqual(
            summarize(text, False), ["3 line(s), %d characters; first line: API_TOKEN=abcdef123456" % len(text)]
        )


class ClipCommandTest(ToolTest):
    def test_copy_check_clear_with_a_fake_xclip(self):
        fakebin = install_fake(self.base, "xclip", FAKE_XCLIP)
        store = os.path.join(self.base, "clipboard.txt")
        env = {"PATH": fakebin, "DISPLAY": ":0", "FAKE_CLIP_STORE": store}
        values = os.path.join(self.base, "values.env")
        write(values, "API_TOKEN=abcdef123456\nOTHER=x\n")
        rc, out = self.run_tool("clip", "copy", values, "--secret", env=env)
        self.assertEqual(rc, 0, out)
        self.assertEqual(read(store), "API_TOKEN=abcdef123456\nOTHER=x\n")
        self.assertIn("API_TOKEN (12 characters)", out)
        self.assertNotIn("abcdef", out)
        rc, out = self.run_tool("clip", "check", "--secret", env=env)
        self.assertEqual(rc, 0, out)
        self.assertIn("OTHER (1 characters)", out)
        self.assertNotIn("abcdef", out)
        rc, out = self.run_tool("clip", "copy", values, env=env)
        self.assertIn("2 line(s)", out)
        self.assertNotIn("abcdef", out)
        rc, out = self.run_tool("clip", "check", env=env)
        self.assertIn("first line: API_TOKEN=abcdef123456", out)
        rc, out = self.run_tool("clip", "clear", env=env)
        self.assertEqual(rc, 0, out)
        self.assertEqual(read(store), "")

    def test_no_backend_is_exit_3(self):
        empty = os.path.join(self.base, "emptybin")
        os.makedirs(empty)
        rc, out = self.run_tool("clip", "check", env={"PATH": empty})
        self.assertEqual(rc, 3, out)
        self.assertIn("no clipboard tool found", out)


if __name__ == "__main__":
    unittest.main()
