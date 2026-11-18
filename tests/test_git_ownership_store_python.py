"""0.5.3: two problems met on a Windows 11 machine, tested on any OS.

1. git's "dubious ownership" refusal. A store or tool clone made from an elevated shell belongs to the
   Administrators group, and git started without elevation (Task Scheduler, the agent app) refuses it.
   sync, session-start, self-update, bootstrap, init-store and probe said only "not a git clone"; now they
   name the refusal and the exact `git config --global --add safe.directory ...` to run, and doctor reports
   it as a PROBLEM. Nothing changes the user's git config. Git's own test switch
   GIT_TEST_ASSUME_DIFFERENT_OWNER plays the foreign owner where this git honours it.
2. The Microsoft Store Python keeps what it writes under AppData in its package folder, so a printed cache or
   log path did not exist for other programs. The places that print one also print the real location. The
   path computation is tested with made-up Windows paths; no Windows is needed.
"""

import argparse
import contextlib
import io
import os
import subprocess
import tempfile
import unittest
from unittest import mock

from helpers import setUpModule  # noqa: F401 (the sandbox guard runs first)
from helpers import REPO, ToolTest, clean_env, read, remove_tree

from agent_mirror import timer
from agent_mirror.platforms import Platform, packaged_location, store_python_family
from agent_mirror.util import dubious_ownership, git_refusal, safe_directory_command

LINUX_STDERR = (
    "fatal: detected dubious ownership in repository at '/srv/agent-store'\n"
    "To add an exception for this directory, call:\n\n"
    "\tgit config --global --add safe.directory /srv/agent-store\n"
)
WINDOWS_STDERR = (
    "fatal: detected dubious ownership in repository at 'C:/Users/dev/agent-store'\n"
    "'C:/Users/dev/agent-store' is owned by:\n"
    "\tBUILTIN/Administrators (S-1-5-32-544)\n"
    "but the current user is:\n"
    "\tDESKTOP-1/dev (S-1-5-21-1-2-3-1001)\n"
    "To add an exception for this directory, call:\n\n"
    "\tgit config --global --add safe.directory C:/Users/dev/agent-store\n"
)
TRANSLATED_STDERR = "fatal: <a translated message>\n\n\tgit config --global --add safe.directory 'D:/work/my store'\n"

FAMILY = "PythonSoftwareFoundation.Python.3.12_qbz5n2kfra8p0"
LOCAL = "Z:\\fake\\AppData\\Local"  # a made-up drive: nothing here is read on a real Windows machine
ROAMING = "Z:\\fake\\AppData\\Roaming"
STORE_EXE = LOCAL + "\\Microsoft\\WindowsApps\\" + FAMILY + "\\python.exe"
STORE_BASE = "C:\\Program Files\\WindowsApps\\PythonSoftwareFoundation.Python.3.12_3.12.2800.0_x64__qbz5n2kfra8p0"
ORG_EXE = LOCAL + "\\Programs\\Python\\Python312\\python.exe"
ORG_BASE = LOCAL + "\\Programs\\Python\\Python312"
WIN_ENV = {"USERPROFILE": "Z:\\fake", "LOCALAPPDATA": LOCAL, "APPDATA": ROAMING}


def git_honours_the_owner_switch():
    base = tempfile.mkdtemp(prefix="agent-mirror-owner-probe-")
    try:
        env = clean_env(os.path.join(base, "home"), {"GIT_TEST_ASSUME_DIFFERENT_OWNER": "1"})
        subprocess.run(["git", "init", "-q", os.path.join(base, "r")], env=env, check=True, timeout=60)
        proc = subprocess.run(
            ["git", "-C", os.path.join(base, "r"), "rev-parse", "--show-toplevel"],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            timeout=60,
        )
        return proc.returncode != 0 and dubious_ownership(proc.stderr) is not None
    except (OSError, subprocess.SubprocessError):
        return False
    finally:
        remove_tree(base)


