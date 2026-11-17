# Reconciliation: `plan` and `apply`

A machine is rarely empty. It may already have its own instruction file, skills, Claude memory and
bulk project files. agent-mirror compares this machine with the store one unit at a time and never
replaces something it was not told to replace.

- `agent-mirror plan` is read-only. It builds the table below for this machine and says what `apply`
  would do. `--json` prints it for a person or an agent to read.
- `agent-mirror apply` recomputes the plan and applies one action per unit.
- `agent-mirror install` is for a fresh machine: it applies every unit that needs no decision and lists
  the units that wait for one (see "install" below).

## Units

| Unit id | What it is | Machine side | Store side |
| --- | --- | --- | --- |
| `instructions:claude` | Claude's user instruction file | `<claude home>/CLAUDE.md` | `AGENTS.md` |
| `instructions:codex` | Codex's user instruction file | `<codex home>/AGENTS.md` | `AGENTS.md` |
| `skill:NAME` | one skill | `~/.agents/skills/NAME` and, for Claude, `<claude home>/skills/NAME` | `skills/NAME/` |
| `memory:PROJECT` | one project's Claude memory folder | `<claude home>/projects/<slug>/memory` for each checkout of the project | `memory/<memory_project>/` |
| `box:PROJECT` | one project's bulk-file box | the configured `box.include` roots and `box.archives` folders in the checkout | the committed `projects/PROJECT/box_index.jsonl` |
| `skills-folder`, `skills-folder:claude` | a whole skills folder that is one link | the folder | `skills/` |

Projects considered: the ones in the machine config (`projects` in the user config file), the ones given
with `--project NAME --project-path PATH` (repeatable, in pairs), and the one found from the current
folder (the same discovery as `session-start`). Memory units exist only when Claude is among the agents.
Box units exist only for projects whose `box.backend` is set.

## Cases

| Machine | Store | Case | Default action |
| --- | --- | --- | --- |
| has content | no record | `first-setup` | adopt: copy into the store, then link (box: `writer`) |
| empty | has content | `join` | link (box: `reader`); no question |
| has content | has content, different | `reconcile` | none: needs a choice |
| has content | has content, identical | `identical` | link, silently (box: `writer` when the box names this machine or no writer, else `reader`) |
| empty | no record | `nothing` | nothing (memory: link the empty folder so the first memory lands in the store) |
| a link into the store | any | `linked` | nothing; drift (a broken link, or a link to another place in the store) is reported |

Two more cases need no decision:

- `external`: a skill that is a link into a project repo (a folder with its own `.git`), or a skills
  folder that is a link to somewhere outside the store. Action `none`; agent-mirror never writes there.
- `legacy-folder-link`: older versions replaced the whole shared skills folder with one link into the
  store. Default action `convert`: the link is replaced by a real folder that holds one link per store
  skill (skills recorded as `skip` are left out).

How the sides are compared:

- Skills and memory are compared by content: a fingerprint over the relative paths and the bytes of
  every file. `__pycache__`, `.git`, `*.pyc` and editor or OS leftovers (`*~`, `*.swp`, `.#*`, `#*#`,
  `.DS_Store`, `Thumbs.db`) do not count. Links inside a folder are recorded, never followed.
- A machine entry that is itself a link to another folder on this machine is followed once and compared
  by what it points at. A link into the store is `linked`.
- An instruction file that is empty or only whitespace is "empty". The store's `AGENTS.md` counts as
  "no record" while it is still the template that `init-store` wrote, so a machine with its own
  instruction file shows `first-setup`, not `reconcile`.
- A box's machine side "has content" when an include root or an archived folder holds files (a size walk
  with a time budget; a walk that stopped early is marked `lower_bound`). Its store side comes from the
  committed index. `identical` means the same file count and bytes as the index.
- Never skills: folders named `synced` (vendor-synced), names starting with a dot (trash and hidden
  folders), `__pycache__`, and plain files. `plan` lists them under `ignored`.
- When a skill has different copies in two places on this machine, `adopt` takes the newest copy; every
  other copy is backed up before it is replaced by a link.

## Actions per unit kind

