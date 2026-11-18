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

# 3. compare this machine with the store, unit by unit (read-only)
agent-mirror plan --project my-project --project-path ~/code/my-project

# 4a. a fresh machine (plan says "fresh: yes"): link everything
agent-mirror install --project my-project --project-path ~/code/my-project

# 4b. a lived-in machine (its own instructions, skills, memory): choose, then apply
agent-mirror plan --json --project my-project --project-path ~/code/my-project > plan.json
#     write choices.json, for example {"skill:my-draft": "adopt", "memory:my-project": "merge"}
agent-mirror apply --dry-run --choices choices.json --project my-project --project-path ~/code/my-project
agent-mirror apply --choices choices.json --project my-project --project-path ~/code/my-project

# 5. keep it in sync
agent-mirror hooks install     # Claude Code: session start, memory write and session end hooks
agent-mirror timer install     # optional: sync every 30 minutes
agent-mirror doctor            # what is linked, what is not
```

`plan` compares each unit on its own: the instruction file per agent, each skill, each project's Claude
memory folder and each project's box. A unit only this machine has is `first-setup` (adopt it into the
store), one only the store has is `join` (link it), one both have with different content is `reconcile`
(you choose), and identical or already linked units need nothing. `install` applies only what needs no
decision and lists the rest; `apply` takes your choices from a JSON file. Nothing is replaced without a
backup, and nothing is copied into the store before the secret scan passed. The table, the actions and
the JSON: [plugin/docs/reconciliation.md](plugin/docs/reconciliation.md).

On another machine, see "Join a second machine" below. To undo everything: `agent-mirror hooks uninstall`, `agent-mirror timer
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

## Join a second machine

The store carries everything a machine needs to know, including a declarative runbook for each cloud
environment, but never secret values. On a second machine:

```bash
git clone https://github.com/moodykong/code-agent-memory-mirror ~/code-agent-memory-mirror
ln -s ~/code-agent-memory-mirror/plugin/bin/agent-mirror ~/.local/bin/agent-mirror
git clone git@github.com:you/agent-store.git ~/agent-store
agent-mirror init-store ~/agent-store --adopt          # writes the user config on this machine
agent-mirror plan                                      # what this machine has, unit by unit
agent-mirror install                                   # fresh machine: link what needs no decision
agent-mirror apply --choices choices.json              # lived-in machine: apply your choices
agent-mirror cloud secrets --project my-project --check # which values this machine can produce
agent-mirror cloud runbook --project my-project         # the steps to set up the cloud environment
```

`cloud secrets --check` names each value's recipe (an rclone remote, an owner-only local file, the
environment, a key vault, a constant, or something only you can create) and says what this machine still
needs. `cloud secrets --out FILE` then writes the values to an owner-only file outside any repo, to paste
on the vendor's settings page.

## Commands

| Command | What it does |
| --- | --- |
| `detect` | print the runtime: `claude-local`, `claude-cloud`, `codex-local`, `codex-cloud` or `unknown` |
| `init-store PATH [--remote URL] [--adopt] [--commit]` | make a store from the template (git init), or adopt an existing store folder; writes the user config |
| `plan [--agents claude,codex] [--project NAME --project-path PATH]... [--json]` | compare this machine with the store unit by unit: `first-setup`, `join`, `reconcile`, `identical`, `nothing`, `linked`; read-only |
| `apply [--choices FILE] [--only UNIT_ID ...] [--yes-defaults] [--dry-run]` | apply one action per unit (from the choices file, a recorded choice, or the default); units that need a choice wait |
| `install [--agents claude,codex] [--project NAME --project-path PATH] [--memory-dir PATH] [--strict]` | for a fresh machine: apply every unit that needs no decision and list the rest (`--strict`: exit 4, change nothing, when the machine is not fresh) |
| `machine [--name NAME]` | show this machine's id, name and recorded choices, or set its name |
| `uninstall` | remove the links into the store and restore the backups |
| `doctor` | runtime, config, store state, every expected link, hooks, tools, box settings |
| `session-start` | pull the store; on a cloud runtime also update agent-mirror to the pinned ref, dependency sync, project start commands, install and companion repo links; print the memory index path |
| `sync [--message MSG]` | adopt stray memory files (for projects this machine linked), secret scan, commit, `pull --rebase`, push (never force) |
| `probe [--tcp HOST:PORT] [--url URL]`, `probe --full` | reachability and machine checks; `--full` is the long first-session report |
| `hook memory-write` | hook handler: runs `sync` when the file just written is a store memory, skill or instruction file |
| `hooks install\|uninstall\|status [--agent claude]` | add or remove the three Claude Code hooks in its user settings, keeping everything else |
| `timer install\|uninstall\|status [--minutes 30]` | periodic `sync`: systemd user timer, launchd agent or Windows scheduled task |
| `box push [--only FOLDER]... [--take-over [FOLDER]]... [--delete]` | upload the box folders this machine owns or claims; folders another machine owns are skipped with a note (same or different here) |
| `box owners [--json]`, `box release FOLDER...` | list every folder of the box with its owning machine and how the local copy compares (read-only); give up folders this machine owns (no data deleted) |
| `box index`, `box fetch PATH [--all]`, `box send PATH [--archive]` | list the box into the store, fetch on demand, send results; Azure blob storage or any rclone remote |
| `box candidates [--json] [--min-bytes N]` | list the project's untracked and ignored entries with sizes and a kind guess, to choose what goes in the box; read-only |
| `box ls [PATH]`, `box status`, `box evict PATH` | browse the box offline, see what was fetched here, delete fetched copies to free disk |
| `box receive [--into DIR]` | download the agents' results from the outbound side (developer machine) |
| `box mount\|unmount [--cache-max-size 10G]` | rclone: mount the inbound side read-only with a capped cache (needs FUSE) |
| `box rclone-env --remote NAME [--out FILE]` | turn a local rclone remote into `RCLONE_CONFIG_*` variables for a cloud machine |
| `cloud render --project NAME [--vendor claude\|codex\|all]` | write paste-ready setup scripts, host list, variable names, `runbook.json` and steps for Claude cloud and Codex cloud into the store |
| `cloud secrets --project NAME [--vendor claude\|codex] (--check \| --out FILE [--replace])` | produce the environment's values on this machine from the recipes in env.json; never prints a value |
| `cloud runbook --project NAME [--vendor V] [--json]` | print the setup steps for a person, or the runbook JSON |
| `review [list \| show BRANCH \| accept BRANCH [--paths ...] \| discard BRANCH]` | handle inbox branches pushed by cloud sessions (developer machines only) |
| `clip copy FILE [--secret]`, `clip check [--secret]`, `clip clear` | put text on the clipboard and check it without printing secrets |
| `debug paths` | print every home-like location agent-mirror resolves, as JSON (see `AGENT_MIRROR_HOME` in plugin/docs/platforms.md) |

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
them. **Inbound**: developer machines upload (`box push`), each the folders it owns, and agents read
(`box index`, `box ls`, `box fetch`). **Outbound**: agents upload results (`box send`), the developer pulls them (`box receive`).
Backends: `azure-blob` (through the Azure CLI, Azure AD sign-in only) and `rclone` (OneDrive, S3,
Google Drive and many more).

