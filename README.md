# code-agent-memory-mirror

Make your coding agents (Claude Code and OpenAI Codex) work with the same context everywhere: on each
of your machines and in vendor-hosted cloud sessions. Your user instructions, personal skills, agent
memory and per-project environment settings live in one private git repo that you own, and a small
program mirrors it into each agent's home folder.

Why: a cloud agent session starts on a fresh machine with only the project clone. Everything that
normally lives in your home folder (`~/.claude`, `~/.codex`, `~/.agents`) is missing there, and
memory written on one machine never reaches the others. This plugin carries it.

## The three parts

1. **The store**: a git repo you choose and own (keep it private). Data only: `AGENTS.md` (your
   instructions), `skills/`, `memory/<project>/`, and `projects/<project>/env.json` plus generated
   cloud settings. Layout: [plugin/docs/store-layout.md](plugin/docs/store-layout.md).
2. **The program**, `agent-mirror` (this repo, `plugin/`): links the store into each agent's home
   folder, syncs it with git, moves bulk untracked files through a "box", renders cloud environment
   settings, and probes a cloud machine. Python 3.8+ standard library only; no bash needed.
3. **The skill** (`plugin/skills/`): the interactive workflow for curating the store with an agent.

Your project repos are never changed and never point at the store. Links only ever point from a home
folder into the store.

## Quick start (Linux, macOS)

```bash
# 1. get the program somewhere stable and put it on PATH
git clone https://github.com/moodykong/code-agent-memory-mirror ~/code-agent-memory-mirror
mkdir -p ~/.local/bin
ln -s ~/code-agent-memory-mirror/plugin/bin/agent-mirror ~/.local/bin/agent-mirror

# 2. make the store (first create an empty PRIVATE repo for it on your git host)
agent-mirror init-store ~/agent-store --remote git@github.com:you/agent-store.git --commit
git -C ~/agent-store push -u origin main

# 3. link the agents' home folders into it (look first, then do it)
agent-mirror install --dry-run --project my-project --project-path ~/code/my-project
agent-mirror install --project my-project --project-path ~/code/my-project

# 4. keep it in sync
agent-mirror hooks install     # Claude Code: session start, memory write and session end hooks
agent-mirror timer install     # optional: sync every 30 minutes
agent-mirror doctor            # what is linked, what is not
```

On another machine: clone the store, run `agent-mirror init-store ~/agent-store --adopt`, then
`agent-mirror install`. To undo everything: `agent-mirror hooks uninstall`, `agent-mirror timer
uninstall` and `agent-mirror uninstall` (it puts back whatever the links replaced).

Windows (UNVERIFIED): clone the repo, add `...\code-agent-memory-mirror\plugin\bin` to `PATH` (it holds
`agent-mirror.cmd`), and use the same commands. See [plugin/docs/platforms.md](plugin/docs/platforms.md).

Cloud sessions: write `projects/my-project/env.json` in the store (start from
`projects/_example/env.json`), run `agent-mirror cloud render --project my-project`, and follow the
README it writes under `projects/my-project/cloud/`.

The skill: in Claude Code, `/plugin marketplace add moodykong/code-agent-memory-mirror`, then
`/plugin install code-agent-memory-mirror@code-agent-memory-mirror`. The Codex manifest is
`plugin/.codex-plugin/plugin.json`. Installing the plugin does not register any hooks by itself:
hooks commit and push your store, so they start only when you run `agent-mirror hooks install`
(which is also what the cloud setup scripts do).

## Commands

| Command | What it does |
| --- | --- |
| `detect` | print the runtime: `claude-local`, `claude-cloud`, `codex-local`, `codex-cloud` or `unknown` |
| `init-store PATH [--remote URL] [--adopt] [--commit]` | make a store from the template (git init), or adopt an existing store folder; writes the user config |
| `install [--agents claude,codex] [--project NAME --project-path PATH] [--memory-dir PATH]` | link the home folders into the store, backing up whatever a link replaces |
| `uninstall` | remove the links into the store and restore the backups |
| `doctor` | runtime, config, store state, every expected link, hooks, tools, box settings |
| `session-start` | pull the store; on a cloud runtime also dependency sync, project start commands and install; print the memory index path |
| `sync [--message MSG]` | adopt stray memory files, secret scan, commit, `pull --rebase`, push (never force) |
| `probe [--tcp HOST:PORT] [--url URL]`, `probe --full` | reachability and machine checks; `--full` is the long first-session report |
| `hook memory-write` | hook handler: runs `sync` when the file just written is a store memory, skill or instruction file |
| `hooks install\|uninstall\|status [--agent claude]` | add or remove the three Claude Code hooks in its user settings, keeping everything else |
| `timer install\|uninstall\|status [--minutes 30]` | periodic `sync`: systemd user timer, launchd agent or Windows scheduled task |
| `box push\|index`, `box fetch PATH [--all]`, `box send PATH [--archive]` | move bulk untracked files through Azure blob storage or any rclone remote, on demand |
| `box ls [PATH]`, `box status`, `box evict PATH` | browse the box offline, see what was fetched here, delete fetched copies to free disk |
| `box receive [--into DIR]` | download the agents' results from the outbound side (developer machine) |
| `box mount\|unmount [--cache-max-size 10G]` | rclone: mount the inbound side read-only with a capped cache (needs FUSE) |
| `box rclone-env --remote NAME [--out FILE]` | turn a local rclone remote into `RCLONE_CONFIG_*` variables for a cloud machine |
| `cloud render --project NAME [--vendor claude\|codex\|all]` | write paste-ready setup scripts, host list, variable names and steps for Claude cloud and Codex cloud into the store |

