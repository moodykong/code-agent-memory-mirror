"""plan and apply: the per-unit reconciliation between this machine and the store.

Every case of the table for each unit kind, skills compared by content, entries that are never
skills, the legacy whole-folder link, choices files, waiting units, --yes-defaults, the secret scan
before adopt, the memory merge, recorded choices, the machine profile and its sync.
"""

import json
import os
import shutil
import unittest

from helpers import setUpModule  # noqa: F401 (the sandbox guard runs first)
from helpers import (
    LOCAL_INDEX,
    SHARED_BODY,
    STORE_INDEX,
    SYMLINKS,
    ToolTest,
    dir_link,
    is_link,
    memory_dir,
    read,
    readlink,
    write,
)

FAKE_KEY = "s" + "k-" + "Zq7" * 8  # key-shaped, built at runtime


class ReconcileBase(ToolTest):
    def setUp(self):
        super().setUp()
        rc, out = self.run_tool("machine", "--name", "test-machine")
        self.assertEqual(rc, 0, out)

    def P(self, project="demo", path=None):
        return ["--project", project, "--project-path", path or self.project]

    def plan(self, *extra, **kw):
        rc, out = self.run_tool("plan", "--json", *(list(extra) or self.P()), **kw)
        self.assertEqual(rc, 0, out)
        return json.loads(out)

    def unit(self, data, uid):
        found = [u for u in data["units"] if u["id"] == uid]
        self.assertEqual(len(found), 1, "%s in %s" % (uid, [u["id"] for u in data["units"]]))
        return found[0]

    def case(self, uid, *extra, **kw):
        return self.unit(self.plan(*extra, **kw), uid)["case"]

    def choices(self, data):
        path = os.path.join(self.base, "choices.json")
        write(path, json.dumps(data))
        return path

    def apply(self, *extra, **kw):
        return self.run_tool("apply", *(list(extra) + self.P()), **kw)

    def make_fresh(self):
        os.remove(self.h(".claude", "CLAUDE.md"))
        shutil.rmtree(self.h(".claude", "skills", "alpha"))
        shutil.rmtree(self.h(".agents"))
        shutil.rmtree(memory_dir(self.home, self.project))

    def config(self):
        return json.loads(read(self.h(".config", "agent-mirror", "config.json")))


