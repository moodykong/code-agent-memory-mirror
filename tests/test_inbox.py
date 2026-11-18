"""Cloud pushes go to inbox branches; a developer sync applies memory and holds the rest.

Real git in temp folders: a bare remote, a "developer" clone and one or more "cloud" clones.
"""

import json
import os
import unittest

from helpers import setUpModule  # noqa: F401 (the sandbox guard runs first)
from helpers import SHARED_BODY, STORE_INDEX, SYMLINKS, ToolTest, read, write


class InboxBase(ToolTest):
    def setUp(self):
        super().setUp()
        meta = json.loads(read(self.s("store.json")))
        self.assertEqual(meta.get("cloud_push"), "inbox")  # the template default
        self.init_git()  # self.store is the developer clone, self.origin the bare remote
        self.dev = self.store
        self.clouds = 0

    def cloud_clone(self):
        self.clouds += 1
        path = os.path.join(self.base, "cloud%d" % self.clouds)
        self.git("clone", "-q", self.origin, path, cwd=self.base)
        return path

    def sync(self, store, runtime, *extra):
        env = {"CLAUDECODE": "1"} if runtime == "local" else {"AGENT_RUNTIME": runtime}
        return self.run_tool("--store", store, "sync", *extra, env=env, with_store=False)

    def remote_refs(self):
        refs = self.git("--git-dir", self.origin, "for-each-ref", "--format=%(refname)", cwd=self.base)
        return refs.splitlines()

    def inbox_refs(self):
        return [r for r in self.remote_refs() if r.startswith("refs/heads/inbox/")]

    def remote_main(self):
        return self.git("--git-dir", self.origin, "rev-parse", "refs/heads/main", cwd=self.base)


