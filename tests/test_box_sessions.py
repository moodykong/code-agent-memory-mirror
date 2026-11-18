"""Session namespaces on the sessions side of the box: send, the manifest, box sessions, fetch --session,
promote, prune, receive, the summary in the index, and the old and new config key names.

Several checkouts of the project stand in for separate machines and sessions; the machine identity in the
sandboxed config is switched between runs. The azure-blob and rclone stories use the fake tools with a
folder standing in for the remote; one story runs with the real rclone against local folders when rclone
is installed.
"""

import datetime
import json
import os
import shutil
import tarfile
import unittest

from helpers import setUpModule  # noqa: F401 (the sandbox guard runs first)
from helpers import FAKE_RCLONE, ToolTest, install_fake, read, write

ID_RX = r"(claude|codex)-(local|cloud)-\d{8}T\d{6}Z-[0-9a-f]{6}"
NOTE = ("--note", "Results of one test run; fetch them to compare with the next run.")  # a cloud send needs one


def read_bytes(path):
    with open(path, "rb") as fh:
        return fh.read()
ID_A, ID_B = "a" * 32, "b" * 32


class SessionsMixin:
    """The same stories on each backend; each backend class says where the remote keeps files."""

    def setUp(self):
        super().setUp()
        self.git("init", "-q", "-b", "work", cwd=self.project)
        write(self.p("README.md"), "tracked\n")
        self.git("add", "README.md", cwd=self.project)
        self.git("commit", "-q", "-m", "first", cwd=self.project)
        self.commit = self.git("rev-parse", "HEAD", cwd=self.project)
        self.configure_box()
        self.set_box(min_free_bytes=0)

    # helpers ---------------------------------------------------------------------------------

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

    def local_session(self, path=None):
        return json.loads(read(os.path.join(path or self.project, ".agent-mirror", "session.json")))

    def manifest(self, sid):
        return json.loads(read(self.sessions_path(sid, "_session.json")))

    def namespaces(self):
        base = self.sessions_path()
        return sorted(os.listdir(base)) if os.path.isdir(base) else []

    def checkout(self, name):
        path = os.path.join(self.base, name, "demo")
        os.makedirs(path)
        return path

    # stories ----------------------------------------------------------------------------------

    def test_one_session_per_run_and_its_manifest(self):
        self.as_machine("desk-a", ID_A)
        write(self.p("results", "r1.csv"), "1,2\n")
        write(self.p("results", "run", "a.txt"), "a\n")
        write(self.p("results", "run", "sub", "b.txt"), "bb\n")
        write(self.p("results", "run", "creds.env"), "TOKEN=x\n")
        write(self.p("results", "arch", "x.txt"), "x\n")
        write(self.p("results", "arch", "y", "z.txt"), "zz\n")
        secret = "s3cr3t-" + "value" * 3
        rc, out = self.box("send", "results/r1.csv", "--label", "exp-1", MY_SERVICE_TOKEN=secret)
        self.assertEqual(rc, 0, out)
        sid = self.local_session()["id"]
        self.assertRegex(sid, "^claude-local-" + ID_RX.split("-", 2)[2] + "$")
        self.assertIn("(new)", out)
        rc, out = self.box("send", "results/run", "--label", "other")
        self.assertEqual(rc, 0, out)
        self.assertIn("already has the label exp-1", out)
        rc, out = self.box("send", "results/arch", "--archive")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.local_session()["id"], sid)  # one namespace for the whole session
        self.assertEqual(self.namespaces(), [sid])
        m = self.manifest(sid)
        self.assertEqual((m["schema"], m["id"], m["label"], m["runtime"]), (1, sid, "exp-1", "claude-local"))
        self.assertEqual(m["machine"], {"id": ID_A, "name": "desk-a"})
        self.assertEqual((m["project"], m["repo"]), ("demo", "Example/demo"))
        self.assertEqual(m["git"], {"branch": "work", "commit": self.commit})
        for key in ("started", "updated"):
            self.assertRegex(m[key], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertEqual(m["tool"].split()[0], "agent-mirror")
        files = dict((f["path"], f) for f in m["files"])
        self.assertEqual(sorted(files), ["results/arch", "results/r1.csv", "results/run/a.txt", "results/run/sub/b.txt"])
        self.assertEqual(files["results/r1.csv"]["bytes"], os.path.getsize(self.p("results", "r1.csv")))
        self.assertRegex(files["results/r1.csv"]["mtime"], r"Z$")
        arch = files["results/arch"]
        self.assertEqual((arch["archive"], arch["files"], arch["object"]), (True, 2, "results/arch.tar.gz"))
        self.assertTrue(os.path.isfile(self.sessions_path(sid, "results", "arch.tar.gz")))
        self.assertEqual(m["promoted"], [])
        text = read(self.sessions_path(sid, "_session.json"))
        for private in (secret, self.home, self.base, self.project, "TOKEN"):
            self.assertNotIn(private, text)
        # a second send of the same path replaces it within the session
        write(self.p("results", "r1.csv"), "1,2,3\n")
        rc, out = self.box("send", "results/r1.csv")
        self.assertEqual(rc, 0, out)
        self.assertEqual(read(self.sessions_path(sid, "results", "r1.csv")), "1,2,3\n")
        self.assertEqual(self.manifest(sid)["files"][1]["bytes"], os.path.getsize(self.p("results", "r1.csv")))
        # a new session: on request, or a day later on a developer runtime
        rc, out = self.box("send", "results/r1.csv", "--new-session")
        sid2 = self.local_session()["id"]
        self.assertNotEqual(sid2, sid)
        rec = self.local_session()
        rec["started"] = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=25)).strftime("%Y-%m-%dT%H:%M:%SZ")
        write(os.path.join(self.project, ".agent-mirror", "session.json"), json.dumps(rec))
        rc, out = self.box("send", "results/r1.csv")
        sid3 = self.local_session()["id"]
        self.assertNotIn(sid3, (sid, sid2))
        self.assertIn("/.agent-mirror/", read(self.p(".git", "info", "exclude")))
        # a cloud session on a new machine is a new session; on the same machine it is kept
        rc, out = self.box("send", "results/r1.csv", *NOTE, AGENT_RUNTIME="claude-cloud")
        cloud = self.local_session()["id"]
        self.assertTrue(cloud.startswith("claude-cloud-"))
        rc, out = self.box("send", "results/r1.csv", *NOTE, AGENT_RUNTIME="claude-cloud")
        self.assertEqual(self.local_session()["id"], cloud)
        rec = self.local_session()
        rec["host"] = "another-machine"
        write(os.path.join(self.project, ".agent-mirror", "session.json"), json.dumps(rec))
        rc, out = self.box("send", "results/r1.csv", *NOTE, AGENT_RUNTIME="claude-cloud")
        self.assertNotEqual(self.local_session()["id"], cloud)
        rc, out = self.box("send", "results/r1.csv", "--label", "bad label!")
        self.assertEqual(rc, 2, out)
        rc, out = self.box("send", "_session.json")
        self.assertEqual(rc, 2, out)

    def test_send_from_the_folder_above_the_clone_and_files_outside_it(self):
        """A cloud session starts in the folder above the clones (0.5.1 made a second namespace there)."""
        above = os.path.dirname(self.project)
        env = dict(self.tool_env(), AGENT_RUNTIME="claude-cloud")

        def send(*args, cwd=above):
            return self.run_tool("box", "send", *(args + NOTE), cwd=cwd, env=env)

        write(self.p("results", "r1.csv"), "1\n")
        # what probe --full does: the clone it discovered, by --project and --project-path
        rc, out = self.run_on(self.project, "box", "send", "results/r1.csv", *NOTE, AGENT_RUNTIME="claude-cloud")
        self.assertEqual(rc, 0, out)
        sid = self.local_session()["id"]
        # --project without --project-path, from the folder above: the clone of demo, the same namespace
        write(os.path.join(above, "probe_output.txt"), "probe\n")
        rc, out = send("probe_output.txt", "--project", "demo", "--label", "first-probe")
        self.assertEqual(rc, 0, out)
        self.assertIn("is outside the project folder: it is sent under its file name, as probe_output.txt", out)
        self.assertEqual(self.namespaces(), [sid])
        self.assertFalse(os.path.exists(os.path.join(above, ".agent-mirror")))
        self.assertEqual(read(self.sessions_path(sid, "probe_output.txt")), "probe\n")
        m = self.manifest(sid)
        self.assertEqual(m["git"], {"branch": "work", "commit": self.commit})  # the branch is recorded
        self.assertEqual(m["label"], "first-probe")
        # no --project: the one clone the scan finds (as session-start and probe find it)
        write(os.path.join(above, "notes.txt"), "notes\n")
        rc, out = send("notes.txt")
        self.assertEqual(rc, 0, out)
        # from inside the clone, a file one folder up: sent under its file name
        write(os.path.join(above, "probe2.txt"), "probe2\n")
        rc, out = send("../probe2.txt", "--project", "demo", "--label", "second-probe", cwd=self.project)
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.namespaces(), [sid])
        self.assertEqual(read(self.sessions_path(sid, "probe2.txt")), "probe2\n")
        # a path from the current folder that lies inside the clone keeps its path there
        rc, out = send(os.path.join("demo", "results", "r1.csv"))
        self.assertEqual(rc, 0, out)
        self.assertNotIn("outside the project folder", out)
        paths = [f["path"] for f in self.manifest(sid)["files"]]
        self.assertEqual(sorted(paths), ["notes.txt", "probe2.txt", "probe_output.txt", "results/r1.csv"])
        # refusals: exit non-zero with one line that says what to do, and nothing uploaded
        os.makedirs(os.path.join(above, "outdir"))
        write(os.path.join(above, "outdir", "x.txt"), "x\n")
        write(os.path.join(above, "secrets.env"), "TOKEN=x\n")
        write(os.path.join(above, "README.md"), "another readme\n")  # the clone has one too
        for args, code, words in (
            (("../outdir",), 2, "copy it into the project folder"),
            (("../secrets.env",), 2, "env files hold secrets"),
            (("../missing.txt",), 1, "pass the path of an existing file"),
            (("--archive", "../probe2.txt"), 2, "send it without --archive"),
        ):
            with self.subTest(args=args):
                rc, out = send(*args, "--project", "demo", cwd=self.project)
                self.assertEqual(rc, code, out)
                errors = [ln for ln in out.splitlines() if ln.startswith("error:")]
                self.assertEqual(len(errors), 1, out)
                self.assertIn(words, errors[0])
        rc, out = send("README.md", "--project", "demo")
        self.assertEqual(rc, 2, out)
        self.assertIn("names two different things", out)
        self.assertEqual(self.namespaces(), [sid])
        self.assertEqual(
            sorted(f["path"] for f in self.manifest(sid)["files"]),
            ["notes.txt", "probe2.txt", "probe_output.txt", "results/r1.csv"],
        )

    def test_sessions_list_fetch_and_receive(self):
        a, b = self.checkout("cloud-a"), self.checkout("cloud-b")
        write(os.path.join(a, "results", "exp-a.csv"), "a\n")
        write(os.path.join(b, "results", "exp-b", "data.txt"), "b data\n")
        write(os.path.join(b, "results", "exp-b", "more", "m.txt"), "more\n")
        rc, out = self.run_on(
            a, "box", "send", "results/exp-a.csv", "--label", "exp-a", *NOTE, AGENT_RUNTIME="claude-cloud"
        )
        self.assertEqual(rc, 0, out)
        rc, out = self.run_on(
            b, "box", "send", "results/exp-b", "--archive", "--label", "exp-b", *NOTE, AGENT_RUNTIME="codex-cloud"
        )
        self.assertEqual(rc, 0, out)
        sid_a, sid_b = self.local_session(a)["id"], self.local_session(b)["id"]
        # an older namespace (before 0.5.0) has no manifest, and a folder that is not a session is ignored
        write(self.sessions_path("codex-cloud-20200101T000000Z", "old.txt"), "old\n")
        write(self.sessions_path("not-a-session", "x.txt"), "x\n")
        rc, out = self.box("sessions")
        self.assertEqual(rc, 0, out)
        self.assertRegex(out, sid_a + r" +exp-a +claude-cloud +- +1 files")  # not sent from a git clone
        self.assertRegex(out, sid_b + r" +exp-b +codex-cloud")
        self.assertRegex(out, r"codex-cloud-20200101T000000Z +- +codex-cloud +no manifest +1 files")
        self.assertIn("1 other folder(s)", out)
        rows = json.loads(self.box("sessions", "--json")[1])["sessions"]
        self.assertEqual(rows[-1]["id"], "codex-cloud-20200101T000000Z")  # newest first
        self.assertEqual(rows[-1]["manifest"], False)
        rc, out = self.box("sessions", "--limit", "1")
        self.assertIn("(2 more; show them with --limit 3)", out)
        # fetch by id, by a unique start of the id, by label; into _sessions/<id>; never overwriting here
        rc, out = self.box("fetch", "--session", sid_a)
        self.assertEqual(rc, 0, out)
        got = self.p("_sessions", sid_a, "results", "exp-a.csv")
        self.assertEqual(read(got), "a\n")
        self.assertFalse(os.path.exists(self.p("results", "exp-a.csv")))  # never over the project tree
        self.assertIn("/_sessions/", read(self.p(".git", "info", "exclude")))
        write(got, "edited here\n")
        rc, out = self.box("fetch", "--session", sid_a[:-3])
        self.assertEqual(rc, 0, out)
        self.assertEqual(read(got), "edited here\n")
        self.assertIn("kept 1 existing local file(s)", out)
        rc, out = self.box("fetch", "--session", "exp-b")
        self.assertEqual(rc, 0, out)
        self.assertEqual(read(self.p("_sessions", sid_b, "results", "exp-b", "more", "m.txt")), "more\n")
        rc, out = self.box("fetch", "--session", "exp-b", "results/exp-b/data.txt", "--into", "picked")
        self.assertEqual(rc, 0, out)
        self.assertEqual(read(self.p("picked", "results", "exp-b", "data.txt")), "b data\n")
        self.assertFalse(os.path.exists(self.p("picked", "results", "exp-b", "more")))
        rc, out = self.box("fetch", "--session", "nope")
        self.assertEqual(rc, 1, out)
        rc, out = self.box("fetch", "--session", "c")
        self.assertIn("matches 3 sessions", out)
        self.set_box(min_free_bytes=10**18)
        rc, out = self.box("fetch", "--session", sid_b, "--into", "again")
        self.assertEqual(rc, 1, out)
        self.assertIn("not enough free disk", out)
        rc, out = self.box("fetch", "--session", sid_b, "--into", "again", "--force")
        self.assertEqual(rc, 0, out)
        self.set_box(min_free_bytes=0)
        rc, out = self.box("status")
        self.assertIn("session %s: 1 file(s)" % sid_a, out)
        rc, out = self.box("evict", "_sessions/" + sid_b, "--force")
        self.assertEqual(rc, 0, out)
        self.assertFalse(os.path.exists(self.p("_sessions", sid_b, "results", "exp-b", "data.txt")))
        # receive still works: every session not received yet, one folder each
        rc, out = self.box("receive")
        self.assertEqual(rc, 0, out)
        self.assertEqual(read(self.p("_agent_outbound", sid_a, "results", "exp-a.csv")), "a\n")
        self.assertTrue(os.path.isfile(self.p("_agent_outbound", sid_b, "results", "exp-b.tar.gz")))
        rc, out = self.box("receive")
        self.assertIn("nothing new", out)
        # the index keeps a summary; box sessions falls back to it without the transfer program
        rc, out = self.box("index")
        self.assertEqual(rc, 0, out)
        lines = [json.loads(x) for x in read(self.s("projects", "demo", "box_index.jsonl")).splitlines()]
        summary = [x for x in lines if x.get("path") == "_sessions"][0]
        self.assertEqual([r["id"] for r in summary["sessions"]][:2], sorted([sid_a, sid_b], reverse=True))
        empty = os.path.join(self.base, "emptybin")
        os.makedirs(empty)
        rc, out = self.box("sessions", PATH=empty)
        self.assertEqual(rc, 0, out)
        self.assertIn("listing the sessions recorded by the last `box index`", out)
        self.assertIn(sid_a, out)

    def test_promote_into_an_archive_and_an_include_folder(self):
        self.set_box(archives=["runs/run-*"])
        c = self.checkout("cloud-c")
        write(os.path.join(c, "runs", "run-7", "metrics.json"), '{"m": 1}\n')
        write(os.path.join(c, "temporary", "keep", "fig.png"), "png\n")
        write(os.path.join(c, "notes", "n.txt"), "n\n")
        rc, out = self.run_on(
            c, "box", "send", "runs/run-7", "--archive", "--label", "run7", *NOTE, AGENT_RUNTIME="claude-cloud"
        )
        self.assertEqual(rc, 0, out)
        for path in ("temporary/keep", "notes"):
            rc, out = self.run_on(c, "box", "send", path, *NOTE, AGENT_RUNTIME="claude-cloud")
            self.assertEqual(rc, 0, out)
        sid = self.local_session(c)["id"]
        self.as_machine("desk-a", ID_A)
        rc, out = self.box("promote", "--session", "run7", "runs/run-7", AGENT_RUNTIME="claude-cloud")
        self.assertEqual(rc, 2, out)
        rc, out = self.box("promote", "--session", "run7", "notes")
        self.assertEqual(rc, 2, out)
        self.assertIn('Add "notes/**" to box.include', out)
        rc, out = self.box("promote", "--session", "run7", "runs/run-7", "--dry-run")
        self.assertEqual(rc, 0, out)
        self.assertFalse(os.path.exists(self.p("runs")))
        rc, out = self.box("promote", "--session", "run7", "runs/run-7")
        self.assertEqual(rc, 0, out)
        self.assertEqual(read(self.p("runs", "run-7", "metrics.json")), '{"m": 1}\n')
        self.assertTrue(os.path.isfile(self.shared_path("_archives", "runs", "run-7.tar.gz")))
        self.assertEqual(self.owners()["runs/run-7"]["machine_name"], "desk-a")
        rc, out = self.box("promote", "--session", "run7", "temporary/keep", "--as", "temporary/figs")
        self.assertEqual(rc, 0, out)
        self.assertEqual(read(self.p("temporary", "figs", "fig.png")), "png\n")
        self.assertEqual(self.owners()["temporary"]["machine_name"], "desk-a")
        promoted = self.manifest(sid)["promoted"]
        self.assertEqual([(p["path"], p["to"]) for p in promoted], [("runs/run-7", "runs/run-7"), ("temporary/keep", "temporary/figs")])
        self.assertEqual((promoted[0]["machine_name"], promoted[0]["folders"]), ("desk-a", ["runs/run-7"]))
        # a folder owned by another machine is refused
        self.as_machine("desk-b", ID_B)
        rc, out = self.box("promote", "--session", "run7", "temporary/keep", "--as", "temporary/other")
        self.assertEqual(rc, 2, out)
        self.assertIn("owned by desk-a", out)
        self.assertFalse(os.path.exists(self.p("temporary", "other")))
        # a different local file is never overwritten
        self.as_machine("desk-a", ID_A)
        write(self.p("temporary", "figs2", "fig.png"), "mine\n")
        rc, out = self.box("promote", "--session", "run7", "temporary/keep", "--as", "temporary/figs2")
        self.assertEqual(rc, 1, out)
        self.assertEqual(read(self.p("temporary", "figs2", "fig.png")), "mine\n")

    def test_prune(self):
        c = self.checkout("cloud-c")
        write(os.path.join(c, "temporary", "t.txt"), "t\n")
        rc, out = self.run_on(
            c, "box", "send", "temporary/t.txt", "--label", "keepme", *NOTE, AGENT_RUNTIME="claude-cloud"
        )
        keep = self.local_session(c)["id"]
        rc, out = self.run_on(c, "box", "send", "temporary/t.txt", "--new-session", *NOTE, AGENT_RUNTIME="claude-cloud")
        drop = self.local_session(c)["id"]
        write(self.sessions_path("codex-cloud-20200101T000000Z", "old.txt"), "old\n")
        write(self.shared_path("temporary", "shared.txt"), "shared\n")
        self.as_machine("desk-a", ID_A)
        rc, out = self.box("promote", "--session", keep, "temporary/t.txt", "--as", "temporary/t.txt")
        self.assertEqual(rc, 0, out)
        rc, out = self.box("prune", "--older-than", "0", AGENT_RUNTIME="codex-cloud")
        self.assertEqual(rc, 2, out)
        rc, out = self.box("prune", "--session", "../shared")
        self.assertEqual(rc, 2, out)
        rc, out = self.box("prune")
        self.assertEqual(rc, 2, out)
        rc, out = self.box("prune", "--older-than", "1")
        self.assertEqual(rc, 0, out)
        self.assertRegex(out, r"would delete 1 session\(s\)")
        self.assertIn("codex-cloud-20200101T000000Z", out)
        self.assertIn("run the same command with --yes", out)
        self.assertEqual(len(self.namespaces()), 3)
        rc, out = self.box("prune", "--older-than", "0", "--keep-promoted", "--dry-run")
        self.assertIn("kept %s" % keep, out)
        self.assertEqual(len(self.namespaces()), 3)
        rc, out = self.box("prune", "--older-than", "0", "--keep-promoted", "--yes")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.namespaces(), [keep])
        self.assertNotIn(drop, out.split("deleted")[0].split("kept")[0])
        self.assertEqual(read(self.shared_path("temporary", "shared.txt")), "shared\n")  # never the shared side
        rc, out = self.box("prune", "--session", keep, "--yes")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.namespaces(), [])

    def test_dry_run_saves_nothing_and_into_the_project_tree(self):
        write(self.p("results", "r.csv"), "r\n")
        rc, out = self.box("send", "results/r.csv", "--dry-run", "--label", "dry")
        self.assertEqual(rc, 0, out)
        self.assertIn("would record the sent files", out)
        self.assertFalse(os.path.exists(os.path.join(self.project, ".agent-mirror", "session.json")))
        self.assertEqual(self.namespaces(), [])
        c = self.checkout("cloud-c")
        write(os.path.join(c, "results", "r.csv"), "from the session\n")
        write(os.path.join(c, "results", "new.csv"), "new\n")
        rc, out = self.run_on(c, "box", "send", "results", "--label", "over", *NOTE, AGENT_RUNTIME="claude-cloud")
        self.assertEqual(rc, 0, out)
        rc, out = self.box("fetch", "--session", "over", "--into", ".")
        self.assertEqual(rc, 0, out)
        self.assertEqual(read(self.p("results", "r.csv")), "r\n")  # never overwritten on a developer runtime
        self.assertEqual(read(self.p("results", "new.csv")), "new\n")

    def test_a_and_b_then_c(self):
        """Two cloud sessions send their results; a third lists both and fetches them."""
        a, b, c = self.checkout("cloud-a"), self.checkout("cloud-b"), self.checkout("cloud-c")
        write(os.path.join(a, "results", "a", "summary.json"), '{"session": "a"}\n')
        write(os.path.join(b, "results", "b", "plots", "p.txt"), "plot b\n")
        rc, out = self.run_on(a, "box", "send", "results/a", "--label", "exp-a", *NOTE, AGENT_RUNTIME="claude-cloud")
        self.assertEqual(rc, 0, out)
        rc, out = self.run_on(
            b, "box", "send", "results/b", "--archive", "--label", "exp-b", *NOTE, AGENT_RUNTIME="codex-cloud"
        )
        self.assertEqual(rc, 0, out)
        rc, out = self.run_on(c, "box", "sessions", "--json", AGENT_RUNTIME="claude-cloud")
        self.assertEqual(rc, 0, out)
        labels = dict((r["label"], r["id"]) for r in json.loads(out)["sessions"])
        self.assertEqual(sorted(labels), ["exp-a", "exp-b"])
        for label in ("exp-a", "exp-b"):
            rc, out = self.run_on(c, "box", "fetch", "--session", label, AGENT_RUNTIME="claude-cloud")
            self.assertEqual(rc, 0, out)
        self.assertEqual(read(os.path.join(c, "_sessions", labels["exp-a"], "results", "a", "summary.json")), '{"session": "a"}\n')
        self.assertEqual(read(os.path.join(c, "_sessions", labels["exp-b"], "results", "b", "plots", "p.txt")), "plot b\n")
        self.assertFalse(os.path.exists(os.path.join(c, "results")))


