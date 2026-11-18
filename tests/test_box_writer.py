"""Folder owners on both backends for one include folder, the old single-writer marker, and `box candidates`."""

import json
import os
import subprocess
import unittest

from helpers import setUpModule  # noqa: F401 (the sandbox guard runs first)
from helpers import FAKE_RCLONE, ToolTest, clean_env, install_fake, read, write


class WriterMarkerMixin:
    """The same story on each backend: the first push claims, another machine skips, take-over, reader."""

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

    def owners(self):
        path = self.remote("_owners.json")
        return json.loads(read(path))["folders"] if os.path.isfile(path) else None

    def test_writer_marker_story(self):
        write(self.p("temporary", "a.txt"), "abc")
        rc, out = self.run_tool("machine", "--name", "first-desk")
        self.assertEqual(rc, 0, out)
        rc, out = self.push("--dry-run")
        self.assertEqual(rc, 0, out)
        self.assertIn("would claim and upload:", out)
        self.assertIn("would record in", out)
        self.assertIsNone(self.owners())
        rc, out = self.push()
        self.assertEqual(rc, 0, out)
        self.assertIn("recorded in", out)
        first = self.owners()["temporary"]
        self.assertEqual(first["machine_name"], "first-desk")
        for key in ("machine_id", "machine_name", "since", "fingerprint", "updated"):
            self.assertIn(key, first)
        rc, out = self.push()
        self.assertEqual(rc, 0, out)
        self.assertIn("upload when changed:", out)
        self.assertNotIn("recorded in", out)  # nothing changed, so _owners.json is not written again
        # the index records the owners, so plan and box ls name them offline (the fakes keep no synced
        # files, so put the pushed file where the remote keeps it)
        write(self.remote("temporary", "a.txt"), "abc")
        rc, out = self.index()
        self.assertEqual(rc, 0, out)
        lines = [json.loads(x) for x in read(self.s("projects", "demo", "box_index.jsonl")).splitlines()]
        self.assertEqual([x["owners"]["temporary"]["machine_name"] for x in lines if "owners" in x], ["first-desk"])
        self.assertNotIn("_owners.json", [x["path"] for x in lines if "owners" not in x])
        rc, out = self.run_tool("box", "ls", "--project", "demo", "--project-path", self.project, env=self.tool_env())
        self.assertNotIn("_owners.json", out)
        self.assertIn("owner: first-desk", out)
        # another machine skips the folder (not an error), before any transfer, and says who owns it
        self.other_machine()
        self.reset_log()
        rc, out = self.push()
        self.assertEqual(rc, 0, out)
        self.assertRegex(out, r"owned by first-desk since \S+; local copy same")
        self.assertEqual(self.transfers(), [])
        rc, out = self.run_tool("plan", "--json", "--project", "demo", "--project-path", self.project)
        unit = [u for u in json.loads(out)["units"] if u["id"] == "box:demo"][0]
        self.assertEqual((unit["case"], unit["default_action"]), ("identical", "reader"))
        self.assertIn("owned elsewhere, the same here: temporary", unit["note"])
        # --take-over FOLDER moves it
        rc, out = self.push("--take-over", "temporary")
        self.assertEqual(rc, 0, out)
        self.assertIn("take-over temporary", out)
        self.assertEqual(self.owners()["temporary"]["machine_name"], "second-desk")
        # a reader is refused before anything else, even with --take-over
        self.set_role("reader")
        self.reset_log()
        rc, out = self.push("--take-over", "temporary")
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

    def test_old_single_writer_marker_keeps_working(self):
        write(self.p("temporary", "a.txt"), "abc")
        write(self.remote("temporary", "a.txt"), "abc")  # the folder is in the box already
        marker = {"machine_id": "a" * 32, "machine_name": "big-desk", "date": "2026-11-01T09:00:00", "tool": "x"}
        write(self.remote("_writer.json"), json.dumps(marker))
        rc, out = self.run_tool("machine", "--name", "first-desk")
        rc, out = self.push()
        self.assertEqual(rc, 0, out)
        self.assertIn("single-writer marker", out)
        self.assertIn("owned by big-desk since 2026-11-01T09:00:00", out)
        self.assertIsNone(self.owners())  # nothing claimed, and this is not the old writer: nothing written
        rc, out = self.index()
        lines = [json.loads(x) for x in read(self.s("projects", "demo", "box_index.jsonl")).splitlines()]
        self.assertEqual([x["writer"]["machine_name"] for x in lines if "writer" in x], ["big-desk"])
        rc, out = self.run_tool("plan", "--json", "--project", "demo", "--project-path", self.project)
        unit = [u for u in json.loads(out)["units"] if u["id"] == "box:demo"][0]
        self.assertIn("the current writer is big-desk", unit["note"])
        rc, out = self.push("--take-over")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.owners()["temporary"]["machine_name"], "first-desk")
        self.assertEqual(json.loads(read(self.remote("_writer.json")))["machine_name"], "first-desk")


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

    def remote(self, *parts):
        return os.path.join(self.root, "in-box", "demo", *parts)

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

    def remote(self, *parts):
        return os.path.join(self.root, "fakeremote", "box", "in", *parts)

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
            or (c[0] == "copyto" and not c[1].startswith(self.INBOUND))
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
        write(p("decompile", "proj-a", "db.bin"), "a\n")
        write(p("decompile", "proj-b", "db.bin"), "b\n")
        write(p("decompile", "notes.txt"), "n\n")
        write(p("docs", "readme.md"), "tracked\n")
        write(p("docs", "slides", "deck.pptx"), "s" * 50)
        subprocess.run(["git", "init", "-q", self.project], env=clean_env(self.home), check=True)
        git = lambda *a: subprocess.run(
            ["git", "-C", self.project] + list(a), env=clean_env(self.home), check=True
        )  # noqa: E731
        git("add", "src", ".gitignore", "docs/readme.md")
        git("commit", "-q", "-m", "tracked files")
        git("worktree", "add", "-q", "--detach", "wt")  # a real worktree inside the project
        self.set_box(archives=["ws-*", "decompile/proj-*"], archive_large_file_bytes=1000)

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
        self.assertEqual(rows["decompile/"]["box"], "partly covered (archived: decompile/proj-a, decompile/proj-b)")
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
