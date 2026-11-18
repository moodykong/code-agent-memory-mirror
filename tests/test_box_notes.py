"""0.5.3: notes that travel with the box's files.

`box send --note` (required on a cloud runtime) records what the files are in the session manifest;
`box sessions`, `box fetch --session` and the index summary show it. `box note FOLDER TEXT` records a shared
folder's note in its _owners.json entry, owner only; push and take-over keep it; `box owners` and `box ls`
show it. A cloud session start prints the newest sessions with their notes and the folders that have notes,
within a time bound, with the same calls as `box sessions`. Last, records written by 0.5.3 and by 0.5.2
are read by the other version (when this clone has the 0.5.2 commit).

Each story runs on azure-blob and rclone with the fake tools and a folder standing in for the remote.
"""

import io
import json
import os
import subprocess
import tarfile
import time
import unittest

from helpers import setUpModule  # noqa: F401 (the sandbox guard runs first)
from helpers import FAKE_RCLONE, REPO, ToolTest, clean_env, install_fake, read, write

ID_A, ID_B = "a" * 32, "b" * 32
OLD_COMMIT = "0ea28af"  # the 0.5.2 release
SLOW_RCLONE = r"""
import json, os, sys, time
with open(os.environ["FAKE_RCLONE_LOG"], "a") as fh:
    fh.write(json.dumps({"argv": sys.argv[1:]}) + "\n")
time.sleep(60)
"""


def last_record(home):
    with open(os.path.join(home, ".cache", "agent-mirror", "session-start.jsonl"), encoding="utf-8") as fh:
        lines = [ln for ln in fh.read().splitlines() if ln.strip()]
    return json.loads(lines[-1])


def step_status(home, name):
    return dict((s["name"], s["status"]) for s in last_record(home)["steps"]).get(name)


