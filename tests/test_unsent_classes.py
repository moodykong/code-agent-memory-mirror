"""0.5.7: files for the box and files left to git.

`box unsent` classes each unsent file: for the box (ignored by git, or untracked and large, binary above a limit, or
one of many small files of a top-level folder), or left to git (every other untracked file, which a vendor's
commit-and-push stop hook asks to commit). The stop check blocks only on files for the box and logs every run;
`probe` shows the log and the hook's state. On a cloud runtime `box send` and `box skip` add what they dealt with
to the clone's local git exclude file.
"""

import hashlib
import json
import os
import subprocess
import unittest

from helpers import setUpModule  # noqa: F401 (the sandbox guard runs first)
from helpers import TEMPLATE_STORE, ToolTest, clean_env, read, write

from test_unsent import CloneMixin

KIB = 1024
# the suggested rule line of the template AGENTS.md of 0.5.6, which 0.5.7 changed
UNSENT_LINE_056 = (
    "- The machine is thrown away after the session. Before finishing, run `agent-mirror box unsent`: it lists the "
    "files of the clone that this session made or changed and that are in no git commit and not in the box. For "
    'each one, either upload it with `agent-mirror box send <path> --label <short-name> --note "<what these files '
    'are, why they were made, what to do next>"` (a send without a note is refused; every send of this session '
    "goes into the same session, and later sessions can list and fetch it), or declare it disposable with "
    '`agent-mirror box skip <path>... --reason "<why it can go>"`. On Claude Code a stop hook asks for this when '
    "such files are left.\n"
)
DIGEST_056 = "bba042ac4c93787f873e75db974b1b4ec56b8aa0154df5d35107271ccab2615d"


def binary(size):
    return b"\0" + b"b" * (size - 1)


