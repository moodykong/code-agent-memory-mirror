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
  machines/<machine name>.json   one non-secret profile per developer machine, written by `apply` and `install`
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
  cloud sessions. Since 0.5.2 the scripts read it from `store.json` on the store's default branch when they
  run, so a new pin needs no new paste: a cloud `session-start` moves agent-mirror to it (see "Cloud
  files"). Pin 0.5.2 or later: an older agent-mirror has no `cloud bootstrap`, and the scripts then only
  link the store.
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
- NEVER applies changes outside `memory/` (instructions, skills, projects, machine profiles under
  `machines/`, store.json, anything else): they are reported as "held for review" with the branch and
  file names;
- commits the applied changes as one commit per branch, pushes main, then deletes each branch whose
  changes were all handled. Branches with held changes, a secret-scan hit or a kept deletion stay.

`agent-mirror review list | show BRANCH | accept BRANCH [--paths ...] | discard BRANCH` handles held
branches on a developer machine: `accept` applies the held paths onto main after showing them (and deletes
the branch once all of them are accepted), `discard` deletes the remote branch. `doctor` lists inbox
branches as of the last fetch.

Why: a cloud session must not be able to change the instructions or runbooks that a local agent with real
credentials will follow. The cost: memory written in one cloud session reaches other machines only after a
developer machine syncs. The store token in the setup script only needs read access: it is used to clone the store when the environment is built. Inside a session on Claude Code on the web, the inbox branch is pushed through the vendor's GitHub access for the selected repos (verified), so the store repo must be selected in the session.

The same rule protects cloud setup: `cloud bootstrap` and the drift repair at a cloud session start run
`projects/*/env.json` (`python`, `setup_commands`, `companion_repos`) as root on the cloud machine, and the
self-update follows `store.json`. They read these files only from the default branch, and the developer
sync never applies `projects/`, `store.json` or anything else outside `memory/` from an inbox branch. A
memory path with an empty, `.` or `..` part, a backslash or a colon is held as well, a memory path that
resolves outside `memory/` through a link blocks the branch, and a link committed on a branch is written
as a plain file.

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
    {"kind": "first-session", "title": "...", "text": "...",
     "prompt": "Run this command, with <clone> replaced by ...:\ncd <clone> && agent-mirror probe --full > probe_output.txt 2>&1; agent-mirror box send probe_output.txt --label first-probe --note \"Report of agent-mirror probe --full ...\"; cat probe_output.txt; rm -f probe_output.txt\n..."},
    {"kind": "cleanup", "title": "...", "text": "...", "items": ["delete the values file ...", "..."]}
  ]
}
```

Step kinds come from a fixed set: `network-hosts`, `env-values`, `setup-script`, `first-session`,
`cleanup`. For Codex there are two `setup-script` steps, with `"product": "vm"` (`vm-install.sh`, also
`vm-start-skill.md`) and `"product": "container"` (`container-setup.sh`, also `container-maintenance.sh`).
A variable's `recipe` is `none` when no recipe covers it.

## Cloud files

The setup scripts in `projects/<project>/cloud/<vendor>/` are fixed bootstraps (since 0.5.2). The only
project-specific text in them is the project name, the store URL, the agent-mirror URL (`tool.repo`) and
the runtime marker. Each one clones the store (Claude: with a read-only token on the settings page) or
finds the vendor's checkout of it (Codex VM), reads `tool.ref` from `store.json` on the store's default
branch, checks agent-mirror out at that ref, puts it on PATH and runs `agent-mirror cloud bootstrap
--project <project> --vendor claude|codex`, which reads the rest from `projects/<project>/env.json` on the
default branch (see project-config.md, "Cloud setup and drift repair"). If the checked-out agent-mirror
has no `cloud bootstrap` (a pin older than 0.5.2), the script runs the older steps instead: `init-store
--adopt`, `install` and, for Claude, `hooks install`.

- Paste each script once per environment. A change to `env.json` or to `tool.ref` takes effect at the next
  session start; re-rendering updates the README, the host list and the variable names, never the
  script text. A new store URL, a new `tool.repo` or another project needs a new paste.
- The second line, `# revision: 1`, is the manual fallback: raise the number and save to make the vendor
  rebuild its cached machine from scratch (a changed script text makes the vendor rebuild it).