class PlanCasesTest(ReconcileBase):
    def test_plan_json_shape_and_exit_code(self):
        data = self.plan()
        for key in (
            "schema",
            "tool",
            "machine",
            "runtime",
            "store",
            "agents",
            "projects",
            "units",
            "ignored",
            "waiting",
            "fresh",
        ):
            self.assertIn(key, data)
        self.assertEqual(data["machine"]["name"], "test-machine")
        u = self.unit(data, "skill:alpha")
        for key in (
            "id",
            "kind",
            "name",
            "machine",
            "store",
            "case",
            "default_action",
            "choices",
            "recorded",
            "decision",
            "note",
            "waiting",
        ):
            self.assertIn(key, u)
        self.assertEqual(u["kind"], "skill")
        self.assertEqual(u["machine"]["state"], "content")
        for key in ("files", "bytes", "newest"):
            self.assertIn(key, u["machine"])
            self.assertIn(key, u["store"])
        self.assertFalse(data["fresh"])
        self.assertEqual(sorted(data["waiting"]), ["instructions:claude", "memory:demo", "skill:alpha"])
        rc, out = self.run_tool("plan", *self.P())
        self.assertEqual(rc, 0, out)
        self.assertIn("reconcile: this machine and the store differ", out)
        self.assertIn("first-setup: only this machine has it", out)
        self.assertLess(out.index("reconcile:"), out.index("first-setup:"))
        self.assertIn("the rest: ", out)
        self.assertIn("fresh: no.", out)
        self.assertFalse(os.path.exists(self.h(".agent-mirror-backups")))  # plan is read-only

    def test_instruction_cases(self):
        claude = self.h(".claude", "CLAUDE.md")
        self.assertEqual(self.case("instructions:claude"), "first-setup")  # the store holds only the template
        self.assertEqual(self.case("instructions:codex"), "join")  # an empty file is empty
        write(self.s("AGENTS.md"), "my own instructions\n")
        self.assertEqual(self.case("instructions:claude"), "reconcile")
        write(claude, "my own instructions\n")
        u = self.unit(self.plan(), "instructions:claude")
        self.assertEqual((u["case"], u["default_action"], u["waiting"]), ("identical", "link", False))
        os.remove(claude)
        write(self.s("AGENTS.md"), "  \n")
        self.assertEqual(self.case("instructions:claude"), "nothing")
        write(self.s("AGENTS.md"), "my own instructions\n")
        rc, out = self.apply()
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.case("instructions:claude"), "linked")
        if SYMLINKS:
            os.remove(claude)
            os.symlink(self.s("skills", "beta", "SKILL.md"), claude)
            u = self.unit(self.plan(), "instructions:claude")
            self.assertEqual(u["case"], "linked")
            self.assertIn("drift", u["note"])
            self.assertEqual(u["choices"], ["none", "link"])

    def test_an_older_release_template_counts_as_no_record(self):
        from helpers import TEMPLATE_STORE

        current = read(os.path.join(TEMPLATE_STORE, "AGENTS.md"))
        older = current.replace(" or `agent-mirror apply`", "")  # the 0.2.x wording
        self.assertNotEqual(older, current)
        with open(self.s("AGENTS.md"), "wb") as fh:  # as a Windows checkout may hold it
            fh.write(older.replace("\r\n", "\n").replace("\n", "\r\n").encode("utf-8"))
        u = self.unit(self.plan(), "instructions:claude")
        self.assertEqual((u["case"], u["store"]["state"]), ("first-setup", "template"))
        write(self.s("AGENTS.md"), older + "- one line of my own\n")
        self.assertEqual(self.case("instructions:claude"), "reconcile")

    def test_skill_cases_compared_by_content(self):
        data = self.plan()
        self.assertEqual(self.unit(data, "skill:alpha")["case"], "reconcile")
        self.assertEqual(self.unit(data, "skill:beta")["case"], "join")
        # a skill only this machine has
        write(self.h(".claude", "skills", "mine", "SKILL.md"), "mine\n")
        self.assertEqual(self.case("skill:mine"), "first-setup")
        # identical content, different times, plus caches and editor leftovers that do not count
        write(self.s("skills", "gamma", "SKILL.md"), "gamma body\n")
        write(self.h(".claude", "skills", "gamma", "SKILL.md"), "gamma body\n")
        write(self.h(".claude", "skills", "gamma", "__pycache__", "x.pyc"), "cache")
        write(self.h(".claude", "skills", "gamma", ".SKILL.md.swp"), "swap")
        os.utime(self.h(".claude", "skills", "gamma", "SKILL.md"), (1000000, 1000000))
        self.assertEqual(self.case("skill:gamma"), "identical")
        write(self.h(".claude", "skills", "gamma", "SKILL.md"), "gamma body!\n")
        self.assertEqual(self.case("skill:gamma"), "reconcile")
        # an empty folder and no store record
        os.makedirs(self.h(".claude", "skills", "empty"))
        self.assertEqual(self.case("skill:empty"), "nothing")

    def test_entries_that_are_never_skills(self):
        os.makedirs(os.path.join(self.base, "otherproject", ".git"))  # projlink points into a project repo
        os.makedirs(self.h(".claude", "skills", ".trash", "old"))
        write(self.h(".agents", "skills", "README.md"), "a file, not a skill\n")
        data = self.plan()
        ids = [u["id"] for u in data["units"]]
        self.assertNotIn("skill:synced", ids)
        self.assertNotIn("skill:.trash", ids)
        self.assertNotIn("skill:README.md", ids)
        reasons = dict((os.path.basename(i["path"]), i["reason"]) for i in data["ignored"])
        self.assertEqual(reasons["synced"], "vendor-synced folder")
        self.assertIn("dot folder", reasons[".trash"])
        self.assertEqual(reasons["README.md"], "not a folder")
        u = self.unit(data, "skill:projlink")
        self.assertEqual((u["case"], u["choices"], u["decision"]), ("external", ["none"], "none"))
        self.assertIn("a link into the repo", u["note"])
        rc, out = self.run_tool("install", *self.P())
        self.assertEqual(rc, 0, out)
        self.assertLinked(self.h(".claude", "skills", "projlink"), os.path.join(self.base, "otherproject", "plugin"))
        self.assertTrue(os.path.isdir(self.h(".claude", "skills", "synced", "vendor")))
        self.assertTrue(os.path.isdir(self.h(".claude", "skills", ".trash", "old")))

    def test_memory_cases(self):
        self.assertEqual(self.case("memory:demo"), "reconcile")
        mem = memory_dir(self.home, self.project)
        os.remove(os.path.join(mem, "local_only.md"))
        write(os.path.join(mem, "MEMORY.md"), STORE_INDEX)
        self.assertEqual(self.case("memory:demo"), "identical")
        shutil.rmtree(mem)
        self.assertEqual(self.case("memory:demo"), "join")
        other = os.path.join(self.base, "work", "solo")
        os.makedirs(other)
        u = self.unit(self.plan("--project", "solo", "--project-path", other), "memory:solo")
        self.assertEqual((u["case"], u["default_action"]), ("nothing", "link"))
        write(os.path.join(memory_dir(self.home, other), "note.md"), "solo note\n")
        u = self.unit(self.plan("--project", "solo", "--project-path", other), "memory:solo")
        self.assertEqual((u["case"], u["default_action"]), ("first-setup", "adopt"))
        rc, out = self.run_tool("apply", "--yes-defaults", "--project", "solo", "--project-path", other)
        self.assertEqual(rc, 0, out)
        self.assertEqual(read(self.s("memory", "solo", "note.md")), "solo note\n")
        self.assertLinked(memory_dir(self.home, other), self.s("memory", "solo"))
        self.assertEqual(self.case("memory:solo", "--project", "solo", "--project-path", other), "linked")

    def test_memory_nothing_links_an_empty_folder(self):
        other = os.path.join(self.base, "work", "brandnew")
        os.makedirs(other)
        rc, out = self.run_tool("install", "--agents", "claude", "--project", "brandnew", "--project-path", other)
        self.assertEqual(rc, 0, out)
        self.assertTrue(os.path.isdir(self.s("memory", "brandnew")))
        self.assertLinked(memory_dir(self.home, other), self.s("memory", "brandnew"))

    def test_box_cases(self):
        index = self.s("projects", "demo", "box_index.jsonl")
        self.assertEqual(self.case("box:demo"), "nothing")
        write(self.p("temporary", "a.txt"), "abc")
        u = self.unit(self.plan(), "box:demo")
        self.assertEqual((u["case"], u["default_action"]), ("first-setup", "writer"))
        write(index, json.dumps({"path": "temporary/a.txt", "size": 3, "modtime": "2026-11-01T00:00:00Z"}) + "\n")
        u = self.unit(self.plan(), "box:demo")
        self.assertEqual((u["case"], u["default_action"], u["waiting"]), ("identical", "writer", False))
        writer = {"machine_id": "a" * 32, "machine_name": "big-desk", "date": "2026-11-01T00:00:00", "tool": "x"}
        write(index, read(index) + json.dumps({"path": "_writer.json", "writer": writer}) + "\n")
        write(self.p("temporary", "b.txt"), "more")
        u = self.unit(self.plan(), "box:demo")
        self.assertEqual(u["case"], "reconcile")
        self.assertIn("the current writer is big-desk", u["note"])
        self.assertEqual(u["store"]["writer"]["machine_name"], "big-desk")
        shutil.rmtree(self.p("temporary"))
        u = self.unit(self.plan(), "box:demo")
        self.assertEqual((u["case"], u["default_action"]), ("join", "reader"))
        rc, out = self.apply("--only", "box:demo")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.config()["choices"]["box:demo"], "reader")
        u = self.unit(self.plan(), "box:demo")
        self.assertEqual((u["decision"], u["decided_by"]), ("reader", "recorded"))