| Kind | Actions |
| --- | --- |
| skill | `adopt` (copy the machine's skill into the store, then replace the machine copies with links, backup first), `keep-local` (leave it; recorded), `link` (link the store's skill here), `skip` (do not link this store skill on this machine; recorded), `take-store` (back up the machine copies, link the store's), `keep-machine` (for `reconcile` and `identical`: leave the machine copy unlinked; recorded), `none` |
| instructions | `adopt` (the machine's file becomes the store's `AGENTS.md`; the old one is kept in the backup folder), `link`, `take-store`, `keep-local` (recorded), `none` |
| memory | `adopt` (copy every file into the store, then link), `link` (the store wins; the machine folder goes to the backup folder), `merge` (copy files missing from the store; a file that differs on both sides keeps the store's version and saves the machine's next to it as `<stem>.from-<machine name>.md`, not indexed; `MEMORY.md` files are union-merged; then link), `skip` (recorded), `none` |
| box | `writer`, `reader`, `none`: records this machine's role for the project only; apply never uploads or downloads anything |
| skills-folder | `convert` |

Allowed actions per case are in each unit's `choices`:

| Kind | first-setup | join | reconcile | identical | nothing | linked |
| --- | --- | --- | --- | --- | --- | --- |
| skill | adopt, keep-local | link, skip | take-store, keep-machine, adopt | link, keep-machine | none | none (with drift: none, link) |
| instructions | adopt, keep-local | link, keep-local | take-store, keep-local, adopt | link, keep-local | none | none (with drift: none, link) |
| memory | adopt, skip | link, skip | merge, link, skip | link, skip | link, skip | none (with drift: none, link) |
| box | writer, reader, none | the same | the same | the same | the same | - |

## Which action a unit gets

1. The choices file (`--choices FILE`), when it names the unit. An unknown unit id or an action that is
   not in the unit's `choices` stops apply before anything changes (exit code 2).
2. Else a choice recorded for this machine: `keep-local`, `keep-machine` and `skip` for skills,
   instructions and memory, and the box role. The unit shows `"decided_by": "recorded"` and apply leaves it.
3. Else the default, when the case needs no decision.
4. `reconcile` and `first-setup` units without any of the above wait. They are listed as "waiting for a
   choice". With `--yes-defaults`, `first-setup` units take their default; `reconcile` units still wait.

`--only UNIT_ID ...` restricts apply to those units. `--dry-run` shows every step and changes nothing.

On a cloud runtime nothing is adopted: cloud sessions do not publish machine content to the store's main
line. `first-setup` units are left alone (and say so), and an explicit `adopt` is refused the same way.

### Safety

- Before anything is copied into the store (`adopt`, and `merge` for memory), the secret scan that `sync`
  uses runs on every file to be copied. On a hit the unit is refused and the message names the file and
  the pattern, never the text.
- Everything a link replaces is moved to `~/.agent-mirror-backups/<timestamp>/` with a manifest, as
  install always did. A store file replaced by an `adopt` is kept there too (under `_store/`).
  `agent-mirror uninstall` removes the links, removes folders agent-mirror created once they are empty,
  and puts the backups back. The old whole-folder link of a converted skills folder is dropped, not put
  back, because uninstall removes links into the store; the folder the older version backed up is
  restored from that version's manifest.
- Skills are linked one by one into the shared skills folder and into each selected agent's own skills
  folder. A whole skills folder is never replaced by one link again.

## Recorded choices and the machine identity

The machine config (the user config file, never the store) holds:

```json
{
  "machine": {"id": "3f2a9c1e5b7d4e6f8a0b1c2d3e4f5a6b", "name": "my-laptop"},
  "choices": {
    "instructions:claude": "keep-local",
    "skill:alpha": "keep-machine",
    "skill:beta": "link",
    "memory:my-project": "merge",
    "box:my-project": "writer"
  }
}
```

- `machine.id` is stable; `machine.name` defaults to the host name. `init-store`, `install` and `apply`
  create both when missing. `agent-mirror machine` shows them and the recorded choices;
  `agent-mirror machine --name NAME` sets the name.
- Every applied action is recorded. Only `keep-local`, `keep-machine`, `skip` and box roles stand as the
  machine's decision in later plans. A recorded `link`, `adopt` or `merge` for a memory unit also tells
  `sync` that a real (unlinked) memory folder for that project may be copied into the store; memory a
  machine never decided about is left where it is.

After a real apply (or install) on a developer runtime, a non-secret profile goes to the store as
`machines/<machine name>.json`, and `sync` commits it (see store-layout.md).

## JSON

`agent-mirror plan --json`:

```json
{
  "schema": 1,
  "tool": "agent-mirror 0.3.0",
  "machine": {"id": "3f2a9c1e5b7d4e6f8a0b1c2d3e4f5a6b", "name": "my-laptop"},
  "runtime": "claude-local",
  "store": "/home/you/agent-store",
  "agents": ["claude", "codex"],
  "projects": [{"project": "my-project", "path": "/home/you/code/my-project"}],
  "units": [ ... ],
  "ignored": [{"path": "~/.claude/skills/synced", "reason": "vendor-synced folder"}],
  "waiting": ["skill:alpha"],
  "fresh": false
}
```

One unit:

```json
{
  "id": "skill:alpha",
  "kind": "skill",
  "name": "alpha",
  "machine": {
    "state": "content",
    "copies": 2,
    "files": 1,
    "bytes": 17,
    "newest": "2026-11-17T10:03:11Z",
    "places": [
      {"where": "shared", "path": "~/.agents/skills/alpha", "state": "content", "files": 1, "bytes": 17, "newest": "2026-11-17T10:03:11Z"},
      {"where": "claude", "path": "~/.claude/skills/alpha", "state": "content", "files": 1, "bytes": 17, "newest": "2026-11-17T10:03:11Z"}
    ]
  },
  "store": {"state": "content", "files": 1, "bytes": 43, "newest": "2026-11-17T10:03:11Z", "path": "skills/alpha"},
  "case": "reconcile",
  "default_action": null,
  "choices": ["take-store", "keep-machine", "adopt"],
  "recorded": null,
  "decision": null,
  "decided_by": null,
  "note": "2 different copies on this machine; adopt takes the newest copy (~/.agents/skills/alpha)",
  "waiting": true
}
```

- `machine.state`: `content`, `empty`, `linked`, `drift`, `external`. Each place has its own `state`
  (`absent`, `empty`, `content`, `linked`, `drift`, `external`), and `target` and `drift` (`broken` or
  `elsewhere`) for links.
- `store.state`: `content`, `none`, or `template` (instructions only). A box unit's store side also has
  `writer` (from the index), and its machine side `folders` and `lower_bound`.
- `default_action`: the action for this case, or `null` (a choice is needed, or for a box unit in the
  `nothing` case, nothing happens unless you choose).
- `decision` and `decided_by` (`default`, `recorded` or `null`): what apply does without a choices file.
- `waiting`: true when the unit needs a choice before apply touches it.
- `fresh` is true when no unit waits for a choice. Units decided earlier on this machine count as settled.

A choices file:

```json
{
  "instructions:claude": "adopt",
  "skill:alpha": "take-store",
  "skill:my-draft": "keep-local",
  "memory:my-project": "merge",
  "box:my-project": "reader"
}
```

## install

`agent-mirror install` (the cloud setup scripts use it) computes the plan and:

- on a fresh machine applies the defaults: the same links as before 0.3.0, except that the shared skills
  folder now holds one link per skill;
- on a machine with units in `first-setup` or `reconcile`, applies only the units that need no decision,
  lists the units waiting for a choice and says to run `agent-mirror plan`;
- converts a legacy whole-folder skills link;
- with `--strict`, exits with code 4 and changes nothing when the machine is not fresh.

On a cloud runtime, `session-start` does the same for the runtime's agent.

## One writer per box

`box push` keeps one writer per box with a marker at `<inbound>/_writer.json` (machine id, machine
name, date, tool version):

- The first push for a project writes the marker. A push from a machine with another id is refused, and
  the message names the current writer; `box push --take-over` rewrites the marker.
- A machine whose recorded role for the project is `reader` or `none` is refused before any transfer.
- This works on both backends (azure-blob and rclone) and in `--dry-run`, which reads the marker and says
  what it would do.
- `box index` copies the marker into the index as one line `{"path": "_writer.json", "writer": {...}}`,
  so `plan` names the writer offline. Readers of the index skip that line.

`agent-mirror box candidates --project NAME --project-path PATH [--json] [--min-bytes N]` helps decide
what goes in a box. It is read-only. It lists the entries git does not track or ignores (from
`git status --porcelain --ignored`, with the ignore rule from `git check-ignore -v`), or every top-level
entry when the folder is not a git repo, each with its total bytes, file count, files at or above the
large-file threshold, newest change, a kind guess and whether `box.include` or `box.archives` already
cover it:

| Kind | Examples |
| --- | --- |
| `rebuildable` | `.venv`, `venv`, `node_modules`, `__pycache__`, `.mypy_cache`, `.ruff_cache`, `.pytest_cache`, `dist`, `build`, `.tox`, `.gradle`, `target`, `*.egg-info` |
| `secret` | `.env`, `*.env`, `*.pem`, `*.key`, `*.p12`, names with `credential` or `secret` in them |
| `worktree` | a folder with its own `.git` (a git worktree or a nested clone) |
| `candidate` | everything else |

Size walks use `os.scandir`, never follow links, stop below 12 folder levels and share a time budget
(`--time-budget`, default 30 s); a size that stopped early is shown with `+` (`"lower_bound": true`).
