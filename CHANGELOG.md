# Changelog

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