class ApplyTest(ReconcileBase):
    def test_reconcile_units_wait_without_a_choice(self):
        rc, out = self.apply()
        self.assertEqual(rc, 0, out)
        self.assertIn("wait for a choice", out)
        self.assertIn("skill:alpha", out)
        self.assertEqual(read(self.h(".claude", "skills", "alpha", "SKILL.md")), "old claude alpha\n")
        self.assertFalse(is_link(memory_dir(self.home, self.project)))
        self.assertEqual(read(self.h(".claude", "CLAUDE.md")), "old claude user file\n")
        self.assertLinked(self.h(".claude", "skills", "beta"), self.s("skills", "beta"))

    def test_choices_file_and_only(self):
        path = self.choices({"skill:alpha": "keep-machine", "instructions:claude": "keep-local"})
        rc, out = self.apply("--choices", path, "--only", "skill:alpha", "instructions:claude", "skill:beta")
        self.assertEqual(rc, 0, out)
        self.assertLinked(self.h(".claude", "skills", "beta"), self.s("skills", "beta"))
        self.assertFalse(is_link(self.h(".codex", "AGENTS.md")))  # not in --only
        self.assertEqual(read(self.h(".claude", "skills", "alpha", "SKILL.md")), "old claude alpha\n")
        cfg = self.config()
        self.assertEqual(cfg["choices"]["skill:alpha"], "keep-machine")
        self.assertEqual(cfg["choices"]["instructions:claude"], "keep-local")
        data = self.plan()
        u = self.unit(data, "skill:alpha")
        self.assertEqual((u["decision"], u["decided_by"], u["waiting"]), ("keep-machine", "recorded", False))
        self.assertNotIn("skill:alpha", data["waiting"])
        rc, out = self.run_tool("plan", *self.P())
        self.assertIn("decided earlier on this machine: ", out)
        self.assertIn("skill:alpha=keep-machine", out)
        rc, out = self.run_tool("doctor", *self.P())
        self.assertIn("DECIDED", out)

    def test_bad_choices_are_refused_before_anything_changes(self):
        for data in ({"skill:nope": "link"}, {"skill:alpha": "link"}, {"memory:demo": "adopt"}):
            with self.subTest(data=data):
                rc, out = self.apply("--choices", self.choices(data))
                self.assertEqual(rc, 2, out)
        rc, out = self.apply("--choices", self.choices({"skill:alpha": "link"}))
        self.assertIn("choose one of take-store, keep-machine, adopt", out)
        write(os.path.join(self.base, "bad.json"), "[1, 2]")
        rc, out = self.apply("--choices", os.path.join(self.base, "bad.json"))
        self.assertNotEqual(rc, 0, out)
        self.assertFalse(os.path.exists(self.h(".agent-mirror-backups")))

    def test_yes_defaults_takes_first_setup_but_not_reconcile(self):
        write(self.h(".claude", "skills", "mine", "SKILL.md"), "mine\n")
        rc, out = self.apply("--yes-defaults")
        self.assertEqual(rc, 0, out)
        self.assertEqual(read(self.s("skills", "mine", "SKILL.md")), "mine\n")
        self.assertLinked(self.h(".claude", "skills", "mine"), self.s("skills", "mine"))
        self.assertLinked(self.h(".agents", "skills", "mine"), self.s("skills", "mine"))
        self.assertEqual(read(self.s("AGENTS.md")), "old claude user file\n")  # first-setup: adopted
        self.assertLinked(self.h(".claude", "CLAUDE.md"), self.s("AGENTS.md"))
        self.assertIn("skill:alpha", out)  # reconcile still waits
        self.assertEqual(read(self.h(".claude", "skills", "alpha", "SKILL.md")), "old claude alpha\n")
        backups = [e for m in self.manifests() for e in json.loads(read(m))["entries"]]
        self.assertTrue(any(e["path"] == self.h(".claude", "skills", "mine") and e["backup"] for e in backups))

    def test_adopt_is_blocked_by_a_secret(self):
        write(self.h(".claude", "skills", "leaky", "SKILL.md"), "---\nname: leaky\n---\n")
        write(self.h(".claude", "skills", "leaky", "notes", "keys.md"), "key " + FAKE_KEY + "\n")
        rc, out = self.apply("--choices", self.choices({"skill:leaky": "adopt"}), "--only", "skill:leaky")
        self.assertEqual(rc, 1, out)
        self.assertIn("refused skill:leaky", out)
        self.assertIn("openai-style-key", out)
        self.assertIn(os.path.join("leaky", "notes", "keys.md"), out)
        self.assertNotIn(FAKE_KEY, out)
        self.assertFalse(os.path.exists(self.s("skills", "leaky")))
        self.assertFalse(is_link(self.h(".claude", "skills", "leaky")))

    def test_memory_merge_with_a_conflict_on_both_sides(self):
        mem = memory_dir(self.home, self.project)
        write(os.path.join(mem, "shared.md"), SHARED_BODY + "edited on this machine\n")
        rc, out = self.apply("--choices", self.choices({"memory:demo": "merge"}), "--only", "memory:demo")
        self.assertEqual(rc, 0, out)
        store = self.s("memory", "demo")
        self.assertEqual(read(os.path.join(store, "shared.md")), SHARED_BODY)  # the store's version is kept
        self.assertEqual(
            read(os.path.join(store, "shared.from-test-machine.md")), SHARED_BODY + "edited on this machine\n"
        )
        index = read(os.path.join(store, "MEMORY.md"))
        self.assertEqual(index, STORE_INDEX + LOCAL_INDEX)  # union-merged, no duplicate lines
        self.assertNotIn("shared.from-", index)
        self.assertEqual(read(os.path.join(store, "local_only.md")), "local only memory\n")
        self.assertIn("conflict", out)
        self.assertLinked(mem, store)

    def test_skill_drift_relinked_only_when_chosen(self):
        if not SYMLINKS:
            self.skipTest("needs symlinks to build a drifted link")
        os.symlink(self.s("skills", "alpha"), self.h(".claude", "skills", "beta"))
        os.makedirs(self.h(".agents", "skills"), exist_ok=True)
        os.symlink(self.s("skills", "alpha"), self.h(".agents", "skills", "beta"))
        u = self.unit(self.plan(), "skill:beta")
        self.assertEqual((u["case"], u["decision"]), ("linked", "none"))
        self.assertIn("points elsewhere in the store", u["note"])
        rc, out = self.apply("--choices", self.choices({"skill:beta": "link"}), "--only", "skill:beta")
        self.assertEqual(rc, 0, out)
        self.assertEqual(
            os.path.normcase(readlink(self.h(".claude", "skills", "beta"))), os.path.normcase(self.s("skills", "beta"))
        )

    def test_legacy_whole_folder_link_is_converted_and_uninstall_restores(self):
        # what an older version left behind: the original folder in a backup, a whole-folder link
        shared = self.h(".agents", "skills")
        old = self.h(".agent-mirror-backups", "20200101-000000-000000")
        os.makedirs(os.path.join(old, ".agents"))
        shutil.move(shared, os.path.join(old, ".agents", "skills"))
        dir_link(self.s("skills"), shared)
        write(
            os.path.join(old, "manifest.json"),
            json.dumps(
                {
                    "version": 1,
                    "store": self.store,
                    "entries": [
                        {
                            "path": shared,
                            "target": self.s("skills"),
                            "before": "real folder",
                            "backup": os.path.join(old, ".agents", "skills"),
                            "method": "symlink",
                        }
                    ],
                    "restored_at": None,
                }
            ),
        )
        u = self.unit(self.plan(), "skills-folder")
        self.assertEqual((u["case"], u["default_action"]), ("legacy-folder-link", "convert"))
        rc, out = self.run_tool("install", *self.P())
        self.assertEqual(rc, 0, out)
        self.assertFalse(is_link(shared))
        self.assertTrue(os.path.isdir(shared))
        self.assertLinked(os.path.join(shared, "alpha"), self.s("skills", "alpha"))
        self.assertLinked(os.path.join(shared, "beta"), self.s("skills", "beta"))
        self.assertTrue(os.path.isfile(self.s("skills", "alpha", "SKILL.md")))
        self.assertNotIn("skills-folder", [u["id"] for u in self.plan()["units"]])
        rc, out = self.run_tool("uninstall")
        self.assertEqual(rc, 0, out)
        self.assertFalse(is_link(shared))
        self.assertEqual(sorted(os.listdir(shared)), ["alpha"])
        self.assertEqual(read(os.path.join(shared, "alpha", "SKILL.md")), "old agents alpha\n")

    def test_uninstall_restores_after_apply_with_adopt(self):
        write(self.h(".claude", "skills", "mine", "SKILL.md"), "mine\n")
        rc, out = self.apply("--yes-defaults")
        self.assertEqual(rc, 0, out)
        rc, out = self.run_tool("uninstall")
        self.assertEqual(rc, 0, out)
        self.assertEqual(read(self.h(".claude", "skills", "mine", "SKILL.md")), "mine\n")
        self.assertFalse(is_link(self.h(".claude", "skills", "mine")))
        self.assertFalse(os.path.lexists(self.h(".agents", "skills", "mine")))
        self.assertEqual(read(self.h(".claude", "CLAUDE.md")), "old claude user file\n")
        self.assertEqual(read(self.s("skills", "mine", "SKILL.md")), "mine\n")  # the store keeps what was adopted

    def test_cloud_runtime_never_adopts(self):
        write(self.h(".claude", "skills", "mine", "SKILL.md"), "mine\n")
        cloud = {"AGENT_RUNTIME": "claude-cloud"}
        rc, out = self.apply("--yes-defaults", env=cloud)
        self.assertEqual(rc, 0, out)
        self.assertIn("left alone: skill:mine", out)
        self.assertIn("does not publish", out)
        rc, out = self.apply("--choices", self.choices({"instructions:claude": "adopt"}), env=cloud)
        self.assertIn("left alone: instructions:claude", out)
        self.assertFalse(os.path.exists(self.s("skills", "mine")))
        self.assertNotEqual(read(self.s("AGENTS.md")), "old claude user file\n")
        self.assertFalse(os.path.exists(self.s("machines")))  # no profile from a cloud runtime
        rc, out = self.run_tool("install", *self.P(), env=cloud)
        self.assertEqual(rc, 0, out)
        self.assertIn("left alone: skill:mine (first-setup)", out)


