"""The rclone box backend (fake rclone on PATH), cloud render, and probe with an empty config."""

import json
import os
import shlex
import shutil
import subprocess
import unittest

from helpers import setUpModule  # noqa: F401 (the sandbox guard runs first)
from helpers import FAKE_AZ, FAKE_RCLONE, ToolTest, clean_env, install_fake, read, write

from agent_mirror.box.rclone import rclone_pattern


class RcloneBoxTest(ToolTest):
    INBOUND = "fakeremote:box/in"

    def setUp(self):
        super().setUp()
        env = json.loads(read(self.s("projects", "demo", "env.json")))
        env["box"] = {
            "backend": "rclone",
            "inbound_remote": self.INBOUND,
            "outbound_remote": "fakeremote:box/out",
            "include": ["temporary/**", "figures/**"],
            "exclude": ["temporary/scratch/**", "**/__pycache__/**"],
        }
        write(self.s("projects", "demo", "env.json"), json.dumps(env))

    def rclone_env(self, files=None, **extra):
        fakebin = install_fake(self.base, "rclone", FAKE_RCLONE)
        self.rclone_log = os.path.join(self.base, "rclone_log.jsonl")
        env = {
            "PATH": fakebin + os.pathsep + os.environ.get("PATH", ""),
            "FAKE_RCLONE_LOG": self.rclone_log,
            "FAKE_RCLONE_INBOUND": self.INBOUND,
        }
        if files is not None:
            env["FAKE_RCLONE_FILES"] = os.path.join(self.base, "rclone_files.json")
            write(env["FAKE_RCLONE_FILES"], json.dumps(files))
        env.update(extra)
        return env

    def calls(self):
        if not os.path.isfile(self.rclone_log):
            return []
        return [json.loads(x)["argv"] for x in read(self.rclone_log).splitlines()]

    def test_pattern_translation(self):
        self.assertEqual(rclone_pattern("temporary/**"), "/temporary/**")
        self.assertEqual(rclone_pattern("**/__pycache__/**"), "__pycache__/**")
        self.assertEqual(rclone_pattern("*.env"), "*.env")
        self.assertEqual(rclone_pattern("/docs/slides/**"), "/docs/slides/**")

    def test_push_dry_run_and_cloud_refusal(self):
        rc, out = self.box_run("box", "push", "--dry-run", env=self.rclone_env(CLAUDECODE="1"), az={})
        self.assertEqual(rc, 0, out)
        self.assertIn(
            "would run: rclone copy %s fakeremote:box/in --filter '- .env' --filter '- *.env' "
            "--filter '- /_archives/**' --filter '- /.agent-mirror/**' --filter '- /_box/**' "
            "--filter '- /_agent_outbound/**' --filter '- /temporary/scratch/**' --filter '- __pycache__/**' "
            "--filter '+ /temporary/**' --filter '+ /figures/**' --filter '- **'" % shlex.quote(self.project),
            out,
        )
        rc, out = self.box_run("box", "push", "--dry-run", "--delete", env=self.rclone_env(CLAUDECODE="1"), az={})
        self.assertIn("would run: rclone sync ", out)
        self.assertIn("would write the writer marker fakeremote:box/in/_writer.json", out)
        reads = [c for c in self.calls() if c[:2] == ["copyto", "fakeremote:box/in/_writer.json"]]
        self.assertEqual(self.calls(), reads)  # a dry run only reads the writer marker
        os.remove(self.rclone_log)
        rc, out = self.box_run("box", "push", env=self.rclone_env(AGENT_RUNTIME="codex-cloud"), az={})
        self.assertEqual(rc, 2, out)
        self.assertIn("refusing box push on codex-cloud", out)
        self.assertEqual(self.calls(), [])

    def test_push_runs_copy_never_sync(self):
        rc, out = self.box_run("box", "push", env=self.rclone_env(CLAUDECODE="1"), az={})
        self.assertEqual(rc, 0, out)
        transfers = [c[0] for c in self.calls() if not any(a.endswith("_writer.json") for a in c)]
        self.assertEqual(transfers, ["copy"])

    def test_index_filters_and_writes_jsonl(self):
        files = ["temporary/a.txt", "temporary/scratch/b.txt", "figures/f.png", "other/x.txt"]
        rc, out = self.box_run("box", "index", env=self.rclone_env(files, AGENT_RUNTIME="claude-cloud"), az={})
        self.assertEqual(rc, 0, out)
        lines = [json.loads(x) for x in read(self.s("projects", "demo", "box_index.jsonl")).splitlines()]
        self.assertEqual([x["path"] for x in lines], ["figures/f.png", "temporary/a.txt"])
        self.assertIn("left out 2 files", out)
        self.assertEqual(self.calls()[0][:4], ["lsjson", "-R", "--files-only", "--no-mimetype"])

    def test_fetch_keeps_local_files_on_a_local_runtime(self):
        files = ["temporary/r/a.txt", "temporary/r/b.txt", "temporary/one.txt"]
        write(self.p("temporary", "r", "a.txt"), "local\n")
        rc, out = self.box_run("box", "fetch", "temporary/r", env=self.rclone_env(files, CLAUDECODE="1"), az={})
        self.assertEqual(rc, 0, out)
        self.assertEqual(read(self.p("temporary", "r", "a.txt")), "local\n")
        self.assertEqual(read(self.p("temporary", "r", "b.txt")), "remote temporary/r/b.txt")
        copy = [c for c in self.calls() if c[0] == "copy"][0]
        self.assertIn("--ignore-existing", copy)
        rc, out = self.box_run(
            "box", "fetch", "temporary/one.txt", env=self.rclone_env(files, AGENT_RUNTIME="claude-cloud"), az={}
        )
        self.assertEqual(rc, 0, out)
        copyto = [c for c in self.calls() if c[0] == "copyto"][0]
        self.assertNotIn("--ignore-existing", copyto)
        self.assertEqual(read(self.p("temporary", "one.txt")), "remote temporary/one.txt")
        rc, out = self.box_run("box", "fetch", "temporary/none", env=self.rclone_env(files, CLAUDECODE="1"), az={})
        self.assertEqual(rc, 1, out)
        self.assertIn("nothing found at fakeremote:box/in/temporary/none", out)
        rc, out = self.box_run(
            "box", "fetch", "temporary/x.txt", "--dry-run", env=self.rclone_env(files, CLAUDECODE="1"), az={}
        )
        self.assertIn("then, if it is a file: rclone copyto fakeremote:box/in/temporary/x.txt", out)
        self.assertIn("--ignore-existing", out)

    def test_send_folder_uses_a_file_list_without_env_or_excluded_files(self):
        for rel in ("out.csv", "sub/plot.png", ".env", "creds.env", "sub/__pycache__/m.pyc"):
            write(self.p("temporary", "run", *rel.split("/")), "x\n")
        rc, out = self.box_run("box", "send", "temporary/run", env=self.rclone_env(CLAUDECODE="1"), az={})
        self.assertEqual(rc, 0, out)
        self.assertIn("skipping 3 files", out)
        entry = [json.loads(x) for x in read(self.rclone_log).splitlines()][0]
        self.assertEqual(entry["files_from"], ["out.csv", "sub/plot.png"])
        argv = entry["argv"]
        self.assertEqual(argv[:2], ["copy", self.p("temporary", "run")])
        self.assertRegex(argv[2], r"^fakeremote:box/out/claude-local-\d{8}T\d{6}Z/temporary/run$")
        rc, out = self.box_run("box", "send", "temporary/run/.env", env=self.rclone_env(CLAUDECODE="1"), az={})
        self.assertEqual(rc, 2, out)

    def test_dry_run_hides_secret_values(self):
        secret = "s3cr3t-" + "value" * 3
        self.set_box(inbound_remote="fakeremote:" + secret + "/in")
        rc, out = self.box_run(
            "box", "push", "--dry-run", env=self.rclone_env(CLAUDECODE="1", MY_SERVICE_TOKEN=secret), az={}
        )
        self.assertEqual(rc, 0, out)
        self.assertIn("fakeremote:***/in", out)
        self.assertNotIn(secret, out)


