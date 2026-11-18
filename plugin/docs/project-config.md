# Project config: `projects/<name>/env.json`

One file per project in the store. Every key is optional except `repo`. A complete example lives in
`plugin/templates/store/projects/_example/env.json` (copied into every new store).

Never put a secret value in this file. It holds environment variable NAMES only; the values go on the
cloud vendor's settings page or in your local environment.

| Key | Type | Used by | What it does |
| --- | --- | --- | --- |
| `repo` | `"owner/name"` | `session-start`, `doctor`, `probe`, `cloud render` | Recognises a clone of this project by its `origin` URL (https or ssh, with or without `.git`, any letter case), so sessions find the project without flags, including sessions that start in a folder above several clones (see below). |
| `python` | version text, e.g. `"3.11.9"` | `cloud render` | The setup scripts install this Python with `uv` (and install `uv` itself if missing). `probe --full` checks `uv python find`. |
| `dependency_sync` | one command | `session-start` (cloud), `probe --full` | Run in the project folder at cloud session start, e.g. `"uv sync --frozen"`. |
| `memory_project` | text | `install`, `session-start`, `sync` | Folder name under `memory/` for this project (default: the project name). |
| `allowed_hosts` | list of host names | `cloud render` | Hosts the cloud network must allow; joined with the hosts agent-mirror itself needs. |
| `env_var_names` | list of names | `probe`, `cloud render`, `cloud secrets` | Environment variables the project needs. `probe` reports which are set (never values); `cloud render` writes the paste-ready name list. |
| `secrets` | list of recipe entries | `cloud secrets`, `cloud render`, `cloud runbook`, `doctor` | How each value in `env_var_names` is produced on a machine, never the value itself. See "Secret recipes" below. |
| `setup_commands` | list of shell lines | `cloud render` | Extra lines for the cloud setup scripts (for example installing a cloud CLI). Each runs in a subshell and may fail without stopping the script. agent-mirror never runs them itself. |
| `session_start_commands` | list of commands | `session-start` (cloud) | Run in the project folder after `dependency_sync` on a cloud runtime (for example a sign-in script kept in the store). |
| `box` | object | `box`, `doctor`, `probe --full`, `cloud render` | Where bulk files go. See below. |
| `probe` | object | `probe`, `probe --full` | Extra checks. See below. |
| `pointers_file` | path relative to the store root | `session-start` | A file you keep in the store that says where the project's large data lives. `session-start` prints its path so the agent can read it. The format is yours. |
| `companion_repos` | list of `{"repo", "ref", "dir"}` | `cloud render`, `session-start` (cloud) | Public repos a cloud session needs next to the project but that cannot be attached to the session. See "Companion repos" below. |

## Commands that agent-mirror runs (`dependency_sync`, `session_start_commands`, `probe.commands`)

agent-mirror never passes these to a shell, so they work the same on Linux, macOS and Windows:

- A text value is split into words the way a shell would (`shlex`), but pipes, `&&`, redirects and
  `$VARIABLES` are not interpreted. Wrap anything like that in a script and run the script.
- A list value is used as the argument list as it is.
- `{store}` becomes the store folder and `{project_dir}` the project folder (after splitting, so
  Windows paths survive). A leading `~` is expanded.

`setup_commands` are different: they are pasted into bash setup scripts as written.

## Secret recipes (`secrets`)

A second machine should be able to set up a cloud environment from the store alone. The store says where
each value comes from; `agent-mirror cloud secrets` produces the values on the machine you run it on.
Only these recipe types exist. Anything else is rejected with a clear error, and no free-form command from
the store is ever run.