class MachineProfileTest(ReconcileBase):
    def test_machine_command_and_name_check(self):
        rc, out = self.run_tool("machine")
        self.assertEqual(rc, 0, out)
        self.assertIn("machine name: test-machine", out)
        self.assertRegex(out, r"machine id: +[0-9a-f]{32}")
        rc, out = self.run_tool("machine", "--name", "../evil")
        self.assertEqual(rc, 2, out)
        rc, out = self.run_tool("machine", "--name", "laptop-2")
        self.assertEqual(rc, 0, out)
        self.assertIn("changed from test-machine to laptop-2", out)
        self.assertEqual(self.config()["machine"]["name"], "laptop-2")

    def test_init_store_assigns_a_machine_id(self):
        os.remove(self.h(".config", "agent-mirror", "config.json"))
        rc, out = self.run_tool("init-store", self.store, "--adopt", with_store=False)
        self.assertEqual(rc, 0, out)
        mid = self.config()["machine"]["id"]
        self.assertRegex(mid, r"^[0-9a-f]{32}$")
        rc, out = self.run_tool("init-store", self.store, "--adopt", with_store=False)
        self.assertEqual(self.config()["machine"]["id"], mid)  # stable

    def test_profile_written_after_apply_and_committed_by_sync(self):
        self.init_git()
        self.make_fresh()
        rc, out = self.run_tool("install", *self.P())
        self.assertEqual(rc, 0, out)
        path = self.s("machines", "test-machine.json")
        profile = json.loads(read(path))
        self.assertEqual(profile["machine_name"], "test-machine")
        self.assertEqual(profile["machine_id"], self.config()["machine"]["id"])
        self.assertEqual(profile["skills"]["linked"], ["alpha", "beta"])
        self.assertEqual(profile["projects"]["demo"]["memory"], "linked")
        text = read(path)
        self.assertNotIn(self.home, text)
        self.assertNotIn(self.base, text)
        self.assertNotIn("Test User", text)
        rc, out = self.run_tool("sync")
        self.assertEqual(rc, 0, out)
        self.assertIn("machines/test-machine.json", self.git("ls-files", "machines").splitlines())
        # recorded choices show up in the profile
        rc, out = self.apply("--choices", self.choices({"skill:alpha": "link"}))
        write(self.h(".claude", "skills", "mine", "SKILL.md"), "mine\n")
        rc, out = self.apply("--choices", self.choices({"skill:mine": "keep-local"}))
        self.assertEqual(rc, 0, out)
        self.assertEqual(json.loads(read(path))["skills"]["kept_local"], ["mine"])

    def test_a_cloud_session_cannot_publish_a_profile_to_main(self):
        self.init_git()
        cloud = os.path.join(self.base, "cloud1")
        self.git("clone", "-q", self.origin, cloud, cwd=self.base)
        write(os.path.join(cloud, "machines", "intruder.json"), '{"machine_name": "intruder"}\n')
        # agent-mirror on a cloud runtime leaves machines/ out of its commits altogether
        rc, out = self.run_tool("--store", cloud, "sync", env={"AGENT_RUNTIME": "claude-cloud"}, with_store=False)
        self.assertEqual(rc, 0, out)
        self.assertNotIn("inbox/", self.git("for-each-ref", "--format=%(refname)", cwd=self.origin))
        # an inbox branch that carries one anyway (an older client, or a hand-made push) is held for review
        self.git("add", "machines", cwd=cloud)
        self.git("-c", "user.name=c", "-c", "user.email=c@example.com", "commit", "-q", "-m", "profile", cwd=cloud)
        self.git("push", "-q", "origin", "HEAD:refs/heads/inbox/claude-cloud-test", cwd=cloud)
        rc, out = self.run_tool("sync", env={"CLAUDECODE": "1"})
        self.assertEqual(rc, 0, out)
        self.assertIn("held for review (not applied): machines/intruder.json", out)
        self.assertFalse(os.path.exists(self.s("machines", "intruder.json")))


if __name__ == "__main__":
    unittest.main()
