"""Values written by `box rclone-env --out` must survive a shell and a dotenv reader."""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "plugin"))

from agent_mirror.box.extras import quote_env_value  # noqa: E402
from helpers import setUpModule  # noqa: E402,F401 (the sandbox guard runs first)

JSON_TOKEN = '{"access_token":"abc.def","token_type":"Bearer","refresh_token":"M.C5_x!y*z$1","expiry":"2030-01-01T00:00:00Z"}'


class EnvQuoting(unittest.TestCase):
    def test_plain_values_stay_bare(self):
        self.assertEqual(quote_env_value("onedrive"), "onedrive")
        self.assertEqual(quote_env_value("personal"), "personal")

    def test_json_token_is_single_quoted(self):
        self.assertEqual(quote_env_value(JSON_TOKEN), "'%s'" % JSON_TOKEN)

    def test_value_with_single_quote_uses_escaped_double_quotes(self):
        quoted = quote_env_value("it's $HOME")
        self.assertTrue(quoted.startswith('"') and quoted.endswith('"'))
        self.assertIn("\\$HOME", quoted)

    @unittest.skipUnless(shutil.which("sh"), "needs a POSIX shell")
    def test_shell_reads_the_value_back_unchanged(self):
        for value in (JSON_TOKEN, "it's $HOME `x`", "plain", "with space"):
            with tempfile.TemporaryDirectory() as tmp:
                path = os.path.join(tmp, "values.env")
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write("TOKEN=%s\n" % quote_env_value(value))
                got = subprocess.run(
                    ["sh", "-c", '. "$1"; printf %s "$TOKEN"', "sh", path],
                    stdout=subprocess.PIPE,
                    universal_newlines=True,
                    check=True,
                ).stdout
                self.assertEqual(got, value)


if __name__ == "__main__":
    unittest.main()
