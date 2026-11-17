"""hooks install/uninstall/status, the memory-write hook, and the timer content for three platforms."""

import json
import os
import plistlib
import unittest

from helpers import PLUGIN, ToolTest, read, write

from agent_mirror.hooks import classify, merge_install, merge_uninstall
from agent_mirror.timer import preview, systemd_quote

EXISTING = {
    "model": "some-model",
    "permissions": {"allow": ["Bash(ls:*)"]},
    "hooks": {
        "PostToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "echo after-bash"}]}],
        "SessionStart": [{"hooks": [{"type": "command", "command": "echo hello"}]}],
    },
}


class HooksTest(ToolTest):
    def settings_path(self):
        return self.h(".claude", "settings.json")

    def test_install_keeps_existing_is_idempotent_and_uninstall_restores(self):
        write(self.settings_path(), json.dumps(EXISTING, indent=2) + "\n")
        rc, out = self.run_tool("hooks", "install")
        self.assertEqual(rc, 0, out)
        self.assertIn("backed up", out)
        data = json.loads(read(self.settings_path()))
        self.assertEqual(data["model"], "some-model")
        self.assertEqual(data["permissions"], EXISTING["permissions"])
        launcher = os.path.join(PLUGIN, "bin", "agent-mirror")
        cmds = {}
        for event, groups in data["hooks"].items():
            for g in groups:
                for h in g["hooks"]:
                    cmds.setdefault(event, []).append((g.get("matcher"), h))
        self.assertIn((None, {"type": "command", "command": "echo hello"}), cmds["SessionStart"])
        self.assertIn(("Bash", {"type": "command", "command": "echo after-bash"}), cmds["PostToolUse"])
        start = [h for m, h in cmds["SessionStart"] if "agent-mirror" in h["command"]][0]
        self.assertEqual(start["command"], launcher + " session-start")
        write_hook = [(m, h) for m, h in cmds["PostToolUse"] if "agent-mirror" in h["command"]][0]
        self.assertEqual(write_hook[0], "Write|Edit")
        self.assertIs(write_hook[1]["async"], True)
        self.assertEqual(write_hook[1]["command"], launcher + " hook memory-write")
        end = cmds["SessionEnd"][0][1]
        self.assertEqual(end["command"], launcher + " sync")
        self.assertTrue(os.path.isabs(launcher))

        before = read(self.settings_path())
        rc, out = self.run_tool("hooks", "install")
        self.assertEqual(rc, 0, out)
        self.assertIn("nothing to change", out)
        self.assertEqual(read(self.settings_path()), before)

        rc, out = self.run_tool("hooks", "status")
        self.assertIn("3 of 3 installed", out)

        rc, out = self.run_tool("hooks", "uninstall", "--dry-run")
        self.assertEqual(rc, 0, out)
        self.assertEqual(read(self.settings_path()), before)
        rc, out = self.run_tool("hooks", "uninstall")
        self.assertEqual(rc, 0, out)
        self.assertEqual(json.loads(read(self.settings_path())), EXISTING)

    def test_install_into_missing_file_and_uninstall_leaves_empty_object(self):
        rc, out = self.run_tool("hooks", "install")
        self.assertEqual(rc, 0, out)
        self.assertEqual(
            sorted(json.loads(read(self.settings_path()))["hooks"]), ["PostToolUse", "SessionEnd", "SessionStart"]
        )
        rc, out = self.run_tool("hooks", "uninstall")
        self.assertEqual(json.loads(read(self.settings_path())), {})

    def test_store_flag_is_written_into_the_commands(self):
        rc, out = self.run_tool("--store", self.store, "hooks", "install", with_store=False)
        self.assertEqual(rc, 0, out)
        data = json.loads(read(self.settings_path()))
        cmd = data["hooks"]["SessionEnd"][0]["hooks"][0]["command"]
        self.assertTrue(cmd.endswith("--store %s sync" % self.store), cmd)
        self.assertEqual(classify(cmd), "sync")

    def test_codex_and_bad_json_are_refused(self):
        rc, out = self.run_tool("hooks", "install", "--agent", "codex")
        self.assertEqual(rc, 2, out)
        self.assertIn("not supported", out)
        write(self.settings_path(), "{not json")
        rc, out = self.run_tool("hooks", "install")
        self.assertEqual(rc, 1, out)
        self.assertEqual(read(self.settings_path()), "{not json")

    def test_classify_and_merge_functions(self):
        self.assertEqual(classify("/x/bin/agent-mirror session-start"), "session-start")
        self.assertEqual(classify('"C:/x/bin/agent-mirror.cmd" hook memory-write'), "hook memory-write")
        self.assertEqual(classify("'/a b/agent-mirror' --store '/s t' sync"), "sync")
        self.assertIsNone(classify("/x/bin/agent-mirror doctor"))
        self.assertIsNone(classify("echo sync"))
        settings = json.loads(json.dumps(EXISTING))
        wanted = [("SessionEnd", None, "sync", {"type": "command", "command": "/old/agent-mirror sync"})]
        self.assertEqual(len(merge_install(settings, wanted)), 1)
        wanted = [("SessionEnd", None, "sync", {"type": "command", "command": "/new/agent-mirror sync"})]
        self.assertEqual(len(merge_install(settings, wanted)), 1)  # a moved launcher is rewritten, not duplicated
        self.assertEqual(len(settings["hooks"]["SessionEnd"]), 1)
        self.assertEqual(merge_uninstall(settings), ["SessionEnd: agent-mirror sync"])
        self.assertEqual(settings, EXISTING)

    def test_memory_write_hook_syncs_only_store_files(self):
        self.init_git()
        head = self.git("rev-parse", "HEAD")
        other = os.path.join(self.base, "elsewhere.md")
        write(other, "x\n")
        rc, out = self.run_tool("hook", "memory-write", stdin=json.dumps({"tool_input": {"file_path": other}}))
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.git("rev-parse", "HEAD"), head)
        rc, out = self.run_tool("hook", "memory-write", stdin="not json")
        self.assertEqual(rc, 0, out)
        new = self.s("memory", "demo", "hooked.md")
        write(new, "---\nname: hooked\ndescription: test\nmetadata:\n  type: project\n---\nbody\n")
        rc, out = self.run_tool("hook", "memory-write", stdin=json.dumps({"tool_input": {"file_path": new}}))
        self.assertEqual(rc, 0, out)
        self.assertNotEqual(self.git("rev-parse", "HEAD"), head)
        self.assertIn("memory/demo/hooked.md", self.git("ls-files", "memory/demo"))
        log = read(self.h(".cache", "agent-mirror", "sync.log"))
        self.assertIn("memory-write: " + new, log)
        self.assertIn("committed: memory: sync from", log)