class AzureSessionsTest(SessionsMixin, ToolTest):
    """azure-blob with the older key names (inbound_container, outbound_container)."""

    def configure_box(self):
        self.root = os.path.join(self.base, "containers")

    def tool_env(self):
        env = self.fake_az()
        env["FAKE_AZ_ROOT"] = self.root
        return env

    def sessions_path(self, *parts):
        return os.path.join(self.root, "out-box", "demo", *[x for p in parts for x in p.split("/")])

    def shared_path(self, *parts):
        return os.path.join(self.root, "in-box", "demo", *[x for p in parts for x in p.split("/")])

    def owners(self):
        return json.loads(read(self.shared_path("_owners.json")))["folders"]

    def test_cloud_sign_in_before_listing_sessions(self):
        secret = "fake-" + "S3cr3t" * 4  # built at runtime
        sp = {
            "AGENT_RUNTIME": "claude-cloud",
            "AZURE_CLIENT_ID": "client-id-1",
            "AZURE_TENANT_ID": "tenant-id-1",
            "AZURE_CLIENT_SECRET": secret,
            "FAKE_AZ_SECRET": secret,
            "FAKE_AZ_ACCOUNT_SHOW_RC": "1",
        }
        rc, out = self.box("sessions", **sp)
        self.assertEqual(rc, 0, out)
        login = self.az_calls("login")
        self.assertEqual(len(login), 1, out)
        self.assertTrue(login[0]["stdin_is_secret"])
        self.assertLess(self.az_calls().index(login[0]), self.az_calls().index(self.az_calls("storage", "blob", "list")[0]))
        self.assertNotIn(secret, out)

    def test_a_clobbered_manifest_write_is_retried(self):
        write(self.p("results", "r.csv"), "r\n")
        rc, out = self.box("send", "results/r.csv", "--label", "first")
        sid = self.local_session()["id"]
        other = dict(self.manifest(sid), files=[], label="clobbered")
        path = os.path.join(self.base, "other-manifest.json")
        write(path, json.dumps(other))
        hooks = os.path.join(self.base, "hooks.json")
        write(hooks, json.dumps([{"after_put": "_session.json", "copy_from": path, "copy_to": self.sessions_path(sid, "_session.json"), "times": 1}]))
        write(self.p("results", "s.csv"), "s\n")
        rc, out = self.box("send", "results/s.csv", FAKE_HOOKS=hooks)
        self.assertEqual(rc, 0, out)
        self.assertIn("trying again", out)
        m = self.manifest(sid)
        self.assertIn("results/s.csv", [f["path"] for f in m["files"]])
        self.assertEqual(m["label"], "clobbered")  # the other writer's label stands; this send added its file

    def test_promote_never_shrinks_an_archive_that_is_not_here(self):
        self.set_box(archives=["runs/run-*"])
        self.as_machine("desk-a", ID_A)
        write(self.p("runs", "run-1", "a.txt"), "a\n")
        write(self.p("runs", "run-1", "b.txt"), "b\n")
        rc, out = self.box("push")
        self.assertEqual(rc, 0, out)
        bundle = read_bytes(self.shared_path("_archives", "runs", "run-1.tar.gz"))
        shutil.rmtree(self.p("runs"))
        c = self.checkout("cloud-c")
        write(os.path.join(c, "runs", "run-1", "c.txt"), "c\n")
        rc, out = self.run_on(
            c, "box", "send", "runs/run-1/c.txt", "--label", "more", *NOTE, AGENT_RUNTIME="claude-cloud"
        )
        self.assertEqual(rc, 0, out)
        rc, out = self.box("promote", "--session", "more", "runs/run-1/c.txt")
        self.assertEqual(rc, 2, out)
        self.assertIn("fetch it first with `agent-mirror box fetch runs/run-1 --all`", out)
        self.assertEqual(read_bytes(self.shared_path("_archives", "runs", "run-1.tar.gz")), bundle)
        self.assertFalse(os.path.exists(self.p("runs", "run-1", "c.txt")))

    def test_new_key_names(self):
        env = json.loads(read(self.s("projects", "demo", "env.json")))
        box = env["box"]
        box["shared_container"], box["sessions_container"] = "shared-c", "sessions-c"
        del box["inbound_container"], box["outbound_container"]
        write(self.s("projects", "demo", "env.json"), json.dumps(env))
        write(self.p("temporary", "t.txt"), "t\n")
        rc, out = self.box("send", "temporary/t.txt")
        self.assertEqual(rc, 0, out)
        sid = self.local_session()["id"]
        self.assertTrue(os.path.isfile(os.path.join(self.root, "sessions-c", "demo", sid, "temporary", "t.txt")))
        rc, out = self.box("push")
        self.assertEqual(rc, 0, out)
        self.assertTrue(os.path.isfile(os.path.join(self.root, "shared-c", "demo", "_owners.json")))
        rc, out = self.run_tool("doctor", "--project", "demo", "--project-path", self.project, env=self.tool_env())
        self.assertIn("shared   shared-c/demo/", out)
        self.assertIn("sessions sessions-c/demo/", out)
        self.assertNotIn("older key names", out)