class DubiousOwnershipTextTest(unittest.TestCase):
    def test_git_messages_and_the_remedy(self):
        self.assertEqual(dubious_ownership(LINUX_STDERR), "/srv/agent-store")
        self.assertEqual(dubious_ownership(WINDOWS_STDERR), "C:/Users/dev/agent-store")
        self.assertEqual(dubious_ownership(TRANSLATED_STDERR), "D:/work/my store")  # the command is never translated
        self.assertEqual(dubious_ownership("fatal: detected dubious ownership in repository"), "")
        for other in ("fatal: not a git repository (or any of the parent directories): .git", "", None):
            self.assertIsNone(dubious_ownership(other))
        self.assertEqual(
            safe_directory_command("C:\\Users\\dev\\agent-store"),
            "git config --global --add safe.directory C:/Users/dev/agent-store",
        )
        self.assertEqual(
            safe_directory_command("D:/work/my store"), 'git config --global --add safe.directory "D:/work/my store"'
        )
        line = git_refusal("C:\\Users\\dev\\agent-store", stderr=WINDOWS_STDERR)
        self.assertIn('git refuses C:\\Users\\dev\\agent-store: "detected dubious ownership"', line)
        self.assertTrue(
            line.endswith("Allow it once with: git config --global --add safe.directory C:/Users/dev/agent-store")
        )
        self.assertNotIn("\n", line)
        self.assertIsNone(git_refusal("/srv/x", stderr="fatal: not a git repository"))


@unittest.skipUnless(
    git_honours_the_owner_switch(), "this git ignores GIT_TEST_ASSUME_DIFFERENT_OWNER (or is older than 2.35.2)"
)
class DubiousOwnershipCommandsTest(ToolTest):
    OWNER = {"GIT_TEST_ASSUME_DIFFERENT_OWNER": "1"}

    def test_sync_doctor_and_session_start_name_the_refusal_and_its_remedy(self):
        self.init_git()
        remedy = "git config --global --add safe.directory %s" % os.path.realpath(self.store).replace("\\", "/")
        rc, out = self.run_tool("sync", env=self.OWNER)
        self.assertEqual(rc, 1, out)
        errors = [ln for ln in out.splitlines() if ln.startswith("error:")]
        self.assertEqual(len(errors), 1, out)
        self.assertIn('git refuses %s: "detected dubious ownership"' % self.store, errors[0])
        self.assertTrue(errors[0].endswith(remedy), errors[0])
        self.assertNotIn("is not a git clone", out)
        rc, out = self.run_tool("doctor", env=self.OWNER)
        self.assertEqual(rc, 0, out)
        self.assertIn("  git: PROBLEM  git refuses %s" % self.store, out)
        self.assertIn(remedy, out)
        if os.path.isdir(os.path.join(REPO, ".git")):  # the tests run from a clone of agent-mirror
            self.assertIn("  PROBLEM  the agent-mirror clone: git refuses ", out)
        rc, out = self.run_tool("session-start", env=dict(self.OWNER, CLAUDECODE="1"))
        self.assertEqual(rc, 0, out)
        self.assertIn("warning: git refuses %s" % self.store, out)
        self.assertIn("; skipped the pull", out)
        rc, out = self.run_tool("init-store", self.store, "--adopt", "--dry-run", env=self.OWNER)
        self.assertIn("git refuses %s" % self.store, out)
        # without the foreign owner, the same commands work, and nothing touched the git config
        rc, out = self.run_tool("doctor")
        self.assertNotIn("PROBLEM", out)
        self.assertNotIn("safe.directory", read(self.h(".gitconfig")))


