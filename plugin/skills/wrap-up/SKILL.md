---
name: wrap-up
description: End-of-day wrap-up on the user's own machine, to persist the day's work in the current project before the user leaves. Lists what the work made that is in no git commit and not in the box, sends what is worth keeping to the box with a note and declares the rest disposable, commits small files that belong in git, pushes the box folders this machine owns when they changed, syncs memory, and ends with a short list of what went where. Use when the user says "wrap up", "wrap-up", "end of day", "before I leave", "I'm leaving", "persist my work", "save my work before I go", or runs /agent-mirror:wrap-up. With "preview" or "dry run" it changes nothing and reports what it would do.
---

# Wrap up the day

A developer machine has no stop hook: nothing asks, when a session ends, about the files the work left outside git and the box. This skill does that check once, for the current project, and ends with a short record of what went where.

## Rules

1. **One project**: the one the session runs in (see Scope). Never act on other clones.
2. **Never delete files.** Leaving a file behind is `box skip`, which only records it.
3. **Never change the box's settings or ownership**: no `--take-over`, `--delete`, `box release`, `box note` or `box prune`, and no edits to the store's `env.json` or `store.json`.
4. **Never send secrets or env files**, and never put secret text in a note or a reason.
5. **Ask only when an action needs the user's word**: anything destructive, or, on a developer machine, an upload above 1 GiB (one send, or one push in all). Otherwise decide, act and report.
6. **Never push git commits or open pull requests** unless the user or the project's instructions say so.
7. **Preview.** If the arguments or the user's words contain `preview` or `dry run`, change nothing: run only the read-only commands (those of Scope and step 1, and `agent-mirror sync --dry-run`) and report what each step would do.
8. **Cloud machine** (`agent-mirror detect` prints `claude-cloud` or `codex-cloud`): the skill works the same, but skip step 4 (a cloud machine never writes the shared side). On Claude cloud the stop check also asks for the box decisions when the session ends.

## Scope (read-only)

From the folder the session runs in:

```
agent-mirror detect
agent-mirror box unsent --all --json
```

`project` and `project_path` in the JSON name the project and its clone; work only there. When the session runs in a folder above several clones, add `--project <name>` to every `agent-mirror box` command.

- It says that no project of the store matched (exit 1), or the JSON has `"box_configured": false`: say so in one line, do step 3 only (in the git repo of the current folder, if there is one), report and stop.
- `agent-mirror` is missing or has no store: the same.

## Steps

### 1. Look (read-only)

```
agent-mirror box unsent --all
git status --short
```

- `box unsent` lists the files of the clone made or changed since the session baseline it prints, in no git commit and not in the box: those for the box, then the small untracked files left to git. In the JSON from Scope, `entries` gives each one's `path`, `bytes`, `files`, `folder` and `class` (`for_the_box` or `left_to_git`). Older files are not listed; `git status --short` still shows them when untracked.
- On a developer machine, also see which shared folders this machine owns and whether they changed since its last push:

```
agent-mirror box owners --json
```

Each row of `folders` has `folder`, `mine` (this machine owns it), `local` (the copy here against the owner's last push: `same`, `different`, `fetched`, `unknown`, `absent` or `unowned`), `files_here` and `bytes_here`.

### 2. Files for the box

For each entry of class `for_the_box`:

- **Inside a folder that `box owners` lists with `mine` true**: step 4 carries it. Do not also send it. (The folder `.` stands only for the files that the box's top-level include patterns name.)
- **KEEP** results that took real time or money to produce, analysis outputs, generated reports and figures, anything a later session would otherwise have to redo:

```
agent-mirror box send <path> --label <label> --note "<what these files are, why they were made, what to do next>"
```

  One path per send: for related files, send the folder that holds them, with one note (`--archive` packs a folder of many small files into one file). Use one short label for the whole wrap-up, for example `wrap-up-<date>`. On a developer machine, above 1 GiB name the path and its size and ask first.
- **LEAVE** caches, anything rebuildable in minutes, scratch, secrets and env files:

```
agent-mirror box skip <path>... --reason "<why it can go>"
```

Then run `agent-mirror box unsent` again: what it still lists for the box is carried by step 4, or still needs a decision.

### 3. Files left to git

First read the project's instructions (`AGENTS.md`, `CLAUDE.md`, `CONTRIBUTING.md`) for its commit rules (message format, signing, hooks, trailers, and whether you may commit at all) and follow them. Stage files by name, never everything at once.

- Small new files (class `left_to_git`, or `??` in `git status --short`) that belong to the work: commit them, one commit per group of related files.
- New files that look unrelated, generated or secret: do not commit them; list them as "left untracked".
- Tracked files with uncommitted changes: commit them only when they clearly belong to finished work and the project's rules allow it; otherwise list them as "uncommitted changes left as they are".
- Do not push and do not open a pull request unless the user or the project's instructions say so.

### 4. Box folders this machine owns (developer machine only)

From step 1, the rows with `mine` true and `local` `different` or `unknown`. If there are none, say "nothing changed". Otherwise name them with their size first (`bytes_here` in all, an upper bound for the upload; above 1 GiB, ask), then push only those, one `--only` per folder:

```
agent-mirror box push --project <name> --only <folder>
```

`box push` uploads only what changed. Without `--only` it would also claim the folders nobody owns (`local` `unowned`): leave those and list them under "Left as it is" (who owns a folder is the user's call). In a preview, report the rows; do not push.

### 5. Memory

```
agent-mirror sync
```

It commits and pushes the memory written today to the store (the hooks have usually done it already). In a preview: `agent-mirror sync --dry-run`.

### 6. Report

A short list under these headings, one line per item, "none" under an empty heading, no prose:

- **Sent to the box**: path, label
- **Skipped**: path, reason
- **Committed**: subject, files; then whether the commits were pushed
- **Pushed to the shared box**: folders, or "nothing changed"
- **Left as it is**: path, why ("left untracked", "uncommitted changes left as they are", "owned by nobody", "waits for your word")
- **Failed**: the command and its error

In a preview, the same headings with "would".
