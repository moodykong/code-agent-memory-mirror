# Changelog

## 0.3.0

- Cloud setup scripts export AGENT_RUNTIME, because the vendor runs them before it sets its own session markers. Without it, install during setup looked like a developer machine and wrote a machine profile into the store, which every cloud sync then pushed and a developer machine held for review. Sync on a cloud runtime also leaves `machines/` out of its commits.

- Per-unit reconciliation. `agent-mirror plan` (read-only, `--json`) compares this machine with the store
  for every unit: the instruction file per agent, each skill by name (compared by content), each
  project's Claude memory folder and each project's box. Cases: `first-setup` (only this machine has it),
  `join` (only the store has it), `reconcile` (both, different), `identical`, `nothing`, `linked`, plus
  `external` (a skill linked into a project repo) and `legacy-folder-link`. It ends by saying whether the
  machine is fresh (nothing waits for a choice). See plugin/docs/reconciliation.md.
- `agent-mirror apply [--choices FILE] [--only UNIT_ID ...] [--yes-defaults] [--dry-run]` applies one
  action per unit: skills `adopt`, `keep-local`, `link`, `skip`, `take-store`, `keep-machine`;
  instructions `adopt`, `link`, `take-store`, `keep-local`; memory `adopt`, `link`, `merge`, `skip`; box
  `writer`, `reader`, `none` (a role only); `convert` for a legacy whole-folder skills link. Units that
  need a choice wait. Everything replaced is backed up and `uninstall` restores it. The secret scan runs
  before anything is copied into the store. Cloud runtimes never adopt.
- `install` is for fresh machines: it applies every unit that needs no decision, lists the units waiting
  for a choice and says to run `agent-mirror plan`. `--strict` exits with code 4 and changes nothing when
  the machine is not fresh. Skills are linked one by one into the shared skills folder; a whole skills
  folder is never replaced by one link, and the old whole-folder link is converted.
- Machine identity: a stable id and a name (default: the host name) in the user config, made by
  `init-store`, `install` and `apply`; `agent-mirror machine [--name NAME]`. Recorded choices live there
  too. After a real apply on a developer runtime a non-secret profile goes to `machines/<name>.json` in
  the store; `sync` commits `machines/`, and from a cloud session it is held for review.
- `sync` copies a real (unlinked) Claude memory folder into the store only for a project whose memory
  this machine decided to keep there (before, any folder whose name ended with a project name).
- One writer per box: the first `box push` writes `<inbound>/_writer.json`; pushes from another machine
  are refused until `box push --take-over`; a machine whose recorded role is `reader` or `none` is refused
  before any transfer. Both backends, and `--dry-run`. `box index` records the marker in the index.
- `agent-mirror box candidates`: a read-only list of untracked and ignored entries with sizes, large
  files, newest change, a kind guess (`rebuildable`, `secret`, `worktree`, `candidate`) and box coverage.
- `companion_repos` in env.json: public repos that a cloud session cannot attach. `cloud render` adds a
  token-free clone step into `~/companions/<dir>`; cloud `session-start` links them as `../<dir>` beside
  the project clone.
- Cloud `session-start` updates agent-mirror's own clone to the ref pinned in store.json (when its origin
  matches) and runs the new code once. In a test sandbox it never touches a clone outside the sandbox.
- `probe --full` tests a sub-path fetch from an archive when the index has no small loose file, then
  evicts it. `doctor` reports a missing `az` or `rclone` as a problem only when a project's box needs it,
  shows the machine and a plan summary, and marks links decided against as `DECIDED`.
- Tests: plan and apply for every case and unit kind, the writer marker on both backends, candidates,
  companions, the self-update with a local bare repo. `tests/profile_fingerprint.py` and CONTRIBUTING.md
  describe the read-only before and after check of a real profile.

## 0.2.1

- Verified on Claude Code on the web (2026-11): setup script timing, store clone by token at setup, user-level hooks written by the setup script, rclone box through the proxy (list, fetch, send), public companion repo cloned at setup, memory round trip through an inbox branch. Finding: inside a running session GitHub is reached only through the vendor proxy, so the store repo must be selected in the session; the Claude runbook now says so.

- Safety: one resolver (`platforms.py`) for every home-like location. `AGENT_MIRROR_HOME` moves them all
  into one folder on every platform. With `AGENT_MIRROR_TEST_SANDBOX` set, every command exits with code
  97 when any location is outside the sandbox, and real schedulers and clipboard tools are never started.
  `agent-mirror debug paths` prints the resolved locations. The test harness sets both for every run and
  refuses to start a test run that is not sandboxed. (An earlier Windows test run changed a real user
  profile because the tests isolated HOME only.)
- Windows: the test suite passes natively (Windows 11, Python 3.11). Fixed: mixed path separators when
  joining box paths, case-sensitive inside-folder checks, "owner-only" secret files (Windows ignores 0600;
  agent-mirror now checks and reports "inside the user profile" there). Fake tools work as `.cmd` shims.
- CONTRIBUTING.md with the test rules.

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
