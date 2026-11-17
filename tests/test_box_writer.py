"""One writer per box (the _writer.json marker) on both backends, and `box candidates`."""

import json
import os
import subprocess
import unittest

from helpers import setUpModule  # noqa: F401 (the sandbox guard runs first)
from helpers import FAKE_RCLONE, ToolTest, clean_env, install_fake, read, write


class WriterMarkerMixin:
    """The same story on each backend: first push claims, another machine is refused, take-over, reader."""

    def other_machine(self):
        cfg_path = self.h(".config", "agent-mirror", "config.json")
        cfg = json.loads(read(cfg_path))
        cfg["machine"] = {"id": "b" * 32, "name": "second-desk"}
        write(cfg_path, json.dumps(cfg))

    def set_role(self, role):
        path = os.path.join(self.base, "role.json")
        write(path, json.dumps({"box:demo": role}))
        rc, out = self.run_tool(
            "apply", "--choices", path, "--only", "box:demo", "--project", "demo", "--project-path", self.project
        )
        self.assertEqual(rc, 0, out)

    def test_writer_marker_story(self):
        write(self.p("temporary", "a.txt"), "abc")
        rc, out = self.run_tool("machine", "--name", "first-desk")
        self.assertEqual(rc, 0, out)
        rc, out = self.push("--dry-run")
        self.assertEqual(rc, 0, out)
        self.assertIn("would write the writer marker", out)
        self.assertIsNone(self.marker())
        rc, out = self.push()
        self.assertEqual(rc, 0, out)
        self.assertIn("is the writer", out)
        first = self.marker()
        self.assertEqual(first["machine_name"], "first-desk")
        for key in ("machine_id", "machine_name", "date", "tool"):
            self.assertIn(key, first)
        rc, out = self.push()
        self.assertEqual(rc, 0, out)
        self.assertIn("writer: this machine (first-desk)", out)
        # the index records the marker, so plan names the writer offline
        rc, out = self.index()
        self.assertEqual(rc, 0, out)
        lines = [json.loads(x) for x in read(self.s("projects", "demo", "box_index.jsonl")).splitlines()]
        self.assertEqual([x["writer"]["machine_name"] for x in lines if "writer" in x], ["first-desk"])
        self.assertNotIn("_writer.json", [x["path"] for x in lines if "writer" not in x])
        rc, out = self.run_tool("box", "ls", "--project", "demo", "--project-path", self.project, env=self.tool_env())
        self.assertNotIn("_writer.json", out)
        # another machine is refused, before any transfer, and told who writes
        self.other_machine()
        self.reset_log()
        rc, out = self.push()
        self.assertEqual(rc, 2, out)
        self.assertIn("the box's writer is first-desk", out)
        self.assertIn("--take-over", out)
        self.assertEqual(self.transfers(), [])
        rc, out = self.push("--dry-run")
        self.assertEqual(rc, 2, out)
        rc, out = self.run_tool("plan", "--json", "--project", "demo", "--project-path", self.project)
        unit = [u for u in json.loads(out)["units"] if u["id"] == "box:demo"][0]
        self.assertIn("the current writer is first-desk", unit["note"])
        # --take-over rewrites the marker
        rc, out = self.push("--take-over")
        self.assertEqual(rc, 0, out)
        self.assertIn("took over", out)
        self.assertEqual(self.marker()["machine_name"], "second-desk")
        # a reader is refused before anything else, even with --take-over
        self.set_role("reader")
        self.reset_log()
        rc, out = self.push("--take-over")
        self.assertEqual(rc, 2, out)
        self.assertIn("recorded role is reader", out)
        self.assertEqual(self.tool_calls(), [])
        self.set_role("none")
        rc, out = self.push("--dry-run")
        self.assertEqual(rc, 2, out)
        self.assertIn("recorded role is none", out)
        self.set_role("writer")
        rc, out = self.push()
        self.assertEqual(rc, 0, out)


class AzureWriterTest(WriterMarkerMixin, ToolTest):
    def setUp(self):
        super().setUp()
        self.root = os.path.join(self.base, "containers")
        os.makedirs(self.root)

    def tool_env(self):
        env = self.fake_az()
        env["FAKE_AZ_ROOT"] = self.root
        env["CLAUDECODE"] = "1"
        return env

    def push(self, *extra):
        return self.box_run("box", "push", *extra, az=self.tool_env())

    def index(self):
        return self.box_run("box", "index", az=self.tool_env())

    def marker(self):
        path = os.path.join(self.root, "in-box", "demo", "_writer.json")
        return json.loads(read(path)) if os.path.isfile(path) else None

    def reset_log(self):
        if os.path.exists(os.path.join(self.base, "az_log.jsonl")):
            os.remove(os.path.join(self.base, "az_log.jsonl"))

    def tool_calls(self):
        self.az_log = os.path.join(self.base, "az_log.jsonl")
        return self.az_calls()

    def transfers(self):
        return [
            c
            for c in self.tool_calls()
            if c["argv"][:3] in (["storage", "blob", "sync"], ["storage", "blob", "upload"])
        ]