| Recipe | Shape | Produces |
| --- | --- | --- |
| `rclone-remote` | `{"recipe": "rclone-remote", "remote": "NAME"}` | every `RCLONE_CONFIG_<NAME>_*` variable from this machine's rclone config (as `box rclone-env` does). A missing remote becomes "needs you: run `rclone config`, create a remote named NAME of the same storage type". |
| `local-file` | `{"recipe": "local-file", "path": "~/...", "names": ["A", "B"]}` | the listed names from `NAME=value` lines in an owner-only file on this machine (`export ` and quotes allowed). A missing file, a file others can read, or a missing name becomes "needs you". |
| `env` | `{"recipe": "env", "names": ["A"]}` | the values from the current process environment. |
| `literal` | `{"recipe": "literal", "name": "X", "value": "..."}` | a non-secret constant, for example a runtime marker or a tenant id. A value that matches the secret-scan patterns is refused. |
| `azure-keyvault` | `{"recipe": "azure-keyvault", "vault": "V", "secrets": {"VAR_NAME": "secret-name"}}` | `az keyvault secret show --vault-name V --name secret-name --query value -o tsv`; the value is captured and never printed. `az` missing or failing becomes "needs you". |
| `ask-user` | `{"recipe": "ask-user", "name": "X", "where": "env" or "setup-script", "placeholder": "<X>", "instructions": "..."}` | nothing: only the user can create it (a token, say). With `"where": "setup-script"` the value replaces the placeholder in the setup script on the vendor page and never goes into a values file. |

Automatic entries, added even when the config does not list them:

- Claude: `STORE_REPO_TOKEN`, `ask-user` with `"where": "setup-script"` and placeholder `<STORE_REPO_TOKEN>`
  (the store token in `setup.sh`).
- Codex: `AGENT_RUNTIME` as the literal `codex-cloud`, and `STORE_REPO_TOKEN` as `ask-user` (the
  setup-only secret of the legacy container product).

Every variable in `env_var_names` (plus the Azure sign-in names an `azure-blob` box adds) should be
covered by a recipe. `doctor` and `cloud render` warn about names no recipe covers, and about recipe
names that `env_var_names` does not list.

- `agent-mirror cloud secrets --project NAME [--vendor claude|codex] --check` prints, for each variable,
  its recipe and whether this machine can produce it now: `ready`, `ask the user (...)` or
  `needs you: <instruction>`. It never prints a value. Exit code 0 when everything is ready or only
  `ask-user` items remain, 3 when something else is missing.
- `... --out FILE` writes `NAME=value` lines (quoted so a shell and a dotenv reader both read them back)
  for every value it can produce to a new file readable only by you (mode 0600 on Linux and macOS; on
  Windows, which ignores 0600, the file should be inside your user profile, and the output says whether
  it is). It refuses a path inside the store,
  inside any git work tree, or an existing file (`--replace` overwrites an existing owner-only file). It
  prints names and value lengths only, lists what still needs you, and reminds you to delete the file
  after pasting. `agent-mirror clip copy FILE --secret` puts the file on the clipboard without printing it.
- `agent-mirror cloud runbook --project NAME [--vendor claude|codex|all] [--json]` prints the setup
  steps for a person, or the runbook JSON (see store-layout.md).

## Companion repos (`companion_repos`)

A cloud session can only attach repos you own or were given access to. A public repo that the project
expects next to its clone (a library checked out beside it, say) is cloned by the setup script instead:

```json
"companion_repos": [
  {"repo": "https://github.com/example-org/helper-lib", "ref": "v1.2.0", "dir": "helper-lib"}
]
```

- `cloud render` adds a step to every setup script that clones each entry into `~/companions/<dir>` at
  `ref` without any token: `git init`, `git fetch --depth 1 origin <ref>`, `git checkout FETCH_HEAD`. A
  failure is noted and never stops the script. The repo's host is added to the allowed hosts.
- On a cloud runtime, `session-start` links `~/companions/<dir>` next to the project clone as `../<dir>`
  when nothing is there yet (a link made in Python, no shell). Something already at `../<dir>` is left
  alone.
- `repo` must be a public `https://host/owner/name` URL (no credentials, port, query or other scheme).
  `ref` is a branch, tag or commit (letters, digits, `.`, `_`, `/`, `-`). `dir` is one folder name: no
  path separators and not dots only. Anything else makes `cloud render` stop with a clear message, and
  `session-start` skips that entry with a warning.

## Sessions above several clones

The environment is per user, not per repo. A session may run inside one clone, or in a folder above
several clones (a cloud session that cloned several repos). `session-start`, `doctor` and `probe` work
out which projects a session touches:

