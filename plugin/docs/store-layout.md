# Store layout

The store is a git repo you own (keep it private). It holds data only, never a program.
`agent-mirror init-store PATH` creates this skeleton from `plugin/templates/store/`.

```
<store>/
  store.json                  schema version and the agent-mirror version to use in cloud sessions
  AGENTS.md                   your user-level instructions, for every agent
  CLAUDE.md                   one line, "@AGENTS.md", so Claude reads AGENTS.md when the store itself is opened
  .gitattributes              line endings, and union merges for MEMORY.md
  .gitignore                  logs, backups, env files
  skills/<name>/SKILL.md      personal skills
  memory/<project>/MEMORY.md  memory index for one project
  memory/<project>/*.md       one memory per file
  projects/<project>/env.json the project's environment settings (see project-config.md)
  projects/<project>/cloud/<vendor>/   files written by `agent-mirror cloud render`: setup scripts,
                              allowed-hosts.txt, env-vars.txt, runbook.json and README.md
  projects/<project>/box_index.jsonl   written by `agent-mirror box index`
  projects/<project>/box_toc/<folder>.json[.gz]   tables of contents of archived folders, from `box index`
  projects/_example/env.json  a complete example; folders starting with "_" are ignored
```

## store.json

```json
{
  "schema": 1,
  "tool": {
    "repo": "https://github.com/moodykong/code-agent-memory-mirror",
    "ref": "main"
  },
  "cloud_push": "inbox"
}
```

- `schema`: the layout version. agent-mirror warns when a store's schema is newer than it understands.
- `tool.repo`: where cloud setup scripts get agent-mirror from (a public https URL, no token).
- `tool.ref`: the branch, tag or commit those scripts check out. Pin a tag or commit for stable
  cloud sessions; `cloud render` writes it into the scripts.
- `cloud_push`: `"inbox"` (the default for new and adopted stores, and what agent-mirror assumes when the
  key is missing) or `"main"` (the old behaviour: cloud sessions push straight to the main branch).
  See "Inbox branches" below.

## Inbox branches (`cloud_push: "inbox"`)

