"""0.5.6: files a session made that are persisted nowhere.

`box unsent` lists the files of the project clone that are untracked or ignored, changed since the session
baseline, and neither fetched from the box unchanged, sent in this session unchanged, nor declared disposable
with `box skip`; env files, links, dependency and cache folders, nested clones, box.exclude and
box.unsent_ignore are left out. session-start records the baseline. `hook stop-check` (cloud runtimes only)
blocks the end of a turn once per new set of unsent files. `hooks install` adds that Stop hook on cloud runtimes
only. A developer session start says when box folders this machine owns changed since its last push.
"""

import datetime
import io
import json
import os
import socket
import subprocess
import tarfile
import time
import unittest

from helpers import setUpModule  # noqa: F401 (the sandbox guard runs first)
from helpers import FAKE_RCLONE, REPO, SYMLINKS, ToolTest, clean_env, install_fake, read, write

ID_A, ID_B = "a" * 32, "b" * 32
OLD_RELEASES = (("0ea28af", "0.5.2"), ("526db38", "0.5.5"))


def has_commit(commit):
    return (
        subprocess.run(
            ["git", "-C", REPO, "cat-file", "-e", commit + "^{commit}"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ).returncode
        == 0
    )


FAILING_RCLONE = r"""
import json, os, sys
with open(os.environ["FAKE_RCLONE_LOG"], "a") as fh:
    fh.write(json.dumps({"argv": sys.argv[1:]}) + "\n")
sys.stderr.write("error: the remote cannot be reached\n")
sys.exit(1)
"""


def iso(ts):
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def last_record(home):
    with open(os.path.join(home, ".cache", "agent-mirror", "session-start.jsonl"), encoding="utf-8") as fh:
        lines = [ln for ln in fh.read().splitlines() if ln.strip()]
    return json.loads(lines[-1])


def steps(home):
    return dict((s["name"], s["status"]) for s in last_record(home)["steps"])


class CloneMixin:
    """The demo project as a git clone with a commit, and helpers to make files and records."""

    def setUp(self):
        super().setUp()
        self.git("init", "-q", "-b", "main", cwd=self.project)
        write(self.p("README.md"), "readme\n")
        write(self.p(".gitignore"), "*.log\nout/\n")
        self.git("add", "-A", cwd=self.project)
        self.git("commit", "-q", "-m", "first", cwd=self.project)

    def baseline(self, ago=60, runtime="claude-cloud", host=None):
        now = time.time() - ago
        rec = {"schema": 1, "seconds": now, "at": iso(now), "runtime": runtime, "host": host or socket.gethostname()}
        write(self.p(".agent-mirror", "baseline.json"), json.dumps(rec))
        return rec

    def make(self, rel, text="x\n", age=None):
        path = self.p(*rel.split("/"))
        write(path, text)
        if age is not None:
            t = time.time() - age
            os.utime(path, (t, t))
        return path

    def local_record(self):
        path = self.p(".agent-mirror", "session.json")
        return json.loads(read(path)) if os.path.isfile(path) else {}

    def unsent(self, *extra, **env):
        env.setdefault("AGENT_RUNTIME", "claude-cloud")
        return self.run_tool("box", "unsent", "--project", "demo", "--project-path", self.project, *extra, env=env)

    def unsent_json(self, *extra, **env):
        rc, out = self.unsent("--json", *extra, **env)
        self.assertEqual(rc, 0, out)
        return json.loads(out)


class UnsentTest(CloneMixin, ToolTest):
    def test_each_rule_that_leaves_a_file_out(self):
        self.set_box(unsent_ignore=["**/*.tmp"])
        self.baseline()
        self.make("results/new.csv", "a,b\n")  # untracked, new: listed
        self.make("out/model.bin", "weights\n")  # ignored by .gitignore, new: listed
        self.make("run.log", "log\n")  # ignored by .gitignore, new: listed
        write(self.p("README.md"), "changed\n")  # tracked: git's business
        self.make(".env", "A=1\n")
        self.make("conf/x.env", "B=2\n")
        self.make(".venv/lib/a.py")
        self.make("node_modules/m/index.js")
        self.make("results/__pycache__/m.pyc")
        self.make("temporary/downloads/big.bin")  # box.exclude
        self.make("scratch.tmp")  # box.unsent_ignore
        self.make("vendorlib/.git/HEAD", "ref: refs/heads/main\n")
        self.make("vendorlib/lib.py")  # a nested clone: its own git carries it
        self.make("old.csv", age=7200)  # older than the baseline
        fetched = self.make("figures/f.png", "png\n")
        st = os.stat(fetched)
        write(
            self.p(".agent-mirror", "fetched.json"),
            json.dumps(
                {
                    "version": 1,
                    "files": {"figures/f.png": {"size": st.st_size, "mtime": st.st_mtime, "source": "shared"}},
                }
            ),
        )
        sent = self.make("results/sent.csv", "sent\n")
        changed = self.make("results/changed.csv", "changed since the send\n")
        st_sent, st_changed = os.stat(sent), os.stat(changed)
        self.make("tmp/a.txt")
        self.make("tmp/b/c.txt")
        write(
            self.p(".agent-mirror", "session.json"),
            json.dumps(
                {
                    "id": "claude-cloud-20261119T100000Z-0a0b0c",
                    "runtime": "claude-cloud",
                    "host": socket.gethostname(),
                    "started": iso(time.time() - 60),
                    "sent": {
                        "results/sent.csv": {"bytes": st_sent.st_size, "mtime": st_sent.st_mtime},
                        "results/changed.csv": {"bytes": st_changed.st_size - 1, "mtime": st_changed.st_mtime},
                    },
                    "skipped": [{"at": iso(time.time()), "reason": "scratch of the test run", "paths": ["tmp"]}],
                }
            ),
        )
        if SYMLINKS:
            os.symlink(self.p("results", "new.csv"), self.p("results", "link.csv"))
        data = self.unsent_json()
        got = sorted(p["path"] for p in data["paths"])
        self.assertEqual(got, ["out/model.bin", "results/changed.csv", "results/new.csv", "run.log"])
        left = data["left_out"]
        for reason in (
            "env",
            "builtin",
            "excluded",
            "ignored",
            "nested_git",
            "before_baseline",
            "fetched",
            "sent",
            "skipped",
        ):
            self.assertGreaterEqual(left[reason], 1, reason)
        self.assertEqual(left["env"], 2)
        self.assertEqual(left["skipped"], 2)  # both files under the folder declared disposable
        if SYMLINKS:
            self.assertGreaterEqual(left["links"], 1)
        self.assertTrue(data["complete"])
        self.assertEqual(data["baseline"]["source"], "session-start")

    def test_folders_that_are_all_new_collapse_and_the_listing_is_capped(self):
        self.baseline()
        self.make("results/run-3/a.csv", "a" * 3000)
        self.make("results/run-3/b.csv", "b" * 3000)
        self.make("results/summary.csv", "s" * 100)
        self.make("results/old.csv", age=7200)
        self.make("figs/one.png", "1" * 500)
        self.make("figs/deep/two.png", "2" * 500)
        self.make("notes.md", "n\n")
        rc, out = self.unsent(CLAUDECODE="1", AGENT_RUNTIME=None)
        self.assertEqual(rc, 0, out)
        lines = out.splitlines()
        self.assertRegex(lines[0], r"^unsent in demo \(.*\): 6 files, .*changed since .* \(session start\), in no git")
        self.assertIn("  in results/: 3 files, 6.0 KiB", lines)
        self.assertIn("    results/run-3/ (2 files, 5.9 KiB)", lines)
        self.assertIn("    results/summary.csv (100 B)", lines)
        self.assertIn("  figs/ (2 files, 1000 B)", lines)
        self.assertIn("  notes.md (2 B)", lines)
        self.assertLess(lines.index("  in results/: 3 files, 6.0 KiB"), lines.index("  figs/ (2 files, 1000 B)"))
        self.assertIn(
            'keep a file or folder: agent-mirror box send PATH --label NAME --note "what, why, what next"', out
        )
        self.assertIn("left out: 1 older than the baseline", out)
        for i in range(40):
            self.make("many/f%02d.txt" % i, "m" * (100 + i))
        self.make("many/old.txt", age=7200)
        rc, out = self.unsent()
        listing = [ln for ln in out.splitlines() if ln.startswith("  ")]
        self.assertEqual(len(listing), 31, out)  # 30 lines, then one "and N more"
        self.assertRegex(listing[-1], r"^  and \d+ more files, ")

    def test_files_sent_in_this_session_are_left_out_until_they_change(self):
        env = self.fake_az()
        env.update({"AGENT_RUNTIME": "claude-cloud", "FAKE_AZ_ROOT": os.path.join(self.base, "containers")})
        self.set_box(min_free_bytes=0)
        self.baseline()
        self.make("results/r.csv", "r\n")
        self.make("results/s.csv", "s\n")
        rc, out = self.box_run("box", "send", "results", "--label", "exp", "--note", "the results of run 1", env=env)
        self.assertEqual(rc, 0, out)
        self.assertEqual(sorted(self.local_record()["sent"]), ["results/r.csv", "results/s.csv"])
        rc, out = self.unsent("--check", **env)
        self.assertEqual(rc, 0, out)
        self.assertIn("nothing unsent in demo", out)
        self.assertIn("2 sent in this session, unchanged", out)
        self.make("results/s.csv", "changed\n")
        rc, out = self.unsent("--check", **env)
        self.assertEqual(rc, 3, out)
        self.assertIn("results/s.csv (8 B)", out)

    def test_without_a_baseline_the_clone_time_stands_in(self):
        self.make("results/new.csv")
        data = self.unsent_json()
        self.assertEqual(data["baseline"]["source"], "clone")
        self.assertEqual([p["path"] for p in data["paths"]], ["results/new.csv"])
        rc, out = self.unsent()
        self.assertEqual(rc, 0, out)
        self.assertIn(
            "no session baseline in %s (a session start records one); counting what changed since the clone was made"
            % self.project,
            out,
        )
        # not a git clone, and no baseline: every file counts
        plain = os.path.join(self.base, "plain", "demo")
        write(os.path.join(plain, "old.txt"), "x\n")
        t = time.time() - 86400 * 30
        os.utime(os.path.join(plain, "old.txt"), (t, t))
        rc, out = self.run_tool("box", "unsent", "--project", "demo", "--project-path", plain, env={"CLAUDECODE": "1"})
        self.assertEqual(rc, 0, out)
        self.assertIn(
            "no session baseline in %s and no git HEAD log; counting every untracked or ignored file" % plain, out
        )
        self.assertIn("  old.txt (2 B)", out)

    def test_check_exit_codes_time_limit_and_an_unconfigured_box(self):
        self.baseline()
        rc, out = self.unsent("--check")
        self.assertEqual(rc, 0, out)
        self.make("results/new.csv")
        rc, out = self.unsent("--check")
        self.assertEqual(rc, 3, out)
        rc, out = self.unsent("--check", "--time-limit", "0")
        self.assertEqual(rc, 3, out)  # a check cut short is not a clean result
        self.assertIn("the check was cut short after 0 s (--time-limit); the list may be incomplete", out)
        self.assertFalse(json.loads(self.unsent("--json", "--time-limit", "0")[1])["complete"])
        self.set_box(backend="none")
        rc, out = self.unsent("--check")
        self.assertEqual(rc, 0, out)
        self.assertIn("the box of demo is not configured", out)

    def test_session_start_records_the_baseline_once_per_session(self):
        here = ("--project", "demo", "--project-path", self.project, "--box-timeout", "0")
        rc, out = self.run_tool("session-start", *here, env={"AGENT_RUNTIME": "claude-cloud"})
        self.assertEqual(rc, 0, out)
        first = json.loads(read(self.p(".agent-mirror", "baseline.json")))
        self.assertEqual((first["runtime"], first["host"]), ("claude-cloud", socket.gethostname()))
        self.assertIn("/.agent-mirror/", read(self.p(".git", "info", "exclude")))
        rc, out = self.run_tool("session-start", *here, env={"AGENT_RUNTIME": "claude-cloud"})
        self.assertEqual(rc, 0, out)
        self.assertEqual(json.loads(read(self.p(".agent-mirror", "baseline.json"))), first)  # same session: kept
        self.baseline(ago=60, host="another-machine")  # a new cloud machine is a new session
        rc, out = self.run_tool("session-start", *here, env={"AGENT_RUNTIME": "claude-cloud"})
        self.assertEqual(json.loads(read(self.p(".agent-mirror", "baseline.json")))["host"], socket.gethostname())
        # a developer runtime keeps a baseline for 24 hours
        rc, out = self.run_tool("session-start", *here, env={"CLAUDECODE": "1"})
        self.assertEqual(rc, 0, out)
        dev = json.loads(read(self.p(".agent-mirror", "baseline.json")))
        self.assertEqual(dev["runtime"], "claude-local")
        rc, out = self.run_tool("session-start", *here, env={"CLAUDECODE": "1"})
        self.assertEqual(json.loads(read(self.p(".agent-mirror", "baseline.json"))), dev)
        self.baseline(ago=25 * 3600, runtime="claude-local")
        rc, out = self.run_tool("session-start", *here, env={"CLAUDECODE": "1"})
        self.assertGreater(json.loads(read(self.p(".agent-mirror", "baseline.json")))["seconds"], time.time() - 600)
        # no box, no baseline
        os.remove(self.p(".agent-mirror", "baseline.json"))
        self.set_box(backend="none")
        rc, out = self.run_tool("session-start", *here, env={"CLAUDECODE": "1"})
        self.assertFalse(os.path.exists(self.p(".agent-mirror", "baseline.json")))


class SkipMixin(CloneMixin):
    def setUp(self):
        super().setUp()
        self.configure_box()
        self.baseline()

    def skip(self, *args, **env):
        e = dict(self.tool_env())
        e.setdefault("AGENT_RUNTIME", "claude-cloud")
        e.update(env)
        return self.run_tool("box", "skip", *args, "--project", "demo", "--project-path", self.project, env=e)

    def test_skip_is_recorded_here_and_in_the_manifest(self):
        self.make("cache/a.bin")
        self.make("cache/b/c.bin")
        self.make("scratch.txt")
        self.make("results/keep.csv")
        before = self.calls()
        rc, out = self.skip("cache", "scratch.txt", "--reason", "rebuildable in a minute by the test script")
        self.assertEqual(rc, 0, out)
        self.assertIn("left behind on purpose in session ", out)
        self.assertIn("also recorded in ", out)
        sid = self.local_record()["id"]
        entry = self.local_record()["skipped"][-1]
        self.assertEqual(
            (entry["paths"], entry["reason"]), (["cache", "scratch.txt"], "rebuildable in a minute by the test script")
        )
        manifest = json.loads(read(self.sessions_path(sid, "_session.json")))
        self.assertEqual(manifest["skipped"][-1]["paths"], ["cache", "scratch.txt"])
        self.assertEqual(manifest["files"], [])
        self.assertLessEqual(self.calls() - before, 4)
        data = self.unsent_json(**self.tool_env())
        self.assertEqual([p["path"] for p in data["paths"]], ["results/keep.csv"])
        self.assertEqual(data["left_out"]["skipped"], 3)
        # later sessions see it
        rc, out = self.run_tool(
            "box",
            "sessions",
            "--project",
            "demo",
            "--project-path",
            self.project,
            env=dict(self.tool_env(), CLAUDECODE="1"),
        )
        self.assertEqual(rc, 0, out)
        self.assertIn("      (2 paths left behind: rebuildable in a minute by the test script)", out)
        rc, out = self.run_tool(
            "box",
            "fetch",
            "--session",
            sid,
            "--dry-run",
            "--project",
            "demo",
            "--project-path",
            self.project,
            env=dict(self.tool_env(), CLAUDECODE="1"),
        )
        self.assertIn("left behind (", out)
        self.assertIn("cache, scratch.txt; reason: rebuildable in a minute by the test script", out)
        # a second skip with a send between them keeps both entries and the file list
        self.make("results/r.csv")
        rc, out = self.box_send("results/r.csv")
        self.assertEqual(rc, 0, out)
        rc, out = self.skip("results/keep.csv", "--reason", "a copy of r.csv")
        self.assertEqual(rc, 0, out)
        manifest = json.loads(read(self.sessions_path(sid, "_session.json")))
        self.assertEqual([e["paths"] for e in manifest["skipped"]], [["cache", "scratch.txt"], ["results/keep.csv"]])
        self.assertEqual([f["path"] for f in manifest["files"]], ["results/r.csv"])

    def test_refusals(self):
        self.make("cache/a.bin")
        outside = os.path.join(self.base, "elsewhere.txt")
        write(outside, "x\n")
        token = "ghp_" + "Ab1" * 12  # built at runtime: this public repo holds no token-shaped text
        for args, code, words in (
            ((outside, "--reason", "r"), 2, "is outside the project clone"),
            (("../elsewhere.txt", "--reason", "r"), 2, "is outside the project clone"),
            ((self.project, "--reason", "r"), 2, "not the clone itself"),
            (("missing.txt", "--reason", "r"), 1, "does not exist in the project clone"),
            (("cache", "--reason", "  "), 2, "give the reason"),
            (("cache", "--reason", "the key is " + token), 2, "refusing the reason: it looks like it holds a secret"),
            (("cache", "--reason", "x" * 501), 2, "a reason is at most 500 characters"),
        ):
            rc, out = self.skip(*args)
            self.assertEqual(rc, code, out)
            self.assertIn(words, out)
            self.assertNotIn(token, out)
        self.assertNotIn("skipped", self.local_record())

    @unittest.skipUnless(
        all(has_commit(c) for c, _ in OLD_RELEASES), "this clone lacks the 0.5.2 or 0.5.5 commit (no git history)"
    )
    def test_records_are_kept_by_0_5_2_and_0_5_5_and_the_reverse(self):
        for commit, version in OLD_RELEASES:
            data = subprocess.run(
                ["git", "-C", REPO, "archive", "--format=tar", commit, "plugin"],
                stdout=subprocess.PIPE,
                env=clean_env(self.home),
                check=True,
            ).stdout
            root = os.path.join(self.base, "old-" + version)
            with tarfile.open(fileobj=io.BytesIO(data)) as tf:
                tf.extractall(root, **({"filter": "data"} if hasattr(tarfile, "data_filter") else {}))
            old = os.path.join(root, "plugin", "bin", "agent-mirror")
            env = dict(self.tool_env(), AGENT_RUNTIME="claude-cloud")
            here = ("--project", "demo", "--project-path", self.project)
            rc, out = self.run_tool("--version", tool=old)
            self.assertIn(version, out)
            # this version's skip, then the old version's send in the same session: both records stay
            self.make("cache-%s/a.bin" % version)
            self.make("result-%s.csv" % version)
            rc, out = self.skip("cache-%s" % version, "--reason", "rebuildable")
            self.assertEqual(rc, 0, out)
            sid = self.local_record()["id"]
            args = ["box", "send", "result-%s.csv" % version] + (["--note", "a result"] if version != "0.5.2" else [])
            rc, out = self.run_tool(*args + list(here), env=env, tool=old)
            self.assertEqual(rc, 0, out)
            self.assertEqual(self.local_record()["id"], sid)
            manifest = json.loads(read(self.sessions_path(sid, "_session.json")))
            self.assertIn("cache-%s" % version, [p for e in manifest["skipped"] for p in e["paths"]])
            self.assertIn("result-%s.csv" % version, [f["path"] for f in manifest["files"]])
            self.assertIn("cache-%s" % version, [p for e in self.local_record()["skipped"] for p in e["paths"]])
            rc, out = self.run_tool("box", "sessions", *here, env=dict(self.tool_env(), CLAUDECODE="1"), tool=old)
            self.assertEqual(rc, 0, out)
            self.assertIn(sid, out)
            self.assertNotIn("cache-%s/a.bin" % version, [p["path"] for p in self.unsent_json()["paths"]])
            # the old version's hooks install keeps this version's Stop hook (it does not know it)
            self.run_tool("hooks", "install", env={"AGENT_RUNTIME": "claude-cloud"})
            rc, out = self.run_tool("hooks", "install", env={"AGENT_RUNTIME": "claude-cloud"}, tool=old)
            self.assertEqual(rc, 0, out)
            self.assertIn("hook stop-check", read(self.h(".claude", "settings.json")))

    def test_a_box_that_cannot_be_reached_still_records_locally(self):
        self.make("cache/a.bin")
        rc, out = self.skip("cache", "--reason", "rebuildable", **self.unreachable())
        self.assertEqual(rc, 0, out)
        self.assertIn("recorded on this machine only (`agent-mirror box unsent` honours it)", out)
        self.assertEqual(self.local_record()["skipped"][-1]["paths"], ["cache"])
        self.assertEqual(self.unsent_json()["files"], 0)


class AzureSkipTest(SkipMixin, ToolTest):
    def configure_box(self):
        self.root = os.path.join(self.base, "containers")
        self.set_box(min_free_bytes=0)

    def tool_env(self):
        env = self.fake_az()
        env["FAKE_AZ_ROOT"] = self.root
        return env

    def unreachable(self):
        return {"FAKE_AZ_ACCOUNT_SHOW_RC": "1"}  # not signed in, and no AZURE_* to sign in with

    def calls(self):
        log = os.path.join(self.base, "az_log.jsonl")
        return len(read(log).splitlines()) if os.path.isfile(log) else 0

    def sessions_path(self, *parts):
        return os.path.join(self.root, "out-box", "demo", *[x for p in parts for x in p.split("/")])

    def box_send(self, rel):
        return self.run_tool(
            "box",
            "send",
            rel,
            "--note",
            "a result",
            "--project",
            "demo",
            "--project-path",
            self.project,
            env=dict(self.tool_env(), AGENT_RUNTIME="claude-cloud"),
        )


class RcloneSkipTest(SkipMixin, ToolTest):
    def configure_box(self):
        self.root = os.path.join(self.base, "remotes")
        self.log = os.path.join(self.base, "rclone_log.jsonl")
        env = json.loads(read(self.s("projects", "demo", "env.json")))
        env["box"] = {
            "backend": "rclone",
            "shared_remote": "fakeremote:box/shared",
            "sessions_remote": "fakeremote:box/sessions",
            "include": ["temporary/**", "figures/**"],
            "min_free_bytes": 0,
        }
        write(self.s("projects", "demo", "env.json"), json.dumps(env))

    def tool_env(self):
        fakebin = install_fake(self.base, "rclone", FAKE_RCLONE)
        return {
            "PATH": fakebin + os.pathsep + os.environ.get("PATH", ""),
            "FAKE_RCLONE_LOG": self.log,
            "FAKE_RCLONE_INBOUND": "fakeremote:box/shared",
            "FAKE_RCLONE_ROOT": self.root,
        }

    def unreachable(self):
        failing = install_fake(self.base, "rclone", FAILING_RCLONE, folder="failingbin")
        return {"PATH": failing + os.pathsep + os.environ.get("PATH", "")}

    def calls(self):
        return len(read(self.log).splitlines()) if os.path.isfile(self.log) else 0

    def sessions_path(self, *parts):
        return os.path.join(self.root, "fakeremote", "box", "sessions", *[x for p in parts for x in p.split("/")])

    def box_send(self, rel):
        return self.run_tool(
            "box",
            "send",
            rel,
            "--note",
            "a result",
            "--project",
            "demo",
            "--project-path",
            self.project,
            env=dict(self.tool_env(), AGENT_RUNTIME="claude-cloud"),
        )


class StopCheckTest(CloneMixin, ToolTest):
    def setUp(self):
        super().setUp()
        self.baseline()

    def stop(self, session="sess-1", active=False, runtime="claude-cloud", stdin=None, *extra):
        data = {"session_id": session, "hook_event_name": "Stop", "stop_hook_active": active, "cwd": self.project}
        env = {"CLAUDE_PROJECT_DIR": self.project}
        if runtime == "claude-local":
            env["CLAUDECODE"] = "1"
        else:
            env["AGENT_RUNTIME"] = runtime
        return self.run_tool("hook", "stop-check", *extra, stdin=json.dumps(data) if stdin is None else stdin, env=env)

    def decision(self, out):
        self.assertTrue(out.strip(), "expected a decision")
        self.assertEqual(len(out.strip().splitlines()), 1, out)  # one JSON line and nothing else
        return json.loads(out)

    def test_block_once_per_set_and_again_when_files_appear(self):
        rc, out = self.stop()
        self.assertEqual((rc, out), (0, ""))  # nothing unsent
        self.make("results/run-1/a.csv", "a" * 2000)
        self.make("results/run-1/b.csv", "b" * 2000)
        self.make("notes.md", "n\n")
        rc, out = self.stop()
        self.assertEqual(rc, 0, out)
        d = self.decision(out)
        self.assertEqual(d["decision"], "block")
        reason = d["reason"]
        self.assertIn("this cloud machine is thrown away after the session", reason)
        self.assertIn("demo (%s): 3 files" % self.project, reason)
        self.assertIn("  results/ (2 files, 3.9 KiB)", reason)  # a folder that holds only new files is one line
        self.assertIn("  notes.md (2 B)", reason)
        self.assertIn('either send it: agent-mirror box send PATH --label NAME --note "what, why, what next"', reason)
        self.assertIn('or declare it disposable: agent-mirror box skip PATH... --reason "why it can go"', reason)
        self.assertIn("KEEP results that took real time or money to produce", reason)
        self.assertIn(
            "LEAVE caches, anything rebuildable in minutes, files already in git, secrets and env files.", reason
        )
        # the same set: not again, with or without stop_hook_active
        self.assertEqual(self.stop(), (0, ""))
        self.assertEqual(self.stop(active=True), (0, ""))
        # part of it handled: not again
        os.remove(self.p("notes.md"))
        self.assertEqual(self.stop(active=True), (0, ""))
        # a new file: again, also when continuing from a stop hook
        self.make("figure.png", "p\n")
        rc, out = self.stop(active=True)
        self.assertIn("figure.png (2 B)", self.decision(out)["reason"])
        self.assertEqual(self.stop(active=True), (0, ""))
        # the third block is the last one of a session
        self.make("third.txt")
        self.assertEqual(self.decision(self.stop()[1])["decision"], "block")
        self.make("fourth.txt")
        self.assertEqual(self.stop(), (0, ""))
        state = json.loads(read(self.h(".cache", "agent-mirror", "stop-check.json")))
        self.assertEqual(state["blocks"], 3)
        self.assertIn("already blocked 3 times", state["last"]["outcome"])
        self.assertNotIn("fourth", json.dumps(state))  # digests only, never names
        # a new session starts from nothing
        self.assertEqual(self.decision(self.stop(session="sess-2")[1])["decision"], "block")

    def test_silent_off_cloud_and_on_bad_input_errors_and_timeouts(self):
        self.make("results/new.csv")
        self.assertEqual(self.stop(runtime="claude-local"), (0, ""))
        rc, out = self.run_tool("hook", "stop-check", stdin="{}", env={"CLAUDE_PROJECT_DIR": self.project})
        self.assertEqual((rc, out), (0, ""))  # unknown runtime
        self.assertEqual(self.stop(stdin="not json"), (0, ""))
        self.assertEqual(self.stop(stdin=""), (0, ""))
        self.assertEqual(self.stop(stdin="[1, 2]"), (0, ""))
        rc, out = self.stop("sess-t", False, "claude-cloud", None, "--time-limit", "0")
        self.assertEqual((rc, out), (0, ""))  # a check that could not finish never blocks
        outcome = json.loads(read(self.h(".cache", "agent-mirror", "stop-check.json")))["last"]["outcome"]
        self.assertRegex(outcome, r"out of time|cut short")
        write(self.s("projects", "demo", "env.json"), "{not json")  # an error in the store: no block, no output
        self.assertEqual(self.stop(session="sess-e"), (0, ""))

    def test_a_project_without_a_box_is_not_checked(self):
        self.make("results/new.csv")
        self.set_box(backend="none")
        self.assertEqual(self.stop(), (0, ""))


class StopCheckHardStopTest(CloneMixin, ToolTest):
    def test_a_hook_whose_input_never_ends_exits_0_without_output(self):
        import sys

        from helpers import TOOL

        self.baseline()
        self.make("results/new.csv")
        env = clean_env(self.home, {"AGENT_RUNTIME": "claude-cloud", "AGENT_MIRROR_STORE": self.store})
        began = time.time()
        proc = subprocess.Popen(
            [sys.executable, TOOL, "hook", "stop-check", "--time-limit", "0"],
            cwd=self.project,
            env=env,
            stdin=subprocess.PIPE,  # never written to, never closed: only the hard stop ends the hook
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            proc.wait(timeout=90)  # not communicate(): that would close stdin
            took = time.time() - began
            out, errs = proc.stdout.read(), proc.stderr.read()
        finally:
            if proc.poll() is None:
                proc.kill()
            for fh in (proc.stdin, proc.stdout, proc.stderr):
                fh.close()
        self.assertEqual((proc.returncode, out, errs), (0, b"", b""))
        self.assertGreaterEqual(took, 10)  # it waited for its input until the hard stop (12 s after the limit)
        self.assertLess(took, 60)


class HooksPerRuntimeTest(ToolTest):
    def settings(self):
        return json.loads(read(self.h(".claude", "settings.json")))

    def ours(self):
        found = {}
        for event, groups in self.settings().get("hooks", {}).items():
            for g in groups:
                for h in g["hooks"]:
                    if "agent-mirror" in h["command"]:
                        found[event] = h
        return found

    def test_the_stop_hook_only_on_a_cloud_runtime(self):
        rc, out = self.run_tool("hooks", "install", env={"CLAUDECODE": "1"})
        self.assertEqual(rc, 0, out)
        self.assertEqual(sorted(self.ours()), ["PostToolUse", "SessionEnd", "SessionStart"])
        rc, out = self.run_tool("hooks", "status", env={"CLAUDECODE": "1"})
        self.assertIn("hooks: 3 of 3 installed for this launcher (claude-local)", out)
        rc, out = self.run_tool("hooks", "install", env={"AGENT_RUNTIME": "claude-cloud"})
        self.assertEqual(rc, 0, out)
        stop = self.ours()["Stop"]
        self.assertTrue(stop["command"].endswith("hook stop-check"), stop)
        self.assertEqual(stop["timeout"], 30)
        rc, out = self.run_tool("hooks", "status", env={"AGENT_RUNTIME": "claude-cloud"})
        self.assertIn("hooks: 4 of 4 installed for this launcher (claude-cloud)", out)
        rc, out = self.run_tool("doctor", env={"AGENT_RUNTIME": "claude-cloud"})
        self.assertIn("hooks (claude): 4 of 4 agent-mirror hooks in", out)
        # a developer machine never keeps it: status says so, install removes it
        rc, out = self.run_tool("hooks", "status", env={"CLAUDECODE": "1"})
        self.assertIn("EXTRA      Stop agent-mirror hook stop-check", out)
        rc, out = self.run_tool("doctor", env={"CLAUDECODE": "1"})
        self.assertIn("hooks (claude): 3 of 3 agent-mirror hooks in", out)
        rc, out = self.run_tool("hooks", "install", env={"CLAUDECODE": "1"})
        self.assertEqual(rc, 0, out)
        self.assertIn("removed Stop: agent-mirror hook stop-check (only a cloud machine runs it)", out)
        self.assertNotIn("Stop", self.ours())
        # uninstall removes all four
        self.run_tool("hooks", "install", env={"AGENT_RUNTIME": "claude-cloud"})
        rc, out = self.run_tool("hooks", "uninstall", env={"CLAUDECODE": "1"})
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.settings(), {})

    def test_a_cloud_session_start_adds_a_missing_stop_hook_only_where_hooks_are(self):
        here = ("--project", "demo", "--project-path", self.project, "--box-timeout", "0")
        env = {"AGENT_RUNTIME": "claude-cloud"}
        rc, out = self.run_tool("session-start", *here, env=env)
        self.assertEqual(rc, 0, out)
        self.assertEqual(steps(self.home)["hooks check"], "skipped (no agent-mirror hooks here)")
        self.assertFalse(os.path.exists(self.h(".claude", "settings.json")))
        self.run_tool("hooks", "install", env={"CLAUDECODE": "1"})  # three hooks, as a 0.5.5 setup left them
        rc, out = self.run_tool("session-start", *here, env=env)
        self.assertEqual(rc, 0, out)
        self.assertIn("hooks: added Stop (agent-mirror hook stop-check) to ", out)
        self.assertIn("raise the `# revision:` line of the setup script", out)
        self.assertEqual(steps(self.home)["hooks check"], "ok (added 1)")
        self.assertIn("Stop", self.ours())
        rc, out = self.run_tool("session-start", *here, env=env)
        self.assertEqual(steps(self.home)["hooks check"], "ok")


class PushCheckTest(CloneMixin, ToolTest):
    """A developer session start: one line when box folders this machine owns changed since its last push."""

    def setUp(self):
        super().setUp()
        self.set_box(min_free_bytes=0)
        self.as_machine("desk-a", ID_A)
        self.make("figures/a.png", "a\n")
        self.make("figures/b.png", "b\n")

    def as_machine(self, name, mid):
        path = self.h(".config", "agent-mirror", "config.json")
        cfg = json.loads(read(path)) if os.path.isfile(path) else {}
        cfg["machine"] = {"id": mid, "name": name}
        write(path, json.dumps(cfg))

    def start(self, *extra):
        rc, out = self.run_tool(
            "session-start", "--project", "demo", "--project-path", self.project, *extra, env={"CLAUDECODE": "1"}
        )
        self.assertEqual(rc, 0, out)
        return out

    def owners_line(self, folders):
        write(
            self.s("projects", "demo", "box_index.jsonl"),
            json.dumps({"path": "_owners.json", "schema": 1, "owners": folders}) + "\n",
        )

    def fingerprint(self, folder):
        from agent_mirror.box import units

        box = json.loads(read(self.s("projects", "demo", "env.json")))["box"]
        layout = units.Layout(units.settings(box, self.project))
        return layout.summary(layout.by_name[folder])["fingerprint"]

    def test_after_a_push_nothing_then_one_line_once_a_folder_changes(self):
        rc, out = self.box_run("box", "push", env={"CLAUDECODE": "1"})
        self.assertEqual(rc, 0, out)
        pushed = json.loads(read(self.p(".agent-mirror", "pushed.json")))["folders"]
        self.assertEqual(pushed["figures"]["machine_id"], ID_A)
        out = self.start()
        self.assertNotIn("box of demo", out)
        self.assertEqual(steps(self.home)["box push check (demo)"], "ok (none changed)")
        self.make("figures/a.png", "changed\n")
        out = self.start()
        self.assertIn(
            "box of demo: 1 owned folder changed since the last push (figures); upload it with: "
            "agent-mirror box push --project demo",
            out,
        )
        self.assertEqual(steps(self.home)["box push check (demo)"], "ok (1 changed)")
        before = out.split("box of demo")[0]
        self.assertIn("local runtime: only the store pull runs", before)  # the rest of the output is unchanged

    def test_ownership_from_the_committed_index_and_folders_owned_elsewhere(self):
        now = iso(time.time())
        self.owners_line(
            {
                "figures": {
                    "machine_id": ID_B,
                    "machine_name": "desk-b",
                    "since": now,
                    "fingerprint": "x",
                    "updated": now,
                }
            }
        )
        out = self.start()
        self.assertNotIn("box of demo", out)
        self.assertEqual(steps(self.home)["box push check (demo)"], "skipped (nothing owned here)")
        self.owners_line(
            {
                "figures": {
                    "machine_id": ID_A,
                    "machine_name": "desk-a",
                    "since": now,
                    "fingerprint": self.fingerprint("figures"),
                    "updated": now,
                }
            }
        )
        self.assertNotIn("box of demo", self.start())
        self.make("figures/c.png", "new\n")
        self.assertIn("box of demo: 1 owned folder changed since the last push (figures)", self.start())
        # a reader machine is never told to push
        cfg = json.loads(read(self.h(".config", "agent-mirror", "config.json")))
        cfg["choices"] = {"box:demo": "reader"}
        write(self.h(".config", "agent-mirror", "config.json"), json.dumps(cfg))
        self.assertNotIn("box of demo", self.start())
        self.assertEqual(steps(self.home)["box push check (demo)"], "skipped (box role reader)")

    def test_the_time_limit(self):
        now = iso(time.time())
        self.owners_line(
            {
                "figures": {
                    "machine_id": ID_A,
                    "machine_name": "desk-a",
                    "since": now,
                    "fingerprint": "old",
                    "updated": now,
                }
            }
        )
        out = self.start("--push-check-timeout", "0")
        self.assertNotIn("box of demo", out)
        self.assertEqual(steps(self.home)["box push check (demo)"], "skipped (--push-check-timeout 0)")
        out = self.start("--push-check-timeout", "0.000001")
        lines = [ln for ln in out.splitlines() if ln.startswith("box of demo")]
        self.assertEqual(len(lines), 1, out)
        self.assertRegex(lines[0], r"^box of demo: the check of this machine's box folders stopped after 1e-06 s; ")
        self.assertEqual(steps(self.home)["box push check (demo)"], "ok (cut short)")


class TemplateDigestTest(unittest.TestCase):
    def test_the_template_of_0_5_3_to_0_5_5_still_counts_as_the_template(self):
        import hashlib

        from agent_mirror import plan

        self.assertIn("10a591eab19e8b47a50eb61bf0209ee78afc986354cb141bfcc0e22af7d351e6", plan.OLD_TEMPLATE_DIGESTS)
        with open(plan.TEMPLATE_AGENTS, "rb") as fh:
            now = hashlib.sha256(fh.read().replace(b"\r\n", b"\n")).hexdigest()
        self.assertNotIn(now, plan.OLD_TEMPLATE_DIGESTS)
        text = read(plan.TEMPLATE_AGENTS)
        self.assertIn("agent-mirror box unsent", text)
        self.assertIn("KEEP results that took real time or money to produce", text)
        self.assertNotIn(chr(0x2014), text)


if __name__ == "__main__":
    unittest.main()