class InboxTest(InboxBase):
    def test_cloud_sync_pushes_to_inbox_and_never_to_main(self):
        main_before = self.remote_main()
        cloud = self.cloud_clone()
        write(os.path.join(cloud, "memory", "demo", "cloud_fact.md"), "fact from the cloud\n")
        rc, out = self.sync(cloud, "claude-cloud")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.remote_main(), main_before)
        refs = self.inbox_refs()
        self.assertEqual(len(refs), 1, refs)
        self.assertRegex(refs[0], r"^refs/heads/inbox/claude-cloud-[0-9a-f]{10}$")
        self.assertIn("never pushes to main", out)
        rc, out = self.sync(cloud, "claude-cloud")  # the same machine reuses its branch
        self.assertEqual(self.inbox_refs(), refs)

    def test_developer_sync_applies_memory_holds_the_rest(self):
        cloud = self.cloud_clone()
        write(os.path.join(cloud, "memory", "demo", "MEMORY.md"), STORE_INDEX + "- [Cloud](cloud.md): from the cloud\n")
        write(os.path.join(cloud, "memory", "demo", "cloud.md"), "cloud memory\n")
        write(
            os.path.join(cloud, "AGENTS.md"),
            read(os.path.join(cloud, "AGENTS.md")) + "- an instruction from a cloud session\n",
        )
        write(os.path.join(cloud, "skills", "alpha", "SKILL.md"), "changed in the cloud\n")
        self.assertEqual(self.sync(cloud, "codex-cloud")[0], 0)
        agents_before = read(self.s("AGENTS.md"))
        write(self.s("memory", "demo", "MEMORY.md"), STORE_INDEX + "- [Dev](dev.md): from the developer\n")
        rc, out = self.sync(self.dev, "local")
        self.assertEqual(rc, 0, out)
        index = read(self.s("memory", "demo", "MEMORY.md"))
        self.assertIn("from the cloud", index)
        self.assertIn("from the developer", index)
        self.assertEqual(index.count("[Shared]"), 1)
        self.assertEqual(read(self.s("memory", "demo", "cloud.md")), "cloud memory\n")
        self.assertEqual(read(self.s("AGENTS.md")), agents_before)
        self.assertNotEqual(read(self.s("skills", "alpha", "SKILL.md")), "changed in the cloud\n")
        self.assertIn("held for review (not applied): AGENTS.md, skills/alpha/SKILL.md", out)
        self.assertEqual(len(self.inbox_refs()), 1)  # kept: it has held changes
        self.assertEqual(self.remote_main(), self.git("rev-parse", "HEAD"))
        rc, out = self.run_tool("--store", self.dev, "doctor", env={"CLAUDECODE": "1"}, with_store=False)
        self.assertIn("HELD", out)
        self.assertIn("AGENTS.md, skills/alpha/SKILL.md", out)
        # a second sync changes nothing more and keeps the branch
        head = self.git("rev-parse", "HEAD")
        rc, out = self.sync(self.dev, "local")
        self.assertEqual(self.git("rev-parse", "HEAD"), head)
        self.assertEqual(read(self.s("memory", "demo", "MEMORY.md")), index)

    def test_memory_only_branch_is_deleted_after_apply(self):
        cloud = self.cloud_clone()
        write(os.path.join(cloud, "memory", "demo", "new.md"), "new memory\n")
        self.sync(cloud, "claude-cloud")
        self.assertEqual(len(self.inbox_refs()), 1)
        rc, out = self.sync(self.dev, "local")
        self.assertEqual(rc, 0, out)
        self.assertEqual(read(self.s("memory", "demo", "new.md")), "new memory\n")
        self.assertEqual(self.inbox_refs(), [])
        self.assertIn("everything on it was applied", out)
        self.assertIn(
            "memory/demo/new.md",
            self.git("--git-dir", self.origin, "ls-tree", "-r", "--name-only", "main", cwd=self.base),
        )

    def test_conflict_keeps_main_and_writes_a_side_file(self):
        cloud = self.cloud_clone()
        write(os.path.join(cloud, "memory", "demo", "shared.md"), SHARED_BODY + "cloud version\n")
        self.sync(cloud, "claude-cloud")
        branch = self.inbox_refs()[0][len("refs/heads/inbox/") :]
        write(self.s("memory", "demo", "shared.md"), SHARED_BODY + "developer version\n")
        rc, out = self.sync(self.dev, "local")
        self.assertEqual(rc, 0, out)
        self.assertEqual(read(self.s("memory", "demo", "shared.md")), SHARED_BODY + "developer version\n")
        side = self.s("memory", "demo", "shared.from-%s.md" % branch)
        self.assertEqual(read(side), SHARED_BODY + "cloud version\n")
        self.assertNotIn("shared.from-", read(self.s("memory", "demo", "MEMORY.md")))
        self.assertIn("changed on both sides", out)

    def test_secret_in_a_branch_blocks_it(self):
        cloud = self.cloud_clone()
        write(os.path.join(cloud, "memory", "demo", "ok.md"), "fine\n")
        key = "s" + "k-" + "Ab3" * 8
        write(os.path.join(cloud, "memory", "demo", "leak.md"), "key " + key + "\n")
        self.git("add", "-A", cwd=cloud)
        self.git("commit", "-q", "-m", "committed by hand, past the local scan", cwd=cloud)
        self.sync(cloud, "claude-cloud")
        rc, out = self.sync(self.dev, "local")
        self.assertEqual(rc, 0, out)
        self.assertIn("the secret scan blocked it; nothing applied", out)
        self.assertNotIn(key, out)
        self.assertFalse(os.path.exists(self.s("memory", "demo", "ok.md")))
        self.assertFalse(os.path.exists(self.s("memory", "demo", "leak.md")))
        self.assertEqual(len(self.inbox_refs()), 1)

    def test_review_accept_and_discard(self):
        cloud = self.cloud_clone()
        write(os.path.join(cloud, "AGENTS.md"), read(os.path.join(cloud, "AGENTS.md")) + "- reviewed instruction\n")
        self.sync(cloud, "claude-cloud")
        other = self.cloud_clone()
        write(os.path.join(other, "skills", "beta", "SKILL.md"), "unwanted change\n")
        self.sync(other, "codex-cloud")
        names = [r[len("refs/heads/") :] for r in self.inbox_refs()]
        self.assertEqual(len(names), 2)
        local = {"CLAUDECODE": "1"}
        rc, out = self.run_tool("--store", self.dev, "review", "list", env=local, with_store=False)
        self.assertEqual(rc, 0, out)
        self.assertIn("held for review: AGENTS.md", out)
        claude_branch = [n for n in names if "claude-cloud" in n][0]
        codex_branch = [n for n in names if "codex-cloud" in n][0]
        rc, out = self.run_tool("--store", self.dev, "review", "show", claude_branch, env=local, with_store=False)
        self.assertIn("+- reviewed instruction", out)
        rc, out = self.run_tool("--store", self.dev, "review", "accept", claude_branch, env=local, with_store=False)
        self.assertEqual(rc, 0, out)
        self.assertIn("- reviewed instruction", read(self.s("AGENTS.md")))
        self.assertEqual(self.remote_main(), self.git("rev-parse", "HEAD"))
        rc, out = self.run_tool(
            "--store", self.dev, "review", "discard", codex_branch.split("/", 1)[1], env=local, with_store=False
        )
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.inbox_refs(), [])
        self.assertNotEqual(read(self.s("skills", "beta", "SKILL.md")), "unwanted change\n")
        rc, out = self.run_tool(
            "--store", self.dev, "review", "list", env={"AGENT_RUNTIME": "claude-cloud"}, with_store=False
        )
        self.assertEqual(rc, 2, out)

    def test_cloud_push_main_keeps_the_old_behaviour(self):
        meta = json.loads(read(self.s("store.json")))
        meta["cloud_push"] = "main"
        write(self.s("store.json"), json.dumps(meta))
        self.git("commit", "-q", "-am", "push to main from the cloud")
        self.git("push", "-q")
        cloud = self.cloud_clone()
        write(os.path.join(cloud, "memory", "demo", "direct.md"), "direct\n")
        rc, out = self.sync(cloud, "claude-cloud")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.inbox_refs(), [])
        self.assertEqual(self.remote_main(), self.git("rev-parse", "HEAD", cwd=cloud))

    def test_cloud_session_start_rebases_its_unpushed_commits(self):
        cloud = self.cloud_clone()
        write(os.path.join(cloud, "memory", "demo", "mine.md"), "mine\n")
        self.sync(cloud, "claude-cloud")
        write(self.s("memory", "demo", "later.md"), "later on main\n")
        self.sync(self.dev, "local")
        rc, out = self.run_tool(
            "--store",
            cloud,
            "session-start",
            "--project",
            "demo",
            "--project-path",
            self.project,
            env={"AGENT_RUNTIME": "claude-cloud"},
            with_store=False,
        )
        self.assertEqual(rc, 0, out)
        self.assertTrue(os.path.isfile(os.path.join(cloud, "memory", "demo", "later.md")))
        self.assertTrue(os.path.isfile(os.path.join(cloud, "memory", "demo", "mine.md")))