A cloud session holds a token that can push to the store, so its pushes must not land on the main line.
In a cloud session `agent-mirror sync` commits as usual and pushes to `refs/heads/inbox/<runtime>-<id>`
on the store remote, where `<id>` is a short hash kept in the clone's `.git` folder, stable for the
machine. It never pushes to the main branch. (The only forced push agent-mirror ever makes is to the
machine's own inbox branch, after `session-start` rebased its unpushed commits onto a newer main.)

On a developer machine, after the normal pull, `agent-mirror sync` fetches every `inbox/*` branch and:

- applies changes under `memory/` to main: a new file is added; a changed file is taken when main has not
  changed it since the branch started; `MEMORY.md` indexes are union-merged; when both sides changed the
  same memory file, main's version stays and the branch's version is saved next to it as
  `<stem>.from-<branch>.md` (not added to the index) and reported; a deletion is applied only when main
  has not changed the file;
- runs the secret scan on what it would apply, and applies nothing from a branch that trips it;
- NEVER applies changes outside `memory/` (instructions, skills, projects, store.json, anything else):
  they are reported as "held for review" with the branch and file names;
- commits the applied changes as one commit per branch, pushes main, then deletes each branch whose
  changes were all handled. Branches with held changes, a secret-scan hit or a kept deletion stay.

`agent-mirror review list | show BRANCH | accept BRANCH [--paths ...] | discard BRANCH` handles held
branches on a developer machine: `accept` applies the held paths onto main after showing them (and deletes
the branch once all of them are accepted), `discard` deletes the remote branch. `doctor` lists inbox
branches as of the last fetch.

Why: a cloud session must not be able to change the instructions or runbooks that a local agent with real
credentials will follow. The cost: memory written in one cloud session reaches other machines only after a
developer machine syncs. The store token still needs write access, to push the inbox branch.

## runbook.json

`agent-mirror cloud render` writes `projects/<project>/cloud/<vendor>/runbook.json`; the README next to
it is generated from the same data, and `agent-mirror cloud runbook --json` prints it.

```json
{
  "schema": 1,
  "tool": "agent-mirror 0.2.0",
  "project": "my-project",
  "vendor": "claude",
  "product": "Claude Code on the web (claude.ai/code)",
  "environment_name": "my-project",
  "project_repo": "example-org/my-project",
  "store_repo": "github.com/example-org/agent-store",
  "tool_ref": "main",
  "steps": [
    {"kind": "network-hosts", "title": "...", "text": "...", "file": "allowed-hosts.txt", "hosts": ["github.com"]},
    {"kind": "env-values", "title": "...", "text": "...", "file": "env-vars.txt",
     "variables": [{"name": "EXAMPLE_API_TOKEN", "recipe": "local-file", "where": "env", "placeholder": null,
                    "automatic": false, "instructions": null}],
     "check_command": "agent-mirror cloud secrets --project my-project --vendor claude --check",
     "values_command": "agent-mirror cloud secrets --project my-project --vendor claude --out <FILE outside any repo>"},
    {"kind": "setup-script", "title": "...", "text": "...", "file": "setup.sh",
     "placeholders": [{"placeholder": "<STORE_REPO_TOKEN>", "secret": "STORE_REPO_TOKEN", "instructions": "..."}]},
    {"kind": "first-session", "title": "...", "text": "...", "prompt": "Run agent-mirror probe --full ..."},
    {"kind": "cleanup", "title": "...", "text": "...", "items": ["delete the values file ...", "..."]}
  ]
}
```

Step kinds come from a fixed set: `network-hosts`, `env-values`, `setup-script`, `first-session`,
`cleanup`. For Codex there are two `setup-script` steps, with `"product": "vm"` (`vm-install.sh`, also
`vm-start-skill.md`) and `"product": "container"` (`container-setup.sh`, also `container-maintenance.sh`).
A variable's `recipe` is `none` when no recipe covers it.

## Links that `install` makes

All links point from a home folder into the store, never the other way:

| Link | Target | For |
| --- | --- | --- |
| `<claude home>/CLAUDE.md` | `AGENTS.md` | Claude Code |
| `<codex home>/AGENTS.md` | `AGENTS.md` | Codex |
| `~/.agents/skills` | `skills/` | Codex and other agents that read shared skills |
| `<claude home>/skills/<name>` | `skills/<name>` | Claude Code, one link per store skill |
| `<claude home>/projects/<slug>/memory` | `memory/<memory_project>/` | Claude Code, per project |

Other entries in `<claude home>/skills` are never touched. Anything a link replaces is moved to
`~/.agent-mirror-backups/<timestamp>/` and listed in that folder's `manifest.json`, which records the
link method (`symlink`, `junction` or `copy`). `uninstall` removes the links and puts the backups back.
Before linking an existing real Claude memory folder, its files are copied into the store where the
store lacks them; store files are never overwritten and conflicts are reported.

## Memory files

`MEMORY.md` is an index, one line per memory: `- [Title](file.md): one-line summary`. Each memory is
a markdown file with YAML front matter (`name`, `description`, `metadata.type`, optionally
`metadata.author`) and a body. The template `AGENTS.md` explains the protocol to the agents.

`MEMORY.md` files merge with git's union driver (`.gitattributes`), so two machines adding index lines
at the same time do not conflict.

## The box index

`box_index.jsonl` has one JSON object per line. A plain file: `{"path", "size", "modtime"}`. An archived
folder: `{"path": "<folder>", "archive": true, "files", "bytes", "archive_bytes", "large_files",
"large_bytes", "modtime", "toc": "box_toc/<folder>.json"}`; the table of contents file lists every file
of the folder with its size and whether it is in the bundle or travels on its own. Commit the index and
`box_toc/` so `agent-mirror box ls` works offline on other machines.

## In the project folder (never committed)

`box fetch` records what it wrote in `<project>/.agent-mirror/fetched.json` (used by `box status` and
`box evict`). agent-mirror adds `/.agent-mirror/` to the clone's `.git/info/exclude`, never to the tracked
`.gitignore`. `box receive` (default `_agent_outbound/`) and `box mount` (default `_box/`) folders inside
the project are added there too.

## What `sync` commits

Only `memory/`, `skills/`, `AGENTS.md` and `CLAUDE.md` (in a cloud session with `cloud_push: "inbox"`,
pushed to an inbox branch instead of main). Changes to `store.json` and `projects/`
(including rendered cloud files, the box index and `box_toc/`) are committed by hand. Before committing, `sync` scans the changed
files for key-shaped strings and refuses if it finds any (it prints the file and pattern names only).
It never force-pushes; a rebase conflict is aborted and the local commit is kept.

## The user config file

Not in the store: it lives at `${XDG_CONFIG_HOME:-~/.config}/agent-mirror/config.json`
(Windows: `%APPDATA%\agent-mirror\config.json`).

```json
{
  "store_path": "/home/you/agent-store",
  "store_remote": "git@github.com:you/agent-store.git",
  "agents": ["claude", "codex"],
  "projects": {
    "my-project": "/home/you/code/my-project",
    "other": ["/home/you/code/other", "/home/you/worktrees/other-feature"]
  }
}
```

- `store_path`: the store folder (written by `init-store`).
- `store_remote`: the store's git remote (used by `cloud render` when the clone has no `origin`).
- `agents`: the default for `install --agents`.
- `projects`: project name to checkout folder(s). `install` with no `--project` links the Claude memory
  folder of every listed folder that exists; `install --project NAME --project-path PATH` adds to it.
