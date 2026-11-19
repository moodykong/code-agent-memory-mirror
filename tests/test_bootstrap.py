"""0.5.2: the fixed cloud setup script, `cloud bootstrap`, drift repair at cloud session start, and the
session-start step log.

The simulations run rendered setup scripts with bash on a fake cloud machine: a home folder inside the
sandbox, local bare repos standing in for the store remote and the agent-mirror remote (the https URLs
in the script text are replaced with their paths), and the script's link into /usr/local/bin redirected
into the sandbox. Every agent-mirror command the scripts start gets the sandbox guard's environment.
"""

import json
import os
import shutil
import subprocess
import sys
import unittest

from helpers import setUpModule  # noqa: F401 (the sandbox guard runs first)
from helpers import PLUGIN, REPO, ToolTest, read, sandbox_env, write

TOOL_URL = "https://github.com/example-org/agent-mirror-tool"
STORE_URL = "https://github.com/example-org/my-agent-store.git"
TOKEN_URL = "https://x-access-token:<STORE_REPO_TOKEN>@github.com/example-org/my-agent-store.git"
OLD_SCRIPT = os.path.join(REPO, "tests", "fixtures", "claude-setup-0.5.1.sh")
BASH = None if os.name == "nt" else shutil.which("bash")
CAN_RUN_SCRIPTS = bool(BASH and shutil.which("timeout"))
NO_SCRIPTS = "needs bash and timeout (the setup scripts are bash scripts for Linux cloud machines)"
NO_BASH = "needs bash (setup_commands run with bash -c, as on a Linux cloud machine)"
OUTPUT_MARKER = "OUTPUT-" + "MARKER-7f3a"  # printed by a command; must never reach a log


def last_record(home):
    with open(os.path.join(home, ".cache", "agent-mirror", "session-start.jsonl"), encoding="utf-8") as fh:
        lines = [ln for ln in fh.read().splitlines() if ln.strip()]
    return json.loads(lines[-1]), "\n".join(lines)


def steps_of(record):
    return dict((s["name"], s["status"]) for s in record["steps"])