1. `--project NAME` forces one project (at `--project-path`, or the current repo or folder).
2. If the session folder is inside a git repo, that repo is the only candidate.
3. Otherwise the child folders at depth 1 and 2 are scanned for git repos. Hidden folders and vendor
   folders (`node_modules`, `venv`, `vendor`, `dist`, `build`, `target` and similar) are skipped,
   symbolic links are not followed, and the scan stops after 2000 folders or 50 repos.

Each repo is matched to a project by the config's `projects` map, then by its origin URL against `repo`,
then by folder name. A repo with no project entry is listed as "no project entry, nothing to carry" and
otherwise ignored; finding nothing is not an error. For every match, a cloud `session-start` runs that
project's `dependency_sync` and `session_start_commands` inside that clone and links Claude's memory
folder for that clone to the project's memory.

Claude keys memory to the folder the session runs in. When the session runs above the clones and exactly
one project matched, the session folder's memory folder is linked to that project's memory as well. When
several projects matched, `session-start` prints each project's memory index path so the agent can read
them; how Claude picks its memory folder in that case is UNVERIFIED. Commands that work on one project
(`box`, `install --project-path`) stop with "several projects found" and ask for `--project`.

## `box`

The box moves untracked bulk files (results, figures, slides) between machines and sessions. It has two
sides:

- **shared**: one tree of durable files. Developer machines upload it with `box push`, each the folders
  it owns ("Folder owners" below); every machine and session reads it (`box index`, `box ls`,
  `box fetch`). Cloud sessions never write it.
- **sessions**: one namespace per session. `box send` writes into the namespace of the session it runs
  in; every machine and session lists them (`box sessions`) and fetches them (`box fetch --session`). A
  developer machine copies what is worth keeping into the shared tree (`box promote`) and deletes old
  sessions (`box prune`). See "Sessions" below.

Older versions called the two sides **inbound** (shared) and **outbound** (sessions). The old key names
still work, and `doctor` says so in one line:

| Backend | Shared side | Sessions side | Older names (still read) |
| --- | --- | --- | --- |
| azure-blob | `shared_container` | `sessions_container` | `inbound_container`, `outbound_container` |
| rclone | `shared_remote` | `sessions_remote` | `inbound_remote`, `outbound_remote` |

When both a new and an old name are set, the new one is used. Objects inside an existing box keep their
places (`_owners.json`, `_writer.json` and `_archives/` on the shared side).

Common keys:

- `backend`: `"azure-blob"`, `"rclone"` or `"none"`. Missing or `"none"`: every box command prints
  "box not configured" and exits 0.
- `include`: patterns of files that belong in the box, relative to the project folder.
- `exclude`: patterns that never go in the box. Env files (`.env`, `*.env`) are always excluded and
  `box send` refuses them, whatever the config says.

Settings for on-demand retrieval (all optional):

| Key | Default | Meaning |
| --- | --- | --- |
| `archives` | none | folder paths or globs relative to the project folder (for example `["ws-*", "decompile/project*"]`); every matching folder is sent as one bundle; see "Archive mode" |
| `archive_large_file_bytes` | `52428800` (50 MiB) | in archived folders, files this big or bigger travel on their own instead of inside the bundle; `0` keeps everything in the bundle |
| `toc_inline_bytes` | `5000000` | a table of contents bigger than this goes into its own `<name>.toc.json.gz` instead of the sidecar |
| `min_free_bytes` | `2147483648` (2 GiB) | free disk that every fetch and receive must leave; `--force` overrides |
| `receive_dir` | `_agent_outbound` | where `box receive` puts the sessions it downloads (relative to the project folder, or absolute; the default keeps its older name) |
| `mount_dir` | `_box` | where `box mount` mounts the shared side (relative to the project folder, or absolute) |

Patterns: `**` crosses folders, `*` and `?` do not, and `**/` also matches no folder. A pattern without
`/` matches a file name at any depth.

### `"backend": "azure-blob"`

Uses the Azure CLI (`az`) with Azure AD sign-in only (`--auth-mode login`, never account keys or SAS).

| Key | Meaning |
| --- | --- |
| `storage_account` | the storage account name, e.g. `"mystorageaccount"`. While empty, box commands do nothing. |
| `shared_container` | container of the shared tree (developer machines write, everyone reads); older name `inbound_container` |
| `sessions_container` | container of the session namespaces (every session writes its own, everyone reads); older name `outbound_container` |
| `prefix` | folder inside both containers, e.g. the project name |

