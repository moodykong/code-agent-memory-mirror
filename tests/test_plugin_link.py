"""0.5.9: the plugin link (<claude home>/skills/agent-mirror -> this checkout's plugin folder), the plugin's
manifest name, and the wrap-up skill's command lines.

Claude Code loads a link in its skills folder that leads to a folder with .claude-plugin/plugin.json as a
skills-directory plugin named by the manifest, so the plugin's skills are /agent-mirror:<skill>. install and
apply make the link for Claude, a cloud session start repairs it, doctor and plan report it in every state,
uninstall removes it, and nothing agent-mirror did not make is ever replaced.
"""

import argparse
import contextlib
import glob
import io
import json
import os
import re
import shlex
import shutil
import tempfile
import unittest
from unittest import mock

from helpers import setUpModule  # noqa: F401 (the sandbox guard runs first)
from helpers import PLUGIN, REPO, ToolTest, dir_link, is_link, make_home, make_store, read, reactivate_sandbox
from helpers import readlink, remove_tree, write

from agent_mirror import __version__, pluginlink
from agent_mirror.apply import apply_plan
from agent_mirror.cli import build_parser
from agent_mirror.config import Context
from agent_mirror.install import Installer
from agent_mirror.links import remove_link
from agent_mirror.plan import Plan
from agent_mirror.platforms import Platform
from agent_mirror.steplog import StepLog

SKILL = os.path.join(PLUGIN, "skills", "wrap-up", "SKILL.md")


