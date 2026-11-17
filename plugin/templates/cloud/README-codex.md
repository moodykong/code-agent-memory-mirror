# Codex cloud: settings for @@PROJECT@@

Rendered by `agent-mirror cloud render --project @@PROJECT@@` from
`projects/@@PROJECT@@/env.json`. Re-render after changing that file; do not edit these files by hand.
Items marked UNVERIFIED have not been proven on a real cloud machine; `agent-mirror probe --full` checks them.

There are two Codex cloud products. Both are covered here.

## Both products

- Environment variables: add every name in `env-vars.txt`. `AGENT_RUNTIME=codex-cloud` is
  required (Codex local and cloud are otherwise hard to tell apart); fill in the other values on
  the settings page. The store only ever holds the names.
- Network: allow every host in `allowed-hosts.txt`.
- First task: ask Codex to run `agent-mirror probe --full` and show the output.

## Newer VM product

1. Select BOTH repos in the environment: `@@PROJECT_REPO@@` and the store `@@STORE_REPO_PATH@@`.
   Codex checks out the store with its own access, so no token is needed.
2. Install script: paste `vm-install.sh`. It looks for the store checkout by its folder name
   (`@@STORE_NAME@@`). UNVERIFIED: where Codex puts the second repo; if the search fails, set
   `AGENT_MIRROR_STORE` on the settings page to that folder.
3. Start step: add `vm-start-skill.md` as the environment's start skill (UNVERIFIED: how the
   product names and triggers a start skill). It runs `agent-mirror session-start` and reads
   the memory index.
4. UNVERIFIED: whether `agent-mirror sync` can push from the VM with the access Codex used to
   check out the store.

## Legacy container product

1. Select only `@@PROJECT_REPO@@`.
2. Make a fine-grained token for the store repo (read and write, that repo only) and store it
   as a setup-only secret named `STORE_REPO_TOKEN`.
3. Setup script: paste `container-setup.sh`. Maintenance script: paste `container-maintenance.sh`.
4. Secrets exist only while the setup script runs, and the script removes the token from the
   store clone's remote, so `agent-mirror sync` cannot push from the agent phase (it warns and
   keeps the commit local, which is lost with the container). Ask the agent to paste any new
   memory file into its final message.
5. UNVERIFIED: internet access in the agent phase (off by default in this product).

## What the scripts do

- Check out agent-mirror (`@@TOOL_REPO@@`) at the ref the store pins (`@@TOOL_REF@@`) to
  `~/code-agent-memory-mirror`, and link it onto PATH.
@@PYTHON_LINE@@
@@SETUP_LINE@@
- Run `agent-mirror init-store --adopt` and `install --agents codex`. Codex has no built-in
  memory folder: it reads `memory/<project>/MEMORY.md` in the store directly, as `AGENTS.md` says.
- Write timings to `~/agent_setup_probe.txt`, and always exit 0.
- Several repos in one session: when the session starts in a folder above several clones,
  `agent-mirror session-start` scans the child folders (depth 1 and 2) for git repos, matches each origin URL
  to a project's `repo` in the store, and sets up every match. Repos without a project entry are listed and
  left alone. Claude keys memory to the folder the session runs in: with exactly one matching project that
  folder's memory is linked too; with several, the agent is given each project's memory index path to read
  (UNVERIFIED how Claude picks the folder in that case).
@@BOX_NOTE@@