- `include` entries must be `folder/**` (no wildcards); `exclude` entries must be `folder/**`,
  `**/name`, `name` or `**/folder-name/**`, because `az storage blob sync` can only exclude path
  prefixes and file names. Other shapes are refused with a clear message.
- `box push` runs one `az storage blob sync` per include folder with `--delete-destination false`
  (pass `--delete` to delete remote files whose local file is gone). It is refused on a cloud runtime.
  Each folder has one owning machine: see "Folder owners" below.
- `box fetch` downloads through a staging folder and never overwrites a local file on a local runtime.
- `box send` uploads to `<sessions_container>/<prefix>/<session id>/<path>`; a second send of a path in
  the same session replaces it. `box sessions` lists the namespaces with a `/` delimiter listing (when the
  listing comes back flat, the first path parts are used); `box prune` deletes one with
  `az storage blob delete-batch --pattern '<prefix>/<session id>/*'`.
- A cloud identity needs read access to the shared container and read and write access to the sessions
  container (it reads other sessions there).
- On a cloud runtime, when `az account show` fails, agent-mirror signs `az` in as a service principal
  from `AZURE_CLIENT_ID`, `AZURE_CLIENT_SECRET` and `AZURE_TENANT_ID`. The secret goes to `az` on stdin,
  never on a command line, and is replaced by `***` in all output.
- Install `az` in cloud sessions with a `setup_commands` line. One way that needs only the PyPI hosts:
  `uv tool install --prerelease=allow azure-cli` (current azure-cli pins pre-release SDK packages)
  plus a small wrapper on PATH that runs the tool's own Python with `-m azure.cli`.

### `"backend": "rclone"`

Uses `rclone`, which reaches OneDrive, S3, Google Drive, SFTP and many more.

| Key | Meaning |
| --- | --- |
| `shared_remote` | `"remote:path"` as rclone names it, e.g. `"onedrive:agent-box/shared/my-project"`; older name `inbound_remote` |
| `sessions_remote` | `"remote:path"` for the session namespaces, e.g. `"onedrive:agent-box/sessions/my-project"`; older name `outbound_remote` |

- `box push`: `rclone copy <project> <shared_remote>` with `--filter` rules built from `include` and
  `exclude` (env files first, then excludes, then the include patterns of the folders this push carries,
  then `- **`). `rclone sync` only with `--delete`; folders the push does not carry are filtered out, so
  sync never deletes them.
- `box index`: `rclone lsjson -R --files-only` into `projects/<name>/box_index.jsonl`.
- `box fetch`: `rclone copyto` (a file) or `rclone copy` (a folder), with `--ignore-existing` on a
  local runtime so local files are never overwritten.
- `box send`: `rclone copyto`, or `rclone copy --files-from <list>` for a folder (the list leaves out
  env files and excluded paths), into `<sessions_remote>/<session id>/<path>`. `box sessions` lists with
  `rclone lsjson --dirs-only`, `box fetch --session` downloads with one `rclone copy --files-from`, and
  `box prune` deletes with `rclone purge <sessions_remote>/<session id>`.
- Credentials stay in rclone's own config (`rclone config`), or, in a cloud session, in
  `RCLONE_CONFIG_<REMOTE>_*` environment variables listed in `env_var_names`.

### Archive mode

For folders with very many small files on slow backends (a consumer cloud drive, say), set `box.archives`.
Each entry is a folder path or glob relative to the project folder, with one or more `/`-separated
levels: `"ws-*"`, `"decompile/project*"`, `"decompile/binaries-*"`. Every folder that matches becomes its
own archive, named by its relative path (`decompile/project-cli`), is left out of the file-by-file push,
and travels as:

```
<shared>/_archives/<name>.tar.gz          the bundle: every file below archive_large_file_bytes
<shared>/_archives/<name>.large/<path>    each larger file, on its own
<shared>/_archives/<name>.json            the sidecar (uploaded last)
<shared>/_archives/<name>.toc.json.gz     the table of contents, only when it is too big for the sidecar
```

