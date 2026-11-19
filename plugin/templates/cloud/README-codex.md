# Codex cloud: settings for @@PROJECT@@

Rendered by `agent-mirror cloud render --project @@PROJECT@@` from
`projects/@@PROJECT@@/env.json`. Re-render after changing that file to update this README, the host list
and the variable names; the setup scripts stay the same. Do not edit these files by hand.
Items marked UNVERIFIED have not been proven on a real cloud machine; `agent-mirror probe --full` checks them.
The same steps are in `runbook.json` (machine-readable) and `agent-mirror cloud runbook --project @@PROJECT@@ --vendor codex`.

There are two Codex cloud products: the newer VM product and the legacy container product. Both are covered here.

@@STEPS@@

## What the scripts do

The setup scripts are fixed bootstraps: paste each once. The only project-specific text in them is the
project name, the store URL, the agent-mirror URL and the runtime marker.

- Read `tool.ref` from `store.json` on the store's default branch (now `@@TOOL_REF@@`), check agent-mirror
  (`@@TOOL_REPO@@`) out at that ref to `~/code-agent-memory-mirror`, and link it onto PATH.
- Run `agent-mirror cloud bootstrap --project @@PROJECT@@ --vendor codex`, which reads
  `projects/@@PROJECT@@/env.json` from the store's default branch and, with the settings as they are now:
@@COMPANIONS_LINE@@
@@PYTHON_LINE@@
@@SETUP_LINE@@
  - runs `agent-mirror init-store --adopt` and `install --agents codex`. Codex has no built-in memory
    folder: it reads `memory/<project>/MEMORY.md` in the store directly, as `AGENTS.md` says;
  - records a stamp of those setup inputs on the machine, never in the store.
- Write timings to `~/agent_setup_probe.txt`, and always exit 0.
- A later change to `python`, `setup_commands`, `companion_repos` or `tool.ref` takes effect at the next
  `session-start` (drift repair: the setup steps run again for a project whose inputs changed). A cached
  machine keeps its old setup, so the repair repeats at every session start until the vendor rebuilds it:
  raise the number on the `# revision:` line and save to make it rebuild. If `store.json` pins an
  agent-mirror older than 0.5.2 (no `cloud bootstrap`), the scripts only link the store.
- Codex has no stop hook here: nothing checks for unsent files when a turn ends. The store's `AGENTS.md`
  tells the agent to run `agent-mirror box unsent` before finishing, to commit the small untracked files it
  leaves to git as usual, and to send (`box send`) or leave behind (`box skip`) each file it lists for the box.
- Every `agent-mirror session-start` ends with each project's newest box sessions and their notes, and the
  shared folders that have notes, within `--box-timeout` (20 s by default). `box send` in a cloud session
  needs `--note "<what these files are and why>"`; pin agent-mirror 0.5.3 or later in `store.json`, since
  the first-session prompt uses `--note` (`cloud render` warns).
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
