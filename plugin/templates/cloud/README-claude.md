# Claude Code on the web: settings for @@PROJECT@@

Rendered by `agent-mirror cloud render --project @@PROJECT@@` from
`projects/@@PROJECT@@/env.json`. Re-render after changing that file to update this README, the host list
and the variable names; the setup script stays the same. Do not edit these files by hand.
Items marked UNVERIFIED have not been proven on a real cloud machine; `agent-mirror probe --full` checks them.
The same steps are in `runbook.json` (machine-readable) and `agent-mirror cloud runbook --project @@PROJECT@@`.

@@STEPS@@

## What the setup script does

`setup.sh` is a fixed bootstrap: paste it once per environment. The only project-specific text in it is
the project name, the store URL, the agent-mirror URL and the runtime marker, so changing the project
settings never means pasting it again.

- Clones the store to `~/agent-mirror-store` with the token. A read-only token is enough: it is used only
  here, before any session exists. Inside a session, pulls and the push of memory to an inbox branch go
  through the vendor's GitHub access for the repos you select, so select the store in every session; see
  "Inbox" below.
- Reads `tool.ref` from `store.json` on the store's default branch (now `@@TOOL_REF@@`), checks agent-mirror
  (`@@TOOL_REPO@@`) out at that ref to `~/code-agent-memory-mirror`, and links it onto PATH.
- Runs `agent-mirror cloud bootstrap --project @@PROJECT@@ --vendor claude`, which reads
  `projects/@@PROJECT@@/env.json` from the store's default branch (never an inbox branch) and, with the
  settings as they are now:
@@COMPANIONS_LINE@@
@@PYTHON_LINE@@
@@SETUP_LINE@@
  - runs `agent-mirror init-store --adopt` (writes the user config), `install --agents claude` and
    `hooks install` (session start, memory write and session end hooks in `~/.claude/settings.json`);
  - records a stamp of those setup inputs and the agent-mirror commit on the machine
    (`~/.config/agent-mirror/bootstrap-stamp.json`, never in the store).
- Writes timings to `~/agent_setup_probe.txt`, and always exits 0.

## When the settings change

- A change to `python`, `setup_commands` or `companion_repos` in env.json, or to `tool.ref` in store.json,
  takes effect at the next session start: `session-start` pulls the store, moves agent-mirror to the pinned
  ref, compares the stamp with the default branch and runs the setup steps again for a project whose inputs
  changed (drift repair), within `--repair-timeout` (900 s by default). The steps must be safe to run twice
  (guard installs with `command -v`). A failed step is reported and the session goes on. Commit and push
  the change to the store's default branch.
- Each new session starts from the vendor's cached machine, which was set up with the old settings, so the
  repair repeats at every session start until the vendor rebuilds the machine. The vendor keeps its cached
  machine as long as the setup script is unchanged: to make it rebuild the machine with the new settings,
  raise the number on the `# revision:` line of the pasted script and save.
- If `store.json` pins an agent-mirror older than 0.5.2 (no `cloud bootstrap`), the script only links the
  store and installs the hooks; `cloud render` warns about it.
- A changed store URL, a changed `tool.repo` or a different project needs a new paste. New hosts and new
  variable names (re-render to see them) still go on the settings page.
- `agent-mirror probe` shows the last session start's steps with their times and exit status.

## Notes

- `CLAUDE_CODE_REMOTE=true` already tells agent-mirror it runs in `claude-cloud`; do not set `AGENT_RUNTIME`.
- UNVERIFIED: whether Claude Code in the cloud honours a user-level `~/.claude/settings.json`
  written by the setup script, and whether `$HOME` in the setup script is the session's home.
  If the session-start lines do not appear at the start of a session, ask the agent to run
  `agent-mirror session-start` and read the memory index it prints.
- UNVERIFIED: Claude may load its memory folder before the hook links it, so the first session
  after setup may not see the memory by itself; the printed index path still lets the agent read it.
- Several repos in one session: when the session starts in a folder above several clones,
  `agent-mirror session-start` scans the child folders (depth 1 and 2) for git repos, matches each origin URL
  to a project's `repo` in the store, and sets up every match. Repos without a project entry are listed and
  left alone. Claude keys memory to the folder the session runs in: with exactly one matching project that
  folder's memory is linked too; with several, the agent is given each project's memory index path to read
  (UNVERIFIED how Claude picks the folder in that case).

@@INBOX_NOTE@@
@@BOX_NOTE@@