class RcloneWriterTest(WriterMarkerMixin, ToolTest):
    INBOUND = "fakeremote:box/in"

    def setUp(self):
        super().setUp()
        env = json.loads(read(self.s("projects", "demo", "env.json")))
        env["box"] = {
            "backend": "rclone",
            "inbound_remote": self.INBOUND,
            "outbound_remote": "fakeremote:box/out",
            "include": ["temporary/**"],
        }
        write(self.s("projects", "demo", "env.json"), json.dumps(env))
        self.root = os.path.join(self.base, "remotes")
        self.log = os.path.join(self.base, "rclone_log.jsonl")

    def tool_env(self):
        fakebin = install_fake(self.base, "rclone", FAKE_RCLONE)
        return {
            "PATH": fakebin + os.pathsep + os.environ.get("PATH", ""),
            "FAKE_RCLONE_LOG": self.log,
            "FAKE_RCLONE_INBOUND": self.INBOUND,
            "FAKE_RCLONE_ROOT": self.root,
            "CLAUDECODE": "1",
        }

    def push(self, *extra):
        return self.box_run("box", "push", *extra, az=self.tool_env())

    def index(self):
        return self.box_run("box", "index", az=self.tool_env())

    def marker(self):
        path = os.path.join(self.root, "fakeremote", "box", "in", "_writer.json")
        return json.loads(read(path)) if os.path.isfile(path) else None

    def reset_log(self):
        if os.path.exists(self.log):
            os.remove(self.log)

    def tool_calls(self):
        if not os.path.isfile(self.log):
            return []
        return [json.loads(x)["argv"] for x in read(self.log).splitlines()]

    def transfers(self):
        return [
            c
            for c in self.tool_calls()
            if c[0] in ("copy", "sync")
            or (c[0] == "copyto" and c[-1].endswith("_writer.json") and not c[1].startswith(self.INBOUND))
        ]


class CandidatesTest(ToolTest):
    def setUp(self):
        super().setUp()
        p = self.p
        write(p(".gitignore"), ".venv/\n*.env\nresults/\nws-*/\n")
        write(p("src", "main.py"), "print(1)\n")
        write(p(".venv", "lib", "site.py"), "x" * 300)
        write(p("results", "big.bin"), "y" * 2000)
        write(p("results", "small.csv"), "1,2\n")
        write(p("prod.env"), "A=1\n")
        write(p("credentials.json"), "{}\n")
        write(p("temporary", "notes.txt"), "n\n")
        write(p("ws-1", "f.txt"), "w\n")
        write(p("docs", "readme.md"), "tracked\n")
        write(p("docs", "slides", "deck.pptx"), "s" * 50)
        subprocess.run(["git", "init", "-q", self.project], env=clean_env(self.home), check=True)
        git = lambda *a: subprocess.run(
            ["git", "-C", self.project] + list(a), env=clean_env(self.home), check=True
        )  # noqa: E731
        git("add", "src", ".gitignore", "docs/readme.md")
        git("commit", "-q", "-m", "tracked files")
        git("worktree", "add", "-q", "--detach", "wt")  # a real worktree inside the project
        self.set_box(archives=["ws-*"], archive_large_file_bytes=1000)

    def candidates(self, *extra, **kw):
        return self.run_tool(
            "box", "candidates", "--project", "demo", "--project-path", kw.get("path", self.project), *extra
        )

    def test_kinds_sizes_and_coverage(self):
        rc, out = self.candidates("--json")
        self.assertEqual(rc, 0, out)
        data = json.loads(out)
        self.assertTrue(data["git"])
        rows = dict((r["path"], r) for r in data["entries"])
        self.assertEqual(rows[".venv/"]["kind"], "rebuildable")
        self.assertEqual(rows["prod.env"]["kind"], "secret")
        self.assertEqual(rows["credentials.json"]["kind"], "secret")
        self.assertEqual(rows["wt/"]["kind"], "worktree")
        self.assertEqual(rows["results/"]["kind"], "candidate")
        on_disk = os.path.getsize(self.p("results", "big.bin")) + os.path.getsize(self.p("results", "small.csv"))
        self.assertEqual(
            (rows["results/"]["files"], rows["results/"]["bytes"], rows["results/"]["large_files"]), (2, on_disk, 1)
        )
        self.assertEqual(rows["results/"]["git"], "ignored")
        self.assertIn(".gitignore:3:results/", rows["results/"]["ignored_by"])
        self.assertEqual(rows["temporary/"]["box"], "covered")
        self.assertEqual(rows["ws-1/"]["box"], "covered (archive)")
        self.assertEqual(rows["docs/slides/"]["box"], "covered")
        self.assertEqual(rows["results/"]["box"], "")
        self.assertNotIn("src/", rows)
        self.assertNotIn("docs/", rows)
        self.assertFalse(any(r["lower_bound"] for r in data["entries"] if r["path"] != "wt/"))
        rc, out = self.candidates("--min-bytes", "1000")
        self.assertEqual(rc, 0, out)
        self.assertIn("results/", out)
        self.assertNotIn("temporary/", out)
        self.assertIn("rebuildable", self.candidates()[1])

    def test_not_a_git_repo_lists_the_top_level(self):
        plain = os.path.join(self.base, "plain")
        write(os.path.join(plain, "data", "x.csv"), "1\n")
        write(os.path.join(plain, "node_modules", "m.js"), "m\n")
        rc, out = self.run_tool("box", "candidates", "--json", "--project-path", plain, "--project", "demo")
        self.assertEqual(rc, 0, out)
        data = json.loads(out)
        self.assertFalse(data["git"])
        kinds = dict((r["path"], r["kind"]) for r in data["entries"])
        self.assertEqual(kinds, {"data/": "candidate", "node_modules/": "rebuildable"})

    def test_time_budget_marks_a_lower_bound(self):
        for i in range(30):
            write(self.p("results", "deep", *["d%d" % j for j in range(14)], "f%d.txt" % i), "z")
        rc, out = self.candidates("--json")
        rows = dict((r["path"], r) for r in json.loads(out)["entries"])
        self.assertTrue(rows["results/"]["lower_bound"])  # deeper than the depth cap
        rc, out = self.candidates()
        self.assertIn("lower bound", out)


if __name__ == "__main__":
    unittest.main()