class CloudSetupInputsTest(InboxBase):
    """Cloud setup (`cloud bootstrap`, drift repair) runs env.json, projects/*/cloud and store.json from the
    default branch as root, so a developer sync must never apply them from an inbox branch, by any path."""

    WATCHED = ("projects/demo/env.json", "projects/demo/cloud/claude/setup.sh", "store.json")

    def push_branch(self, cloud, name):
        self.git("add", "-A", cwd=cloud)
        self.git("commit", "-q", "-m", "from a cloud session", cwd=cloud)
        self.git("push", "-q", "origin", "HEAD:refs/heads/inbox/" + name, cwd=cloud)

    def watched(self):
        """The watched paths on the developer clone and on the remote main branch."""
        here = dict(
            (p, read(self.s(*p.split("/"))) if os.path.isfile(self.s(*p.split("/"))) else None) for p in self.WATCHED
        )
        listed = self.git("--git-dir", self.origin, "ls-tree", "-r", "--name-only", "main", cwd=self.base).splitlines()
        there = dict(
            (p, self.git("--git-dir", self.origin, "show", "main:" + p, cwd=self.base) if p in listed else None)
            for p in self.WATCHED
        )
        return here, there

    def test_setup_inputs_on_an_inbox_branch_are_held(self):
        before = self.watched()
        main_before = self.remote_main()
        cloud = self.cloud_clone()
        env = json.loads(read(os.path.join(cloud, "projects", "demo", "env.json")))
        env["setup_commands"] = ["echo planted"]
        write(os.path.join(cloud, "projects", "demo", "env.json"), json.dumps(env))
        write(os.path.join(cloud, "projects", "demo", "cloud", "claude", "setup.sh"), "#!/bin/bash\necho planted\n")
        meta = json.loads(read(os.path.join(cloud, "store.json")))
        meta["tool"] = {"repo": "https://github.com/example-org/other-tool", "ref": "planted"}
        write(os.path.join(cloud, "store.json"), json.dumps(meta))
        write(os.path.join(cloud, "memory", "demo", "note.md"), "a cloud note\n")
        self.push_branch(cloud, "claude-cloud-inputs")
        # a rename of a setup input into memory/ is a deletion outside memory/ (held) and a new memory file
        cloud2 = self.cloud_clone()
        os.makedirs(os.path.join(cloud2, "memory", "demo", "moved"))
        self.git("mv", "projects/demo/env.json", "memory/demo/moved/env.json", cwd=cloud2)
        self.push_branch(cloud2, "claude-cloud-rename")
        rc, out = self.sync(self.dev, "local")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.watched(), before)  # on this clone and on the remote main branch
        self.assertEqual(read(self.s("memory", "demo", "note.md")), "a cloud note\n")  # memory is applied
        self.assertIn(
            "held for review (not applied): projects/demo/cloud/claude/setup.sh, projects/demo/env.json, store.json",
            out,
        )
        self.assertIn("claude-cloud-rename: held for review (not applied): projects/demo/env.json", out)
        self.assertNotEqual(self.remote_main(), main_before)  # the memory changes were pushed
        self.assertEqual(len(self.inbox_refs()), 2)  # both kept: they hold changes for review

    def test_memory_path_rule(self):
        from agent_mirror.inbox import is_memory_path

        for path in ("memory/demo/a.md", "memory/demo/sub/MEMORY.md"):
            self.assertTrue(is_memory_path(path), path)
        for path in (
            "memory",
            "memory/",
            "Memory/demo/a.md",
            "memory/../projects/demo/env.json",
            "memory/demo/../../store.json",
            "memory/./demo/a.md",
            "memory//demo/a.md",
            "memory/demo\\..\\..\\store.json",
            "memory/c:/x.md",
            "projects/demo/env.json",
            "projects/demo/cloud/claude/setup.sh",
            "store.json",
            "./memory/demo/a.md",
        ):
            self.assertFalse(is_memory_path(path), path)

    @unittest.skipUnless(SYMLINKS, "needs symlinks")
    def test_links_never_carry_a_memory_change_outside_memory(self):
        env_before = read(self.s("projects", "demo", "env.json"))
        # a link committed on the branch becomes a plain file holding the link text, never a link
        cloud = self.cloud_clone()
        os.symlink(os.path.join("..", "..", "projects"), os.path.join(cloud, "memory", "demo", "planted"))
        self.push_branch(cloud, "claude-cloud-link")
        rc, out = self.sync(self.dev, "local")
        self.assertEqual(rc, 0, out)
        planted = self.s("memory", "demo", "planted")
        self.assertFalse(os.path.islink(planted))
        self.assertEqual(read(self.s("projects", "demo", "env.json")), env_before)
        # a link in this machine's working tree: a memory path through it resolves outside memory/, blocked
        os.symlink(os.path.join("..", "..", "projects", "demo"), self.s("memory", "demo", "through"))
        cloud = self.cloud_clone()
        write(os.path.join(cloud, "memory", "demo", "through", "env.json"), '{"setup_commands": ["planted"]}\n')
        self.push_branch(cloud, "claude-cloud-through")
        rc, out = self.sync(self.dev, "local")
        self.assertEqual(rc, 0, out)
        self.assertIn("resolves outside memory/", out)
        self.assertEqual(read(self.s("projects", "demo", "env.json")), env_before)
        self.assertIn("refs/heads/inbox/claude-cloud-through", self.inbox_refs())


if __name__ == "__main__":
    unittest.main()