class StepLogTest(ToolTest):
    def test_session_start_records_each_step_without_any_output(self):
        say = "import sys; print(%r); sys.stderr.write(%r)" % (OUTPUT_MARKER, OUTPUT_MARKER)
        self.set_env_json(
            session_start_commands=[[sys.executable, "-c", say], [sys.executable, "-c", "import sys; sys.exit(3)"]]
        )
        here = ("--project", "demo", "--project-path", self.project)
        rc, out = self.run_tool("session-start", *here, env={"AGENT_RUNTIME": "claude-cloud"})
        self.assertEqual(rc, 0, out)
        self.assertIn("timing: session-start took", out)
        rec, text = last_record(self.home)
        self.assertEqual((rec["schema"], rec["command"], rec["runtime"]), (1, "session-start", "claude-cloud"))
        steps = steps_of(rec)
        for name in ("store pull", "tool update", "setup drift check", "dependency sync (demo)", "linking"):
            self.assertIn(name, steps)
        self.assertEqual(steps["session start command 1 of 2 (demo)"], "ok")
        self.assertEqual(steps["session start command 2 of 2 (demo)"], "failed (exit 3)")
        self.assertEqual(steps["setup drift check"], "skipped (no stamp)")
        self.assertEqual(rec["failed"], ["session start command 2 of 2 (demo)"])
        for s in rec["steps"]:
            self.assertGreaterEqual(s["seconds"], 0)
            self.assertRegex(s["started"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
            self.assertRegex(s["ended"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertNotIn(OUTPUT_MARKER, text)  # names and timings only, never output
        # probe shows the last record briefly
        rc, out = self.run_tool("probe", *here, env={"AGENT_RUNTIME": "claude-cloud"})
        self.assertEqual(rc, 0, out)
        self.assertIn("last session-start: ", out)
        self.assertRegex(out, r"session start command 2 of 2 \(demo\) +[0-9.]+ s  failed \(exit 3\)")
        self.assertNotIn(OUTPUT_MARKER, out)
        # a developer runtime records its one step and prints no timing line
        rc, out = self.run_tool("session-start", *here, env={"CLAUDECODE": "1"})
        self.assertEqual(rc, 0, out)
        self.assertNotIn("timing:", out)
        rec, _ = last_record(self.home)
        self.assertEqual([s["name"] for s in rec["steps"]], ["store pull"])

    def test_probe_without_a_record(self):
        rc, out = self.run_tool("probe", env={"AGENT_RUNTIME": "claude-cloud"})
        self.assertEqual(rc, 0, out)
        self.assertIn("no session-start record on this machine yet", out)


class BootstrapCommandTest(ToolTest):
    def setUp(self):
        super().setUp()
        self.marker = os.path.join(self.base, "setup_marker.txt")
        self.set_env_json(setup_commands=['echo "committed $STORE" >> "%s"' % self.marker])
        self.init_git()

    def bootstrap(self, *extra, **env):
        env.setdefault("AGENT_RUNTIME", "claude-cloud")
        return self.run_tool("cloud", "bootstrap", "--project", "demo", "--vendor", "claude", *extra, env=env)

    def test_runs_only_on_a_cloud_machine(self):
        rc, out = self.bootstrap(AGENT_RUNTIME=None, CLAUDECODE="1")
        self.assertEqual(rc, 2, out)
        self.assertIn("runs only on a cloud machine", out)
        self.assertFalse(os.path.exists(self.marker))

    @unittest.skipUnless(BASH, NO_BASH)
    def test_reads_the_default_branch_never_the_working_tree_or_an_inbox_branch(self):
        # an edit that is not on the default branch is never run, here or from an inbox branch
        self.set_env_json(setup_commands=['echo "uncommitted" >> "%s"' % self.marker])
        rc, out = self.bootstrap()
        self.assertEqual(rc, 0, out)
        self.assertEqual(read(self.marker), "committed %s\n" % self.store)  # STORE is set, as in 0.5.1 scripts
        stamp = self.stamp()
        self.assertEqual(stamp["projects"]["demo"]["failed"], [])
        self.assertEqual(stamp["store"]["ref"], "refs/remotes/origin/main")
        self.assertTrue(
            os.path.islink(self.h(".claude", "CLAUDE.md")) or os.path.isfile(self.h(".claude", "CLAUDE.md"))
        )
        self.assertIn("session-start", read(self.h(".claude", "settings.json")))
        self.git("checkout", "-q", "-b", "inbox/claude-cloud-0123456789")
        self.git("commit", "-qam", "a cloud session's change")
        rc, out = self.bootstrap()
        self.assertEqual(rc, 0, out)
        self.assertIn("inbox branch", out)
        self.assertEqual(read(self.marker), "committed %s\n" % self.store)  # nothing more ran
        # a project it could not set up is recorded without inputs, so a later session start sets it up
        self.assertIsNone(self.stamp()["projects"]["demo"]["inputs"])
        env = self.without_real_transfer_tools({"AGENT_RUNTIME": "claude-cloud"})
        rc, out = self.run_tool("probe", "--full", env=env, timeout=300)
        self.assertEqual(rc, 0, out)
        self.assertIn("demo: not set up (the store's checkout is on the inbox branch", out)

    @unittest.skipUnless(BASH, NO_BASH)
    def test_a_detached_checkout_without_a_default_branch_reads_the_checked_out_commit(self):
        # a vendor's own checkout of the store may be detached with no origin/HEAD (as the script reads the
        # tool ref from HEAD there, the setup steps are read from the same commit)
        self.git("checkout", "-q", "--detach")
        rc, out = self.bootstrap()
        self.assertEqual(rc, 0, out)
        self.assertEqual(read(self.marker), "committed %s\n" % self.store)
        self.assertEqual(self.stamp()["store"]["ref"], "HEAD")

    def stamp(self):
        return json.loads(read(self.h(".config", "agent-mirror", "bootstrap-stamp.json")))

    @unittest.skipUnless(BASH, NO_BASH)
    def test_dry_run_changes_nothing(self):
        rc, out = self.bootstrap("--dry-run")
        self.assertEqual(rc, 0, out)
        self.assertIn("would run (setup command 1 of 1 (demo))", out)
        self.assertFalse(os.path.exists(self.marker))
        self.assertFalse(os.path.exists(self.h(".config", "agent-mirror", "bootstrap-stamp.json")))


class RenderTest(ToolTest):
    def render(self, ref="main"):
        write(self.s("store.json"), json.dumps({"schema": 1, "tool": {"repo": TOOL_URL, "ref": ref}}))
        self.set_env_json(python="3.11.9", setup_commands=["apt-get install -y -qq jq"])
        self.git("init", "-q", "-b", "main")
        self.git("remote", "add", "origin", STORE_URL)
        rc, out = self.run_tool("cloud", "render", "--project", "demo")
        self.assertEqual(rc, 0, out)
        return out

    def test_the_scripts_are_fixed_and_fall_back_for_an_older_tool(self):
        out = self.render()
        self.assertNotIn("has no `cloud bootstrap`", out)
        scripts = {}
        for rel in ("claude/setup.sh", "codex/vm-install.sh", "codex/container-setup.sh"):
            text = read(self.s("projects", "demo", "cloud", *rel.split("/")))
            scripts[rel] = text
            self.assertRegex(text, r"(?m)^# revision: 1$", rel)
            self.assertIn("cloud bootstrap --help", text, rel)
            self.assertIn("cloud bootstrap --vendor", text, rel)
            self.assertIn("install --agents", text, rel)  # the fallback for a tool without cloud bootstrap
            for value in ("3.11.9", "apt-get", "Example/demo"):  # the project settings stay in the store
                self.assertNotIn(value, text, rel)
        # a change to the project settings renders the same scripts
        self.set_env_json(python="3.12.1", setup_commands=["echo other"])
        rc, out = self.run_tool("cloud", "render", "--project", "demo")
        self.assertEqual(rc, 0, out)
        for rel, text in scripts.items():
            self.assertEqual(read(self.s("projects", "demo", "cloud", *rel.split("/"))), text, rel)

    def test_render_warns_when_the_store_pins_a_tool_without_cloud_bootstrap(self):
        out = self.render("v0.5.1")
        self.assertIn("store.json pins agent-mirror v0.5.1, which has no `cloud bootstrap`", out)
        self.assertIn("store.json pins agent-mirror v0.5.1, which has no `box send --note`", out)

    def test_render_warns_when_the_store_pins_a_tool_without_box_send_note(self):
        out = self.render("v0.5.2")
        self.assertNotIn("has no `cloud bootstrap`", out)
        self.assertIn("store.json pins agent-mirror v0.5.2, which has no `box send --note`: the runbook's", out)
        write(self.s("store.json"), json.dumps({"schema": 1, "tool": {"repo": TOOL_URL, "ref": "v0.5.3"}}))
        rc, out = self.run_tool("cloud", "render", "--project", "demo")
        self.assertEqual(rc, 0, out)
        self.assertNotIn("has no `box send --note`", out)

    def test_runbook_prompt_writes_the_report_inside_the_clone(self):
        self.render()
        book = json.loads(read(self.s("projects", "demo", "cloud", "claude", "runbook.json")))
        prompt = [s for s in book["steps"] if s["kind"] == "first-session"][0]["prompt"]
        self.assertIn(
            "cd <clone> && agent-mirror probe --full > probe_output.txt 2>&1; "
            "agent-mirror box send probe_output.txt --label first-probe --note ",
            prompt,
        )
        # the report is shown and removed: one left in the clone makes the vendor ask the session to commit it
        self.assertIn("; cat probe_output.txt; rm -f probe_output.txt", prompt)
        self.assertIn("your clone of Example/demo", prompt)


@unittest.skipUnless(CAN_RUN_SCRIPTS, NO_SCRIPTS)
class CloudMachineSimulationTest(ToolTest):
    """(a) fresh bootstrap then a session start without drift, (b) setup_commands changed in the store,
    (c) the pinned tool ref changed, (d) the setup script rendered by 0.5.1, (e) a failing repair."""

    def setUp(self):
        super().setUp()
        self.make_tool_origin()
        self.session_dir = os.path.join(self.base, "session")
        self.clone = os.path.join(self.session_dir, "demo")
        os.makedirs(self.clone)
        self.git("init", "-q", "-b", "work", cwd=self.clone)
        # the developer's store clone: pin the tool, set the setup command, render, then publish
        write(
            self.s("store.json"),
            json.dumps({"schema": 1, "tool": {"repo": TOOL_URL, "ref": "main"}, "cloud_push": "inbox"}),
        )
        env = json.loads(read(self.s("projects", "demo", "env.json")))
        env.update({"setup_commands": [self.marker_command("first")], "box": {"backend": "none"}})
        write(self.s("projects", "demo", "env.json"), json.dumps(env))
        self.git("init", "-q", "-b", "main")
        self.git("remote", "add", "origin", STORE_URL)
        rc, out = self.run_tool("cloud", "render", "--project", "demo", "--vendor", "claude")
        self.assertEqual(rc, 0, out)
        self.setup_script = read(self.s("projects", "demo", "cloud", "claude", "setup.sh"))
        self.pin(repo=self.tool_bare)  # the tool clone's origin is the bare repo: self-update may move it
        self.store_bare = os.path.join(self.base, "store-origin.git")
        self.git("init", "--bare", "-q", self.store_bare, cwd=self.base)
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "store")
        self.git("remote", "set-url", "origin", self.store_bare)
        self.git("push", "-q", "-u", "origin", "main")
        self.usr_bin = os.path.join(self.base, "usr-local-bin")
        os.makedirs(self.usr_bin)

    # the remotes -------------------------------------------------------------------------------

    def make_tool_origin(self):
        """A bare repo with a copy of this plugin on main, and a newer commit on the branch next."""
        work = os.path.join(self.base, "toolwork")
        shutil.copytree(PLUGIN, os.path.join(work, "plugin"), ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        self.git("init", "-q", "-b", "main", cwd=work)
        self.git("add", "-A", cwd=work)
        self.git("commit", "-q", "-m", "tool", cwd=work)
        self.tool_main = self.git("rev-parse", "HEAD", cwd=work)
        self.tool_bare = os.path.join(self.base, "tool-origin.git")
        self.git("init", "--bare", "-q", self.tool_bare, cwd=self.base)
        self.git("remote", "add", "origin", self.tool_bare, cwd=work)
        self.git("push", "-q", "origin", "main", cwd=work)
        init = os.path.join(work, "plugin", "agent_mirror", "__init__.py")
        write(init, read(init).replace('__version__ = "', '__version__ = "9.9.9-sim+', 1))
        self.git("commit", "-q", "-am", "newer tool", cwd=work)
        self.git("push", "-q", "origin", "HEAD:refs/heads/next", cwd=work)
        self.tool_next = self.git("rev-parse", "HEAD", cwd=work)

    def pin(self, repo=None, ref=None):
        meta = json.loads(read(self.s("store.json")))
        meta["tool"]["repo"] = repo or meta["tool"]["repo"]
        meta["tool"]["ref"] = ref or meta["tool"]["ref"]
        write(self.s("store.json"), json.dumps(meta))

    def publish(self, message="change", **fields):
        """A person changes the store on a developer machine and pushes to the default branch."""
        if fields:
            self.set_env_json(**fields)
        self.git("commit", "-q", "-am", message)
        self.git("push", "-q")

    def marker_command(self, word):
        return 'echo "%s $STORE" >> "$HOME/setup_marker.txt"' % word

    # the machine -----------------------------------------------------------------------------------

    def new_machine(self, name="cloudhome"):
        home = os.path.join(self.base, name)
        write(
            os.path.join(home, ".gitconfig"),
            "[user]\n\tname = Cloud\n\temail = cloud@example.com\n[init]\n\tdefaultBranch = main\n"
            "[commit]\n\tgpgsign = false\n",
        )
        return home

    def run_script(self, text, home):
        text = text.replace(TOKEN_URL, self.store_bare).replace(TOOL_URL, self.tool_bare)
        text = text.replace("/usr/local/bin/agent-mirror", os.path.join(self.usr_bin, "agent-mirror"))
        for line in text.splitlines():
            if not line.lstrip().startswith("#"):
                self.assertNotIn("https://", line)  # nothing reaches a network
                self.assertNotIn("/usr/local", line)  # nothing outside the sandbox
        path = os.path.join(self.base, "setup-under-test.sh")
        write(path, text)
        proc = subprocess.run(
            [BASH, path],
            cwd=self.session_dir,
            env=sandbox_env(home, sandbox=self.base),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            universal_newlines=True,
            timeout=300,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout)
        return proc.stdout

    def session_start(self, home, *extra):
        am = os.path.join(home, "code-agent-memory-mirror", "plugin", "bin", "agent-mirror")
        store = os.path.join(home, "agent-mirror-store")
        proc = subprocess.run(
            [sys.executable, am, "--store", store, "session-start", "--project", "demo", "--project-path", self.clone]
            + list(extra),
            cwd=self.session_dir,
            env=sandbox_env(home, {"AGENT_RUNTIME": "claude-cloud"}, sandbox=self.base),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            universal_newlines=True,
            timeout=300,
        )
        return proc.returncode, proc.stdout

    def markers(self, home):
        path = os.path.join(home, "setup_marker.txt")
        return read(path).splitlines() if os.path.isfile(path) else []

    def stamp(self, home):
        return json.loads(read(os.path.join(home, ".config", "agent-mirror", "bootstrap-stamp.json")))

    def tool_head(self, home):
        return self.git("rev-parse", "HEAD", cwd=os.path.join(home, "code-agent-memory-mirror"))

    def bootstrapped(self):
        home = self.new_machine()
        out = self.run_script(self.setup_script, home)
        store = os.path.join(home, "agent-mirror-store")
        self.assertTrue(os.path.isdir(os.path.join(store, ".git")), out)
        self.assertEqual(self.tool_head(home), self.tool_main)
        self.assertEqual(self.markers(home), ["first %s" % store])
        self.assertIn("session-start", read(os.path.join(home, ".claude", "settings.json")))
        note = read(os.path.join(home, "agent_setup_probe.txt"))
        self.assertIn("store cloned: yes", note)
        self.assertIn("setup command 1 of 1 (demo): ok in", note)
        self.assertIn("bootstrap done", note)
        stamp = self.stamp(home)
        self.assertEqual((stamp["vendor"], stamp["tool"]["commit"]), ("claude", self.tool_main))
        self.assertEqual(stamp["projects"]["demo"]["failed"], [])
        return home

    # the stories ---------------------------------------------------------------------------------

    def test_a_fresh_bootstrap_then_a_session_start_without_drift(self):
        home = self.bootstrapped()
        stamp = self.stamp(home)
        rc, out = self.session_start(home)
        self.assertEqual(rc, 0, out)
        self.assertIn("setup drift: none for demo", out)
        self.assertIn("already current", out)
        self.assertEqual(len(self.markers(home)), 1)  # nothing ran again
        self.assertEqual(self.stamp(home)["projects"], stamp["projects"])
        rec, _ = last_record(home)
        self.assertEqual(steps_of(rec)["setup drift check"], "ok")

    def test_changed_setup_commands_run_again_at_the_next_session_start(self):
        home = self.bootstrapped()
        before = self.stamp(home)["projects"]["demo"]["inputs"]
        self.publish(setup_commands=[self.marker_command("first"), self.marker_command("second")])
        rc, out = self.session_start(home)
        self.assertEqual(rc, 0, out)
        self.assertIn("setup drift: setup_commands changed for demo; running its setup steps now", out)
        self.assertIn("setup drift: demo repaired", out)
        store = os.path.join(home, "agent-mirror-store")
        self.assertEqual(self.markers(home), ["first %s" % store, "first %s" % store, "second %s" % store])
        after = self.stamp(home)["projects"]["demo"]
        self.assertNotEqual(after["inputs"], before)
        self.assertTrue(after["repaired"])
        steps = steps_of(last_record(home)[0])
        self.assertEqual(steps["setup command 2 of 2 (demo)"], "ok")
        self.assertIn("drift repair at", read(os.path.join(home, "agent_setup_probe.txt")))
        rc, out = self.session_start(home)  # the stamp now matches: nothing runs again
        self.assertEqual(rc, 0, out)
        self.assertIn("setup drift: none for demo", out)
        self.assertEqual(len(self.markers(home)), 3)

    def test_a_changed_tool_ref_moves_the_tool(self):
        home = self.bootstrapped()
        self.pin(ref="next")
        self.publish("pin next")
        rc, out = self.session_start(home)
        self.assertEqual(rc, 0, out)
        self.assertIn("tool updated from %s to %s (ref next)" % (self.tool_main[:12], self.tool_next[:12]), out)
        self.assertIn("agent-mirror 9.9.9-sim+", out)  # the session start ran again with the new code
        self.assertEqual(self.tool_head(home), self.tool_next)
        self.assertIn("setup drift: agent-mirror is at %s" % self.tool_next[:12], out)
        self.assertEqual(len(self.markers(home)), 1)  # the setup inputs did not change
        self.assertEqual(self.stamp(home)["tool"]["commit"], self.tool_next)
        rec, _ = last_record(home)
        self.assertTrue(rec["self_updated"])
        self.assertEqual([s["name"] for s in rec["steps"]][:2], ["store pull", "tool update"])

    def test_the_setup_script_rendered_by_0_5_1_still_works(self):
        home = self.new_machine("oldmachine")
        out = self.run_script(read(OLD_SCRIPT), home)
        store = os.path.join(home, "agent-mirror-store")
        self.assertTrue(os.path.isdir(os.path.join(store, ".git")), out)
        self.assertEqual(self.tool_head(home), self.tool_main)
        self.assertEqual(self.markers(home), ["setup ran"])  # its own inline setup command
        self.assertIn("session-start", read(os.path.join(home, ".claude", "settings.json")))
        self.assertTrue(os.path.lexists(os.path.join(home, ".claude", "CLAUDE.md")))
        note = read(os.path.join(home, "agent_setup_probe.txt"))
        self.assertIn("install and hooks done", note)
        self.assertFalse(os.path.exists(os.path.join(home, ".config", "agent-mirror", "bootstrap-stamp.json")))
        rc, out = self.session_start(home)
        self.assertEqual(rc, 0, out)
        self.assertIn("no bootstrap stamp on this machine", out)
        self.assertEqual(self.markers(home), ["setup ran"])

    def test_a_project_skipped_at_setup_is_set_up_once_the_store_is_fixed(self):
        self.publish(setup_commands="not a list")  # a mistake on the default branch when the machine is built
        home = self.new_machine()
        self.run_script(self.setup_script, home)
        self.assertEqual(self.markers(home), [])
        self.assertIsNone(self.stamp(home)["projects"]["demo"]["inputs"])
        self.assertIn("session-start", read(os.path.join(home, ".claude", "settings.json")))  # links and hooks ran
        self.publish(setup_commands=[self.marker_command("fixed")])
        rc, out = self.session_start(home)
        self.assertEqual(rc, 0, out)
        self.assertIn("setup drift: the setup steps of demo never ran here (env.json has problems)", out)
        self.assertEqual(self.markers(home), ["fixed %s" % os.path.join(home, "agent-mirror-store")])
        self.assertTrue(self.stamp(home)["projects"]["demo"]["inputs"])

    def test_a_failing_repair_never_fails_the_session(self):
        home = self.bootstrapped()
        self.publish(setup_commands=["exit 7"])
        rc, out = self.session_start(home)
        self.assertEqual(rc, 0, out)
        self.assertIn("setup drift: 1 setup step(s) of demo failed", out)
        steps = steps_of(last_record(home)[0])
        self.assertEqual(steps["setup command 1 of 1 (demo)"], "failed (exit 7)")
        self.assertEqual(steps["setup drift check"], "failed (1 step(s))")
        self.assertEqual(self.stamp(home)["projects"]["demo"]["failed"], ["setup command 1 of 1 (demo)"])
        # out of time: the session goes on, and the old record stays so that the next start tries again
        inputs = self.stamp(home)["projects"]["demo"]["inputs"]
        self.publish(setup_commands=["sleep 60"])
        rc, out = self.session_start(home, "--repair-timeout", "2")
        self.assertEqual(rc, 0, out)
        self.assertIn("the next session start tries again", out)
        self.assertEqual(self.stamp(home)["projects"]["demo"]["inputs"], inputs)
        self.assertRegex(steps_of(last_record(home)[0])["setup command 1 of 1 (demo)"], r"^failed \(out of time after")
        # a damaged stamp: not checked, the session goes on
        write(os.path.join(home, ".config", "agent-mirror", "bootstrap-stamp.json"), "{not json")
        rc, out = self.session_start(home)
        self.assertEqual(rc, 0, out)
        self.assertIn("the bootstrap stamp cannot be read", out)

    def test_a_tool_without_cloud_bootstrap_gets_the_older_steps(self):
        """The store pins an agent-mirror older than 0.5.2: the fixed script falls back to links and hooks."""
        work = os.path.join(self.base, "stubwork")
        log = os.path.join(self.base, "stub-calls.txt")
        stub = (
            "#!%s\nimport sys\nargs = sys.argv[1:]\nopen(%r, 'a').write(' '.join(args) + '\\n')\n"
            "sys.exit(2 if 'bootstrap' in args else 0)\n" % (sys.executable, log)
        )
        write(os.path.join(work, "plugin", "bin", "agent-mirror"), stub)
        self.git("init", "-q", "-b", "main", cwd=work)
        self.git("add", "-A", cwd=work)
        self.git("commit", "-q", "-m", "an older tool", cwd=work)
        bare = os.path.join(self.base, "stub-origin.git")
        self.git("init", "--bare", "-q", bare, cwd=self.base)
        self.git("push", "-q", bare, "main", cwd=work)
        self.tool_bare = bare
        self.run_script(self.setup_script, self.new_machine())
        calls = read(log).splitlines()
        self.assertEqual(calls[0], "cloud bootstrap --help")
        self.assertTrue(calls[1].startswith("init-store "), calls)
        self.assertIn("install --agents claude", calls[2])
        self.assertIn("hooks install --agent claude", calls[3])
        self.assertEqual(len(calls), 4, calls)


if __name__ == "__main__":
    unittest.main()