For `decompile/project-cli` that is `_archives/decompile/project-cli.tar.gz` and so on.

- `*`, `?` and `[...]` match within one level; `**` is not allowed. Matching never follows links.
- A folder inside an archived folder is never an archive of its own (with `["decompile",
  "decompile/project*"]`, `decompile` is the one archive).
- A folder should match one entry. When it matches two, the first entry wins and `doctor` warns (also
  for the case above).
- Entries cannot start at `_archives`, `.agent-mirror` or `.git`, and a parent folder cannot end in
  `.large`.
- An include folder inside an archived folder is carried by the archive; an archived folder inside an
  include folder is left out of that folder's sync and travels as its own archive.
- Changing an entry (say from `decompile` to `decompile/project*`) leaves the old `_archives/decompile.*`
  objects in the box until you delete them; `box ls` and `box fetch` use the archive the current entries
  name.

The sidecar holds the file count, total bytes, newest modification time, the archive size, the
agent-mirror version, a fingerprint (made from the sorted relative paths, sizes and modification times),
and the table of contents: every file's relative path and size, marked `"in": "bundle"` or `"in": "large"`.

- `box push` packs with the `tar` program when present (Python's `tarfile` otherwise), applies the exclude
  rules, always leaves out env files, and never follows or packs symbolic links. It checks free disk in
  the temp folder first (set `TMPDIR` to use another disk). It skips the upload when the remote sidecar's
  fingerprint matches, re-sends the bundle only when a small file changed, sends only the large files that
  changed, and removes the temp archive afterwards. Remote large files that no longer exist locally stay.
- `box index` lists each archive as one entry (`"archive": true`, file count, bytes, large files,
  fingerprint) and writes its table of contents to `projects/<name>/box_toc/<folder>.json` (gzipped when
  big, in sub-folders for nested names) in the store.
  Commit those files with the index so a cloud agent can browse without downloading anything.
- `box fetch <archived folder>` downloads the bundle, extracts it safely (absolute paths, `..` members,
  devices and links pointing outside are rejected) and prints the large files it did not fetch, with their
  sizes and the exact command for each. `box fetch <path to a large file>` fetches that one file.
  `box fetch <folder> --all` fetches the bundle and every large file. `box fetch <sub-path>` extracts only
  that part of the bundle and lists the large files under it. This works the same for nested names
  (`box fetch decompile/project-cli/data`). A local runtime never overwrites existing files; a cloud
  runtime does.
- `box send <folder> --archive` sends a folder into the session as one `.tar.gz`; `box fetch --session`
  extracts it with the same safety checks.

### Folder owners

A box can have several writer machines, each owning its own folders. The units of ownership are the
**folder units**:

- every archived folder (a folder matching a `box.archives` entry, named by its relative path);
- every include folder: the folder part of an include pattern (`results` for `results/**`). An include
  folder inside another one belongs to the outer one;
- on rclone, include patterns that do not start with a folder (`*.pdf`, `notes.md`) form the unit `.`: the
  matching files that are not inside another unit's folder.

`<shared>/_owners.json` (azure-blob: `<shared_container>/<prefix>/_owners.json`; rclone:
`<shared_remote>/_owners.json`) names the owning machine of each folder:

```json
{"schema": 1, "folders": {"<folder>": {"machine_id": "...", "machine_name": "...", "since": "...",
                                       "fingerprint": "...", "updated": "..."}}}
```

`box push` claims folders nobody owns, uploads the folders this machine owns, and skips (not an error)
folders another machine owns, saying whether the local copy is the same. `--only FOLDER` limits a push,
`--take-over FOLDER` moves a folder to this machine, `box release FOLDER` gives one up (no data is
deleted), and `box owners` lists them all. A machine whose recorded role for the project
(`agent-mirror apply`, unit `box:<project>`) is `reader` or `none` is refused before any transfer. The
remote has no locking: every change reads the file again, changes only this machine's entries, writes and
checks. Neither `--delete` mode removes `_owners.json`, and `--delete` deletes only inside the folders the
push carries. The fingerprint, the old single-writer marker (`_writer.json`) and a worked example with two
machines: reconciliation.md, "Folder owners".

### What could go in the box (`box candidates`)

`agent-mirror box candidates --project NAME --project-path PATH [--json] [--min-bytes N]` is a
read-only helper: it lists the project's entries that git ignores or does not track (every top-level
entry when the folder is not a git repo) with sizes, file counts, large files, newest change, a kind
guess (`rebuildable`, `secret`, `worktree`, `candidate`) and whether `box.include` or `box.archives`
already cover them (`partly covered` when archived folders sit inside an entry). Details: reconciliation.md.

