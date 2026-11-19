"""Folder ownership in a box (_owners.json) and nested archive entries, on every backend.

Two machines are played in one sandbox: two checkouts of the project and the machine identity in the
sandboxed config switched between runs. The azure-blob and rclone stories use the fake tools with a
folder standing in for the remote; the rclone story also runs with the real rclone against local
folders when rclone is installed. Another machine writing _owners.json at the same moment is played
by the FAKE_HOOKS rule of the fake tools.
"""

import json
import os
import shutil
import tempfile
import unittest

from helpers import setUpModule  # noqa: F401 (the sandbox guard runs first)
from helpers import FAKE_RCLONE, ToolTest, install_fake, read, remove_tree, write

from agent_mirror.box.archive import entry_problem, sidecar_name
from agent_mirror.box.units import TOP, archive_unit_of, archive_units, folder_units, unit_of_file

STAMP = 1700000000  # every fixture file gets this modification time, so copies compare as the same
ENTRY_KEYS = ["fingerprint", "machine_id", "machine_name", "since", "updated"]
ID_A, ID_B, ID_C = "a" * 32, "b" * 32, "c" * 32
ARCHIVES = ["ws-*", "decompile/proj-*", "decompile/binaries-*"]


def put(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(data)
    os.utime(path, (STAMP, STAMP))


class OwnersMixin:
    """The two-machine story, written once; each backend class says where the remote keeps files."""

    def setUp(self):
        super().setUp()
        self.a = self.project
        self.b = os.path.join(self.base, "work-b", "demo")
        # machine A: ws-a1, two project folders, a snapshot only A has
        put(os.path.join(self.a, "ws-a1", "f1.txt"), b"a one\n")
        put(os.path.join(self.a, "ws-a1", "sub", "f2.txt"), b"a two\n")
        for root in (self.a, self.b):
            put(os.path.join(root, "decompile", "proj-same", "db", "a.bin"), b"same bytes\n")
            put(os.path.join(root, "decompile", "proj-same", "notes.txt"), b"notes\n")
        put(os.path.join(self.a, "decompile", "proj-diff", "db", "a.bin"), b"version from A\n")
        put(os.path.join(self.a, "decompile", "proj-diff", "db", "big.bin"), b"L" * 2000)
        put(os.path.join(self.a, "decompile", "binaries-1.0", "x.bin"), b"snapshot\n")
        # machine B: its own workspace and a different copy of proj-diff
        put(os.path.join(self.b, "ws-b1", "g.txt"), b"b one\n")
        put(os.path.join(self.b, "decompile", "proj-diff", "db", "a.bin"), b"a longer version from B\n")
        self.configure_box()

    # machines and runs ---------------------------------------------------------------------

    def as_machine(self, name, mid):
        path = self.h(".config", "agent-mirror", "config.json")
        cfg = json.loads(read(path)) if os.path.isfile(path) else {}
        cfg["machine"] = {"id": mid, "name": name}
        write(path, json.dumps(cfg))

    def run_on(self, path, *args, **env):
        e = dict(self.tool_env())
        e.update(env)
        return self.run_tool(*args, "--project", "demo", "--project-path", path, env=e)

    def push_a(self, *extra, **env):
        self.as_machine("desk-a", ID_A)
        return self.run_on(self.a, "box", "push", *extra, **env)

    def push_b(self, *extra, **env):
        self.as_machine("desk-b", ID_B)
        return self.run_on(self.b, "box", "push", *extra, **env)

    def owners(self):
        path = self.stored("_owners.json")
        if not os.path.isfile(path):
            return None
        data = json.loads(read(path))
        self.assertEqual(data["schema"], 1)
        return data["folders"]

    def sidecar(self, name):
        return json.loads(read(self.stored("_archives/%s.json" % name)))

    def who(self):
        return dict((f, e["machine_name"]) for f, e in (self.owners() or {}).items())

    # the story -----------------------------------------------------------------------------------

    def test_two_machines_each_own_their_folders(self):
        rc, out = self.push_a("--dry-run")
        self.assertEqual(rc, 0, out)
        self.assertIn("would claim and upload:", out)
        self.assertIn("decompile/proj-diff", out)
        self.assertIsNone(self.owners())
        rc, out = self.push_a()
        self.assertEqual(rc, 0, out)
        # nested archives are named by their relative path in the box
        for name in ("decompile/proj-same", "decompile/proj-diff", "decompile/binaries-1.0", "ws-a1"):
            self.assertTrue(os.path.isfile(self.stored("_archives/%s.tar.gz" % name)), name)
            self.assertEqual(self.sidecar(name)["name"], name)
        self.assertTrue(os.path.isfile(self.stored("_archives/decompile/proj-diff.large/db/big.bin")))
        folders = self.owners()
        self.assertEqual(sorted(folders), ["decompile/binaries-1.0", "decompile/proj-diff", "decompile/proj-same", "ws-a1"])
        for name, entry in folders.items():
            self.assertEqual(sorted(entry), ENTRY_KEYS)
            self.assertEqual((entry["machine_id"], entry["machine_name"]), (ID_A, "desk-a"))
            self.assertEqual(entry["fingerprint"], self.sidecar(name)["fingerprint"])
            self.assertRegex(entry["since"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        before = read(self.stored("_owners.json"))
        rc, out = self.push_a()
        self.assertEqual(rc, 0, out)
        self.assertIn("unchanged since the last push", out)
        self.assertNotIn("recorded in", out)
        self.assertEqual(read(self.stored("_owners.json")), before)  # nothing changed: not rewritten

        # machine B: claims its own workspace, skips A's folders and says how its copies compare
        diff_fp = self.sidecar("decompile/proj-diff")["fingerprint"]
        rc, out = self.push_b()
        self.assertEqual(rc, 0, out)
        self.assertRegex(out, r"skip: +decompile/proj-same .*owned by desk-a since \S+; local copy same")
        self.assertRegex(out, r"skip: +decompile/proj-diff .*owned by desk-a since \S+; local copy different")
        self.assertIn("claim ws-b1", out)
        self.assertEqual(self.sidecar("decompile/proj-diff")["fingerprint"], diff_fp)  # B's copy not uploaded
        self.assertEqual(
            self.who(),
            {
                "decompile/binaries-1.0": "desk-a",
                "decompile/proj-diff": "desk-a",
                "decompile/proj-same": "desk-a",
                "ws-a1": "desk-a",
                "ws-b1": "desk-b",
            },
        )

        # box owners, from B
        rc, out = self.run_on(self.b, "box", "owners", "--json")
        self.assertEqual(rc, 0, out)
        rows = dict((r["folder"], r) for r in json.loads(out)["folders"])
        self.assertEqual(rows["decompile/proj-same"]["local"], "same")
        self.assertEqual(rows["decompile/proj-diff"]["local"], "different")
        self.assertEqual(rows["decompile/binaries-1.0"]["local"], "absent")
        self.assertEqual(rows["ws-b1"]["local"], "same")
        self.assertTrue(rows["ws-b1"]["mine"])
        self.assertFalse(rows["ws-a1"]["here"])
        # 0.5.9: the size here, so that the wrap-up skill can say how large a push may be before it runs
        self.assertEqual((rows["ws-b1"]["files_here"], rows["ws-b1"]["bytes_here"]), (1, len(b"b one\n")))
        self.assertEqual(rows["decompile/binaries-1.0"]["bytes_here"], 0)
        rc, out = self.run_on(self.b, "box", "owners")
        self.assertRegex(out, r"decompile/proj-diff +desk-a since \S+ +here: yes +local vs owner: different")

        # the index records the owners; box ls names them; plan sees the one conflict
        rc, out = self.run_on(self.b, "box", "index")
        self.assertEqual(rc, 0, out)
        lines = [json.loads(x) for x in read(self.s("projects", "demo", "box_index.jsonl")).splitlines()]
        owners_lines = [x for x in lines if x["path"] == "_owners.json"]
        self.assertEqual(len(owners_lines), 1)
        self.assertEqual(owners_lines[0]["owners"]["ws-b1"]["machine_name"], "desk-b")
        archived = sorted(x["path"] for x in lines if x.get("archive"))
        self.assertEqual(archived, ["decompile/binaries-1.0", "decompile/proj-diff", "decompile/proj-same", "ws-a1", "ws-b1"])
        self.assertTrue(os.path.isfile(self.s("projects", "demo", "box_toc", "decompile", "proj-diff.json")))
        rc, out = self.run_on(self.b, "box", "ls")
        self.assertEqual(rc, 0, out)
        self.assertRegex(out, r"decompile/  .*3 archived folder\(s\) inside.*owner: desk-a")
        self.assertRegex(out, r"ws-b1/  archived folder: 1 files  owner: desk-b")
        rc, out = self.run_on(self.b, "box", "ls", "decompile")
        self.assertRegex(out, r"decompile/proj-diff/  archived folder: 2 files, 1 large .*owner: desk-a")
        rc, out = self.run_on(self.b, "plan", "--json")
        unit = [u for u in json.loads(out)["units"] if u["id"] == "box:demo"][0]
        self.assertEqual(unit["case"], "reconcile")
        self.assertEqual(unit["box_folders"]["conflicts"], ["decompile/proj-diff"])
        self.assertEqual(unit["box_folders"]["same"], ["decompile/proj-same"])
        self.assertEqual(unit["box_folders"]["owned_here"], ["ws-b1"])
        self.assertIn("conflicts, owned elsewhere and different here: decompile/proj-diff (owner desk-a)", unit["note"])
        self.assertIsNone(unit["default_action"])

        # --only, then take-over, then release and a claim by the other machine
        rc, out = self.push_b("--only", "ws-b1", "--dry-run")
        self.assertEqual(rc, 0, out)
        self.assertIn("folders of demo here (1):", out)
        rc, out = self.push_b("--only", "nope")
        self.assertEqual(rc, 2, out)
        rc, out = self.push_b("--take-over")
        self.assertEqual(rc, 2, out)
        self.assertIn("name the folders to take over", out)
        rc, out = self.push_b("--take-over", "decompile/proj-diff")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.who()["decompile/proj-diff"], "desk-b")
        self.assertNotEqual(self.sidecar("decompile/proj-diff")["fingerprint"], diff_fp)
        self.assertEqual(self.owners()["decompile/proj-diff"]["fingerprint"], self.sidecar("decompile/proj-diff")["fingerprint"])
        self.as_machine("desk-a", ID_A)
        rc, out = self.run_on(self.a, "box", "release", "decompile/proj-diff")
        self.assertEqual(rc, 2, out)
        self.assertIn("owned by desk-b", out)
        self.as_machine("desk-b", ID_B)
        rc, out = self.run_on(self.b, "box", "release", "decompile/proj-diff", "--dry-run")
        self.assertEqual(rc, 0, out)
        self.assertIn("decompile/proj-diff", self.owners())
        rc, out = self.run_on(self.b, "box", "release", "decompile/proj-diff")
        self.assertEqual(rc, 0, out)
        self.assertNotIn("decompile/proj-diff", self.owners())
        self.assertTrue(os.path.isfile(self.stored("_archives/decompile/proj-diff.tar.gz")))  # no data deleted
        rc, out = self.push_a()
        self.assertEqual(rc, 0, out)
        self.assertIn("claim decompile/proj-diff", out)
        self.assertEqual(self.who()["decompile/proj-diff"], "desk-a")
        self.assertEqual(self.sidecar("decompile/proj-diff")["fingerprint"], diff_fp)

    def test_reader_is_refused_before_any_transfer(self):
        self.as_machine("desk-b", ID_B)
        path = os.path.join(self.base, "role.json")
        for role in ("reader", "none"):
            write(path, json.dumps({"box:demo": role}))
            rc, out = self.run_tool("apply", "--choices", path, "--only", "box:demo", "--project", "demo", "--project-path", self.b)
            self.assertEqual(rc, 0, out)
            self.forget_calls()
            rc, out = self.push_b("--take-over", "ws-b1")
            self.assertEqual(rc, 2, out)
            self.assertIn("recorded role is %s" % role, out)
            self.assertEqual(self.transfer_calls(), [])
            self.assertIsNone(self.owners())

    def pushed_by_a(self):
        """The box after machine A's first push (the same in every test of a class)."""
        rc, out = self.push_a()
        self.assertEqual(rc, 0, out)

    def test_old_single_writer_marker(self):
        self.pushed_by_a()
        # make it a box written by an older version: only the single-writer marker
        os.remove(self.stored("_owners.json"))
        marker = {"machine_id": ID_A, "machine_name": "desk-a", "date": "2026-11-01T09:00:00", "tool": "agent-mirror 0.3.1"}
        write(self.stored("_writer.json"), json.dumps(marker))
        rc, out = self.run_on(self.b, "box", "owners", "--json")
        rows = dict((r["folder"], r) for r in json.loads(out)["folders"])
        self.assertEqual(rows["decompile/proj-diff"]["owner"]["machine_name"], "desk-a")
        self.assertEqual(rows["ws-b1"]["local"], "unowned")
        # B claims what is not in the box and skips the old writer's folders; the first _owners.json
        # carries the old writer's folders too, and _writer.json stays
        rc, out = self.push_b()
        self.assertEqual(rc, 0, out)
        self.assertIn("single-writer marker", out)
        self.assertRegex(out, r"skip: +decompile/proj-same .*owned by desk-a since 2026-11-01T09:00:00; local copy same")
        self.assertEqual(
            self.who(),
            {
                "decompile/binaries-1.0": "desk-a",
                "decompile/proj-diff": "desk-a",
                "decompile/proj-same": "desk-a",
                "ws-a1": "desk-a",
                "ws-b1": "desk-b",
            },
        )
        self.assertEqual(self.owners()["ws-a1"]["fingerprint"], self.sidecar("ws-a1")["fingerprint"])
        self.assertTrue(os.path.isfile(self.stored("_writer.json")))

    def test_old_writer_converts_on_its_next_push_and_bare_take_over(self):
        self.pushed_by_a()
        os.remove(self.stored("_owners.json"))
        marker = {"machine_id": ID_A, "machine_name": "desk-a", "date": "2026-11-01T09:00:00", "tool": "agent-mirror 0.3.1"}
        write(self.stored("_writer.json"), json.dumps(marker))
        rc, out = self.push_a()
        self.assertEqual(rc, 0, out)
        self.assertEqual(set(self.who().values()), {"desk-a"})
        self.assertEqual(len(self.who()), 4)
        # a bare --take-over still works while the box has no _owners.json: B becomes the writer of all
        os.remove(self.stored("_owners.json"))
        rc, out = self.push_b("--take-over")
        self.assertEqual(rc, 0, out)
        self.assertEqual(set(self.who().values()), {"desk-b"})
        self.assertEqual(len(self.who()), 5)
        self.assertEqual(json.loads(read(self.stored("_writer.json")))["machine_name"], "desk-b")


    def test_fetch_a_nested_archive_and_a_sub_path(self):
        self.set_box(toc_inline_bytes=10)  # every table of contents in its own file, nested names included
        rc, out = self.push_a()
        self.assertEqual(rc, 0, out)
        self.assertTrue(os.path.isfile(self.stored("_archives/decompile/proj-diff.toc.json.gz")))
        self.assertIsNone(self.sidecar("decompile/proj-diff")["toc"])
        rc, out = self.run_on(self.a, "box", "index")
        self.assertEqual(rc, 0, out)
        cloud = os.path.join(self.base, "cloud", "demo")
        os.makedirs(cloud)
        rc, out = self.run_on(cloud, "box", "fetch", "decompile/proj-diff", AGENT_RUNTIME="claude-cloud")
        self.assertEqual(rc, 0, out)
        self.assertEqual(read(os.path.join(cloud, "decompile", "proj-diff", "db", "a.bin")), "version from A\n")
        self.assertFalse(os.path.exists(os.path.join(cloud, "decompile", "proj-diff", "db", "big.bin")))
        self.assertIn("agent-mirror box fetch decompile/proj-diff/db/big.bin --project demo", out)
        rc, out = self.run_on(cloud, "box", "fetch", "decompile/proj-same/notes.txt", AGENT_RUNTIME="claude-cloud")
        self.assertEqual(rc, 0, out)
        # copies fetched from the box are not conflicts, even when only part of a folder is here
        self.as_machine("cloud-one", ID_C)
        rc, out = self.run_on(cloud, "plan", "--json", AGENT_RUNTIME="claude-cloud")
        unit = [u for u in json.loads(out)["units"] if u["id"] == "box:demo"][0]
        self.assertEqual(unit["box_folders"]["conflicts"], [])
        self.assertEqual(unit["box_folders"]["fetched"], ["decompile/proj-diff", "decompile/proj-same"])
        self.assertEqual((unit["case"], unit["default_action"]), ("identical", "reader"))
        self.assertEqual(read(os.path.join(cloud, "decompile", "proj-same", "notes.txt")), "notes\n")
        self.assertFalse(os.path.exists(os.path.join(cloud, "decompile", "proj-same", "db")))
        rc, out = self.run_on(cloud, "box", "fetch", "decompile/proj-diff/db/big.bin", AGENT_RUNTIME="claude-cloud")
        self.assertEqual(rc, 0, out)
        self.assertEqual(os.path.getsize(os.path.join(cloud, "decompile", "proj-diff", "db", "big.bin")), 2000)
        rc, out = self.run_on(cloud, "box", "fetch", "decompile", AGENT_RUNTIME="claude-cloud")
        self.assertIn("decompile holds 3 archived folder(s)", out)
        self.assertIn("agent-mirror box fetch decompile/binaries-1.0 --project demo", out)


class ConcurrencyMixin:
    """Another machine writes _owners.json while this one pushes (fake tools only)."""

    def hook(self, rules):
        path = os.path.join(self.base, "hooks.json")
        write(path, json.dumps(rules))
        return {"FAKE_HOOKS": path}

    def other_doc(self, folders):
        path = os.path.join(self.base, "other-%d.json" % len(os.listdir(self.base)))
        write(path, json.dumps({"schema": 1, "folders": folders}))
        return path

    def entry(self, name, mid, fp="f" * 64):
        return {"machine_id": mid, "machine_name": name, "since": "2026-11-02T00:00:00Z", "fingerprint": fp, "updated": "2026-11-02T00:00:00Z"}

    def a_then_b_setup(self):
        rc, out = self.push_a()
        self.assertEqual(rc, 0, out)
        return dict(self.owners())

    def test_a_write_between_read_and_write_is_kept(self):
        mine = self.a_then_b_setup()
        other = dict(mine, **{"ws-c1": self.entry("desk-c", ID_C)})
        env = self.hook([{"after_put": "_archives/ws-b1.json", "copy_from": self.other_doc(other), "copy_to": self.stored("_owners.json")}])
        rc, out = self.push_b(**env)
        self.assertEqual(rc, 0, out)
        who = self.who()
        self.assertEqual((who["ws-c1"], who["ws-b1"], who["ws-a1"]), ("desk-c", "desk-b", "desk-a"))

    def test_a_clobbered_write_is_retried(self):
        mine = self.a_then_b_setup()
        other = dict(mine, **{"ws-c1": self.entry("desk-c", ID_C)})
        env = self.hook([{"after_put": "_owners.json", "copy_from": self.other_doc(other), "copy_to": self.stored("_owners.json"), "times": 1}])
        rc, out = self.push_b(**env)
        self.assertEqual(rc, 0, out)
        self.assertIn("trying again", out)
        who = self.who()
        self.assertEqual((who["ws-c1"], who["ws-b1"]), ("desk-c", "desk-b"))

    def test_a_write_clobbered_every_time_fails_clearly(self):
        mine = self.a_then_b_setup()
        env = self.hook([{"after_put": "_owners.json", "copy_from": self.other_doc(mine), "copy_to": self.stored("_owners.json"), "times": 99}])
        rc, out = self.push_b(**env)
        self.assertEqual(rc, 1, out)
        self.assertIn("after 3 tries", out)
        self.assertNotIn("ws-b1", self.owners())

    def test_a_folder_claimed_meanwhile_is_not_stolen(self):
        mine = self.a_then_b_setup()
        other = dict(mine, **{"ws-b1": self.entry("desk-c", ID_C)})
        env = self.hook([{"after_put": "_archives/ws-b1.json", "copy_from": self.other_doc(other), "copy_to": self.stored("_owners.json")}])
        rc, out = self.push_b(**env)
        self.assertEqual(rc, 1, out)
        self.assertIn("now belongs to desk-c", out)
        self.assertEqual(self.who()["ws-b1"], "desk-c")


class AzureOwnersTest(ConcurrencyMixin, OwnersMixin, ToolTest):
    def configure_box(self):
        self.root = os.path.join(self.base, "containers")
        os.makedirs(self.root)
        self.set_box(archives=ARCHIVES, archive_large_file_bytes=1000, min_free_bytes=0)

    def tool_env(self):
        env = self.fake_az()
        env.update({"FAKE_AZ_ROOT": self.root, "CLAUDECODE": "1"})
        return env

    def stored(self, rel):
        return os.path.join(self.root, "in-box", "demo", *rel.split("/"))

    def forget_calls(self):
        if os.path.exists(os.path.join(self.base, "az_log.jsonl")):
            os.remove(os.path.join(self.base, "az_log.jsonl"))

    def transfer_calls(self):
        self.az_log = os.path.join(self.base, "az_log.jsonl")
        return [c for c in self.az_calls() if c["argv"][:3] in (["storage", "blob", "sync"], ["storage", "blob", "upload"])]

    def test_release_on_an_old_marker_box_and_owners_from_the_index(self):
        rc, out = self.push_a()
        self.assertEqual(rc, 0, out)
        rc, out = self.push_b("--take-over", "decompile/binaries-1.0")
        self.assertEqual(rc, 2, out)
        self.assertIn("not one of the box folders on this machine", out)
        os.remove(self.stored("_owners.json"))
        marker = {"machine_id": ID_A, "machine_name": "desk-a", "date": "2026-11-01T09:00:00", "tool": "agent-mirror 0.3.1"}
        write(self.stored("_writer.json"), json.dumps(marker))
        self.as_machine("desk-a", ID_A)
        rc, out = self.run_on(self.a, "box", "release", "ws-a1")
        self.assertEqual(rc, 0, out)
        self.assertEqual(sorted(self.who()), ["decompile/binaries-1.0", "decompile/proj-diff", "decompile/proj-same"])
        self.assertTrue(os.path.isfile(self.stored("_writer.json")))
        rc, out = self.run_on(self.a, "box", "index")
        self.assertEqual(rc, 0, out)
        empty = os.path.join(self.base, "emptybin")
        os.makedirs(empty)
        rc, out = self.run_on(self.a, "box", "owners", PATH=empty)
        self.assertEqual(rc, 0, out)
        self.assertIn("using the committed index instead", out)
        self.assertRegex(out, r"decompile/proj-diff +desk-a since 2026-11-01T09:00:00 \(this machine\) +here: yes +local vs owner: same")
        self.assertRegex(out, r"ws-a1 +- +here: yes +local vs owner: unowned")

    def test_doctor_warns_about_overlapping_entries(self):
        self.set_box(archives=["decompile/proj-*", "decompile/*"])
        rc, out = self.run_on(self.a, "doctor")
        self.assertEqual(rc, 0, out)
        self.assertIn("warning  decompile/proj-diff matches 2 box.archives entries", out)
        self.assertIn("the first ('decompile/proj-*') wins", out)
        self.set_box(archives=["decompile", "decompile/proj-*"])
        rc, out = self.run_on(self.a, "doctor")
        self.assertIn("warning  decompile/proj-diff matches the box.archives entry 'decompile/proj-*' but is inside the archived folder decompile", out)

    def test_ls_prefers_the_configured_archive_over_an_old_one(self):
        idx = self.s("projects", "demo", "box_index.jsonl")
        old = {"path": "decompile", "archive": True, "files": 1, "bytes": 1, "toc": "box_toc/decompile.json"}
        new = {"path": "decompile/proj-a", "archive": True, "files": 1, "bytes": 1, "toc": "box_toc/decompile/proj-a.json"}
        write(idx, json.dumps(old) + "\n" + json.dumps(new) + "\n")
        write(self.s("projects", "demo", "box_toc", "decompile.json"), json.dumps([{"path": "proj-a/old.txt", "size": 1, "in": "bundle"}]))
        write(self.s("projects", "demo", "box_toc", "decompile", "proj-a.json"), json.dumps([{"path": "new.txt", "size": 1, "in": "bundle"}]))
        rc, out = self.run_on(self.a, "box", "ls", "decompile/proj-a")
        self.assertEqual(rc, 0, out)
        self.assertIn("decompile/proj-a/new.txt", out)
        self.assertNotIn("old.txt", out)

    def test_include_folders_are_owned_like_archives(self):
        write(os.path.join(self.a, "temporary", "t.txt"), "a\n")
        write(os.path.join(self.b, "temporary", "t.txt"), "b, longer\n")
        write(os.path.join(self.b, "figures", "f.png"), "png\n")
        rc, out = self.push_a("--only", "temporary")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.who(), {"temporary": "desk-a"})
        self.forget_calls()
        rc, out = self.push_b("--delete")
        self.assertEqual(rc, 0, out)
        self.assertRegex(out, r"skip: +temporary .*owned by desk-a since \S+; local copy different")
        syncs = [c["argv"] for c in self.transfer_calls() if c["argv"][:3] == ["storage", "blob", "sync"]]
        self.assertEqual([a[a.index("--destination") + 1] for a in syncs], ["demo/figures/"])  # never temporary


class RcloneFakeOwnersTest(ConcurrencyMixin, OwnersMixin, ToolTest):
    INBOUND = "fakeremote:box/in"

    def configure_box(self):
        self.root = os.path.join(self.base, "remotes")
        self.log = os.path.join(self.base, "rclone_log.jsonl")
        env = json.loads(read(self.s("projects", "demo", "env.json")))
        env["box"] = {
            "backend": "rclone",
            "inbound_remote": self.INBOUND,
            "outbound_remote": "fakeremote:box/out",
            "include": ["temporary/**", "figures/**"],
            "exclude": ["**/__pycache__/**"],
            "archives": ARCHIVES,
            "archive_large_file_bytes": 1000,
            "min_free_bytes": 0,
        }
        write(self.s("projects", "demo", "env.json"), json.dumps(env))

    def tool_env(self):
        fakebin = install_fake(self.base, "rclone", FAKE_RCLONE)
        return {
            "PATH": fakebin + os.pathsep + os.environ.get("PATH", ""),
            "FAKE_RCLONE_LOG": self.log,
            "FAKE_RCLONE_INBOUND": self.INBOUND,
            "FAKE_RCLONE_ROOT": self.root,
            "CLAUDECODE": "1",
        }

    def stored(self, rel):
        return os.path.join(self.root, "fakeremote", "box", "in", *rel.split("/"))

    def forget_calls(self):
        if os.path.exists(self.log):
            os.remove(self.log)

    def transfer_calls(self):
        if not os.path.isfile(self.log):
            return []
        calls = [json.loads(x)["argv"] for x in read(self.log).splitlines()]
        return [c for c in calls if c[0] in ("copy", "sync", "copyto") and not c[1].startswith(self.INBOUND)]

    def test_filters_carry_only_folders_this_machine_pushes(self):
        write(os.path.join(self.b, "temporary", "t.txt"), "b\n")
        write(os.path.join(self.b, "figures", "f.png"), "png\n")
        other = {"figures": {"machine_id": ID_A, "machine_name": "desk-a", "since": "2026-11-02T00:00:00Z", "fingerprint": None, "updated": "2026-11-02T00:00:00Z"}}
        write(self.stored("_owners.json"), json.dumps({"schema": 1, "folders": other}))
        rc, out = self.push_b("--dry-run", "--delete", "--only", "temporary", "--only", "figures")
        self.assertEqual(rc, 0, out)
        self.assertIn("--filter '+ /temporary/**' --filter '- **'", out)
        self.assertNotIn("+ /figures/**", out)
        self.assertIn("would run: rclone sync ", out)
        self.assertIn("--filter '- /decompile/proj-*/**'", out)  # nested archives never travel file by file
        self.assertRegex(out, r"skip: +figures .*owned by desk-a since \S+; local copy not compared")

    def test_patterns_without_a_folder_are_the_top_unit(self):
        self.set_box(include=["results/**", "*.pdf"], archives=[])
        write(os.path.join(self.b, "a.pdf"), "pdf\n")
        write(os.path.join(self.b, "results", "r.csv"), "1\n")
        rc, out = self.push_b("--dry-run")
        self.assertEqual(rc, 0, out)
        self.assertRegex(out, r"claim and upload: +\. \(1 files\)")
        self.assertIn("--filter '+ /results/**' --filter '- /results/**' --filter '+ *.pdf' --filter '- **'", out)
        other = {"results": {"machine_id": ID_A, "machine_name": "desk-a", "since": "s", "fingerprint": None, "updated": "u"}}
        write(self.stored("_owners.json"), json.dumps({"schema": 1, "folders": other}))
        rc, out = self.push_b("--dry-run")
        self.assertIn("--filter '- /results/**' --filter '+ *.pdf' --filter '- **'", out)
        self.assertNotIn("+ /results/**", out)


@unittest.skipUnless(shutil.which("rclone"), "rclone is not installed")
class RcloneRealOwnersTest(OwnersMixin, ToolTest):
    """Real rclone, local folders as remotes. No concurrency cases: they need the fake's write hook.

    Every real rclone start costs most of a second, so the box after A's first push (the same in every
    test; test_two_machines_each_own_their_folders checks that push itself) is made once per class and
    copied into the tests that start from it.
    """

    snapshot = None

    @classmethod
    def tearDownClass(cls):
        if cls.snapshot:
            remove_tree(os.path.dirname(cls.snapshot))
        super().tearDownClass()

    def pushed_by_a(self):
        cls = type(self)
        if cls.snapshot:
            remove_tree(self.inbound)
            shutil.copytree(cls.snapshot, self.inbound)
            self.as_machine("desk-a", ID_A)
            return
        super().pushed_by_a()
        holder = tempfile.mkdtemp(prefix="agent-mirror-snapshot-")
        shutil.copytree(self.inbound, os.path.join(holder, "inbound"))
        cls.snapshot = os.path.join(holder, "inbound")

    def configure_box(self):
        self.inbound = os.path.join(self.base, "remote", "in")
        os.makedirs(self.inbound)
        env = json.loads(read(self.s("projects", "demo", "env.json")))
        env["box"] = {
            "backend": "rclone",
            "inbound_remote": self.inbound,
            "outbound_remote": os.path.join(self.base, "remote", "out"),
            "include": ["temporary/**"],
            "archives": ARCHIVES,
            "archive_large_file_bytes": 1000,
            "min_free_bytes": 0,
        }
        write(self.s("projects", "demo", "env.json"), json.dumps(env))

    def tool_env(self):
        return {"RCLONE_CONFIG": os.path.join(self.base, "rclone.conf"), "CLAUDECODE": "1"}

    def stored(self, rel):
        return os.path.join(self.inbound, *rel.split("/"))

    def forget_calls(self):
        self.before = sorted(os.path.join(r, f) for r, _, fs in os.walk(self.inbound) for f in fs)

    def transfer_calls(self):
        now = sorted(os.path.join(r, f) for r, _, fs in os.walk(self.inbound) for f in fs)
        return [] if now == self.before else [p for p in now if p not in self.before]

    def test_include_folder_with_real_rclone(self):
        write(os.path.join(self.a, "temporary", "t.txt"), "a\n")
        rc, out = self.push_a("--only", "temporary")
        self.assertEqual(rc, 0, out)
        self.assertEqual(read(self.stored("temporary/t.txt")), "a\n")
        self.assertEqual(self.who(), {"temporary": "desk-a"})
        write(os.path.join(self.b, "temporary", "t.txt"), "b, longer\n")
        rc, out = self.push_b("--only", "temporary")
        self.assertEqual(rc, 0, out)
        self.assertIn("local copy different", out)
        self.assertEqual(read(self.stored("temporary/t.txt")), "a\n")


class UnitRulesTest(unittest.TestCase):
    def test_archive_entries_and_overlaps(self):
        base = os.path.realpath(tempfile.mkdtemp(prefix="agent-mirror-units-"))
        self.addCleanup(remove_tree, base)
        for rel in ("decompile/proj-a/x", "decompile/proj-b/x", "decompile/binaries-1/x", "ws-1/x", "_archives/x", "other/x"):
            write(os.path.join(base, *rel.split("/")), "x")
        units, warnings = archive_units(base, ["decompile/proj-*", "decompile/*", "ws-*"])
        self.assertEqual(units, ["decompile/binaries-1", "decompile/proj-a", "decompile/proj-b", "ws-1"])
        self.assertEqual(len(warnings), 2)
        self.assertIn("the first ('decompile/proj-*') wins", warnings[0])
        units, warnings = archive_units(base, ["decompile", "decompile/proj-*"])
        self.assertEqual(units, ["decompile"])
        self.assertIn("inside the archived folder decompile", warnings[0])
        self.assertEqual(archive_unit_of("decompile/proj-a/sub/f.txt", ["decompile/proj-*"]), "decompile/proj-a")
        self.assertEqual(archive_unit_of("decompile/notes.txt", ["decompile/proj-*"]), None)
        self.assertEqual(archive_unit_of("_archives/ws-1.json", ["*"]), None)

    def test_folder_units_and_files(self):
        units = folder_units(["docs/**", "docs/slides/**", "decompile/proj-a/**", "*.pdf"], ["decompile/proj-*"])
        self.assertEqual([(u["name"], u["roots"]) for u in units], [(TOP, [TOP]), ("docs", ["docs", "docs/slides"])])
        self.assertEqual(unit_of_file("docs/slides/a.pptx", units, ["decompile/proj-*"], []), "docs")
        self.assertEqual(unit_of_file("x/report.pdf", units, ["decompile/proj-*"], []), TOP)
        self.assertEqual(unit_of_file("docs/report.pdf", units, ["decompile/proj-*"], []), "docs")
        self.assertIsNone(unit_of_file("decompile/proj-a/f.pdf", units, ["decompile/proj-*"], []))

    def test_sidecar_names_and_entry_checks(self):
        self.assertEqual(sidecar_name("_archives/decompile/proj-a.json"), "decompile/proj-a")
        self.assertEqual(sidecar_name("_archives/ws-1.json"), "ws-1")
        self.assertIsNone(sidecar_name("_archives/ws-1.large/data/x.json"))
        self.assertIsNone(sidecar_name("_archives/ws-1.toc.json.gz"))
        for good in ("ws-*", "decompile/proj-*", "decompile/binaries-*"):
            self.assertIsNone(entry_problem(good), good)
        for bad in ("../x", "/abs", "a/**", "a\\b", "_archives/x", "x.large/y", ""):
            self.assertIsNotNone(entry_problem(bad), bad)


if __name__ == "__main__":
    unittest.main()
