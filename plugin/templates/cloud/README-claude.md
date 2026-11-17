# Claude Code on the web: settings for @@PROJECT@@

Rendered by `agent-mirror cloud render --project @@PROJECT@@` from
`projects/@@PROJECT@@/env.json`. Re-render after changing that file; do not edit these files by hand.
Items marked UNVERIFIED have not been proven on a real cloud machine; `agent-mirror probe --full` checks them.

## Steps

1. Make a fine-grained GitHub token that can read and write ONLY the store repo
   (`@@STORE_REPO_PATH@@`, Contents: read and write, so `agent-mirror sync` can push).
   The token ends up in the store clone's `.git/config` on the throwaway machine, where the
   agent can read it. That is why it must be limited to the store repo.
2. Open the environment for `@@PROJECT_REPO@@` on claude.ai/code.
3. Setup script: paste `setup.sh`, then replace `<STORE_REPO_TOKEN>` with the token
   on the settings page. Never put the token in the store.
4. Network: allow every host in `allowed-hosts.txt`.
5. Environment variables: add every name in `env-vars.txt` and fill in the values on the
   settings page. The store only ever holds the names.
6. Start a session and ask the agent to run `agent-mirror probe --full` and show the output.

## What the setup script does

- Clones the store to `~/agent-mirror-store` with the token.
- Checks out agent-mirror (`@@TOOL_REPO@@`) at the ref the store pins in `store.json`
  (`@@TOOL_REF@@`) to `~/code-agent-memory-mirror`, and links it onto PATH.
@@PYTHON_LINE@@
@@SETUP_LINE@@
- Runs `agent-mirror init-store --adopt` (writes the user config), `install --agents claude`
  and `hooks install` (session start, memory write and session end hooks in `~/.claude/settings.json`).
- Writes timings to `~/agent_setup_probe.txt`, and always exits 0.

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
@@BOX_NOTE@@