- Scripts rendered by 0.5.1 and earlier keep working with a newer agent-mirror: they call `init-store`,
  `install` and `hooks install`, which stay. Their machines have no stamp, so the drift repair does not
  run there; render and paste the new script to get it.

Written on the cloud machine, never in the store:

| File | What |
| --- | --- |
| `~/agent_setup_probe.txt` | the setup script's notes: each step's name, result and time, never output |
| `<config>/bootstrap-stamp.json` | what `cloud bootstrap` set up: per project a hash of `python`, `setup_commands` and `companion_repos`, the agent-mirror commit, the store commit |
| `<cache>/session-start.jsonl` | one JSON line per `session-start` run (the newest 50): each step's name, start, end, seconds and status, never output; `probe` prints the last one |

`<config>` and `<cache>` are agent-mirror's folders (`~/.config/agent-mirror` and `~/.cache/agent-mirror`
on Linux; `agent-mirror debug paths` prints them). Developer machines write `session-start.jsonl` too.

## Links that `install` and `apply` make

All links point from a home folder into the store, never the other way. Which units get linked on a
machine is decided per unit (see reconciliation.md); `install` links every unit that needs no decision.

| Link | Target | For |
| --- | --- | --- |
| `<claude home>/CLAUDE.md` | `AGENTS.md` | Claude Code |
| `<codex home>/AGENTS.md` | `AGENTS.md` | Codex |
| `~/.agents/skills/<name>` | `skills/<name>` | Codex and other agents that read shared skills, one link per store skill |
| `<claude home>/skills/<name>` | `skills/<name>` | Claude Code, one link per store skill |
| `<claude home>/projects/<slug>/memory` | `memory/<memory_project>/` | Claude Code, per project |

A whole skills folder is never replaced by one link. (Versions before 0.3.0 linked `~/.agents/skills`
as a whole; `plan` reports that as `skills-folder`, case `legacy-folder-link`, and `install` or `apply`
converts it into a real folder with one link per skill.) When agent-mirror creates `~/.agents/skills`, the
manifest records it, and `uninstall` removes it again once it is empty.

Other entries in the skills folders are never touched: folders named `synced`, names starting with a
dot, links into project repos, and skills recorded as kept local or skipped. Anything a link replaces is
moved to `~/.agent-mirror-backups/<timestamp>/` and listed in that folder's `manifest.json`, which
records the link method (`symlink`, `junction` or `copy`). `uninstall` removes the links and puts the
backups back. A real Claude memory folder is merged into the store only when you choose `merge` (or
`adopt` when the store has none): store files are never overwritten, and a file that differs on both
sides is saved next to the store's as `<stem>.from-<machine name>.md`.

## Machine profiles (`machines/`)

After a real `apply` or `install` on a developer runtime, agent-mirror writes `machines/<machine
name>.json` (only when something other than the date changed). It holds nothing secret and no home path
outside the `~/...` form:

```json
{
  "schema": 1,
  "machine_id": "3f2a9c1e5b7d4e6f8a0b1c2d3e4f5a6b",
  "machine_name": "my-laptop",
  "platform": "linux",
  "tool": "agent-mirror 0.3.0",
  "date": "2026-11-17",
  "agents": ["claude", "codex"],
  "projects": {"my-project": {"paths": ["~/code/my-project"], "memory": "linked", "box_role": "writer"}},
  "skills": {"linked": ["alpha", "beta"], "kept_local": ["my-draft"], "skipped": []}
}
```