class CloudRenderTest(ToolTest):
    def setUp(self):
        super().setUp()
        self.leaked = "TOPSECRET" + "value42"
        self.git("init", "-q", "-b", "main")
        self.git(
            "remote",
            "add",
            "origin",
            "https://x-access-token:" + self.leaked + "@github.com/example-org/my-agent-store.git",
        )
        write(
            self.s("store.json"),
            json.dumps(
                {
                    "schema": 1,
                    "tool": {"repo": "https://github.com/moodykong/code-agent-memory-mirror", "ref": "v0.1.0"},
                }
            ),
        )
        env = json.loads(read(self.s("projects", "demo", "env.json")))
        env.update(
            {
                "python": "3.11.9",
                "allowed_hosts": ["api.example.com", "*.example.net"],
                "env_var_names": ["EXAMPLE_API_TOKEN", "AGENT_RUNTIME"],
                "setup_commands": ["apt-get install -y -qq jq", "echo second"],
            }
        )
        write(self.s("projects", "demo", "env.json"), json.dumps(env))

    def test_render_all(self):
        value = "real-" + "token-value-9"
        rc, out = self.run_tool("cloud", "render", "--project", "demo", env={"EXAMPLE_API_TOKEN": value})
        self.assertEqual(rc, 0, out)
        cloud = self.s("projects", "demo", "cloud")
        files = sorted(
            os.path.relpath(os.path.join(r, f), cloud).replace(os.sep, "/") for r, _, fs in os.walk(cloud) for f in fs
        )
        self.assertEqual(
            files,
            [
                "claude/README.md",
                "claude/allowed-hosts.txt",
                "claude/env-vars.txt",
                "claude/runbook.json",
                "claude/setup.sh",
                "codex/README.md",
                "codex/allowed-hosts.txt",
                "codex/container-maintenance.sh",
                "codex/container-setup.sh",
                "codex/env-vars.txt",
                "codex/runbook.json",
                "codex/vm-install.sh",
                "codex/vm-start-skill.md",
            ],
        )
        setup = read(os.path.join(cloud, "claude", "setup.sh"))
        self.assertIn("https://x-access-token:<STORE_REPO_TOKEN>@github.com/example-org/my-agent-store.git", setup)
        self.assertIn('fetch -q --depth 1 origin "v0.1.0"', setup)
        self.assertIn('remote add origin "https://github.com/moodykong/code-agent-memory-mirror"', setup)
        self.assertIn('uv python install "3.11.9"', setup)
        self.assertIn("( apt-get install -y -qq jq ) || true", setup)
        self.assertIn("hooks install --agent claude", setup)
        self.assertIn("agent_setup_probe.txt", setup)
        self.assertTrue(setup.rstrip().endswith("exit 0"))
        self.assertTrue(os.access(os.path.join(cloud, "claude", "setup.sh"), os.X_OK))
        hosts = read(os.path.join(cloud, "claude", "allowed-hosts.txt")).split()
        for h in (
            "api.example.com",
            "*.example.net",
            "github.com",
            "pypi.org",
            "testacct.blob.core.windows.net",
            "login.microsoftonline.com",
        ):
            self.assertIn(h, hosts)
        claude_vars = read(os.path.join(cloud, "claude", "env-vars.txt")).split()
        self.assertIn("EXAMPLE_API_TOKEN=", claude_vars)
        self.assertIn("AZURE_CLIENT_SECRET=", claude_vars)
        self.assertNotIn("AGENT_RUNTIME=", claude_vars)
        codex_vars = read(os.path.join(cloud, "codex", "env-vars.txt")).split()
        self.assertEqual(codex_vars[0], "AGENT_RUNTIME=codex-cloud")
        container = read(os.path.join(cloud, "codex", "container-setup.sh"))
        self.assertIn("https://x-access-token:${STORE_REPO_TOKEN}@github.com/example-org/my-agent-store.git", container)
        self.assertIn('remote set-url origin "https://github.com/example-org/my-agent-store.git"', container)
        vm = read(os.path.join(cloud, "codex", "vm-install.sh"))
        self.assertIn('-name "my-agent-store"', vm)
        for rel in files:
            text = read(os.path.join(cloud, rel))
            self.assertNotIn(self.leaked, text, rel)
            self.assertNotIn(value, text, rel)
            self.assertNotRegex(text, r"@@[A-Z_]+@@")
        self.assertNotIn(self.leaked, out)
        for rel in files:
            if rel.endswith(".sh"):
                with open(os.path.join(cloud, *rel.split("/")), "rb") as fh:
                    self.assertNotIn(b"\r\n", fh.read(), "%s must use LF line endings on every platform" % rel)
        # `bash -n` syntax check: POSIX only (on Windows `bash` may be WSL's, which cannot read Windows paths)
        if shutil.which("bash") and os.name != "nt":
            for rel in files:
                if rel.endswith(".sh"):
                    proc = subprocess.run(
                        ["bash", "-n", os.path.join(cloud, rel)],
                        stderr=subprocess.PIPE,
                        universal_newlines=True,
                        env=clean_env(self.home),
                    )
                    self.assertEqual(proc.returncode, 0, "%s: %s" % (rel, proc.stderr))

    def test_one_vendor_and_missing_project(self):
        rc, out = self.run_tool("cloud", "render", "--project", "demo", "--vendor", "claude")
        self.assertEqual(rc, 0, out)
        self.assertFalse(os.path.exists(self.s("projects", "demo", "cloud", "codex")))
        rc, out = self.run_tool("cloud", "render", "--project", "nope")
        self.assertEqual(rc, 2, out)
        rc, out = self.run_tool("cloud", "render", "--project", "demo", "--dry-run")
        self.assertEqual(rc, 0, out)
        self.assertIn("would write", out)