Retrieval is on demand, because a cloud machine has little disk: agents browse the box offline with
`box ls`, fetch only what the task needs, and every fetch refuses to leave less than
`box.min_free_bytes` (default 2 GiB) free. `box status` and `box evict` manage what was fetched.
Folders with very many small files can travel as one bundle each (`box.archives`, for example `ws-*` or
`decompile/project*`), with large files kept out of the bundle and fetched only when named. Each archived
folder and each include folder is owned by one machine (`<inbound>/_owners.json`), so a project split
across two machines can share one box: each machine pushes its own folders and `box owners` shows who has
what. Settings and rules: [plugin/docs/project-config.md](plugin/docs/project-config.md) and "Folder
owners" in [plugin/docs/reconciliation.md](plugin/docs/reconciliation.md).

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
- Nothing on a lived-in machine is replaced without a decision: `install` and `apply` only touch units
  that need none (`join`, `identical`, a legacy whole-folder link) unless you chose an action. A whole
  skills folder is never replaced by one link; skills are linked one by one.
- Before machine content is copied into the store (`adopt`, memory `merge`), the secret scan runs on it;
  a hit refuses that unit and names the file and pattern only. Cloud sessions never adopt.
- Each folder of a box has one owning machine (`_owners.json` on the inbound side): `box push` claims
  folders nobody owns and never uploads a folder another machine owns, unless you run
  `box push --take-over FOLDER`. A machine whose box role is `reader` or `none` never pushes. The remote has
  no locking, so every change to `_owners.json` is read back and checked, and retried or refused clearly.
- Never force push to the main branch. A rebase conflict is aborted and the local commit is kept.
- Cloud sessions push to an inbox branch, never to the main branch (`"cloud_push": "inbox"` in store.json,
  the default). A developer machine's `sync` applies memory from those branches automatically and holds
  every other change (instructions, skills, project settings, runbooks) for `agent-mirror review`. Why: a
  cloud session must not be able to change the instructions or runbooks that a local agent with real
  credentials will follow. The cost: memory written in one cloud session reaches other machines only after
  a developer machine syncs. The store token in cloud sessions still needs write access, to push the inbox
  branch.
- Project config names where each secret comes from (fixed recipe types only; no command from the store is
  ever run), never the value. `cloud secrets --out` and `box rclone-env --out` write owner-only files and
  refuse paths inside the store or any git work tree.
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
| Status | verified (tests pass on Python 3.9 and 3.10) | UNVERIFIED | test suite passes natively (Python 3.11, Windows 11); see plugin/docs/platforms.md for what remains UNVERIFIED |
| Links | symlinks | symlinks | symlink, else junction (folders) or managed copy (files) |
| Periodic sync | systemd user timer | launchd agent | schtasks |
| Launcher | `plugin/bin/agent-mirror` | `plugin/bin/agent-mirror` | `plugin/bin/agent-mirror.cmd` |
| `box mount` | FUSE (`fuse3`); UNVERIFIED on cloud machines | macFUSE, UNVERIFIED | WinFsp, UNVERIFIED |
| `clip` | wl-clipboard, xclip or xsel (UNVERIFIED); WSL: clip.exe and PowerShell (UNVERIFIED) | pbcopy and pbpaste, UNVERIFIED | clip.exe and PowerShell, UNVERIFIED |

Every macOS and Windows assumption is listed in [plugin/docs/platforms.md](plugin/docs/platforms.md),
together with the case of one Windows PC with WSL (two homes, two installs, one store remote).

## Tests

```bash
python3 -m unittest discover -s tests
```

Read [CONTRIBUTING.md](CONTRIBUTING.md) first. The tests run `agent-mirror` only inside a sandbox guard:
`AGENT_MIRROR_HOME` moves every home-like location into a throwaway folder, and with
`AGENT_MIRROR_TEST_SANDBOX` set every command exits with code 97 if any location is outside it. The test
run refuses to start unless `agent-mirror debug paths` shows every location inside the sandbox. Fake `az`,
`rclone` and clipboard programs stand in for the real ones. The suite passes on Linux (Python 3.9, 3.10)
and natively on Windows 11 (Python 3.11).

## License

MIT. See [LICENSE](LICENSE).