def load(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def fake_checkout(folder, name="agent-mirror"):
    """A folder that looks like the plugin folder of another agent-mirror checkout."""
    write(os.path.join(folder, ".claude-plugin", "plugin.json"), json.dumps({"name": name}))
    write(os.path.join(folder, "agent_mirror", "__init__.py"), "")
    return folder


def drop(path):
    """Remove whatever is at path: a link (never its target), a folder or a file."""
    if is_link(path):
        remove_link(path)
    elif os.path.isdir(path):
        remove_tree(path)
    elif os.path.lexists(path):
        os.remove(path)


def frontmatter(text):
    m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    return dict(line.split(": ", 1) for line in m.group(1).splitlines() if ": " in line) if m else {}


def skill_commands(text):
    """Every `agent-mirror ...` command line of a skill: in code blocks and in inline code."""
    found = []
    for block in re.findall(r"```[a-z]*\n(.*?)```", text, re.S):
        found += [ln.strip() for ln in block.splitlines() if ln.strip().startswith("agent-mirror ")]
    rest = re.sub(r"```.*?```", "", text, flags=re.S)
    found += [c.strip() for c in re.findall(r"`([^`\n]+)`", rest) if c.strip().startswith("agent-mirror ")]
    return found


def subcommands(parser):
    """{name: parser} of a parser's subcommands ({} when it has none)."""
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return dict(action.choices)
    return {}


def command_group(parser, words):
    """True when words name a command group and nothing else (`agent-mirror box` in a sentence)."""
    for word in words:
        parser = subcommands(parser).get(word)
        if parser is None:
            return False
    return bool(words) and bool(subcommands(parser))


class ManifestTest(unittest.TestCase):
    def test_the_plugin_is_named_agent_mirror_everywhere(self):
        claude = load(os.path.join(PLUGIN, ".claude-plugin", "plugin.json"))
        codex = load(os.path.join(PLUGIN, ".codex-plugin", "plugin.json"))
        market = load(os.path.join(REPO, ".claude-plugin", "marketplace.json"))
        self.assertEqual((claude["name"], codex["name"]), (pluginlink.NAME, pluginlink.NAME))
        self.assertEqual((claude["version"], codex["version"]), (__version__, __version__))
        self.assertEqual(market["name"], "code-agent-memory-mirror")  # the marketplace keeps the repository's name
        self.assertEqual(market["metadata"]["version"], __version__)
        self.assertEqual(
            [(p["name"], p["version"], p["source"]) for p in market["plugins"]],
            [("agent-mirror", __version__, "./plugin")],
        )
        self.assertEqual(pluginlink.manifest_name(PLUGIN), "agent-mirror")
        self.assertTrue(pluginlink.is_tool_plugin(PLUGIN))

    def test_every_skill_is_named_after_its_folder(self):
        names = sorted(os.listdir(os.path.join(PLUGIN, "skills")))
        self.assertIn("wrap-up", names)
        for name in names:
            meta = frontmatter(read(os.path.join(PLUGIN, "skills", name, "SKILL.md")))
            self.assertEqual(meta.get("name"), name)
            self.assertTrue(meta.get("description"), name)

    def test_checkouts_before_and_after_the_rename_are_recognised(self):
        base = os.path.realpath(tempfile.mkdtemp(prefix="agent-mirror-pl-"))
        self.addCleanup(remove_tree, base)
        self.assertTrue(pluginlink.is_tool_plugin(fake_checkout(os.path.join(base, "new"))))
        self.assertTrue(pluginlink.is_tool_plugin(fake_checkout(os.path.join(base, "old"), "code-agent-memory-mirror")))
        self.assertFalse(pluginlink.is_tool_plugin(fake_checkout(os.path.join(base, "other"), "someone-else")))
        os.makedirs(os.path.join(base, "plain"))
        self.assertFalse(pluginlink.is_tool_plugin(os.path.join(base, "plain")))


class WrapUpSkillTest(unittest.TestCase):
    """The skill names only commands and flags the CLI has, so it cannot drift from it."""

    def test_every_command_line_parses(self):
        text = read(SKILL)
        meta = frontmatter(text)
        self.assertEqual(meta["name"], "wrap-up")
        for trigger in ("wrap up", "end of day", "before I leave", "persist my work", "preview", "dry run"):
            self.assertIn(trigger, meta["description"])
        commands = skill_commands(text)
        self.assertGreaterEqual(len(commands), 9, commands)
        parser = build_parser()
        seen, groups = set(), set()
        for line in commands:
            words = shlex.split(re.sub(r"<[^<>]+>", "x", line))[1:]
            with self.subTest(command=line):
                if command_group(parser, words):
                    groups.add(" ".join(words))  # a group named in a sentence, not a command line
                    continue
                buf = io.StringIO()
                try:
                    with contextlib.redirect_stderr(buf), contextlib.redirect_stdout(io.StringIO()):
                        args = parser.parse_args(words)
                except SystemExit:
                    self.fail("%s does not parse: %s" % (line, buf.getvalue().strip()))
                seen.add(" ".join([args.cmd] + ([args.box_action] if args.cmd == "box" else [])))
                # nothing the skill runs deletes data or changes the box's ownership
                for flag in ("take_over", "delete", "yes", "force", "new_session"):
                    self.assertFalse(getattr(args, flag, None), line)
        self.assertEqual(
            seen, {"detect", "box unsent", "box owners", "box send", "box skip", "box push", "sync"}, commands
        )
        self.assertEqual(groups, {"box"}, commands)
        self.assertFalse(command_group(parser, ["box", "unsent"]))
        self.assertFalse(command_group(parser, ["no-such-command"]))

    def test_the_skill_keeps_its_rules(self):
        text = read(SKILL)
        body = text.split("\n---\n", 1)[1]
        for rule in (
            "Never delete files",
            "no `--take-over`",
            "1 GiB",
            "--json",
            "--only <folder>",
            "`preview` or `dry run`",
            "left untracked",
            "uncommitted changes left as they are",
            "**Sent to the box**",
            "**Skipped**",
            "**Committed**",
            "**Pushed to the shared box**",
            "**Left as it is**",
        ):
            self.assertIn(rule, body)
        self.assertNotIn(chr(0x2014), text)  # the em dash


class PluginLinkTest(ToolTest):
    def P(self):
        return ["--project", "demo", "--project-path", self.project]

    def slot(self):
        return self.h(".claude", "skills", "agent-mirror")

    def plan(self, *extra, **kw):
        rc, out = self.run_tool("plan", "--json", *(list(extra) or self.P()), **kw)
        self.assertEqual(rc, 0, out)
        return json.loads(out)

    def doctor_line(self, **kw):
        rc, out = self.run_tool("doctor", "--agent", "claude", **kw)
        self.assertEqual(rc, 0, out)
        lines = [ln for ln in out.splitlines() if "~/.claude/skills/agent-mirror " in ln]
        self.assertEqual(len(lines), 1, out)
        return lines[0]

    def plugin_entries(self):
        found = []
        for path in sorted(glob.glob(self.h(".agent-mirror-backups", "*", "manifest.json"))):
            found += [e for e in load(path)["entries"] if e.get("kind") == pluginlink.ENTRY_KIND]
        return found

    def assertNoSkillUnit(self, data):
        self.assertNotIn("skill:agent-mirror", [u["id"] for u in data["units"]])
        self.assertNotIn("skill:agent-mirror", data["waiting"])
        self.assertFalse([i for i in data["ignored"] if i["path"].endswith("agent-mirror")])

    def assertNeverASkill(self):
        """The link is no skill on a developer machine, nor a vendor skill on a cloud machine."""
        for runtime in ("claude-local", "claude-cloud"):
            data = self.plan(env={"AGENT_RUNTIME": runtime})
            self.assertNoSkillUnit(data)
            places = [p["path"] for u in data["units"] for p in (u.get("machine") or {}).get("places") or []]
            self.assertNotIn("~/.claude/skills/agent-mirror", [p.replace("\\", "/") for p in places], runtime)

    def test_install_links_the_plugin_and_uninstall_removes_it(self):
        data = self.plan()
        self.assertEqual((data["plugin_link"]["state"], data["plugin_link"]["repairable"]), ("missing", True))
        self.assertRegex(
            self.doctor_line(),
            r"^  MISSING +~/.claude/skills/agent-mirror \(install creates it; .*/agent-mirror:wrap-up",
        )
        rc, out = self.install()
        self.assertEqual(rc, 0, out)
        self.assertIn("linked ~/.claude/skills/agent-mirror -> %s" % PLUGIN, out)
        self.assertLinked(self.slot(), PLUGIN)
        entries = self.plugin_entries()
        self.assertEqual([(e["path"], e["target"], e["before"]) for e in entries], [(self.slot(), PLUGIN, "absent")])
        data = self.plan()
        self.assertEqual(data["plugin_link"]["state"], "linked")
        self.assertNeverASkill()
        profiles = glob.glob(self.s("machines", "*.json"))
        self.assertTrue(profiles)
        for path in profiles:
            self.assertFalse([k for k, names in load(path)["skills"].items() if "agent-mirror" in names], path)
        rc, text = self.run_tool("plan", *self.P())
        self.assertIn("plugin link ~/.claude/skills/agent-mirror (Claude Code: /agent-mirror:wrap-up", text)
        self.assertRegex(
            self.doctor_line(), r"^  OK +~/.claude/skills/agent-mirror \((symlink|junction) -> %s; " % re.escape(PLUGIN)
        )
        rc, out = self.install()  # again: nothing to do for it
        self.assertEqual(rc, 0, out)
        self.assertNotIn("linked ~/.claude/skills/agent-mirror", out)
        self.assertEqual(len(self.plugin_entries()), 1)
        rc, out = self.run_tool("uninstall")
        self.assertEqual(rc, 0, out)
        self.assertIn("removed the plugin link ~/.claude/skills/agent-mirror", out)
        self.assertFalse(os.path.lexists(self.slot()))
        self.assertTrue(os.path.isfile(os.path.join(PLUGIN, ".claude-plugin", "plugin.json")))  # never the target

    def test_apply_makes_it_but_not_with_only_or_in_a_dry_run(self):
        rc, out = self.run_tool("apply", "--only", "skill:beta", *self.P())
        self.assertEqual(rc, 0, out)
        self.assertFalse(os.path.lexists(self.slot()))
        rc, out = self.run_tool("apply", "--dry-run", *self.P())
        self.assertEqual(rc, 0, out)
        self.assertIn("would link ~/.claude/skills/agent-mirror -> %s" % PLUGIN, out)
        self.assertFalse(os.path.lexists(self.slot()))
        rc, out = self.run_tool("apply", *self.P())
        self.assertEqual(rc, 0, out)
        self.assertLinked(self.slot(), PLUGIN)

    def test_codex_gets_no_plugin_link(self):
        rc, out = self.run_tool("install", "--agents", "codex", *self.P())
        self.assertEqual(rc, 0, out)
        self.assertFalse(os.path.lexists(self.slot()))
        self.assertIsNone(self.plan("--agents", "codex", *self.P())["plugin_link"])

    def test_a_link_to_another_checkout_is_relinked_and_so_is_a_gone_one(self):
        old_tool = os.path.join(self.base, "oldtool")
        shutil.copytree(PLUGIN, os.path.join(old_tool, "plugin"), ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        old = os.path.join(old_tool, "plugin")
        rc, out = self.run_tool(
            "install", "--agents", "claude,codex", *self.P(), tool=os.path.join(old, "bin", "agent-mirror")
        )
        self.assertEqual(rc, 0, out)
        self.assertLinked(self.slot(), old)
        data = self.plan()
        self.assertEqual((data["plugin_link"]["state"], data["plugin_link"]["repairable"]), ("other-checkout", True))
        self.assertNeverASkill()
        self.assertRegex(self.doctor_line(), r"^  ELSEWHERE +.*another agent-mirror checkout.*install relinks it")
        remove_tree(old_tool)  # the old checkout is gone: a broken link that agent-mirror made
        data = self.plan()
        self.assertEqual(data["plugin_link"]["state"], "stale")
        self.assertNeverASkill()
        rc, out = self.install()
        self.assertEqual(rc, 0, out)
        self.assertIn("relinked ~/.claude/skills/agent-mirror from %s to %s" % (old, PLUGIN), out)
        self.assertLinked(self.slot(), PLUGIN)
        self.assertEqual(self.plugin_entries()[-1]["before_link"], old)
        # a checkout from before the rename (manifest name code-agent-memory-mirror) is one too
        older = fake_checkout(os.path.join(self.base, "oldertool", "plugin"), "code-agent-memory-mirror")
        drop(self.slot())
        dir_link(older, self.slot())
        self.assertEqual(self.plan()["plugin_link"]["state"], "other-checkout")
        self.assertNeverASkill()
        rc, out = self.install()
        self.assertEqual(rc, 0, out)
        self.assertLinked(self.slot(), PLUGIN)

    def test_what_agent_mirror_did_not_make_is_left_alone(self):
        unrelated = os.path.join(self.base, "unrelated", "thing")
        write(os.path.join(unrelated, "SKILL.md"), "someone else's\n")
        os.makedirs(os.path.join(self.base, "unrelated", ".git"))
        gone = os.path.join(self.base, "gone")
        os.makedirs(gone)

        def real_folder():
            write(os.path.join(self.slot(), "SKILL.md"), "---\nname: agent-mirror\ndescription: mine\n---\nmine\n")

        def other_link():
            dir_link(unrelated, self.slot())

        def broken_link():
            dir_link(gone, self.slot())
            os.rmdir(gone)

        def real_file():
            write(self.slot(), "a file\n")

        cases = (
            (real_folder, "folder", "NOT LINKED", "a skill of this machine named agent-mirror"),
            (other_link, "elsewhere", "ELSEWHERE", "which is not the agent-mirror plugin"),
            (broken_link, "broken", "ELSEWHERE", "not made by agent-mirror"),
            (real_file, "file", "NOT LINKED", "a real file"),
        )
        for make, state, label, words in cases:
            with self.subTest(state=state):
                make()
                before = (is_link(self.slot()), readlink(self.slot()) if is_link(self.slot()) else None)
                data = self.plan()
                self.assertEqual((data["plugin_link"]["state"], data["plugin_link"]["repairable"]), (state, False))
                self.assertIn(words, data["plugin_link"]["note"])
                line = self.doctor_line()
                self.assertRegex(line, r"^  %s +~/.claude/skills/agent-mirror \(" % label)
                self.assertIn("left alone, so Claude Code has no /agent-mirror:wrap-up here. To get it:", line)
                rc, out = self.install()
                self.assertEqual(rc, 0, out)
                self.assertIn("~/.claude/skills/agent-mirror is %s" % data["plugin_link"]["note"], out)
                self.assertEqual(
                    (is_link(self.slot()), readlink(self.slot()) if is_link(self.slot()) else None), before
                )
                self.assertFalse(self.plugin_entries())
                drop(self.slot())
        self.assertTrue(os.path.isfile(os.path.join(unrelated, "SKILL.md")))

    def test_a_store_skill_named_agent_mirror_keeps_its_place_and_so_does_the_link(self):
        write(self.s("skills", "agent-mirror", "SKILL.md"), "---\nname: agent-mirror\ndescription: x\n---\nx\n")
        # the name is free: the store skill is linked there as before, and the plugin link says what to do
        rc, out = self.install()
        self.assertEqual(rc, 0, out)
        self.assertLinked(self.slot(), self.s("skills", "agent-mirror"))
        self.assertIn("rename the store's skill (skills/agent-mirror in the store)", out)
        data = self.plan()
        self.assertEqual(data["plugin_link"]["state"], "store-skill")
        self.assertEqual([u["case"] for u in data["units"] if u["id"] == "skill:agent-mirror"], ["linked"])
        text = self.run_tool("doctor", "--agent", "claude")[1]
        lines = [ln for ln in text.splitlines() if "~/.claude/skills/agent-mirror " in ln]
        self.assertEqual(len(lines), 2, text)
        self.assertTrue(lines[0].startswith("  OK "), lines)  # the store skill's own link
        self.assertTrue(lines[1].startswith("  ELSEWHERE "), lines)  # the plugin line says what to do
        self.assertIn("rename the store's skill", lines[1])
        # the other order: the plugin link was there first; the store skill is linked in the shared folder only
        drop(self.slot())
        shutil.move(self.s("skills", "agent-mirror"), os.path.join(self.base, "parked"))
        rc, out = self.install()
        self.assertEqual(rc, 0, out)
        self.assertLinked(self.slot(), PLUGIN)
        shutil.move(os.path.join(self.base, "parked"), self.s("skills", "agent-mirror"))
        data = self.plan()
        self.assertEqual(data["plugin_link"]["state"], "linked")
        unit = [u for u in data["units"] if u["id"] == "skill:agent-mirror"][0]
        self.assertEqual([p["where"] for p in unit["machine"]["places"]], ["shared"])
        self.assertIn("the agent-mirror plugin link holds that name there", unit["note"])
        self.assertFalse(unit["waiting"])
        rc, out = self.install()
        self.assertEqual(rc, 0, out)
        self.assertLinked(self.slot(), PLUGIN)
        self.assertLinked(self.h(".agents", "skills", "agent-mirror"), self.s("skills", "agent-mirror"))
        self.assertRegex(self.doctor_line(), r"^  OK ")  # one line for that path: the plugin link

    def test_a_linked_skills_folder_is_never_written_into(self):
        outside = os.path.join(self.base, "outside-skills")
        shutil.move(self.h(".claude", "skills"), outside)
        dir_link(outside, self.h(".claude", "skills"))
        data = self.plan()
        self.assertEqual(data["plugin_link"]["state"], "folder-link")
        rc, out = self.install()
        self.assertEqual(rc, 0, out)
        self.assertFalse(os.path.lexists(os.path.join(outside, "agent-mirror")))
        self.assertIn("agent-mirror never writes into a linked skills folder", out)


class PluginLinkCloudTest(ToolTest):
    """A cloud session start makes the link on a machine set up before it existed, and never fails over it."""

    def steps(self):
        with open(self.h(".cache", "agent-mirror", "session-start.jsonl"), encoding="utf-8") as fh:
            rec = json.loads([ln for ln in fh.read().splitlines() if ln.strip()][-1])
        return dict((s["name"], s["status"]) for s in rec["steps"])

    def start(self, runtime="claude-cloud"):
        here = ("--project", "demo", "--project-path", self.project, "--box-timeout", "0")
        rc, out = self.run_tool("session-start", *here, env={"AGENT_RUNTIME": runtime})
        self.assertEqual(rc, 0, out)
        return out

    def test_session_start_repairs_the_link(self):
        slot = self.h(".claude", "skills", "agent-mirror")
        out = self.start()
        self.assertEqual(self.steps()["plugin link check"], "ok (linked)")
        self.assertIn("linked ~/.claude/skills/agent-mirror -> %s" % PLUGIN, out)
        self.assertLinked(slot, PLUGIN)
        self.start()
        self.assertEqual(self.steps()["plugin link check"], "ok")
        drop(slot)  # an older snapshot, without the link
        self.start()
        self.assertEqual(self.steps()["plugin link check"], "ok (linked)")
        self.assertLinked(slot, PLUGIN)
        # something else there: left alone, said once, and the session goes on
        drop(slot)
        write(os.path.join(slot, "notes.txt"), "mine\n")
        out = self.start()
        steps = self.steps()
        self.assertEqual((steps["plugin link check"], steps["linking"]), ("skipped (folder)", "ok"))
        self.assertEqual(out.count("~/.claude/skills/agent-mirror is a real folder; left alone"), 1, out)
        self.assertEqual(read(os.path.join(slot, "notes.txt")), "mine\n")

    def test_codex_cloud_has_no_plugin_link(self):
        self.start("codex-cloud")
        self.assertNotIn("plugin link check", self.steps())
        self.assertFalse(os.path.lexists(self.h(".claude", "skills", "agent-mirror")))


class InProcessHome:
    """A sandboxed home, store and project for tests that call agent-mirror in this process."""

    def setUp(self):
        self.base = os.path.realpath(tempfile.mkdtemp(prefix="agent-mirror-pl-links-"))
        self.addCleanup(remove_tree, self.base)
        project = os.path.join(self.base, "work", "demo")
        os.makedirs(project)
        self.store = make_store(self.base)
        self.home = make_home(self.base, project)
        self.plat = Platform(None, {"AGENT_MIRROR_HOME": self.home})
        self.addCleanup(reactivate_sandbox)
        self.ctx = Context(store_flag=self.store, plat=self.plat)
        self.slot = os.path.join(self.home, ".claude", "skills", "agent-mirror")
        # runs before the tree is removed: the link leads to this repo's plugin folder, which must stay
        self.addCleanup(lambda: remove_link(self.slot) if is_link(self.slot) else None)


class PluginLinkRepairTest(InProcessHome, unittest.TestCase):
    """The cloud session start's step: logged, and never failing the session, whatever goes wrong."""

    def repair(self, dry_run=False):
        log = StepLog(self.plat, "session-start", "claude-cloud", env={})
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            pluginlink.repair(self.ctx, "claude-cloud", dry_run, log)
        self.assertEqual([s["name"] for s in log.steps], ["plugin link check"])
        return log.steps[0]["status"], buf.getvalue()

    def test_dry_run_changes_nothing(self):
        status, out = self.repair(dry_run=True)
        self.assertEqual(status, "skipped (dry run)")
        self.assertIn("would link ~/.claude/skills/agent-mirror -> %s" % PLUGIN, out)
        self.assertFalse(os.path.lexists(self.slot))

    def test_a_link_that_cannot_be_made_is_reported_and_the_session_goes_on(self):
        with mock.patch.object(pluginlink, "make_link", side_effect=OSError("links are not allowed here")):
            status, out = self.repair()
        self.assertEqual(status, "failed (could not link)")
        self.assertIn("links are not allowed here (a copy would go stale, so none is made)", out)
        self.assertFalse(os.path.lexists(self.slot))
        self.assertFalse(glob.glob(os.path.join(self.home, ".agent-mirror-backups", "*", "manifest.json")))
        status, out = self.repair()  # the next session start makes it
        self.assertEqual(status, "ok (linked)")
        self.assertEqual(os.path.normcase(readlink(self.slot)), os.path.normcase(PLUGIN))

    def test_an_error_inside_the_step_never_escapes(self):
        with mock.patch.object(pluginlink, "inspect", side_effect=RuntimeError("unexpected")):
            status, out = self.repair()
        self.assertEqual(status, "error")
        self.assertIn("warning: plugin link check failed: unexpected; the session goes on", out)


class PluginLinkFallbackTest(InProcessHome, unittest.TestCase):
    """Windows without symlink rights: a junction, never a copy (a copy of the plugin would go stale)."""

    @staticmethod
    def failing_symlink(*args, **kwargs):
        raise OSError(1314, "A required privilege is not held by the client")

    def apply(self, junction):
        inst = Installer(self.ctx, False, symlink=self.failing_symlink, junction=junction, fallback=True)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            rc, _ = apply_plan(self.ctx, Plan(self.ctx, ["claude"], []), False, "install", installer=inst)
        return rc, buf.getvalue()

    def test_a_junction_when_symlinks_fail(self):
        # on Windows the real junction code runs; elsewhere a symlink stands in for it
        junction = None if os.name == "nt" else (lambda link, target: os.symlink(target, link))  # noqa: E731
        rc, out = self.apply(junction)
        self.assertEqual(rc, 0, out)
        self.assertIn("linked ~/.claude/skills/agent-mirror -> %s (junction)" % PLUGIN, out)
        self.assertEqual(os.path.normcase(readlink(self.slot)), os.path.normcase(PLUGIN))
        manifest = glob.glob(os.path.join(self.home, ".agent-mirror-backups", "*", "manifest.json"))
        entries = [e for e in load(manifest[0])["entries"] if e.get("kind") == pluginlink.ENTRY_KIND]
        self.assertEqual([e["method"] for e in entries], ["junction"])
        self.assertEqual(pluginlink.plan_info(self.plat, self.ctx.require_store())["state"], "linked")

    def test_no_copy_when_no_link_can_be_made(self):
        def failing_junction(link, target):
            raise OSError("mklink /J failed: the volume does not support junctions")

        rc, out = self.apply(failing_junction)
        self.assertEqual(rc, 1, out)
        self.assertIn("a copy would go stale, so none is made", out)
        self.assertFalse(os.path.lexists(self.slot))


if __name__ == "__main__":
    unittest.main()