### On-demand retrieval

Cloud machines have little disk (about 30 GB is common), so nothing is fetched up front.

- `box ls [PATH]` browses the box from the committed index and tables of contents, offline, with sizes and
  what is already here (`here`, `differs`, `-`, or `n/m` files of a folder).
- Every fetch first adds up the bytes it would write (from the listing or the sidecar) and refuses when
  that would leave less than `box.min_free_bytes` free, saying how much is needed and how much is free.
  `--force` overrides.
- Every file a fetch writes is recorded in `<project>/.agent-mirror/fetched.json`. agent-mirror adds
  `/.agent-mirror/` to the clone's own `.git/info/exclude` (never to the tracked `.gitignore`), so the
  record is never committed. `box receive` and `box mount` add their folders there too when they sit
  inside the project.
- `box status` shows what was fetched on this machine, how much of it is still here, and free disk.
- `box evict <path>` deletes fetched copies under a path to free disk. It runs only on cloud runtimes
  unless you pass `--force`, deletes only files recorded as fetched from the box, and keeps any file that
  changed since it was fetched.

### Sessions

Every session writes its own namespace on the sessions side:

```
<sessions>/<session id>/<path>             what box send uploaded
<sessions>/<session id>/<folder>.tar.gz    a folder sent with box send --archive
<sessions>/<session id>/_session.json      the manifest
```

- A session id is `<runtime>-<YYYYMMDDTHHMMSSZ>-<6 hex>`, for example
  `claude-cloud-20261117T101500Z-3fa2c1`. The first `box send` makes it and remembers it in
  `<project>/.agent-mirror/session.json` (git-excluded); every later send of the session uses it. On a
  cloud runtime a new machine is a new session. On a developer runtime a new session starts with
  `box send --new-session`, or when the remembered one is older than 24 hours.
- `box send --label TEXT` gives the session a short label on first use (letters, digits, `-`, `_`, `.`;
  up to 60). A later different label is ignored with a note.
- The manifest is updated after every successful send: read, merge, write, read back (and tried again on
  a mismatch, like `_owners.json`). Its shape:

```json
{
  "schema": 1,
  "id": "claude-cloud-20261117T101500Z-3fa2c1",
  "label": "exp-a",
  "runtime": "claude-cloud",
  "machine": {"id": "3f2a9c1e5b7d4e6f8a0b1c2d3e4f5a6b", "name": "my-laptop"},
  "project": "my-project",
  "repo": "example-org/my-project",
  "git": {"branch": "main", "commit": "1a2b3c4d5e6f..."},
  "started": "2026-11-17T10:15:00Z",
  "updated": "2026-11-17T10:40:12Z",
  "tool": "agent-mirror 0.5.0",
  "files": [
    {"path": "results/summary.csv", "bytes": 1520, "mtime": "2026-11-17T10:39:58Z"},
    {"path": "results/run-3", "object": "results/run-3.tar.gz", "archive": true, "files": 212,
     "bytes": 5242880, "archive_bytes": 901234, "mtime": "2026-11-17T10:30:00Z"}
  ],
  "promoted": [
    {"path": "results/run-3", "to": "results/run-3", "folders": ["results"], "machine_id": "7d4e...",
     "machine_name": "desk-a", "date": "2026-11-18T09:00:00Z"}
  ]
}
```

  `machine` is null when the machine has no id yet; `git` values are null outside a git clone. The
  manifest never holds environment values or absolute paths.
- `box sessions [--json] [--limit N]` (any runtime, read-only) lists the namespaces newest first by start
  time: id, label, runtime, branch and short commit, file count, total size, last update. It reads one
  listing of the sessions side and one manifest per listed session; older namespaces
  (`<runtime>-<YYYYMMDDTHHMMSSZ>`, before 0.5.0) are listed from their files as "no manifest". Without the
  transfer program it shows the summary of the last `box index`.