class ClassesTest(CloneMixin, ToolTest):
    def setUp(self):
        super().setUp()
        self.baseline()

    def make_bytes(self, rel, data):
        path = self.p(*rel.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(data)
        return path

    def by_path(self, data):
        return dict((p["path"], (p["class"], p["reason"])) for p in data["paths"])

    def test_each_class_and_reason_with_the_default_limits(self):
        self.make("out/model.bin", "weights\n")  # in an ignored folder: for the box, whatever its size
        self.make("run.log", "log\n")  # ignored by name
        self.make("results/run-1/a.csv", "a\n")  # untracked, small
        self.make("results/run-1/b.log", "b\n")  # ignored, inside a new untracked folder
        self.make("notes.md", "n\n")
        self.make_bytes("data/small.bin", binary(50 * KIB))  # binary under the binary limit: left to git
        self.make_bytes("data/mid.bin", binary(200 * KIB))  # binary above it: for the box
        self.make("data/mid.txt", "t" * (200 * KIB))  # text between the two limits: left to git
        self.make("data/big.txt", "t" * (1536 * KIB))  # text above the text limit: for the box
        data = self.unsent_json()
        self.assertEqual(
            self.by_path(data),
            {
                "out/model.bin": ("for_the_box", "git_ignored"),
                "run.log": ("for_the_box", "git_ignored"),
                "results/run-1/b.log": ("for_the_box", "git_ignored"),
                "results/run-1/a.csv": ("left_to_git", "small"),
                "notes.md": ("left_to_git", "small"),
                "data/small.bin": ("left_to_git", "small"),
                "data/mid.bin": ("for_the_box", "binary"),
                "data/mid.txt": ("left_to_git", "small"),
                "data/big.txt": ("for_the_box", "large"),
            },
        )
        self.assertEqual(data["for_the_box"]["files"], 5)
        self.assertEqual(data["left_to_git"], {"files": 4, "bytes": 2 + 2 + 50 * KIB + 200 * KIB})
        self.assertEqual((data["files"], data["in_git"]), (9, True))
        self.assertEqual(
            data["limits"],
            {"git_text_max_bytes": 1048576, "git_binary_max_bytes": 102400, "git_total_max_bytes": 20971520},
        )
        self.assertEqual([p["class"] for p in data["paths"]][:5], ["for_the_box"] * 5)  # the files for the box first
        classes = set((e["path"], e["class"], tuple(e["reasons"])) for e in data["entries"])
        self.assertIn(("out/", "for_the_box", ("git_ignored",)), classes)
        self.assertIn(("data/big.txt", "for_the_box", ("large",)), classes)
        rc, out = self.unsent()
        self.assertEqual(rc, 0, out)
        lines = out.splitlines()
        self.assertRegex(lines[0], r"^unsent in demo \(.*\): 5 files for the box, 1\.7 MiB, changed since ")
        self.assertIn("  in data/: 2 files, 1.7 MiB", lines)
        self.assertIn("    data/big.txt (1.5 MiB; large)", lines)
        self.assertIn("    data/mid.bin (200.0 KiB; binary)", lines)
        self.assertIn("  out/ (1 file, 8 B; ignored by git)", lines)
        self.assertIn("  results/run-1/b.log (2 B; ignored by git)", lines)
        self.assertIn("  run.log (4 B; ignored by git)", lines)
        self.assertIn("left to git: 4 small untracked files, 250.0 KiB (commit them; --all lists them)", lines)
        self.assertNotIn("notes.md", out)
        self.assertNotIn("data/mid.txt", out)
        rc, out = self.unsent("--all")
        lines = out.splitlines()
        start = lines.index("left to git: 4 small untracked files, 250.0 KiB (commit them as usual):")
        block = lines[start + 1 :]
        self.assertIn("  in data/: 2 files, 250.0 KiB", block)
        self.assertIn("    data/mid.txt (200.0 KiB)", block)
        self.assertIn("  notes.md (2 B)", block)
        self.assertIn("  results/run-1/a.csv (2 B)", block)
        self.assertLess(lines.index("  run.log (4 B; ignored by git)"), start)

    def test_many_small_files_of_one_top_level_folder_go_to_the_box_as_one_line(self):
        self.set_box(git_total_max_bytes=10000)
        for i in range(30):
            self.make("results/run-%d/f%02d.csv" % (i % 3, i), "r" * 500)  # 15000 bytes in all
        self.make("notes/a.md", "a" * 300)  # another group, under the total
        self.make("notes/b.md", "b" * 300)
        write(self.p("src", "main.py"), "print(1)\n")  # a tracked folder with new generated files in it
        self.git("add", "src", cwd=self.project)
        self.git("commit", "-q", "-m", "src", cwd=self.project)
        for i in range(25):
            self.make("src/gen/g%02d.txt" % i, "g" * 500)  # 12500 bytes
        data = self.unsent_json()
        reasons = self.by_path(data)
        self.assertEqual(reasons["results/run-0/f00.csv"], ("for_the_box", "many_small"))
        self.assertEqual(reasons["src/gen/g00.txt"], ("for_the_box", "many_small"))
        self.assertEqual(reasons["notes/a.md"], ("left_to_git", "small"))
        rc, out = self.unsent()
        lines = out.splitlines()
        self.assertIn("  results/ (30 files, 14.6 KiB; many small files)", lines)
        self.assertIn("  src/gen/ (25 files, 12.2 KiB; many small files)", lines)  # the folder that holds them
        self.assertIn("left to git: 2 small untracked files, 600 B (commit them; --all lists them)", lines)
        rc, out = self.unsent("--check")
        self.assertEqual(rc, 3, out)
        # the defaults: 20 MiB in all
        self.set_box(git_total_max_bytes=None)
        self.assertEqual(self.unsent_json()["for_the_box"]["files"], 0)

    def test_a_limit_of_0_leaves_nothing_to_git_under_its_rule(self):
        self.make("notes.md", "n\n")
        self.make("empty.txt", "")
        self.make_bytes("tiny.bin", binary(10))
        self.make("docs/a.md", "a\n")
        self.set_box(git_text_max_bytes=0)
        reasons = self.by_path(self.unsent_json())
        self.assertEqual(set(reasons.values()), {("for_the_box", "large")})
        self.assertEqual(len(reasons), 4)
        self.set_box(git_text_max_bytes=None, git_binary_max_bytes=0)
        reasons = self.by_path(self.unsent_json())
        self.assertEqual(reasons["tiny.bin"], ("for_the_box", "binary"))
        self.assertEqual(reasons["notes.md"], ("left_to_git", "small"))
        self.assertEqual(reasons["empty.txt"], ("left_to_git", "small"))
        self.set_box(git_binary_max_bytes=None, git_total_max_bytes=0)
        reasons = self.by_path(self.unsent_json())
        self.assertEqual(set(reasons.values()), {("for_the_box", "many_small")})
        rc, out = self.unsent()
        lines = out.splitlines()
        self.assertIn("  notes.md (2 B; many small files)", lines)
        self.assertIn("  docs/ (1 file, 2 B; many small files)", lines)  # a wholly new folder stays one line

    def test_outside_a_git_repo_every_file_is_for_the_box(self):
        plain = os.path.join(self.base, "plain", "demo")
        write(os.path.join(plain, "notes.md"), "n\n")
        write(os.path.join(plain, "results", "a.csv"), "a\n")
        rc, out = self.run_tool(
            "box", "unsent", "--project", "demo", "--project-path", plain, "--json", env={"CLAUDECODE": "1"}
        )
        self.assertEqual(rc, 0, out)
        data = json.loads(out)
        self.assertFalse(data["in_git"])
        self.assertEqual(set((p["class"], p["reason"]) for p in data["paths"]), {("for_the_box", "not_git")})
        self.assertEqual(data["left_to_git"]["files"], 0)
        rc, out = self.run_tool("box", "unsent", "--project", "demo", "--project-path", plain, env={"CLAUDECODE": "1"})
        self.assertIn("  notes.md (2 B)", out.splitlines())  # no tag: every line would carry the same one
        self.assertNotIn("left to git", out)

    def test_check_exit_codes_and_bad_limit_values(self):
        self.make("notes.md", "n\n")
        rc, out = self.unsent("--check")
        self.assertEqual(rc, 0, out)
        self.assertIn("nothing for the box in demo", out)
        self.set_box(git_text_max_bytes="1 MiB", git_binary_max_bytes=-1, git_total_max_bytes=True)
        rc, out = self.unsent("--check")
        self.assertEqual(rc, 0, out)
        for key, default in (
            ("git_text_max_bytes", "1.0 MiB"),
            ("git_binary_max_bytes", "100.0 KiB"),
            ("git_total_max_bytes", "20.0 MiB"),
        ):
            self.assertIn(
                "warning: box.%s must be a whole number of bytes (0 or more); the default %s is used" % (key, default),
                out,
            )
        self.make("big.txt", "t" * (1100 * KIB))
        rc, out = self.unsent("--check")
        self.assertEqual(rc, 3, out)
        rc, out = self.run_tool("doctor", "--project", "demo", "--project-path", self.project, env={"CLAUDECODE": "1"})
        self.assertIn("box.git_text_max_bytes must be a whole number of bytes", out)


class StopSplitTest(CloneMixin, ToolTest):
    def setUp(self):
        super().setUp()
        self.baseline()
        self.set_box(git_text_max_bytes=1000)

    def stop(self, session="sess-1", stdin=None):
        data = {"session_id": session, "hook_event_name": "Stop", "stop_hook_active": False, "cwd": self.project}
        return self.run_tool(
            "hook",
            "stop-check",
            stdin=json.dumps(data) if stdin is None else stdin,
            env={"AGENT_RUNTIME": "claude-cloud", "CLAUDE_PROJECT_DIR": self.project},
        )

    def log_lines(self):
        path = self.h(".cache", "agent-mirror", "stop-check.log")
        return read(path).splitlines() if os.path.isfile(path) else []

    def test_small_untracked_files_pass_and_one_large_file_blocks(self):
        from agent_mirror import __version__

        self.make("notes.md", "n\n")
        self.make("src/new.py", "print(2)\n")
        self.assertEqual(self.stop(), (0, ""))  # only files left to git: the vendor's commit hook asks for them
        self.assertRegex(
            self.log_lines()[-1],
            r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ agent-mirror %s session=sess-1 decision=pass for_the_box=0 "
            r"left_to_git=2 \(passed \(nothing for the box, 2 left to git\)\)$" % __version__.replace(".", r"\."),
        )
        self.make("results/model.ckpt", "w" * 3000)
        rc, out = self.stop()
        self.assertEqual(rc, 0, out)
        reason = json.loads(out)["reason"]
        lines = reason.splitlines()
        self.assertEqual(
            lines[0], "agent-mirror %s stop check (Claude Code Stop hook: agent-mirror hook stop-check)" % __version__
        )
        self.assertIn("demo (%s): 1 file for the box" % self.project, lines)
        self.assertIn("  results/ (1 file, 2.9 KiB; large)", lines)
        self.assertIn(
            "Small untracked files are left to git (commit them as usual); committing a file listed here is not the "
            "remedy: send it or skip it.",
            lines,
        )
        self.assertIn("KEEP results that took real time or money to produce", reason)
        self.assertNotIn("notes.md", reason)
        self.assertNotIn("new.py", reason)
        self.assertIn("decision=block for_the_box=1 left_to_git=2", self.log_lines()[-1])

    def test_the_digest_ignores_changes_among_the_files_left_to_git(self):
        self.make("results/model.ckpt", "w" * 3000)
        self.make("notes.md", "n\n")
        self.assertIn('"decision": "block"', self.stop()[1])
        self.make("more-notes.md", "m\n")  # the set left to git grows: no new block
        self.assertEqual(self.stop(), (0, ""))
        os.remove(self.p("notes.md"))
        self.assertEqual(self.stop(), (0, ""))
        self.assertIn("passed (no new files for the box)", self.log_lines()[-1])
        self.make("results/second.ckpt", "w" * 3000)  # the set for the box grows: blocks again
        self.assertIn('"decision": "block"', self.stop()[1])

    def test_the_log_keeps_200_lines_never_names_files_and_records_errors(self):
        self.make("results/model.ckpt", "w" * 3000)
        path = self.h(".cache", "agent-mirror", "stop-check.log")
        write(path, "".join("old line %d\n" % i for i in range(205)))
        self.stop(session="s/../x y")
        self.stop(stdin="not json")
        lines = self.log_lines()
        self.assertEqual(len(lines), 200)
        self.assertEqual(lines[0], "old line 7")
        self.assertIn("session=s..xy decision=block for_the_box=1 left_to_git=0", lines[-2])
        self.assertIn("session=- decision=error for_the_box=- left_to_git=- (JSONDecodeError)", lines[-1])
        self.assertNotIn("model.ckpt", read(path))
        self.assertNotIn("results", read(path))
        # a developer machine writes nothing
        os.remove(path)
        rc, out = self.run_tool(
            "hook", "stop-check", stdin="{}", env={"CLAUDECODE": "1", "CLAUDE_PROJECT_DIR": self.project}
        )
        self.assertEqual((rc, out), (0, ""))
        self.assertFalse(os.path.exists(path))

    def test_probe_shows_the_hook_state_and_the_last_runs(self):
        cloud = {"AGENT_RUNTIME": "claude-cloud"}
        rc, out = self.run_tool("probe", "--project", "demo", "--project-path", self.project, env={"CLAUDECODE": "1"})
        self.assertEqual(rc, 0, out)
        self.assertNotIn("stop check", out)  # a developer machine as it should be: nothing new
        rc, out = self.run_tool("probe", "--project", "demo", "--project-path", self.project, env=cloud)
        self.assertEqual(rc, 0, out)
        self.assertIn(
            "stop check hook (Stop: agent-mirror hook stop-check, claude-cloud): MISSING (a cloud runtime should have "
            "it: `agent-mirror hooks install` adds it)",
            out,
        )
        self.assertRegex(out, r"stop check log: no runs recorded \(.*stop-check\.log\)")
        self.run_tool("hooks", "install", env=cloud)
        for i in range(7):
            self.stop(session="sess-%d" % i)
        rc, out = self.run_tool("probe", "--project", "demo", "--project-path", self.project, env=cloud)
        self.assertIn("stop check hook (Stop: agent-mirror hook stop-check, claude-cloud): installed", out)
        self.assertRegex(out, r"stop check log \(.*stop-check\.log\), the last 5 of 7 runs kept:")
        runs = [ln for ln in out.splitlines() if ln.startswith("  ") and " session=sess-" in ln]
        self.assertEqual([r.split("session=")[1].split()[0] for r in runs], ["sess-%d" % i for i in range(2, 7)])
        rc, out = self.run_tool("probe", "--project", "demo", "--project-path", self.project, env={"CLAUDECODE": "1"})
        self.assertIn(
            "stop check hook (Stop: agent-mirror hook stop-check, claude-local): installed, but only a cloud machine "
            "runs it (`agent-mirror hooks install` removes it)",
            out,
        )

    def test_probe_full_shows_them_in_what_the_setup_left_behind(self):
        self.make("results/model.ckpt", "w" * 3000)
        self.stop(session="sess-full")
        env = self.fake_az()
        env.update({"AGENT_RUNTIME": "claude-cloud", "FAKE_AZ_ROOT": os.path.join(self.base, "containers")})
        env = self.without_real_transfer_tools(env)
        rc, out = self.run_tool(
            "probe", "--full", "--project", "demo", "--project-path", self.project, env=env, timeout=300
        )
        self.assertEqual(rc, 0, out)
        section = out.split("===== 2 what the setup script left behind =====")[1].split("===== 3 tools")[0]
        self.assertIn("stop check hook (Stop: agent-mirror hook stop-check, claude-cloud): MISSING", section)
        self.assertRegex(section, r"stop check log \(.*stop-check\.log\), the last 1 of 1 runs kept:")
        self.assertIn("session=sess-full decision=block for_the_box=1 left_to_git=0", section)


class LocalExcludeTest(CloneMixin, ToolTest):
    def setUp(self):
        super().setUp()
        self.set_box(min_free_bytes=0, git_text_max_bytes=1000)
        self.baseline()
        self.env = self.fake_az()
        self.env["FAKE_AZ_ROOT"] = os.path.join(self.base, "containers")

    def exclude_lines(self):
        path = self.p(".git", "info", "exclude")
        return read(path).splitlines() if os.path.isfile(path) else []

    def send(self, rel, runtime="claude-cloud"):
        env = dict(self.env, AGENT_RUNTIME=runtime) if runtime != "claude-local" else dict(self.env, CLAUDECODE="1")
        return self.box_run("box", "send", rel, "--note", "a result of the test", env=env, az=self.env)

    def skip(self, *rels, runtime="claude-cloud"):
        env = dict(self.env, AGENT_RUNTIME=runtime) if runtime != "claude-local" else dict(self.env, CLAUDECODE="1")
        return self.box_run("box", "skip", *rels, "--reason", "rebuildable by the test", env=env, az=self.env)

    def git_status(self):
        return self.git("status", "--porcelain=v1", "--ignored", "--untracked-files=all", cwd=self.project)

    def ignored(self, rel):
        proc = subprocess.run(
            ["git", "check-ignore", "-q", "--", rel],
            cwd=self.project,
            env=clean_env(self.home),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=60,
        )
        return proc.returncode == 0

    def test_send_adds_what_is_for_the_box_and_leaves_small_files_visible(self):
        self.make("results/model.ckpt", "w" * 3000)
        self.make("results/small.csv", "s\n")
        rc, out = self.send("results/model.ckpt")
        self.assertEqual(rc, 0, out)
        self.assertIn(
            "added /results/model.ckpt to this clone's local git exclude file (.git/info/exclude, never committed): "
            "it is in the box now, so a stop hook that asks to commit untracked files leaves it alone",
            out,
        )
        self.assertEqual(self.exclude_lines().count("/results/model.ckpt"), 1)
        self.assertIn("!! results/model.ckpt", self.git_status())
        self.assertIn("?? results/small.csv", self.git_status())
        rc, out = self.send("results/model.ckpt")  # again: no second line, nothing said
        self.assertEqual(rc, 0, out)
        self.assertNotIn("local git exclude", out)
        self.assertEqual(self.exclude_lines().count("/results/model.ckpt"), 1)
        rc, out = self.send("results/small.csv")  # a small file may still be committed: left visible
        self.assertEqual(rc, 0, out)
        self.assertNotIn("local git exclude", out)
        self.assertIn("?? results/small.csv", self.git_status())
        # a folder of files for the box: the folder; a folder that also holds a small file: only the large ones
        self.make("ckpts/a.bin", "a" * 2000)
        self.make("ckpts/sub/b.bin", "b" * 2000)
        self.make("mixed/large.bin", "l" * 2000)
        self.make("mixed/readme.txt", "r\n")
        self.assertIn("added /ckpts/ to", self.send("ckpts")[1])
        self.assertIn("added /mixed/large.bin to", self.send("mixed")[1])
        self.assertNotIn("/mixed/", self.exclude_lines())
        self.assertIn("?? mixed/readme.txt", self.git_status())
        # nothing sent comes back in `box unsent`, now that git ignores part of it
        data = self.unsent_json(**self.env)
        self.assertEqual(data["files"], 0, data["paths"])
        self.assertGreaterEqual(data["left_out"]["sent"], 6)
        # a sent file that changes comes back, for the box
        self.make("results/model.ckpt", "x" * 3000)
        self.assertEqual(self.by_class(), {"results/model.ckpt": ("for_the_box", "git_ignored")})

    def by_class(self):
        return dict((p["path"], (p["class"], p["reason"])) for p in self.unsent_json(**self.env)["paths"])

    @unittest.skipIf(os.name == "nt", "a Windows file name cannot hold * or ?")
    def test_skip_adds_any_untracked_path_with_exact_anchoring(self):
        names = ["a*b [1].txt", "x?", "#hash", "!bang", "plain.txt"]
        for name in names:
            self.make(name, "n\n")
        self.make("cache/a.bin")
        self.make("cache/b/c.bin")
        self.make("run.log")  # already ignored
        self.make("axb [1].txt", "near miss\n")
        rc, out = self.skip("cache", "run.log", "README.md", *names)
        self.assertEqual(rc, 0, out)
        self.assertIn(
            "added 6 paths (/cache/, /a\\*b \\[1].txt, /x\\?, /#hash, /!bang and 1 more) to this clone's local git "
            "exclude file (.git/info/exclude, never committed): they are declared disposable, so a stop hook that "
            "asks to commit untracked files leaves them alone",
            out,
        )
        lines = self.exclude_lines()
        for line in ("/cache/", "/a\\*b \\[1].txt", "/x\\?", "/#hash", "/!bang"):
            self.assertEqual(lines.count(line), 1, line)
        self.assertNotIn("/run.log", lines)
        self.assertNotIn("/README.md", lines)
        for name in names + ["cache/a.bin", "cache/b/c.bin"]:
            self.assertTrue(self.ignored(name), name)
        self.assertFalse(self.ignored("axb [1].txt"))  # only the exact names are hidden
        self.assertFalse(self.ignored("xy"))
        rc, out = self.skip("cache")  # again: no duplicate line, nothing said
        self.assertNotIn("local git exclude", out)
        self.assertEqual(self.exclude_lines(), lines)
        self.assertEqual(sorted(self.by_class()), ["axb [1].txt"])  # what was skipped does not come back

    @unittest.skipIf(os.name == "nt", "a Windows file name cannot end with a space")
    def test_the_line_of_a_name_with_trailing_spaces_and_of_a_folder(self):
        from agent_mirror.box.local import exclude_line

        self.assertEqual(exclude_line("trail  ", False), "/trail\\ \\ ")
        self.assertEqual(exclude_line("dir [2] ", True), "/dir \\[2] /")
        self.assertIsNone(exclude_line("two\nlines", False))
        self.make("trail  ", "t\n")
        self.make("trail", "near miss\n")
        self.make("dir [2] /f.txt", "f\n")
        os.makedirs(self.p(".git", "info"), exist_ok=True)
        with open(self.p(".git", "info", "exclude"), "a", encoding="utf-8") as fh:
            fh.write("%s\n%s\n" % (exclude_line("trail  ", False), exclude_line("dir [2] ", True)))
        self.assertTrue(self.ignored("trail  "))
        self.assertTrue(self.ignored("dir [2] /f.txt"))
        self.assertFalse(self.ignored("trail"))

    def test_a_developer_machine_never_touches_the_exclude_file(self):
        self.make("results/model.ckpt", "w" * 3000)
        self.make("scratch.txt", "s\n")
        rc, out = self.send("results/model.ckpt", runtime="claude-local")
        self.assertEqual(rc, 0, out)
        rc, out = self.skip("scratch.txt", runtime="claude-local")
        self.assertEqual(rc, 0, out)
        self.assertNotIn("local git exclude", out)
        self.assertEqual([ln for ln in self.exclude_lines() if ln.startswith("/")], ["/.agent-mirror/"])


class TemplateRuleTest(unittest.TestCase):
    def test_the_template_of_0_5_6_still_counts_as_the_template(self):
        from agent_mirror import plan

        self.assertIn(DIGEST_056, plan.OLD_TEMPLATE_DIGESTS)
        newest = read(os.path.join(TEMPLATE_STORE, "AGENTS.md"))
        lines = [ln for ln in newest.splitlines(True) if "box unsent" in ln]
        self.assertEqual(len(lines), 1)
        old = newest.replace(lines[0], UNSENT_LINE_056)
        self.assertEqual(hashlib.sha256(old.replace("\r\n", "\n").encode("utf-8")).hexdigest(), DIGEST_056)
        rule = lines[0]
        self.assertIn("Small new files that belong in git are committed as usual", rule)
        self.assertIn("Large or binary outputs, and anything in a git-ignored output folder, go to the box", rule)
        self.assertIn("`agent-mirror box unsent` shows which is which", rule)
        self.assertNotIn(chr(0x2014), newest)


if __name__ == "__main__":
    unittest.main()