class StorePythonTest(unittest.TestCase):
    def test_which_python_is_the_store_python(self):
        self.assertEqual(store_python_family(STORE_EXE, ""), FAMILY)  # the app alias folder is the family name
        self.assertEqual(store_python_family("C:\\x\\python.exe", STORE_BASE), FAMILY)  # the install folder
        self.assertEqual(store_python_family(STORE_EXE.replace("\\", "/"), None), FAMILY)
        for exe, base in (
            (ORG_EXE, ORG_BASE),  # python.org, installed under %LOCALAPPDATA%\Programs
            (LOCAL + "\\Microsoft\\WindowsApps\\python.exe", ORG_BASE),  # the alias folder itself names no package
            ("/usr/bin/python3", "/usr"),
            (None, None),
        ):
            self.assertIsNone(store_python_family(exe, base), exe)

    def test_where_the_store_python_keeps_files(self):
        cache = LOCAL + "\\Packages\\" + FAMILY + "\\LocalCache"
        self.assertEqual(
            packaged_location(LOCAL + "\\agent-mirror\\sync.log", LOCAL, ROAMING, FAMILY),
            cache + "\\Local\\agent-mirror\\sync.log",
        )
        self.assertEqual(
            packaged_location(ROAMING + "\\agent-mirror\\config.json", LOCAL, ROAMING, FAMILY),
            cache + "\\Roaming\\agent-mirror\\config.json",
        )
        self.assertEqual(
            packaged_location("z:\\FAKE\\appdata\\local\\agent-mirror", LOCAL, ROAMING, FAMILY),
            cache + "\\Local\\agent-mirror",
        )
        for path in ("D:\\data\\sync.log", cache + "\\Local\\agent-mirror\\sync.log", LOCAL + "x\\a.txt"):
            self.assertIsNone(packaged_location(path, LOCAL, ROAMING, FAMILY), path)
        self.assertIsNone(packaged_location(LOCAL + "\\agent-mirror", LOCAL, ROAMING, None))

    def test_printed_paths_name_the_real_location(self):
        plat = Platform("win32", WIN_ENV, python=(STORE_EXE, STORE_BASE))
        self.assertEqual(plat.store_python(), FAMILY)
        log = plat.join(plat.cache_dir(), "sync.log")
        self.assertEqual(log, LOCAL + "\\agent-mirror\\sync.log")
        real = LOCAL + "\\Packages\\" + FAMILY + "\\LocalCache\\Local\\agent-mirror\\sync.log"
        for existing, expected in (
            (set(), " (will be on disk at %s: " % real),
            ({real, log}, " (on disk at %s: " % real),  # that Python sees it at both places; the package path decides
            ({log}, ""),  # it was there before that Python wrote to it: it stays where it is
        ):
            with mock.patch("agent_mirror.platforms.os.path.lexists", side_effect=lambda p, e=existing: p in e):
                note = plat.disk_note(log)
            if expected:
                self.assertTrue(note.startswith(expected), note)
                self.assertIn("other programs find it only there", note)
            else:
                self.assertEqual(note, "")
        # every other Python, platform or sandbox: nothing changes
        for other in (
            Platform("win32", WIN_ENV, python=(ORG_EXE, ORG_BASE)),
            Platform("linux", {"HOME": "/home/u"}, python=(STORE_EXE, STORE_BASE)),
            Platform("win32", dict(WIN_ENV, AGENT_MIRROR_HOME="Z:\\fake\\sandbox"), python=(STORE_EXE, STORE_BASE)),
        ):
            self.assertIsNone(other.store_python())
            self.assertEqual(other.disk_note(other.join(other.cache_dir(), "sync.log")), "")

    def test_timer_install_names_where_the_log_really_is(self):
        class Ctx:
            def store_source_for_commands(self):
                return False

        args = argparse.Namespace(action="install", minutes=30, dry_run=True)
        for python, shown in (((STORE_EXE, STORE_BASE), True), ((ORG_EXE, ORG_BASE), False)):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                self.assertEqual(timer.cmd_timer(args, Ctx(), plat=Platform("win32", WIN_ENV, python=python)), 0)
            text = buf.getvalue()
            self.assertIn("would run: schtasks /Create", text)
            if shown:
                self.assertIn(
                    "sync log: %s\\agent-mirror\\sync.log (will be on disk at %s\\Packages\\%s\\LocalCache\\Local\\"
                    "agent-mirror\\sync.log" % (LOCAL, LOCAL, FAMILY),
                    text,
                )
            else:
                self.assertNotIn("sync log:", text)  # any other Python: the output is as before


if __name__ == "__main__":
    unittest.main()