class RcloneSessionsTest(SessionsMixin, ToolTest):
    """rclone (fake) with the new key names (shared_remote, sessions_remote)."""

    def configure_box(self):
        self.root = os.path.join(self.base, "remotes")
        self.log = os.path.join(self.base, "rclone_log.jsonl")
        env = json.loads(read(self.s("projects", "demo", "env.json")))
        env["box"] = {
            "backend": "rclone",
            "shared_remote": "fakeremote:box/shared",
            "sessions_remote": "fakeremote:box/sessions",
            "include": ["temporary/**"],
            "exclude": ["**/__pycache__/**"],
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

    def sessions_path(self, *parts):
        return os.path.join(self.root, "fakeremote", "box", "sessions", *[x for p in parts for x in p.split("/")])

    def shared_path(self, *parts):
        return os.path.join(self.root, "fakeremote", "box", "shared", *[x for p in parts for x in p.split("/")])

    def owners(self):
        return json.loads(read(self.shared_path("_owners.json")))["folders"]

    def test_old_key_names(self):
        env = json.loads(read(self.s("projects", "demo", "env.json")))
        box = env["box"]
        box["inbound_remote"], box["outbound_remote"] = box.pop("shared_remote"), box.pop("sessions_remote")
        write(self.s("projects", "demo", "env.json"), json.dumps(env))
        write(self.p("temporary", "t.txt"), "t\n")
        rc, out = self.box("send", "temporary/t.txt")
        self.assertEqual(rc, 0, out)
        self.assertTrue(os.path.isfile(self.sessions_path(self.local_session()["id"], "temporary", "t.txt")))
        rc, out = self.run_tool("doctor", "--project", "demo", "--project-path", self.project, env=self.tool_env())
        self.assertIn("the config uses the older key names inbound_remote, outbound_remote", out)


@unittest.skipUnless(shutil.which("rclone"), "rclone is not installed")
class RcloneRealSessionsTest(ToolTest):
    """Real rclone, local folders as remotes: send, list, fetch (with an archive), promote, prune, receive."""

    def test_sessions_with_real_rclone(self):
        shared = os.path.join(self.base, "remote", "shared")
        sessions = os.path.join(self.base, "remote", "sessions")
        os.makedirs(shared)
        env = json.loads(read(self.s("projects", "demo", "env.json")))
        env["box"] = {
            "backend": "rclone",
            "shared_remote": shared,
            "sessions_remote": sessions,
            "include": ["temporary/**"],
            "min_free_bytes": 0,
        }
        write(self.s("projects", "demo", "env.json"), json.dumps(env))
        renv = {"RCLONE_CONFIG": os.path.join(self.base, "rclone.conf")}
        a = os.path.join(self.base, "cloud-a", "demo")
        write(os.path.join(a, "temporary", "res", "r.txt"), "result\n")
        write(os.path.join(a, "temporary", "res", "sub", "s.txt"), "sub\n")

        def run(path, *args, **extra):
            e = dict(renv)
            e.update(extra)
            return self.run_tool(*args, "--project", "demo", "--project-path", path, env=e)

        rc, out = run(a, "box", "send", "temporary/res", "--label", "real", *NOTE, AGENT_RUNTIME="claude-cloud")
        self.assertEqual(rc, 0, out)
        rc, out = run(a, "box", "send", "temporary/res", "--archive", *NOTE, AGENT_RUNTIME="claude-cloud")
        self.assertEqual(rc, 0, out)
        sid = json.loads(read(os.path.join(a, ".agent-mirror", "session.json")))["id"]
        self.assertEqual(os.listdir(sessions), [sid])
        manifest = json.loads(read(os.path.join(sessions, sid, "_session.json")))
        self.assertEqual(len(manifest["files"]), 3)
        os.makedirs(os.path.join(sessions, "codex-cloud-20200101T000000Z"))
        write(os.path.join(sessions, "codex-cloud-20200101T000000Z", "old.txt"), "old\n")
        rc, out = run(self.project, "box", "sessions", CLAUDECODE="1")
        self.assertEqual(rc, 0, out)
        self.assertRegex(out, sid + r" +real +claude-cloud")
        self.assertRegex(out, r"codex-cloud-20200101T000000Z .*no manifest +1 files")
        rc, out = run(self.project, "box", "fetch", "--session", "real", CLAUDECODE="1")
        self.assertEqual(rc, 0, out)
        self.assertEqual(read(self.p("_sessions", sid, "temporary", "res", "sub", "s.txt")), "sub\n")
        os.remove(self.p("_sessions", sid, "temporary", "res", "r.txt"))
        rc, out = run(self.project, "box", "fetch", "--session", sid, "temporary/res/r.txt", CLAUDECODE="1")
        self.assertEqual(rc, 0, out)
        self.assertEqual(read(self.p("_sessions", sid, "temporary", "res", "r.txt")), "result\n")
        rc, out = run(self.project, "box", "promote", "--session", "real", "temporary/res", CLAUDECODE="1")
        self.assertEqual(rc, 0, out)
        self.assertEqual(read(os.path.join(shared, "temporary", "res", "sub", "s.txt")), "sub\n")
        self.assertIn("temporary", json.loads(read(os.path.join(shared, "_owners.json")))["folders"])
        rc, out = run(self.project, "box", "receive", CLAUDECODE="1")
        self.assertEqual(rc, 0, out)
        self.assertTrue(os.path.isfile(self.p("_agent_outbound", sid, "temporary", "res.tar.gz")))
        rc, out = run(self.project, "box", "prune", "--older-than", "0", "--keep-promoted", "--yes", CLAUDECODE="1")
        self.assertEqual(rc, 0, out)
        self.assertEqual(os.listdir(sessions), [sid])
        with tarfile.open(os.path.join(sessions, sid, "temporary", "res.tar.gz")) as tf:
            self.assertIn("temporary/res/sub/s.txt", tf.getnames())


if __name__ == "__main__":
    unittest.main()