`sync` commits `machines/` with memory and skills. A profile pushed from a cloud session lands on an
inbox branch and is held for review, like every other path outside `memory/`.

## Memory files

`MEMORY.md` is an index, one line per memory: `- [Title](file.md): one-line summary`. Each memory is
a markdown file with YAML front matter (`name`, `description`, `metadata.type`, optionally
`metadata.author`) and a body. The template `AGENTS.md` explains the protocol to the agents.

`MEMORY.md` files merge with git's union driver (`.gitattributes`), so two machines adding index lines
at the same time do not conflict.

## The box index

`box_index.jsonl` has one JSON object per line. A plain file: `{"path", "size", "modtime"}`. An archived
folder: `{"path": "<folder>", "archive": true, "files", "bytes", "archive_bytes", "large_files",
"large_bytes", "modtime", "fingerprint", "toc": "box_toc/<folder>.json"}` (a nested folder such as
`decompile/project-cli` has its table of contents at `box_toc/decompile/project-cli.json`); the table of
contents file lists every file of the folder with its size and whether it is in the bundle or travels on
its own. The folder owners are one more line: `{"path": "_owners.json", "schema": 1, "owners":
{"<folder>": {"machine_id", "machine_name", "since", "fingerprint", "updated"}}}`, where an entry may also
hold the folder's note (`note`, `note_updated`, since 0.5.3; `box note` writes it into this line too). A box written by an
older version may also have its single-writer marker: `{"path": "_writer.json", "writer": {"machine_id",
"machine_name", "date", "tool"}}`. When the sessions side holds sessions, one more line summarizes the
newest sessions: `{"path": "_sessions", "sessions": [{"id", "label", "runtime", "branch", "commit", "updated",
"files", "bytes", "note"}], "more": N}` (the newest 50; `note` since 0.5.3). Readers of the entries skip these lines; `plan`, `box ls`, `box owners`
and `box sessions` (without the transfer program) use them. Commit the index and `box_toc/` so `agent-mirror box ls` and `agent-mirror plan`
work offline on other machines.

## In the project folder (never committed)

`box fetch` records what it wrote in `<project>/.agent-mirror/fetched.json` (used by `box status` and
`box evict`), and `box send` remembers this session in `<project>/.agent-mirror/session.json` (its id,
label, runtime, host and start time). agent-mirror adds `/.agent-mirror/` to the clone's
`.git/info/exclude`, never to the tracked `.gitignore`. Fetched sessions (`_sessions/`), `box receive`
(default `_agent_outbound/`) and `box mount` (default `_box/`) folders inside the project are added there
too.

## What `sync` commits

Only `memory/`, `skills/`, `machines/`, `AGENTS.md` and `CLAUDE.md` (in a cloud session with
`cloud_push: "inbox"`, pushed to an inbox branch instead of main). Before committing, `sync` copies files
from a real (unlinked) Claude memory folder into the store only for a project whose memory this machine
decided to keep there (a recorded `link`, `adopt` or `merge`). Changes to `store.json` and `projects/`
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
  },
  "machine": {"id": "3f2a9c1e5b7d4e6f8a0b1c2d3e4f5a6b", "name": "my-laptop"},
  "choices": {"skill:my-draft": "keep-local", "memory:my-project": "merge", "box:my-project": "writer"}
}
```

- `store_path`: the store folder (written by `init-store`).
- `store_remote`: the store's git remote (used by `cloud render` when the clone has no `origin`).
- `agents`: the default for `install --agents`.
- `projects`: project name to checkout folder(s). `plan`, `apply` and `install` consider every listed
  folder that exists; `--project NAME --project-path PATH` adds to it.
- `machine`: this machine's stable id and its name (default: the host name). `init-store`, `install` and
  `apply` create them; `agent-mirror machine --name NAME` changes the name.
- `choices`: the action applied or chosen for each unit on this machine, including the box role per
  project (`box:<project>`). See reconciliation.md.
