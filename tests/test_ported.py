"""The original suite of the private predecessor tool, ported to agent-mirror's names.

Run from the repo root:  python3 -m unittest discover -s tests -v
"""

import glob
import json
import os
import re
import shlex
import shutil
import unittest

from helpers import setUpModule  # noqa: F401 (the sandbox guard runs first)
from helpers import (
    LOCAL_INDEX,
    SHARED_BODY,
    STORE_INDEX,
    SYMLINKS,
    TOOL,
    ToolTest,
    dir_link,
    is_link,
    memory_dir,
    read,
    readlink,
    write,
)


class PortedTest(ToolTest):
    # detect ------------------------------------------------------------------

    def test_detect_rules(self):
        cases = [
            ({}, "unknown"),
            ({"CLAUDECODE": "1"}, "claude-local"),
            ({"CLAUDE_CODE_REMOTE": "true", "CLAUDECODE": "1"}, "claude-cloud"),
            ({"CLAUDE_CODE_REMOTE": "false", "CLAUDECODE": "1"}, "claude-local"),
            ({"CODEX_THREAD_ID": "t"}, "codex-local"),
            ({"CODEX_SESSION_ID": "s"}, "codex-local"),
            ({"CODEX_CI": "1"}, "codex-local"),
            ({"AGENT_RUNTIME": "codex-cloud", "CODEX_CI": "1"}, "codex-cloud"),
            ({"AGENT_RUNTIME": "codex-cloud", "CLAUDECODE": "1"}, "codex-cloud"),
        ]
        for env, want in cases:
            with self.subTest(env=env):
                rc, out = self.run_tool("detect", env=env, tool=TOOL, with_store=False)
                self.assertEqual(rc, 0, out)
                self.assertEqual(out.strip().splitlines()[-1], want)

    # install -----------------------------------------------------------------

    def make_fresh(self):
        """A fresh machine: no instructions, skills or memory of its own (an empty Codex file is fine)."""
        os.remove(self.h(".claude", "CLAUDE.md"))
        shutil.rmtree(self.h(".claude", "skills", "alpha"))
        shutil.rmtree(self.h(".agents"))
        shutil.rmtree(memory_dir(self.home, self.project))

    def test_install_on_a_fresh_machine_links_everything(self):
        self.make_fresh()
        rc, out = self.install()
        self.assertEqual(rc, 0, out)
        self.assertNotIn("wait for a choice", out)
        expected = {
            self.h(".claude", "CLAUDE.md"): self.s("AGENTS.md"),
            self.h(".codex", "AGENTS.md"): self.s("AGENTS.md"),
            self.h(".agents", "skills", "alpha"): self.s("skills", "alpha"),
            self.h(".agents", "skills", "beta"): self.s("skills", "beta"),
            self.h(".claude", "skills", "alpha"): self.s("skills", "alpha"),
            self.h(".claude", "skills", "beta"): self.s("skills", "beta"),
            memory_dir(self.home, self.project): self.s("memory", "demo"),
        }
        for link, target in expected.items():
            self.assertLinked(link, target)
        # the shared skills folder is a real folder with one link per skill, never one whole-folder link
        self.assertFalse(is_link(self.h(".agents", "skills")))
        # entries that are not store skills are never touched
        self.assertTrue(os.path.isdir(self.h(".claude", "skills", "synced")))
        self.assertFalse(is_link(self.h(".claude", "skills", "synced")))
        self.assertLinked(self.h(".claude", "skills", "projlink"), os.path.join(self.base, "otherproject", "plugin"))
        # backups and the manifest
        manifests = self.manifests()
        self.assertEqual(len(manifests), 1)
        data = json.loads(read(manifests[0]))
        self.assertEqual(data["store"], self.store)
        by_path = {e["path"]: e for e in data["entries"]}
        codex_md = by_path[self.h(".codex", "AGENTS.md")]
        self.assertEqual(codex_md["method"], "symlink")
        self.assertEqual(read(codex_md["backup"]), "")
        self.assertTrue(by_path[self.h(".agents", "skills")]["created_dir"])
        self.assertEqual(by_path[self.h(".claude", "skills", "beta")]["before"], "absent")
        backup_root = os.path.dirname(manifests[0])
        self.assertTrue(codex_md["backup"].startswith(backup_root + os.sep + ".codex" + os.sep))
        # the project, the machine and the applied choices are remembered in the user config
        cfg = json.loads(read(self.h(".config", "agent-mirror", "config.json")))
        self.assertEqual(cfg["projects"]["demo"], self.project)
        self.assertEqual(len(cfg["machine"]["id"]), 32)
        self.assertEqual(cfg["choices"]["memory:demo"], "link")
        # and uninstall puts the fresh machine back as it was
        rc, out = self.run_tool("uninstall")
        self.assertEqual(rc, 0, out)
        self.assertFalse(os.path.lexists(self.h(".agents", "skills")))
        self.assertFalse(os.path.lexists(self.h(".claude", "CLAUDE.md")))
        self.assertEqual(read(self.h(".codex", "AGENTS.md")), "")

    def test_install_on_a_lived_in_machine_applies_only_safe_units(self):
        rc, out = self.install()
        self.assertEqual(rc, 0, out)
        self.assertIn("not fresh", out)
        self.assertIn("agent-mirror plan", out)
        for uid in ("instructions:claude", "skill:alpha", "memory:demo"):
            self.assertRegex(out, r"(first-setup|reconcile) +%s" % re.escape(uid))
        # safe units: the empty Codex file and the skill only the store has
        self.assertLinked(self.h(".codex", "AGENTS.md"), self.s("AGENTS.md"))
        self.assertLinked(self.h(".claude", "skills", "beta"), self.s("skills", "beta"))
        self.assertLinked(self.h(".agents", "skills", "beta"), self.s("skills", "beta"))
        # units that need a decision are left exactly as they were
        self.assertEqual(read(self.h(".claude", "CLAUDE.md")), "old claude user file\n")
        self.assertEqual(read(self.h(".claude", "skills", "alpha", "SKILL.md")), "old claude alpha\n")
        self.assertEqual(read(self.h(".agents", "skills", "alpha", "SKILL.md")), "old agents alpha\n")
        mem = memory_dir(self.home, self.project)
        self.assertFalse(is_link(mem))
        self.assertEqual(read(os.path.join(mem, "MEMORY.md")), LOCAL_INDEX)
        self.assertFalse(os.path.exists(self.s("memory", "demo", "local_only.md")))
        self.assertNotEqual(read(self.s("AGENTS.md")), "old claude user file\n")

    def test_install_strict_refuses_a_lived_in_machine(self):
        rc, out = self.install("--strict")
        self.assertEqual(rc, 4, out)
        self.assertIn("not fresh", out)
        self.assertIn("instructions:claude", out)
        self.assertFalse(is_link(self.h(".codex", "AGENTS.md")))
        self.assertFalse(os.path.exists(self.h(".agent-mirror-backups")))
        self.assertFalse(os.path.exists(self.h(".config", "agent-mirror", "config.json")))
        self.make_fresh()
        rc, out = self.install("--strict")
        self.assertEqual(rc, 0, out)
        self.assertLinked(self.h(".claude", "CLAUDE.md"), self.s("AGENTS.md"))

    def test_install_is_idempotent(self):
        self.make_fresh()
        rc, out = self.install()
        self.assertEqual(rc, 0, out)
        links_before = {p: readlink(p) for p in glob.glob(self.h(".claude", "skills", "*")) if is_link(p)}
        rc, out = self.install()
        self.assertEqual(rc, 0, out)
        self.assertIn("nothing to change", out)
        self.assertEqual(len(self.manifests()), 1)
        links_after = {p: readlink(p) for p in glob.glob(self.h(".claude", "skills", "*")) if is_link(p)}
        self.assertEqual(links_before, links_after)
        # a plain install now finds the project in the config
        rc, out = self.run_tool("install")
        self.assertEqual(rc, 0, out)
        self.assertIn("nothing to change", out)

    def test_install_dry_run_changes_nothing(self):
        rc, out = self.install("--dry-run")
        self.assertEqual(rc, 0, out)
        self.assertIn("would link", out)
        self.assertFalse(is_link(self.h(".claude", "CLAUDE.md")))
        self.assertFalse(is_link(self.h(".codex", "AGENTS.md")))
        self.assertFalse(os.path.exists(self.h(".agent-mirror-backups")))
        self.assertFalse(os.path.exists(self.s("memory", "demo", "local_only.md")))
        self.assertTrue(os.path.isdir(memory_dir(self.home, self.project)))
        self.assertFalse(os.path.exists(self.h(".config", "agent-mirror", "config.json")))
        self.assertFalse(os.path.exists(self.s("machines")))

    def test_memory_merge_never_overwrites(self):
        choices = os.path.join(self.base, "choices.json")
        write(choices, json.dumps({"memory:demo": "merge"}))
        rc, out = self.run_tool("apply", "--choices", choices, "--project", "demo", "--project-path", self.project)
        self.assertEqual(rc, 0, out)
        store = self.s("memory", "demo")
        self.assertEqual(read(os.path.join(store, "local_only.md")), "local only memory\n")
        self.assertEqual(read(os.path.join(store, "shared.md")), SHARED_BODY)
        index = read(os.path.join(store, "MEMORY.md"))
        self.assertTrue(index.startswith(STORE_INDEX), index)  # union merge: the store's lines first
        self.assertIn("[Local](local_only.md)", index)
        self.assertLinked(memory_dir(self.home, self.project), store)
        data = json.loads(read(self.manifests()[0]))
        mem_entry = [e for e in data["entries"] if e["path"] == memory_dir(self.home, self.project)][0]
        self.assertEqual(read(os.path.join(mem_entry["backup"], "MEMORY.md")), LOCAL_INDEX)

    def test_uninstall_restores(self):
        choices = os.path.join(self.base, "choices.json")
        write(
            choices,
            json.dumps({"instructions:claude": "adopt", "skill:alpha": "take-store", "memory:demo": "link"}),
        )
        rc, out = self.run_tool("apply", "--choices", choices, "--project", "demo", "--project-path", self.project)
        self.assertEqual(rc, 0, out)
        self.assertEqual(read(self.s("AGENTS.md")), "old claude user file\n")  # adopted into the store
        for link in (
            self.h(".claude", "CLAUDE.md"),
            self.h(".agents", "skills", "alpha"),
            self.h(".claude", "skills", "alpha"),
        ):
            self.assertTrue(is_link(link) or not SYMLINKS, link)
        rc, out = self.run_tool("uninstall")
        self.assertEqual(rc, 0, out)
        claude_md = self.h(".claude", "CLAUDE.md")
        self.assertFalse(is_link(claude_md))
        self.assertEqual(read(claude_md), "old claude user file\n")
        self.assertFalse(is_link(self.h(".codex", "AGENTS.md")))
        self.assertEqual(read(self.h(".codex", "AGENTS.md")), "")
        self.assertFalse(is_link(self.h(".agents", "skills")))
        self.assertEqual(sorted(os.listdir(self.h(".agents", "skills"))), ["alpha"])
        self.assertEqual(read(self.h(".agents", "skills", "alpha", "SKILL.md")), "old agents alpha\n")
        self.assertEqual(read(self.h(".claude", "skills", "alpha", "SKILL.md")), "old claude alpha\n")
        self.assertFalse(os.path.lexists(self.h(".claude", "skills", "beta")))
        mem = memory_dir(self.home, self.project)
        self.assertFalse(is_link(mem))
        self.assertEqual(read(os.path.join(mem, "MEMORY.md")), LOCAL_INDEX)
        self.assertTrue(os.path.isdir(self.h(".claude", "skills", "synced")))
        self.assertTrue(is_link(self.h(".claude", "skills", "projlink")))
        self.assertTrue(json.loads(read(self.manifests()[0]))["restored_at"])
        rc, out = self.run_tool("uninstall")
        self.assertEqual(rc, 0, out)
        self.assertIn("0 links removed, 0 items restored", out)

    def test_refuses_targets_outside_store_and_loops(self):
        outside = os.path.join(self.base, "outside", "evil")
        write(os.path.join(outside, "SKILL.md"), "outside\n")
        if not SYMLINKS:
            self.skipTest("needs symlink creation to build a link loop (not available on this machine)")
        os.symlink(outside, self.s("skills", "evil"))
        os.symlink("loopy", self.s("skills", "loopy"))
        rc, out = self.run_tool("install", "--agents", "claude")
        self.assertNotEqual(rc, 0, out)
        self.assertIn("outside the store", out)
        self.assertIn("link loop", out)
        self.assertFalse(os.path.lexists(self.h(".claude", "skills", "evil")))
        self.assertFalse(os.path.lexists(self.h(".claude", "skills", "loopy")))
        self.assertLinked(self.h(".claude", "skills", "beta"), self.s("skills", "beta"))

    def test_claude_skills_folder_link_into_store_is_converted(self):
        shutil.rmtree(self.h(".claude", "skills"))
        dir_link(self.s("skills"), self.h(".claude", "skills"))
        rc, out = self.run_tool("plan", "--agents", "claude", "--json")
        self.assertEqual(rc, 0, out)
        unit = [u for u in json.loads(out)["units"] if u["id"] == "skills-folder:claude"][0]
        self.assertEqual((unit["case"], unit["default_action"]), ("legacy-folder-link", "convert"))
        rc, out = self.run_tool("install", "--agents", "claude")
        self.assertEqual(rc, 0, out)
        self.assertFalse(is_link(self.h(".claude", "skills")))
        self.assertLinked(self.h(".claude", "skills", "alpha"), self.s("skills", "alpha"))
        self.assertLinked(self.h(".claude", "skills", "beta"), self.s("skills", "beta"))
        alpha = self.s("skills", "alpha")  # the store itself is never moved
        self.assertTrue(os.path.isdir(alpha) and not is_link(alpha))
        self.assertTrue(os.path.isfile(os.path.join(alpha, "SKILL.md")))

    def test_refuses_link_location_inside_store(self):
        os.makedirs(self.s("codexhome"))
        shutil.rmtree(self.h(".codex"))
        dir_link(self.s("codexhome"), self.h(".codex"))
        rc, out = self.run_tool("install", "--agents", "codex")
        self.assertNotEqual(rc, 0, out)
        self.assertIn("inside the store", out)
        self.assertFalse(os.path.lexists(self.s("codexhome", "AGENTS.md")))

    # sync --------------------------------------------------------------------

    def test_secret_scan_blocks_commit(self):
        self.init_git()
        head = self.git("rev-parse", "HEAD")
        fake_key = "s" + "k-" + "Ab3" * 8  # key-shaped, built at runtime
        write(self.s("memory", "demo", "leak.md"), "---\nname: leak\n---\nkey " + fake_key + "\n")
        rc, out = self.run_tool("sync", env={"CLAUDECODE": "1"})
        self.assertEqual(rc, 3, out)
        self.assertIn("memory/demo/leak.md", out)
        self.assertIn("openai-style-key", out)
        self.assertNotIn(fake_key, out)
        self.assertEqual(self.git("rev-parse", "HEAD"), head)
        self.assertEqual(self.git("--git-dir", self.origin, "rev-parse", "refs/heads/main", cwd=self.base), head)

    def test_sync_commits_adopts_and_pushes(self):
        self.init_git()
        # strays are adopted only for a project whose memory this machine decided to keep in the store
        write(
            self.h(".config", "agent-mirror", "config.json"),
            json.dumps({"projects": {"demo": self.project}, "choices": {"memory:demo": "merge"}}),
        )
        write(
            self.s("memory", "demo", "new_fact.md"),
            "---\nname: new-fact\ndescription: test\nmetadata:\n  type: project\n  author: codex\n---\nbody\n",
        )
        rc, out = self.run_tool("sync", env={"CLAUDECODE": "1"})
        self.assertEqual(rc, 0, out)
        self.assertTrue(self.git("log", "-1", "--format=%s").startswith("memory: sync from claude-local "))
        tracked = self.git("ls-files", "memory/demo").splitlines()
        self.assertIn("memory/demo/new_fact.md", tracked)
        self.assertIn("memory/demo/local_only.md", tracked)  # adopted from the real Claude folder
        self.assertEqual(read(self.s("memory", "demo", "MEMORY.md")), STORE_INDEX)
        head = self.git("rev-parse", "HEAD")
        self.assertEqual(self.git("--git-dir", self.origin, "rev-parse", "refs/heads/main", cwd=self.base), head)

    def test_sync_leaves_undecided_memory_alone(self):
        self.init_git()
        rc, out = self.run_tool("sync", env={"CLAUDECODE": "1"})
        self.assertEqual(rc, 0, out)
        self.assertNotIn("memory/demo/local_only.md", self.git("ls-files", "memory/demo").splitlines())
        self.assertFalse(os.path.exists(self.s("memory", "demo", "local_only.md")))

    def other_clone_pushes(self, rel, text):
        """Simulate another machine pushing a change to the same store."""
        other = os.path.join(self.base, "other")
        self.git("clone", "-q", self.origin, other, cwd=self.base)
        write(os.path.join(other, rel), text)
        self.git("add", "-A", cwd=other)
        self.git("commit", "-q", "-m", "other machine", cwd=other)
        self.git("push", "-q", cwd=other)
        return self.git("rev-parse", "HEAD", cwd=other)

    def test_sync_rebase_conflict_keeps_local_commit(self):
        self.init_git()
        remote_head = self.other_clone_pushes("memory/demo/shared.md", SHARED_BODY + "from the other machine\n")
        write(self.s("memory", "demo", "shared.md"), SHARED_BODY + "from this machine\n")
        rc, out = self.run_tool("sync", env={"CLAUDECODE": "1"})
        self.assertEqual(rc, 0, out)
        self.assertIn("rebase aborted", out)
        self.assertTrue(self.git("log", "-1", "--format=%s").startswith("memory: sync from "))
        self.assertFalse(os.path.exists(os.path.join(self.store, ".git", "rebase-merge")))
        self.assertFalse(os.path.exists(os.path.join(self.store, ".git", "rebase-apply")))
        self.assertEqual(self.git("--git-dir", self.origin, "rev-parse", "refs/heads/main", cwd=self.base), remote_head)

    def test_memory_index_lines_merge_by_union(self):
        self.init_git()
        self.other_clone_pushes("memory/demo/MEMORY.md", STORE_INDEX + "- [Other](other.md): from the other machine\n")
        write(self.s("memory", "demo", "MEMORY.md"), STORE_INDEX + "- [Mine](mine.md): from this machine\n")
        rc, out = self.run_tool("sync", env={"CLAUDECODE": "1"})
        self.assertEqual(rc, 0, out)
        merged = read(self.s("memory", "demo", "MEMORY.md"))
        self.assertIn("from the other machine", merged)
        self.assertIn("from this machine", merged)
        head = self.git("rev-parse", "HEAD")
        self.assertEqual(self.git("--git-dir", self.origin, "rev-parse", "refs/heads/main", cwd=self.base), head)

    # session-start -----------------------------------------------------------

    def test_session_start_local_skips_dependency_sync(self):
        rc, out = self.run_tool(
            "session-start", "--project", "demo", "--project-path", self.project, env={"CLAUDECODE": "1"}
        )
        self.assertEqual(rc, 0, out)
        self.assertFalse(os.path.exists(os.path.join(self.project, "dep_sync_ran.marker")))
        self.assertFalse(is_link(self.h(".claude", "CLAUDE.md")))
        self.assertFalse(os.path.exists(self.h(".agent-mirror-backups")))
        self.assertIn("memory index: " + self.s("memory", "demo", "MEMORY.md"), out)

    def test_session_start_cloud_syncs_and_installs(self):
        # a fresh cloud machine; the project is found from the clone's origin URL, as in a cloud VM
        self.make_fresh()
        self.git("init", "-q", cwd=self.project)
        self.git("remote", "add", "origin", "https://github.com/Example/demo.git", cwd=self.project)
        rc, out = self.run_tool(
            "session-start", env={"AGENT_RUNTIME": "claude-cloud", "CLAUDE_PROJECT_DIR": self.project}
        )
        self.assertEqual(rc, 0, out)
        self.assertTrue(os.path.exists(os.path.join(self.project, "dep_sync_ran.marker")), out)
        self.assertLinked(self.h(".claude", "CLAUDE.md"), self.s("AGENTS.md"))
        self.assertLinked(memory_dir(self.home, self.project), self.s("memory", "demo"))
        self.assertFalse(is_link(self.h(".codex", "AGENTS.md")))  # claude-cloud installs for Claude only
        self.assertIn("memory index: " + self.s("memory", "demo", "MEMORY.md"), out)

    # box (fake az on PATH, no network) -------------------------------------------

    def test_box_commands_report_not_configured(self):
        self.set_box(storage_account="")
        for args in (
            ["box", "index"],
            ["box", "push"],
            ["box", "fetch", "temporary/a.txt"],
            ["box", "send", "temporary/a.txt"],
        ):
            with self.subTest(args=args):
                rc, out = self.box_run(*args)
                self.assertEqual(rc, 0, out)
                self.assertIn("box not configured", out)
        self.set_box(backend="none")
        rc, out = self.box_run("box", "index")
        self.assertEqual(rc, 0, out)
        self.assertIn("box not configured", out)
        self.assertEqual(self.az_calls(), [])

    def test_az_missing_is_one_clear_line(self):
        empty = os.path.join(self.base, "emptybin")
        os.makedirs(empty)
        for args in (["box", "index"], ["box", "fetch", "temporary/a"], ["box", "send", ".gitignore"]):
            with self.subTest(args=args):
                write(self.p(".gitignore"), "x\n")
                rc, out = self.box_run(*args, az={"PATH": empty})
                self.assertNotEqual(rc, 0, out)
                self.assertIn("az (the Azure CLI) is not installed", out)
                self.assertEqual(len(out.strip().splitlines()), 1, out)

    def test_box_push_dry_run_arguments(self):
        for rel in ("temporary/keep.txt", "temporary/a/__pycache__/m.pyc", "temporary/scratch/s.txt", "figures/f.png"):
            write(self.p(*rel.split("/")), "x\n")
        rc, out = self.box_run("box", "push", "--dry-run", env={"CLAUDECODE": "1"})
        self.assertEqual(rc, 0, out)
        self.assertIn(
            "would run: az storage blob sync --account-name testacct --auth-mode login --container in-box "
            "--source %s --destination demo/temporary/ --delete-destination false "
            "--exclude-path 'downloads/;scratch/;a/__pycache__/' --exclude-pattern '.env;*.env' "
            "--only-show-errors" % shlex.quote(self.p("temporary")),
            out,
        )
        self.assertIn(
            "--source %s --destination demo/figures/ --delete-destination false --exclude-pattern"
            % shlex.quote(self.p("figures")),
            out,
        )
        self.assertIn("docs/slides: skipped (no such folder in the project)", out)
        self.assertIn("would write the writer marker in-box/demo/_writer.json", out)
        rc, out = self.box_run("box", "push", "--dry-run", "--delete", env={"CLAUDECODE": "1"})
        self.assertEqual(rc, 0, out)
        self.assertIn("--delete-destination true", out)
        self.assertNotIn("--delete-destination false", out)
        # a dry run only reads the writer marker
        names = [c["argv"][c["argv"].index("--name") + 1] for c in self.az_calls()]
        self.assertEqual(names, ["demo/_writer.json", "demo/_writer.json"])
        self.assertEqual(self.az_calls(), self.az_calls("storage", "blob", "download"))

    def test_box_push_syncs_each_root_and_refuses_cloud(self):
        write(self.p("temporary", "keep.txt"), "x\n")
        write(self.p("figures", "f.png"), "x\n")
        rc, out = self.box_run("box", "push", env={"CLAUDECODE": "1"})
        self.assertEqual(rc, 0, out)
        syncs = self.az_calls("storage", "blob", "sync")
        self.assertEqual(
            [c["argv"][c["argv"].index("--destination") + 1] for c in syncs], ["demo/temporary/", "demo/figures/"]
        )
        self.assertTrue(all("false" == c["argv"][c["argv"].index("--delete-destination") + 1] for c in syncs))
        self.assertIn("synced temporary to in-box/demo/temporary/", out)
        os.remove(self.az_log)
        for runtime in ("claude-cloud", "codex-cloud"):
            with self.subTest(runtime=runtime):
                rc, out = self.box_run("box", "push", env={"AGENT_RUNTIME": runtime})
                self.assertEqual(rc, 2, out)
                self.assertIn("refusing box push on " + runtime, out)
        self.assertEqual(self.az_calls(), [])

    def test_box_index_pages_and_writes_jsonl_next_to_env_file(self):
        stamp = "2026-11-16T10:00:00+00:00"
        blobs = [
            {"name": "demo/temporary/a.txt", "size": 1, "modtime": stamp},
            {"name": "demo/temporary/scratch/old.txt", "size": 2, "modtime": stamp},
            {"name": "demo/figures/f.png", "size": 3, "modtime": stamp},
            {"name": "demo/docs/slides/d.pptx", "size": 4, "modtime": stamp},
            {"name": "other/temporary/x.txt", "size": 5, "modtime": stamp},
        ]
        alt = os.path.join(self.base, "alt", "env.json")
        write(alt, read(self.s("projects", "demo", "env.json")))
        rc, out = self.box_run(
            "box",
            "index",
            "--page-size",
            "2",
            "--env-file",
            alt,
            env={"AGENT_RUNTIME": "claude-cloud"},
            az=self.fake_az(blobs),
        )
        self.assertEqual(rc, 0, out)
        lines = [json.loads(x) for x in read(os.path.join(self.base, "alt", "box_index.jsonl")).splitlines()]
        self.assertEqual([x["path"] for x in lines], ["docs/slides/d.pptx", "figures/f.png", "temporary/a.txt"])
        self.assertEqual(lines[0], {"path": "docs/slides/d.pptx", "size": 4, "modtime": stamp})
        self.assertIn("left out 1 blobs", out)
        self.assertFalse(os.path.exists(self.s("projects", "demo", "box_index.jsonl")))
        lists = self.az_calls("storage", "blob", "list")
        self.assertEqual(len(lists), 2)
        self.assertNotIn("--marker", lists[0]["argv"])
        self.assertEqual(lists[1]["argv"][lists[1]["argv"].index("--marker") + 1], "2")
        self.assertEqual(lists[0]["argv"][lists[0]["argv"].index("--prefix") + 1], "demo/")

    def test_fetch_folder_and_file_never_overwrites_locally(self):
        blobs = [
            {"name": n, "size": 1, "modtime": "t"}
            for n in ("demo/temporary/r/a.txt", "demo/temporary/r/sub/b.txt", "demo/temporary/r2.txt")
        ]
        az = self.fake_az(blobs)
        write(self.p("temporary", "r", "a.txt"), "local\n")
        rc, out = self.box_run("box", "fetch", "temporary/r", env={"CLAUDECODE": "1"}, az=az)
        self.assertEqual(rc, 0, out)
        self.assertEqual(read(self.p("temporary", "r", "a.txt")), "local\n")
        self.assertEqual(read(self.p("temporary", "r", "sub", "b.txt")), "remote demo/temporary/r/sub/b.txt")
        self.assertFalse(os.path.exists(self.p("temporary", "r2.txt")))  # a sibling that shares the name prefix
        self.assertIn("kept 1 existing local files", out)
        self.assertEqual(glob.glob(self.p(".agent-mirror-fetch-*")), [])
        batch = self.az_calls("storage", "blob", "download-batch")[0]["argv"]
        self.assertEqual(batch[batch.index("--pattern") + 1], "demo/temporary/r/*")

        rc, out = self.box_run("box", "fetch", "temporary/r", env={"AGENT_RUNTIME": "claude-cloud"}, az=az)
        self.assertEqual(rc, 0, out)
        self.assertEqual(read(self.p("temporary", "r", "a.txt")), "remote demo/temporary/r/a.txt")

        rc, out = self.box_run("box", "fetch", "temporary/r2.txt", env={"CLAUDECODE": "1"}, az=az)
        self.assertEqual(rc, 0, out)
        self.assertEqual(read(self.p("temporary", "r2.txt")), "remote demo/temporary/r2.txt")
        one = self.az_calls("storage", "blob", "download")[0]["argv"]
        self.assertEqual(one[one.index("--overwrite") + 1], "false")

        rc, out = self.box_run("box", "fetch", "temporary/nope", env={"CLAUDECODE": "1"}, az=az)
        self.assertEqual(rc, 1, out)
        self.assertIn("nothing found at in-box/demo/temporary/nope", out)

    def test_fetch_dry_run_arguments(self):
        rc, out = self.box_run("box", "fetch", "docs/slides/a.pptx", "--dry-run", env={"CLAUDECODE": "1"})
        self.assertEqual(rc, 0, out)
        self.assertIn(
            "would run: az storage blob list --account-name testacct --auth-mode login --container-name in-box "
            "--prefix demo/docs/slides/a.pptx --num-results '*'",
            out,
        )
        self.assertIn("az storage blob download --account-name testacct", out)
        self.assertIn("az storage blob download-batch", out)
        self.assertIn("--pattern 'demo/docs/slides/a.pptx/*'", out)
        self.assertIn("never overwriting existing local files", out)

        rc, out = self.box_run("box", "fetch", "docs/slides/a.pptx", "--dry-run", env={"AGENT_RUNTIME": "claude-cloud"})
        self.assertEqual(rc, 0, out)
        self.assertIn("(overwriting local files)", out)

        write(self.p("docs", "slides", "a.pptx"), "local copy\n")
        rc, out = self.box_run("box", "fetch", "docs/slides/a.pptx", "--dry-run", env={"CLAUDECODE": "1"})
        self.assertEqual(rc, 0, out)
        self.assertIn("already exists locally", out)
        self.assertNotIn("would run", out)

        rc, out = self.box_run("box", "fetch", "../etc/passwd", "--dry-run")
        self.assertEqual(rc, 2, out)
        self.assertEqual(self.az_calls(), [])

    def test_send_destination_and_refusals(self):
        write(self.p("temporary", "result.csv"), "x\n")
        write(self.p("temporary", "scratch", "x.txt"), "x\n")
        rc, out = self.box_run("box", "send", "temporary/result.csv", "--dry-run", env={"AGENT_RUNTIME": "codex-cloud"})
        self.assertEqual(rc, 0, out)
        self.assertRegex(
            out,
            r"would run: az storage blob upload --account-name testacct --auth-mode login --container-name out-box "
            r"--name demo/codex-cloud-\d{8}T\d{6}Z/temporary/result\.csv --file '?\S+[/\\]temporary[/\\]result\.csv'? "
            r"--overwrite false",
        )
        for bad in (
            ".env",
            "temporary/prod.env",
            "temporary/scratch/x.txt",
            "temporary/scratch",
            "x/__pycache__/y.pyc",
        ):
            with self.subTest(send=bad):
                rc, out = self.box_run("box", "send", bad)
                self.assertEqual(rc, 2, out)
                self.assertIn("refusing to upload", out)
        self.assertEqual(self.az_calls(), [])

    def test_send_folder_skips_excluded_and_env_files(self):
        for rel in ("out.csv", "sub/plot.png", ".env", "creds.env", "sub/__pycache__/m.pyc"):
            write(self.p("temporary", "run", *rel.split("/")), "x\n")
        rc, out = self.box_run("box", "send", "temporary/run", env={"CLAUDECODE": "1"})
        self.assertEqual(rc, 0, out)
        self.assertIn("skipping 3 files", out)
        call = self.az_calls("storage", "blob", "upload-batch")[0]
        self.assertEqual(call["files"], ["out.csv", "sub/plot.png"])
        dest = call["argv"][call["argv"].index("--destination-path") + 1]
        self.assertRegex(dest, r"^demo/claude-local-\d{8}T\d{6}Z/temporary/run$")
        self.assertIn("uploaded 2 files to out-box/" + dest + "/", out)
        self.assertEqual(glob.glob(self.p(".agent-mirror-send-*")), [])
        self.assertTrue(os.path.isfile(self.p("temporary", "run", ".env")))  # the source is never touched
        # env files are skipped even when the config has no exclude rule for them
        self.set_box(exclude=[])
        os.remove(self.az_log)
        rc, out = self.box_run("box", "send", "temporary/run", env={"CLAUDECODE": "1"})
        self.assertEqual(rc, 0, out)
        files = self.az_calls("storage", "blob", "upload-batch")[0]["files"]
        self.assertEqual(files, ["out.csv", "sub/__pycache__/m.pyc", "sub/plot.png"])

    def test_cloud_sign_in_passes_secret_on_stdin_and_scrubs_it(self):
        secret = "fake-" + "S3cr3t" * 4  # built at runtime
        write(self.p("temporary", "result.csv"), "x\n")
        sp = {
            "AGENT_RUNTIME": "claude-cloud",
            "AZURE_CLIENT_ID": "client-id-1",
            "AZURE_TENANT_ID": "tenant-id-1",
            "AZURE_CLIENT_SECRET": secret,
            "FAKE_AZ_SECRET": secret,
            "FAKE_AZ_ACCOUNT_SHOW_RC": "1",
        }
        rc, out = self.box_run("box", "send", "temporary/result.csv", env=sp)
        self.assertEqual(rc, 0, out)
        login = self.az_calls("login")[0]
        self.assertTrue(login["stdin_is_secret"])
        for flag in ("--service-principal", "--password=@-", "--allow-no-subscriptions"):
            self.assertIn(flag, login["argv"])
        self.assertEqual(login["argv"][login["argv"].index("--username") + 1], "client-id-1")
        self.assertEqual(self.az_calls()[-1]["argv"][:3], ["storage", "blob", "upload"])
        self.assertNotIn(secret, read(self.az_log))
        self.assertNotIn(secret, out)

        rc, out = self.box_run("box", "send", "temporary/result.csv", env=dict(sp, FAKE_AZ_LOGIN_FAIL="1"))
        self.assertEqual(rc, 1, out)
        self.assertIn("invalid secret ***", out)
        self.assertNotIn(secret, out)

        rc, out = self.box_run("box", "send", "temporary/result.csv", "--dry-run", env=sp)
        self.assertEqual(rc, 0, out)
        self.assertIn("--username '$AZURE_CLIENT_ID' --tenant '$AZURE_TENANT_ID' --password=@-", out)
        self.assertNotIn(secret, out)

        os.remove(self.az_log)
        rc, out = self.box_run("box", "send", "temporary/result.csv", env=dict(sp, AGENT_RUNTIME=""))
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.az_calls("login"), [])  # never signs in on a local runtime
        self.assertEqual(self.az_calls("account"), [])

    def test_doctor_reports_az_and_box(self):
        rc, out = self.box_run("doctor")
        self.assertEqual(rc, 0, out)
        self.assertIn("ok      az (", out)
        self.assertIn("demo: azure-blob, storage account testacct", out)
        self.assertIn("inbound  in-box/demo/", out)
        self.assertIn("outbound out-box/demo/", out)
        self.assertIn("store: %s (from AGENT_MIRROR_STORE)" % self.store, out)
        self.assertIn("CLAUDE.md: ok (imports @AGENTS.md)", out)
        self.assertNotIn("PROBLEM", out)


if __name__ == "__main__":
    unittest.main()
