# Codex cloud: settings for @@PROJECT@@

Rendered by `agent-mirror cloud render --project @@PROJECT@@` from
`projects/@@PROJECT@@/env.json`. Re-render after changing that file; do not edit these files by hand.
Items marked UNVERIFIED have not been proven on a real cloud machine; `agent-mirror probe --full` checks them.
The same steps are in `runbook.json` (machine-readable) and `agent-mirror cloud runbook --project @@PROJECT@@ --vendor codex`.

There are two Codex cloud products: the newer VM product and the legacy container product. Both are covered here.

@@STEPS@@

## What the scripts do

- Check out agent-mirror (`@@TOOL_REPO@@`) at the ref the store pins (`@@TOOL_REF@@`) to
  `~/code-agent-memory-mirror`, and link it onto PATH.
@@PYTHON_LINE@@
@@SETUP_LINE@@
- Run `agent-mirror init-store --adopt` and `install --agents codex`. Codex has no built-in
  memory folder: it reads `memory/<project>/MEMORY.md` in the store directly, as `AGENTS.md` says.
- Write timings to `~/agent_setup_probe.txt`, and always exit 0.
- UNVERIFIED: whether `agent-mirror sync` can push from the VM product with the access Codex used to check out
  the store. In the legacy container product the script removes the token from the store clone's remote, so
  `sync` cannot push from the agent phase (it keeps the commit local, which is lost with the container); ask
  the agent to paste any new memory file into its final message.
- Several repos in one session: when the session starts in a folder above several clones,
  `agent-mirror session-start` scans the child folders (depth 1 and 2) for git repos, matches each origin URL
  to a project's `repo` in the store, and sets up every match. Repos without a project entry are listed and
  left alone. Claude keys memory to the folder the session runs in: with exactly one matching project that
  folder's memory is linked too; with several, the agent is given each project's memory index path to read
  (UNVERIFIED how Claude picks the folder in that case).

@@INBOX_NOTE@@
@@BOX_NOTE@@