class TimerTest(ToolTest):
    ARGV = ["/usr/bin/python3", "/opt/plugin/bin/agent-mirror", "sync"]

    def test_linux_systemd_units(self):
        p = preview("linux", 15, self.ARGV, env={"HOME": "/home/u"})
        service = p["files"]["/home/u/.config/systemd/user/agent-mirror-sync.service"]
        timer = p["files"]["/home/u/.config/systemd/user/agent-mirror-sync.timer"]
        self.assertIn('ExecStart="/usr/bin/python3" "/opt/plugin/bin/agent-mirror" "sync"', service)
        self.assertIn("Type=oneshot", service)
        self.assertIn("OnUnitActiveSec=15min", timer)
        self.assertIn("WantedBy=timers.target", timer)
        self.assertIn(["systemctl", "--user", "enable", "--now", "agent-mirror-sync.timer"], p["run"])
        self.assertEqual(systemd_quote("50%$x"), '"50%%$$x"')

    def test_darwin_launchd_plist(self):
        p = preview("darwin", 20, self.ARGV, env={"HOME": "/Users/u"}, uid=501)
        path = "/Users/u/Library/LaunchAgents/agent-mirror.sync.plist"
        data = plistlib.loads(p["files"][path].encode("utf-8"))
        self.assertEqual(data["Label"], "agent-mirror.sync")
        self.assertEqual(data["ProgramArguments"], self.ARGV)
        self.assertEqual(data["StartInterval"], 1200)
        self.assertEqual(data["StandardOutPath"], "/Users/u/Library/Logs/agent-mirror-sync.log")
        self.assertIn(["launchctl", "bootstrap", "gui/501", path], p["run"])

    def test_win32_schtasks(self):
        argv = ["C:\\Python\\pythonw.exe", "C:\\tools\\plugin\\bin\\agent-mirror", "sync", "--log", "C:\\L\\sync.log"]
        p = preview("win32", 45, argv, env={"USERPROFILE": "C:\\Users\\u"})
        self.assertEqual(p["files"], {})
        cmd = p["run"][0]
        self.assertEqual(
            cmd[:9], ["schtasks", "/Create", "/TN", "agent-mirror-sync", "/SC", "MINUTE", "/MO", "45", "/TR"]
        )
        self.assertEqual(
            cmd[9], "C:\\Python\\pythonw.exe C:\\tools\\plugin\\bin\\agent-mirror sync --log C:\\L\\sync.log"
        )
        self.assertEqual(cmd[10], "/F")

    def test_cli_dry_run_writes_and_runs_nothing(self):
        rc, out = self.run_tool("timer", "install", "--minutes", "10", "--dry-run")
        self.assertEqual(rc, 0, out)
        unit_dir = self.h(".config", "systemd", "user")
        self.assertIn("would write %s:" % os.path.join(unit_dir, "agent-mirror-sync.service"), out)
        self.assertIn("OnUnitActiveSec=10min", out)
        self.assertIn('"--store" "%s"' % self.store, out)  # AGENT_MIRROR_STORE is not seen by the scheduler
        self.assertIn("would run: systemctl --user enable --now agent-mirror-sync.timer", out)
        self.assertFalse(os.path.exists(unit_dir))
        rc, out = self.run_tool("timer", "uninstall", "--dry-run")
        self.assertEqual(rc, 0, out)
        self.assertIn("would run: systemctl --user disable --now agent-mirror-sync.timer", out)
        rc, out = self.run_tool("timer", "install", "--minutes", "0", "--dry-run")
        self.assertEqual(rc, 2, out)


if __name__ == "__main__":
    unittest.main()
