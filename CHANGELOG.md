# Changelog

## 0.2.0

- Fix after release: the Windows and WSL clipboard copy left an invisible byte order mark before the first line; it now copies through PowerShell. `clip copy --secret FILE` works in either argument order. `clip clear` works on Windows.

- Secret recipes: `"secrets"` in a project's env.json names where each environment value comes from
  (`rclone-remote`, `local-file`, `env`, `literal`, `azure-keyvault`, `ask-user`), never the value. Other
  recipe types are rejected; no command from the store is ever run. `doctor` and `cloud render` warn about
  variables no recipe covers.
- `agent-mirror cloud secrets --check | --out FILE [--replace]`: produce the values on this machine, write
  them to an owner-only file outside the store and any git work tree, print names and lengths only.
- `cloud render` also writes `runbook.json` per vendor (network hosts, env values with their recipes, setup
  script placeholders, first-session prompt, cleanup); the README is generated from it.
  `agent-mirror cloud runbook [--json]` prints it.
- Inbox branches: with `"cloud_push": "inbox"` (the default for new and adopted stores) cloud sessions push
  to `inbox/<runtime>-<id>`, never to the main branch. A developer machine's `sync` applies memory changes
  (union-merged indexes, side files on conflicts, secret scan) and holds everything else for
  `agent-mirror review list | show | accept | discard`. `doctor` reports held branches. Cloud
  `session-start` pulls main with a rebase.
- `agent-mirror clip copy | check | clear`: clipboard hand-off without printing secrets (Windows and WSL,
  macOS, Wayland, X11).
- README: "Join a second machine".

## 0.1.0

First public version.

- `agent-mirror`, a Python 3.8+ standard-library program with a POSIX launcher (`plugin/bin/agent-mirror`)
  and a Windows launcher (`plugin/bin/agent-mirror.cmd`).
- The store is a git repo the user owns, found from `--store`, `AGENT_MIRROR_STORE` or the user config
  file. `init-store` makes a new one from `plugin/templates/store` or adopts an existing folder.
- Commands: `detect`, `init-store`, `install`, `uninstall`, `doctor`, `session-start`, `sync`, `probe`
  (and `probe --full`), `hook memory-write`, `hooks install|uninstall|status`,
  `timer install|uninstall|status`, `box push|index|fetch|send`, `cloud render`.
- Box backends: `azure-blob` (Azure CLI), `rclone` (OneDrive, S3, Google Drive and more), `none`.
- Platform layer for Linux, macOS and Windows: agent homes, config and cache folders, link fallbacks
  (symlink, then junction or managed copy on Windows), schedulers (systemd, launchd, schtasks).
  Only Linux (including WSL) is verified; see `plugin/docs/platforms.md`.
- Sessions that start above several clones: `session-start`, `doctor` and `probe` scan child folders for
  git repos, match each origin URL to a project's `repo`, and set up every match; other repos are listed
  and ignored.
- On-demand retrieval for small cloud disks: archive mode (`box.archives`, with large files kept out of the
  bundle and a table of contents), `box ls` (offline), `box fetch --all`, a free-disk guard
  (`box.min_free_bytes`), `box status`, `box evict`, `box send --archive`, `box receive`, `box mount` and
  `box unmount` (rclone, FUSE), and `box rclone-env` for rclone remotes defined by environment variables.
- Windows facts verified on a real machine: Claude's home and its memory folder names.
- Claude Code and Codex plugin manifests. License: MIT.