A session may run inside one clone or in a folder above several clones: `session-start`, `doctor` and
`probe` scan child folders for git repos and set up every repo whose origin matches a project in the
store; other repos are listed and left alone (details in
[plugin/docs/project-config.md](plugin/docs/project-config.md)).

The store is found from `--store PATH`, then `AGENT_MIRROR_STORE`, then the user config file
(`${XDG_CONFIG_HOME:-~/.config}/agent-mirror/config.json`; Windows `%APPDATA%\agent-mirror\config.json`).
Commands that change things take `--dry-run`. Every git or network call has a timeout, and a failed
network step warns and carries on. `agent-mirror <command> -h` shows every option.

## The runtimes

| Runtime | How it is detected | What `session-start` does |
| --- | --- | --- |
| any | `AGENT_RUNTIME` is set to a runtime name | as for that runtime |
| `claude-cloud` | `CLAUDE_CODE_REMOTE=true` | pull the store, dependency sync, start commands, install for Claude |
| `claude-local` | `CLAUDECODE` is set | pull the store only |
| `codex-local` | `CODEX_THREAD_ID`, `CODEX_SESSION_ID` or `CODEX_CI` is set | pull the store only |
| `codex-cloud` | `AGENT_RUNTIME=codex-cloud` (set on the Codex environment page) | pull the store, dependency sync, start commands, install for Codex |
| `unknown` | none of the above | pull the store only |

## The box

Untracked project files (results, slides, figures) do not travel with a git clone. The box carries
them. **Inbound**: the developer machine uploads (`box push`), agents read (`box index`, `box ls`,
`box fetch`). **Outbound**: agents upload results (`box send`), the developer pulls them (`box receive`).
Backends: `azure-blob` (through the Azure CLI, Azure AD sign-in only) and `rclone` (OneDrive, S3,
Google Drive and many more).

Retrieval is on demand, because a cloud machine has little disk: agents browse the box offline with
`box ls`, fetch only what the task needs, and every fetch refuses to leave less than
`box.min_free_bytes` (default 2 GiB) free. `box status` and `box evict` manage what was fetched.
Folders with very many small files can travel as one bundle each (`box.archives`), with large files
kept out of the bundle and fetched only when named. Settings and rules:
[plugin/docs/project-config.md](plugin/docs/project-config.md).

## Safety rules

- Never put a secret value in the store. Project config holds environment variable NAMES only; values
  go on the vendor's settings page or in your own environment.
- `sync` scans changed files for key-shaped strings and refuses to commit if it finds any. It prints
  file and pattern names only, never the matched text.
- Links only point from a home folder into the store. `install` refuses a link whose target is outside
  the store, a link that would sit inside the store, and link loops. `init-store` refuses a store inside
  a project repo, inside this program, or inside an agent home folder.
- Nothing is deleted without a backup: anything a link replaces is moved to
  `~/.agent-mirror-backups/<timestamp>/` with a manifest that `uninstall` uses to put it back.
- Never force push. A rebase conflict is aborted and the local commit is kept.
- The box never deletes remote files unless you pass `--delete`, never overwrites local files on a
  local runtime, refuses `box push` and `box receive` on a cloud runtime, and refuses `.env` files.
  `box evict` deletes only files recorded as fetched from the box and unchanged since, and only on a
  cloud runtime unless you pass `--force`. `box rclone-env` never prints a secret value.
- `--dry-run` shows the exact commands, with secret values replaced by `***`.
- `cloud render` writes a placeholder (`<STORE_REPO_TOKEN>`) where a token goes and drops any
  credentials found in the store's remote URL.

## Platform status

| | Linux (including WSL 2) | macOS | Windows |
| --- | --- | --- | --- |
| Status | verified (tests pass on Python 3.9 and 3.10) | UNVERIFIED | Claude home and memory folder names verified; the rest UNVERIFIED |
| Links | symlinks | symlinks | symlink, else junction (folders) or managed copy (files) |
| Periodic sync | systemd user timer | launchd agent | schtasks |
| Launcher | `plugin/bin/agent-mirror` | `plugin/bin/agent-mirror` | `plugin/bin/agent-mirror.cmd` |
| `box mount` | FUSE (`fuse3`); UNVERIFIED on cloud machines | macFUSE, UNVERIFIED | WinFsp, UNVERIFIED |

Every macOS and Windows assumption is listed in [plugin/docs/platforms.md](plugin/docs/platforms.md),
together with the case of one Windows PC with WSL (two homes, two installs, one store remote).

## Tests

```bash
python3 -m unittest discover -s tests
```

The tests run `agent-mirror` against throwaway fake home folders and stores, with fake `az` and
`rclone` programs, so the real home folder, cloud accounts and schedulers are never touched.

## License

MIT. See [LICENSE](LICENSE).