class NotesMixin:
    def setUp(self):
        super().setUp()
        self.configure_box()

    # helpers -------------------------------------------------------------------------------------------

    def as_machine(self, name, mid):
        path = self.h(".config", "agent-mirror", "config.json")
        cfg = json.loads(read(path)) if os.path.isfile(path) else {}
        cfg["machine"] = {"id": mid, "name": name}
        write(path, json.dumps(cfg))

    def run_on(self, path, *args, **env):
        e = dict(self.tool_env())
        e.update(env)
        return self.run_tool(*args, "--project", "demo", "--project-path", path, env=e)

    def box(self, *args, **env):
        env.setdefault("CLAUDECODE", "1")
        return self.run_on(self.project, "box", *args, **env)

    def cloud(self, path, *args, **env):
        env.setdefault("AGENT_RUNTIME", "claude-cloud")
        return self.run_on(path, "box", *args, **env)

    def checkout(self, name):
        path = os.path.join(self.base, name, "demo")
        os.makedirs(path)
        return path

    def session_of(self, path):
        return json.loads(read(os.path.join(path, ".agent-mirror", "session.json")))["id"]

    def manifest(self, sid):
        return json.loads(read(self.sessions_path(sid, "_session.json")))

    def namespaces(self):
        base = self.sessions_path()
        return sorted(os.listdir(base)) if os.path.isdir(base) else []

    def owners(self):
        return json.loads(read(self.shared_path("_owners.json")))["folders"]

    def push(self, *folders):
        """box push; the fakes do not copy include folders, so their files are put where the remote keeps them."""
        rc, out = self.box("push")
        self.assertEqual(rc, 0, out)
        for folder in folders:
            for root, _, files in os.walk(self.p(folder)):
                for f in files:
                    rel = os.path.relpath(os.path.join(root, f), self.project).replace(os.sep, "/")
                    write(self.shared_path(rel), read(os.path.join(root, f)))
        return out

    def index_lines(self):
        return [json.loads(x) for x in read(self.s("projects", "demo", "box_index.jsonl")).splitlines() if x.strip()]

    def start(self, path, *extra, **env):
        e = dict(self.tool_env())
        e.setdefault("AGENT_RUNTIME", "claude-cloud")
        e.update(env)
        return self.run_tool("session-start", "--project", "demo", "--project-path", path, *extra, env=e)

    # session notes ------------------------------------------------------------------------------------------

    def test_a_cloud_send_needs_a_note_and_bad_notes_are_refused(self):
        c = self.checkout("cloud-c")
        write(os.path.join(c, "temporary", "r.txt"), "r\n")
        token = "ghp_" + "Ab1" * 12  # built at runtime: this public repo holds no token-shaped text
        secret = "s3cr3t-" + "value" * 3
        before = self.calls()
        for args, env, words in (
            ((), {}, 'pass --note "what these files are and why"'),
            (("--note", "  \n\t "), {}, 'pass --note "what these files are and why"'),
            (("--note", "x" * 501), {}, "a note is at most 500 characters (this one has 501"),
            (("--note", "the key is " + token), {}, "looks like it holds a secret (github-classic-token)"),
            (
                ("--note", "made with " + secret),
                {"MY_SERVICE_TOKEN": secret},
                "environment variable whose name looks secret",
            ),
        ):
            with self.subTest(args=args[:1]):
                rc, out = self.cloud(c, "send", "temporary/r.txt", *args, **env)
                self.assertEqual(rc, 2, out)
                errors = [ln for ln in out.splitlines() if ln.startswith("error:")]
                self.assertEqual(len(errors), 1, out)
                self.assertIn(words, errors[0])
                self.assertNotIn(token, out)
                self.assertNotIn(secret, out)
        self.assertEqual(self.calls(), before)  # refused before any transfer
        self.assertFalse(os.path.exists(os.path.join(c, ".agent-mirror")))
        self.assertEqual(self.namespaces(), [])
        rc, out = self.cloud(c, "send", "temporary/r.txt", "--dry-run")
        self.assertEqual(rc, 2, out)
        # a developer machine may send without one
        write(self.p("temporary", "d.txt"), "d\n")
        rc, out = self.box("send", "temporary/d.txt")
        self.assertEqual(rc, 0, out)
        self.assertNotIn("note", self.manifest(self.session_of(self.project)))

    def test_notes_in_the_manifest_box_sessions_fetch_and_the_index(self):
        c = self.checkout("cloud-c")
        for name in ("a", "b", "c"):
            write(os.path.join(c, "temporary", "%s.txt" % name), name + "\n")
        first = "Loss curves of run 7,\n\tthe \x1b[31mbaseline\x1b[0m.   Compare with run 8."
        rc, out = self.cloud(c, "send", "temporary/a.txt", "--label", "run7", "--note", first)
        self.assertEqual(rc, 0, out)
        self.assertIn("and your note", out)
        sid = self.session_of(c)
        rc, out = self.cloud(c, "send", "temporary/b.txt", "--note", first)
        self.assertEqual(rc, 0, out)
        later = "Run 8 with the new loss. " + "Detail. " * 50
        rc, out = self.cloud(c, "send", "temporary/c.txt", "--note", later)
        self.assertEqual(rc, 0, out)
        later = later.strip()
        m = self.manifest(sid)
        clean = "Loss curves of run 7, the [31mbaseline [0m. Compare with run 8."
        self.assertEqual(m["schema"], 1)
        self.assertEqual(m["note"], later)
        self.assertEqual(
            [(n["note"], n["paths"]) for n in m["notes"]],
            [(clean, ["temporary/a.txt", "temporary/b.txt"]), (later, ["temporary/c.txt"])],
        )
        self.assertRegex(m["notes"][0]["at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        # a later send without a note on a developer machine keeps the notes (another session here)
        rc, out = self.box("sessions")
        self.assertEqual(rc, 0, out)
        self.assertIn("      note: " + later[:197].rstrip() + "...", out)  # up to 200 characters in the list
        self.assertIn("(2 notes in this session; `agent-mirror box fetch --session %s --dry-run`" % sid, out)
        row = json.loads(self.box("sessions", "--json")[1])["sessions"][0]
        self.assertEqual((row["note"], row["notes"]), (later, 2))
        before = self.calls()
        rc, out = self.box("fetch", "--session", "run7", "--dry-run")
        self.assertEqual(rc, 0, out)
        self.assertIn("note (%s, on temporary/a.txt, temporary/b.txt): %s" % (m["notes"][0]["at"], clean), out)
        self.assertIn("on temporary/c.txt): Run 8 with the new loss.", out)
        self.assertFalse(os.path.exists(self.p("_sessions")))
        self.assertLessEqual(self.calls() - before, 2)  # the notes cost no call of their own
        # the index summary carries the note; box sessions shows it without the transfer program
        rc, out = self.box("index")
        self.assertEqual(rc, 0, out)
        summary = [x for x in self.index_lines() if x.get("path") == "_sessions"][0]
        self.assertEqual(summary["sessions"][0]["note"], later)
        empty = os.path.join(self.base, "emptybin")
        os.makedirs(empty, exist_ok=True)
        rc, out = self.box("sessions", PATH=empty)
        self.assertEqual(rc, 0, out)
        self.assertIn("      note: Run 8 with the new loss.", out)
        # a namespace without notes (0.5.2 manifests, older namespaces) lists and fetches as before
        write(self.sessions_path("codex-cloud-20200101T000000Z", "old.txt"), "old\n")
        rc, out = self.box("fetch", "--session", "codex-cloud-20200101T000000Z", "--dry-run")
        self.assertEqual(rc, 0, out)
        self.assertIn("note: none (this session was sent without one)", out)

    # folder notes ---------------------------------------------------------------------------------------------

    def test_folder_note_owner_only_kept_by_push_and_take_over(self):
        self.as_machine("desk-a", ID_A)
        write(self.p("temporary", "t.txt"), "t\n")
        self.push("temporary")
        rc, out = self.box("note", "temporary", "Figures for the paper; regenerate them with make figures.")
        self.assertEqual(rc, 0, out)
        self.assertIn("the index has no owners line yet", out)
        entry = self.owners()["temporary"]
        self.assertEqual(entry["note"], "Figures for the paper; regenerate them with make figures.")
        self.assertRegex(entry["note_updated"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        rc, out = self.box("index")
        self.assertEqual(rc, 0, out)
        owners_line = [x for x in self.index_lines() if "owners" in x][0]
        self.assertEqual(owners_line["owners"]["temporary"]["note"], entry["note"])
        # a new note goes to the box and into the owners line of the local index at once
        before = self.calls()
        rc, out = self.box("note", "temporary", "Figures for the paper.")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.calls() - before, 4)  # read, read again right before the write, write, read back
        self.assertIn("also updated the owners line of the index", out)
        owners_line = [x for x in self.index_lines() if "owners" in x][0]
        self.assertEqual(owners_line["owners"]["temporary"]["note"], "Figures for the paper.")
        rc, out = self.box("ls")
        self.assertEqual(rc, 0, out)
        self.assertRegex(out, r"temporary/ .*\n +note: Figures for the paper\.")
        rc, out = self.box("owners")
        self.assertRegex(out, r"temporary +desk-a .*\n      note: Figures for the paper\.")
        rows = json.loads(self.box("owners", "--json")[1])["folders"]
        self.assertEqual([r["note"] for r in rows if r["folder"] == "temporary"], ["Figures for the paper."])
        # a push of changed content keeps the note
        write(self.p("temporary", "t.txt"), "changed\n")
        rc, out = self.box("push")
        self.assertEqual(rc, 0, out)
        self.assertNotEqual(self.owners()["temporary"]["fingerprint"], entry["fingerprint"])
        self.assertEqual(self.owners()["temporary"]["note"], "Figures for the paper.")
        # refusals: another machine, a folder nobody owns, a folder that is not one, a cloud runtime, bad input
        other = self.checkout("desk-b")
        write(os.path.join(other, "temporary", "t.txt"), "b\n")
        write(self.p("figures", "f.txt"), "f\n")
        self.as_machine("desk-b", ID_B)
        before = self.calls()
        for path, args, code, words in (
            (other, ("temporary", "mine now"), 2, "temporary is owned by desk-a since "),
            (self.project, ("figures", "x"), 2, "figures has no owner in "),
            (self.project, ("nope", "x"), 2, "nope is not a folder of this box"),
        ):
            with self.subTest(args=args):
                rc, out = self.run_on(path, "box", "note", *args, CLAUDECODE="1")
                self.assertEqual(rc, code, out)
                self.assertIn(words, out)
        self.assertIn(
            "only its owner sets its note", self.run_on(other, "box", "note", "temporary", "x", CLAUDECODE="1")[1]
        )
        after_reads = self.calls()
        for args, env, words in (
            (("temporary", "x"), {"AGENT_RUNTIME": "codex-cloud"}, "refusing box note on codex-cloud"),
            (("temporary", "x", "--clear"), {}, "the note text or --clear, not both"),
            (("temporary",), {}, "give the note text"),
            (("temporary", "token " + "ghp_" + "Cd2" * 12), {}, "looks like it holds a secret"),
        ):
            with self.subTest(args=args[:1], env=env):
                rc, out = self.run_on(other, "box", "note", *args, **dict({"CLAUDECODE": "1"}, **env))
                self.assertEqual(rc, 2, out)
                self.assertIn(words, out)
        self.assertEqual(self.calls(), after_reads)  # refused before anything is read
        self.assertGreater(after_reads, before)
        self.assertEqual(self.owners()["temporary"]["note"], "Figures for the paper.")
        # a take-over keeps the folder's note; the new owner may change it
        rc, out = self.run_on(other, "box", "push", "--take-over", "temporary", CLAUDECODE="1")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.owners()["temporary"]["machine_name"], "desk-b")
        self.assertEqual(self.owners()["temporary"]["note"], "Figures for the paper.")
        rc, out = self.run_on(other, "box", "note", "temporary", "--clear", "--dry-run", CLAUDECODE="1")
        self.assertEqual(rc, 0, out)
        self.assertIn("would clear the note of temporary", out)
        self.assertIn("note", self.owners()["temporary"])
        rc, out = self.run_on(other, "box", "note", "temporary", "--clear", CLAUDECODE="1")
        self.assertEqual(rc, 0, out)
        self.assertNotIn("note", self.owners()["temporary"])
        self.assertNotIn("note_updated", self.owners()["temporary"])
        rc, out = self.run_on(other, "box", "note", "temporary", "--clear", CLAUDECODE="1")
        self.assertIn("nothing to change: temporary has no note", out)

    def test_probe_full_sends_its_test_file_with_a_note(self):
        c = self.checkout("cloud-c")
        env = self.without_real_transfer_tools(dict(self.tool_env(), AGENT_RUNTIME="claude-cloud"))
        rc, out = self.run_tool("probe", "--full", "--project", "demo", "--project-path", c, env=env, timeout=600)
        self.assertEqual(rc, 0, out)
        sid = self.session_of(c)
        self.assertIn("the session just written (%s) is listed: yes" % sid, out)
        m = self.manifest(sid)
        self.assertEqual([f["path"] for f in m["files"]], [".agent_probe_note.txt"])
        self.assertTrue(m["note"].startswith("Test file written by agent-mirror probe --full"), m.get("note"))

    def test_folder_note_on_a_box_with_the_old_single_writer_marker(self):
        self.as_machine("desk-a", ID_A)
        write(self.p("temporary", "t.txt"), "t\n")
        write(self.p("figures", "f.txt"), "f\n")
        self.push("temporary", "figures")
        os.remove(self.shared_path("_owners.json"))
        marker = {
            "machine_id": ID_A,
            "machine_name": "desk-a",
            "date": "2026-11-01T09:00:00",
            "tool": "agent-mirror 0.3.1",
        }
        write(self.shared_path("_writer.json"), json.dumps(marker))
        rc, out = self.box("note", "figures", "Plots of the last sweep.")
        self.assertEqual(rc, 0, out)
        folders = self.owners()
        self.assertEqual(sorted(folders), ["figures", "temporary"])  # every folder of the old marker stays owned
        self.assertEqual(folders["figures"]["note"], "Plots of the last sweep.")
        self.assertNotIn("note", folders["temporary"])
        self.assertEqual(set(e["machine_name"] for e in folders.values()), {"desk-a"})

    # session start ---------------------------------------------------------------------------------------------

    def test_cloud_session_start_shows_the_newest_sessions_and_folder_notes(self):
        self.as_machine("desk-a", ID_A)
        write(self.p("temporary", "t.txt"), "t\n")
        self.push("temporary")
        rc, out = self.box("note", "temporary", "Shared inputs; read-only.")
        self.assertEqual(rc, 0, out)
        rc, out = self.box("index")  # the committed index carries the folder notes to every machine
        self.assertEqual(rc, 0, out)
        a, b = self.checkout("cloud-a"), self.checkout("cloud-b")
        write(os.path.join(a, "temporary", "a.txt"), "a\n")
        write(os.path.join(b, "temporary", "b.txt"), "b\n")
        rc, out = self.cloud(
            a, "send", "temporary/a.txt", "--label", "exp-a", "--note", "Alpha results; compare with beta."
        )
        self.assertEqual(rc, 0, out)
        rc, out = self.cloud(b, "send", "temporary/b.txt", "--note", "Beta results.", AGENT_RUNTIME="codex-cloud")
        self.assertEqual(rc, 0, out)
        sid_a, sid_b = self.session_of(a), self.session_of(b)
        c = self.checkout("cloud-c")
        before = self.calls()
        rc, out = self.start(c)
        self.assertEqual(rc, 0, out)
        block = out[out.index("box of demo:") :].split("\ntiming:")[0]
        print("\n%s: what a cloud session start prints about the box:\n%s" % (self.name, block))
        self.assertIn(
            "box of demo: 2 sessions, from the box "
            "(fetch one with `agent-mirror box fetch --session ID --project demo`):",
            out,
        )
        self.assertRegex(out, r"\n  %s, \d+ min ago, 1 file; note: Beta results\.\n" % sid_b)
        alpha = r"\n  exp-a \(%s\), \d+ min ago, 1 file; note: Alpha results; compare with beta\.\n" % sid_a
        self.assertRegex(out, alpha)
        self.assertLess(out.index(sid_b), out.index(sid_a))  # newest first
        self.assertIn("box of demo: shared folders with notes, from the committed index", out)
        self.assertIn("\n  temporary: Shared inputs; read-only.\n", out)
        self.assertEqual(step_status(self.home, "box notes (demo)"), "ok")
        self.assertLessEqual(self.calls() - before, self.START_CALLS)
        # a developer runtime prints nothing of it and calls nothing
        before = self.calls()
        rc, out = self.start(c, AGENT_RUNTIME=None, CLAUDECODE="1")
        self.assertEqual(rc, 0, out)
        self.assertNotIn("box of demo", out)
        self.assertEqual(self.calls(), before)
        # --box-timeout 0 skips it; a dry run calls nothing
        rc, out = self.start(c, "--box-timeout", "0")
        self.assertNotIn("box of demo", out)
        self.assertEqual(step_status(self.home, "box notes (demo)"), "skipped (--box-timeout 0)")
        before = self.calls()
        rc, out = self.start(c, "--dry-run")
        self.assertIn("would list the newest sessions and folder notes of the box of demo", out)
        self.assertEqual(self.calls(), before)

    def test_cloud_session_start_without_the_transfer_program_reads_the_index(self):
        a = self.checkout("cloud-a")
        write(os.path.join(a, "temporary", "a.txt"), "a\n")
        rc, out = self.cloud(a, "send", "temporary/a.txt", "--note", "Alpha results.")
        self.assertEqual(rc, 0, out)
        rc, out = self.box("index")
        self.assertEqual(rc, 0, out)
        # the fake is not on PATH: the transfer program is missing, or (where one is installed) it is outside the
        # test sandbox, and session start never starts a transfer program from outside it
        rc, out = self.start(a, PATH=os.environ.get("PATH", ""))
        self.assertEqual(rc, 0, out)
        self.assertIn("1 session, from the committed index as of the last `box index`", out)
        self.assertIn("1 file; note: Alpha results.", out)
        self.assertTrue(step_status(self.home, "box notes (demo)").startswith("skipped ("))
        self.assertEqual(last_record(self.home)["failed"], [])


class AzureNotesTest(NotesMixin, ToolTest):
    name = "azure"
    START_CALLS = 3  # account show (a cloud runtime signs in), one listing, one batched manifest read

    def configure_box(self):
        self.root = os.path.join(self.base, "containers")
        self.set_box(min_free_bytes=0)

    def tool_env(self):
        env = self.fake_az()
        env["FAKE_AZ_ROOT"] = self.root
        return env

    def calls(self):
        log = os.path.join(self.base, "az_log.jsonl")
        return len(read(log).splitlines()) if os.path.isfile(log) else 0

    def sessions_path(self, *parts):
        return os.path.join(self.root, "out-box", "demo", *[x for p in parts for x in p.split("/")])

    def shared_path(self, *parts):
        return os.path.join(self.root, "in-box", "demo", *[x for p in parts for x in p.split("/")])

    def test_session_start_says_so_in_one_line_when_it_cannot_list(self):
        a = self.checkout("cloud-a")
        rc, out = self.start(a, FAKE_AZ_ACCOUNT_SHOW_RC="1")  # not signed in, and no AZURE_* to sign in with
        self.assertEqual(rc, 0, out)
        lines = [ln for ln in out.splitlines() if "box of demo" in ln]
        self.assertEqual(
            lines,
            [
                "box of demo: could not list its sessions (az could not sign in); see them later with "
                "`agent-mirror box sessions --project demo`"
            ],
        )
        self.assertNotIn("error:", out)
        self.assertEqual(step_status(self.home, "box notes (demo)"), "failed (az could not sign in)")


class RcloneNotesTest(NotesMixin, ToolTest):
    name = "rclone"
    START_CALLS = 2  # one listing, one batched manifest read

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

    def calls(self):
        return len(read(self.log).splitlines()) if os.path.isfile(self.log) else 0

    def sessions_path(self, *parts):
        return os.path.join(self.root, "fakeremote", "box", "sessions", *[x for p in parts for x in p.split("/")])

    def shared_path(self, *parts):
        return os.path.join(self.root, "fakeremote", "box", "shared", *[x for p in parts for x in p.split("/")])

    def test_session_start_stops_listing_at_the_time_bound(self):
        slow = install_fake(self.base, "rclone", SLOW_RCLONE, folder="slowbin")
        a = self.checkout("cloud-a")
        began = time.time()
        rc, out = self.start(a, "--box-timeout", "3", PATH=slow + os.pathsep + os.environ.get("PATH", ""))
        took = time.time() - began
        self.assertEqual(rc, 0, out)
        self.assertIn("box of demo: could not list its sessions (the listing failed or took longer than 3 s)", out)
        self.assertLess(took, 45, out)  # the fake would sleep 60 s; it is stopped at the bound
        self.assertEqual(
            step_status(self.home, "box notes (demo)"), "failed (the listing failed or took longer than 3 s)"
        )

    @unittest.skipUnless(
        subprocess.run(
            ["git", "-C", REPO, "cat-file", "-e", OLD_COMMIT + "^{commit}"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ).returncode
        == 0,
        "this clone has no 0.5.2 commit %s (a copy without git history)" % OLD_COMMIT,
    )
    def test_records_are_read_by_0_5_2_and_the_reverse(self):
        data = subprocess.run(
            ["git", "-C", REPO, "archive", "--format=tar", OLD_COMMIT, "plugin"],
            stdout=subprocess.PIPE,
            env=clean_env(self.home),
            check=True,
        ).stdout
        old_root = os.path.join(self.base, "old-0.5.2")
        with tarfile.open(fileobj=io.BytesIO(data)) as tf:
            tf.extractall(old_root, **({"filter": "data"} if hasattr(tarfile, "data_filter") else {}))
        old = os.path.join(old_root, "plugin", "bin", "agent-mirror")

        def run_old(path, *args, **env):
            e = dict(self.tool_env())
            e.update(env)
            return self.run_tool(*args, "--project", "demo", "--project-path", path, env=e, tool=old)

        rc, out = run_old(self.project, "--version")
        self.assertIn("0.5.2", out)
        # a 0.5.3 session with notes: 0.5.2 lists and fetches it, and its later send keeps the notes
        a = self.checkout("cloud-a")
        write(os.path.join(a, "temporary", "a.txt"), "a\n")
        write(os.path.join(a, "temporary", "b.txt"), "b\n")
        rc, out = self.cloud(a, "send", "temporary/a.txt", "--label", "new", "--note", "Written by 0.5.3.")
        self.assertEqual(rc, 0, out)
        sid = self.session_of(a)
        rc, out = run_old(self.project, "box", "sessions", CLAUDECODE="1")
        self.assertEqual(rc, 0, out)
        self.assertIn(sid, out)
        rc, out = run_old(self.project, "box", "fetch", "--session", "new", CLAUDECODE="1")
        self.assertEqual(rc, 0, out)
        rc, out = run_old(a, "box", "send", "temporary/b.txt", AGENT_RUNTIME="claude-cloud")
        self.assertEqual(rc, 0, out)
        m = self.manifest(sid)
        self.assertEqual(sorted(f["path"] for f in m["files"]), ["temporary/a.txt", "temporary/b.txt"])
        self.assertEqual((m["note"], [n["paths"] for n in m["notes"]]), ("Written by 0.5.3.", [["temporary/a.txt"]]))
        # a 0.5.2 manifest (no notes) reads as a session without a note
        b = self.checkout("cloud-b")
        write(os.path.join(b, "temporary", "c.txt"), "c\n")
        rc, out = run_old(b, "box", "send", "temporary/c.txt", "--label", "old", AGENT_RUNTIME="claude-cloud")
        self.assertEqual(rc, 0, out)
        rc, out = self.box("fetch", "--session", "old", "--dry-run")
        self.assertEqual(rc, 0, out)
        self.assertIn("note: none", out)
        # a folder note: 0.5.2 reads _owners.json, keeps the note on an unchanged push, writes it into its index
        self.as_machine("desk-a", ID_A)
        write(self.p("temporary", "t.txt"), "t\n")
        self.push("temporary")
        rc, out = self.box("note", "temporary", "Shared inputs.")
        self.assertEqual(rc, 0, out)
        rc, out = run_old(self.project, "box", "owners", CLAUDECODE="1")
        self.assertEqual(rc, 0, out)
        rc, out = run_old(self.project, "box", "push", CLAUDECODE="1")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.owners()["temporary"]["note"], "Shared inputs.")
        rc, out = run_old(self.project, "box", "index", CLAUDECODE="1")
        self.assertEqual(rc, 0, out)
        rc, out = self.box("ls")
        self.assertIn("note: Shared inputs.", out)
        # the one loss, stated in the CHANGELOG: 0.5.2 rebuilds the owning machine's entry when the content
        # changed, without the note
        write(self.p("temporary", "t.txt"), "changed\n")
        rc, out = run_old(self.project, "box", "push", CLAUDECODE="1")
        self.assertEqual(rc, 0, out)
        self.assertNotIn("note", self.owners()["temporary"])


if __name__ == "__main__":
    unittest.main()