- `box fetch --session ID [PATH] [--into DIR]` downloads a session's files (all, or PATH inside it) into
  `<project>/_sessions/<session id>/` (`--into` is relative to the project folder; `--into .` writes over
  the project tree). ID is the id, a unique start of it, or a label that exactly one session has. A
  developer runtime never overwrites a local file; the disk guard applies; archives are extracted with
  the safety checks of the shared side. `/_sessions/` is added to `.git/info/exclude`; `box status` lists
  fetched sessions and `box evict _sessions/<session id>` deletes them.
- `box promote --session ID PATH [--as FOLDER]` (developer machines) copies a session's file or folder
  into the shared tree at FOLDER (default PATH). It goes through the local disk: the files are downloaded
  into a temp folder, placed into this machine's copy of the folder (never over a different local file),
  and that folder unit is pushed with the normal ownership logic, so a folder nobody owns is claimed. It
  is refused when the folder is owned by another machine, or when FOLDER is not covered by `box.include`
  or `box.archives` (the message says what to add). The manifest records what was promoted, by which
  machine, when and to where. `--dry-run` shows the plan.
- `box prune [--older-than DAYS] [--session ID ...] [--keep-promoted] [--yes]` (developer machines)
  deletes session namespaces. It always lists them with sizes first and deletes only with `--yes`; it
  never touches the shared tree and refuses names that do not look like session ids.
- `box index` adds one line `{"path": "_sessions", "sessions": [...], "more": N}` with the id, label,
  runtime, branch, commit, last update, file count and size of the newest 50 sessions, for offline use;
  `box sessions` stays the live source.

#### Cost on slow remotes

On some remotes (a consumer cloud drive through rclone, say) every call to rclone or az costs seconds: the
program starts, signs in and makes its round trips. So the sessions commands use a fixed number of calls,
however many sessions the box holds:

| Command | Calls |
| --- | --- |
| `box sessions` | 2: one recursive listing of the sessions side (it also gives the file counts and sizes of namespaces without a manifest), one batched read of the manifests |
| `box fetch --session ID` | 3 for a full id (that namespace's listing, its manifest, one download), a unique start of an id (the listing, the manifest, one download) or a label (the listing, the batched manifest read, one download); archives come in the same download |
| `box send` | 3 for the first send of a session (upload, manifest write, read back), 4 later (upload, manifest read, write, read back) |
| `box prune --yes` | rclone: 5 (listing, manifests, one `rclone delete` with an anchored filter `+ /<session id>/**` per selected namespace and `- **`, one `rclone rmdirs` with the same filters for the emptied folders, a check); azure-blob: one `delete-batch` per selected namespace plus 3 |
| `box index` | 2 for its session summary |

Fetching by full id is the cheapest lookup: it never looks at other sessions. `--limit` cuts what
`box sessions` reads: rclone downloads only the manifests it shows; azure-blob downloads every manifest in
the one batched call (one pattern) and the list is cut afterwards. A batched azure-blob download is one
`az storage blob download-batch` whose pattern is chosen from the listing; when every pattern would also
fetch more than 16 MiB of other files, it goes file by file instead.

### `box receive`

`box receive [--into DIR]` downloads every session's files that are not in `DIR` yet (default
`receive_dir`, else `<project>/_agent_outbound`), one folder per session, never overwriting existing local
files, after the same disk check. It is refused on a cloud runtime. To look first, use `box sessions`; to
get one session anywhere, `box fetch --session`.

### `box mount` (rclone only)

`box mount [--cache-max-size 10G]` runs `rclone mount <shared_remote> <mount_dir> --read-only
--vfs-cache-mode full --vfs-cache-max-size <size> --daemon`: files download when opened and the local
cache stays under the cap. `box unmount` runs `fusermount3 -u` (or `fusermount -u`) on Linux and
`umount` on macOS. It needs FUSE: on Linux `/dev/fuse` and a `fusermount3` or `fusermount` program, which
agent-mirror checks first; `probe --full` reports whether mounting looks possible. UNVERIFIED on cloud
machines (many containers have no FUSE), on macOS (macFUSE) and on Windows (WinFsp; agent-mirror only
prints the command there).