class ProbeTest(ToolTest):
    def offline_env(self, **extra):
        """Fake az and rclone first on PATH: the real `az --version` checks online for updates."""
        install_fake(self.base, "az", FAKE_AZ)
        fakebin = install_fake(self.base, "rclone", FAKE_RCLONE)
        env = {
            "PATH": fakebin + os.pathsep + os.environ.get("PATH", ""),
            "FAKE_AZ_LOG": os.path.join(self.base, "az_log.jsonl"),
            "FAKE_RCLONE_LOG": os.path.join(self.base, "rclone_log.jsonl"),
        }
        env.update(extra)
        return env

    def test_full_probe_with_an_empty_config(self):
        empty_store = os.path.join(self.base, "emptystore")
        for d in ("skills", "memory"):
            os.makedirs(os.path.join(empty_store, d))
        write(os.path.join(empty_store, "AGENTS.md"), "x\n")
        rc, out = self.run_tool(
            "probe",
            "--full",
            "--project-path",
            self.project,
            env=self.offline_env(AGENT_MIRROR_STORE=empty_store),
            timeout=300,
        )
        self.assertEqual(rc, 0, out)
        for title in ("1 where am I", "4 environment variable names", "8 box", "10 dependency sync timing", "done"):
            self.assertIn("===== %s" % title, out)
        self.assertIn("skipped: no project matched", out)
        self.assertIn("skipped: no dependency_sync in the project config", out)
        self.assertIn("probe: 0 pass, 0 fail", out)
        rc, out = self.run_tool("probe", "--full", with_store=False, env=self.offline_env(), timeout=300)
        self.assertEqual(rc, 0, out)
        self.assertIn("store: none configured", out)

    def test_quick_probe_without_network_settings(self):
        rc, out = self.run_tool("probe", "--project", "demo", "--project-path", self.project, env={"DEMO_API_URL": "x"})
        self.assertEqual(rc, 0, out)
        self.assertIn("network: nothing to check", out)
        self.assertIn("env vars set (names only): DEMO_API_URL", out)
        self.assertIn("probe: 0 pass, 0 fail", out)


if __name__ == "__main__":
    unittest.main()