### rclone remotes from environment variables

A cloud machine has no rclone config file. rclone also reads a remote from environment variables named
`RCLONE_CONFIG_<REMOTE>_<KEY>`, for example `RCLONE_CONFIG_MYDRIVE_TYPE=onedrive`,
`RCLONE_CONFIG_MYDRIVE_TOKEN`, `RCLONE_CONFIG_MYDRIVE_DRIVE_ID` and `RCLONE_CONFIG_MYDRIVE_DRIVE_TYPE`.

- A remote defined this way is named in lower case (`mydrive:`). Use lower-case names in
  `shared_remote` and `sessions_remote`, and use only letters, digits and `_` in remote names.
- Put the variable names in `env_var_names` (so `cloud render` lists them) and the values on the vendor's
  settings page.
- An OAuth token stored in a variable cannot be refreshed on a throwaway machine: rclone refreshes it in
  memory, but nothing writes the new token back. Replace the value before it expires.
- `agent-mirror box rclone-env --remote mydrive` reads the local remote (`rclone config dump`) and prints
  the variable NAMES only. `--out FILE` writes the `NAME=value` lines to a new file readable only by you
  (it refuses an existing file and any path inside the store). It never prints a value, and it shows the
  token's expiry time.

## `probe`

| Key | Meaning |
| --- | --- |
| `https_urls` | URLs that `probe` fetches (any HTTP answer counts as reachable) |
| `tcp_hosts` | `"host:port"` entries that `probe` connects to; `probe --full` also resolves the names |
| `commands` | list of `{"name": ..., "run": ...}`; `probe --full` runs each in the project folder and shows the last lines |

`probe` also checks `https://<store host>` when the store has a remote.

## Complete example

```json
{
  "repo": "example-org/my-project",
  "python": "3.11.9",
  "dependency_sync": "uv sync --frozen",
  "memory_project": "my-project",
  "allowed_hosts": ["api.example.com", "*.blob.core.windows.net"],
  "env_var_names": ["EXAMPLE_API_TOKEN", "AZURE_CLIENT_ID", "AZURE_CLIENT_SECRET", "AZURE_TENANT_ID"],
  "secrets": [
    {"recipe": "local-file", "path": "~/.secrets/my-project.env", "names": ["EXAMPLE_API_TOKEN"]},
    {"recipe": "env", "names": ["AZURE_CLIENT_ID"]},
    {"recipe": "azure-keyvault", "vault": "my-key-vault", "secrets": {"AZURE_CLIENT_SECRET": "agent-client-secret"}},
    {"recipe": "literal", "name": "AZURE_TENANT_ID", "value": "00000000-0000-0000-0000-000000000000"}
  ],
  "setup_commands": ["apt-get install -y -qq jq"],
  "session_start_commands": ["{store}/projects/my-project/cloud-login.sh"],
  "box": {
    "backend": "azure-blob",
    "storage_account": "mystorageaccount",
    "shared_container": "agent-box-shared",
    "sessions_container": "agent-box-sessions",
    "prefix": "my-project",
    "include": ["results/**", "docs/slides/**", "figures/**"],
    "exclude": ["results/scratch/**", "**/__pycache__/**", "**/.env", "**/*.env"],
    "archives": ["ws-*", "decompile/project*"],
    "archive_large_file_bytes": 52428800,
    "min_free_bytes": 2147483648
  },
  "probe": {
    "https_urls": ["https://api.example.com"],
    "tcp_hosts": ["db.example.com:5432"],
    "commands": [{"name": "API reachable with the project's own client", "run": "uv run --frozen python -c \"print('ok')\""}]
  },
  "pointers_file": "projects/my-project/data_pointers.json"
}
```

An rclone box instead:

```json
"box": {
  "backend": "rclone",
  "shared_remote": "onedrive:agent-box/shared/my-project",
  "sessions_remote": "onedrive:agent-box/sessions/my-project",
  "include": ["results/**", "figures/**"],
  "exclude": ["**/__pycache__/**"],
  "archives": ["decompile/project*", "decompile/binaries-*"],
  "receive_dir": "_agent_outbound",
  "mount_dir": "_box"
}
```
