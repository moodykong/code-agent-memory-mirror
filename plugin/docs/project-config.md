# Project config: `projects/<name>/env.json`

One file per project in the store. Every key is optional except `repo`. A complete example lives in
`plugin/templates/store/projects/_example/env.json` (copied into every new store).

Never put a secret value in this file. It holds environment variable NAMES only; the values go on the
cloud vendor's settings page or in your local environment.

| Key | Type | Used by | What it does |
| --- | --- | --- | --- |
| `repo` | `"owner/name"` | `session-start`, `doctor`, `probe`, `cloud render` | Recognises a clone of this project by its `origin` URL (https or ssh, with or without `.git`, any letter case), so sessions find the project without flags, including sessions that start in a folder above several clones (see below). |
| `python` | version text, e.g. `"3.11.9"` | `cloud bootstrap`, `session-start` (cloud), `cloud render` | Installed with `uv` (and `uv` itself if missing) by `cloud bootstrap` when the cloud machine is set up, and again by the drift repair at session start when the value changes. `probe --full` checks `uv python find`. |
| `dependency_sync` | one command | `session-start` (cloud), `probe --full` | Run in the project folder at cloud session start, e.g. `"uv sync --frozen"`. |
| `memory_project` | text | `install`, `session-start`, `sync` | Folder name under `memory/` for this project (default: the project name). |
| `allowed_hosts` | list of host names | `cloud render` | Hosts the cloud network must allow; joined with the hosts agent-mirror itself needs. |
| `env_var_names` | list of names | `probe`, `cloud render`, `cloud secrets` | Environment variables the project needs. `probe` reports which are set (never values); `cloud render` writes the paste-ready name list. |
| `secrets` | list of recipe entries | `cloud secrets`, `cloud render`, `cloud runbook`, `doctor` | How each value in `env_var_names` is produced on a machine, never the value itself. See "Secret recipes" below. |
| `setup_commands` | list of shell lines | `cloud bootstrap`, `session-start` (cloud), `cloud render` | Extra setup for a cloud machine (for example installing a cloud CLI). `cloud bootstrap` runs each one with `bash -c` as the machine's user (root on a cloud machine) when the machine is set up, and the drift repair at a cloud session start runs them all again when the list changes; they must be safe to run twice. Each may fail without stopping the others. They are read only from the store's default branch. See "Cloud setup and drift repair" below. |
| `session_start_commands` | list of commands | `session-start` (cloud) | Run in the project folder after `dependency_sync` on a cloud runtime (for example a sign-in script kept in the store). |
| `box` | object | `box`, `doctor`, `probe --full`, `cloud render` | Where bulk files go. See below. |
| `probe` | object | `probe`, `probe --full` | Extra checks. See below. |
| `pointers_file` | path relative to the store root | `session-start` | A file you keep in the store that says where the project's large data lives. `session-start` prints its path so the agent can read it. The format is yours. |
| `companion_repos` | list of `{"repo", "ref", "dir"}` | `cloud bootstrap`, `session-start` (cloud), `cloud render` | Public repos a cloud session needs next to the project but that cannot be attached to the session. See "Companion repos" below. |

## Commands that agent-mirror runs (`dependency_sync`, `session_start_commands`, `probe.commands`)

agent-mirror never passes these to a shell, so they work the same on Linux, macOS and Windows:

- A text value is split into words the way a shell would (`shlex`), but pipes, `&&`, redirects and
  `$VARIABLES` are not interpreted. Wrap anything like that in a script and run the script.
- A list value is used as the argument list as it is.
- `{store}` becomes the store folder and `{project_dir}` the project folder (after splitting, so
  Windows paths survive). A leading `~` is expanded.

`setup_commands` are different: they are shell lines, run with `bash -c` (see "Cloud setup and drift repair").

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
expects next to its clone (a library checked out beside it, say) is cloned when the machine is set up instead:

```json
"companion_repos": [
  {"repo": "https://github.com/example-org/helper-lib", "ref": "v1.2.0", "dir": "helper-lib"}
]
```

- `cloud bootstrap` (run by the setup script) clones each entry into `~/companions/<dir>` at `ref` without
  any token: `git init`, `git fetch --depth 1 origin <ref>`, `git checkout FETCH_HEAD`. A failure is noted
  and never stops the setup. The drift repair at a cloud session start does it again when the entries
  change. `cloud render` adds the repo's host to the allowed hosts.
- On a cloud runtime, `session-start` links `~/companions/<dir>` next to the project clone as `../<dir>`
  when nothing is there yet (a link made in Python, no shell). Something already at `../<dir>` is left
  alone.
- `repo` must be a public `https://host/owner/name` URL (no credentials, port, query or other scheme).
  `ref` is a branch, tag or commit (letters, digits, `.`, `_`, `/`, `-`). `dir` is one folder name: no
  path separators and not dots only. Anything else makes `cloud render` stop with a clear message,
  `cloud bootstrap` skip the project's setup steps, and `session-start` skip that entry with a warning.

## Cloud setup and drift repair (`python`, `setup_commands`, `companion_repos`)

The setup script pasted on the vendor page is a fixed bootstrap (see store-layout.md, "Cloud files"): it
clones the store, checks agent-mirror out at the ref `store.json` pins, and runs `agent-mirror cloud
bootstrap --project NAME --vendor claude|codex`. Changing these three keys never needs a new paste:

- `cloud bootstrap` reads `projects/NAME/env.json` from the store's default branch as fetched
  (`refs/remotes/origin/HEAD`, else the upstream of the checked-out branch) with `git show`, never from the
  working tree (a detached checkout without `origin/HEAD`, as a vendor's own checkout of the store may be,
  is read at its checked-out commit, as the setup script reads `tool.ref`), and refuses a checkout on an
  `inbox/*` branch. It runs, in this order, the companion
  clones, the `uv python install` of `python`, and each of the `setup_commands` with `bash -c` in the
  folder the setup script ran in. STORE, TOOL, AM and NOTE are set for the commands, as the setup scripts
  of 0.5.1 set them. Each step's name, time and exit status go to `~/agent_setup_probe.txt`; the output
  goes to the vendor's setup log. Then it runs `init-store --adopt`, `install` and (Claude) `hooks
  install`, and writes a stamp on the machine (`<config>/bootstrap-stamp.json`, never in the store): a
  hash of these three keys per project and the agent-mirror commit.
- At every cloud session start, after the store pull and the move of agent-mirror to the pinned ref,
  `session-start` compares the stamp with the default branch. For a project whose keys changed it runs
  the same steps again (drift repair), with their output kept out of the session, within
  `--repair-timeout` seconds in all (default 900; a command still running then is stopped with
  everything it started). A failed step is reported and the session goes on; when the time ran out, the
  old record stays so that the next start tries again. A project whose steps could not run at setup
  (env.json missing or invalid there, or an inbox checkout) is recorded without inputs, so its steps run
  at the first session start after the store is fixed.
- Each new session starts from the vendor's cached machine, which still holds the stamp of the setup,
  so the repair repeats at every session start until the vendor rebuilds the machine. To make it rebuild,
  raise the number on the `# revision:` line of the pasted script and save.
- On a Claude cloud machine the same session start also checks Claude's settings for the agent-mirror hooks
  (step `hooks check`): when some are there but not all the runtime expects (a machine built before 0.5.6
  has three, without the Stop hook), it adds the missing ones and says so. Claude Code may only use a hook
  added during a running session from a later session on, and the next session starts from the cached
  machine again, without it: raise the `# revision:` line once so the vendor rebuilds the machine with all
  four hooks.
- So `setup_commands` must be safe to run twice (`command -v jq >/dev/null || apt-get install -y jq`).
  They run as root on a cloud machine. They come only from the default branch, which a cloud session never
  writes: a developer machine's sync applies only `memory/` from an inbox branch and holds everything
  else, `projects/*/env.json` and `store.json` included, for `agent-mirror review`.

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

The `box` commands with `--project NAME` and no `--project-path` look for the clone of NAME: the repo of
the current folder, then the repo of the session folder, then the clones in the child folders (the scan
above). So `box send FILE --project NAME` from the folder above the clones writes into the same session
namespace as the other sends of the session, with the clone's git branch recorded. Several clones of NAME
stop with a message that asks for `--project-path`. When none is found, the current repo or folder is
the project folder, as before.

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

Both sides carry short notes, so that a later session knows what files are for and not only their names
and sizes: `box send --note TEXT` (required on a cloud runtime) and `box note FOLDER TEXT` for a shared
folder. See "Notes" below.

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
| `unsent_ignore` | none | more patterns (the include and exclude syntax) that `box unsent` and the stop check leave out, for example `["**/*.ckpt.tmp", "logs/**"]`; see "Unsent files" |
| `git_text_max_bytes` | `1048576` (1 MiB) | an untracked file bigger than this is for the box, not left to git; `0`: every untracked file is; see "Unsent files" |
| `git_binary_max_bytes` | `102400` (100 KiB) | a binary untracked file (a NUL byte in its first 8 KiB) bigger than this is for the box; `0`: every binary one is |
| `git_total_max_bytes` | `20971520` (20 MiB) | when the small untracked files of one top-level folder together are bigger than this, they are all for the box; `0`: every small untracked file is |

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
                                       "fingerprint": "...", "updated": "...",
                                       "note": "...", "note_updated": "..."}}}
```

`note` and `note_updated` (since 0.5.3, optional) are the folder's note: see "Notes" below.

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
- `box send --note TEXT` records what the files are, why they were made and what to do next (one to three
  sentences). It is required on a cloud runtime: a send without one is refused with exit code 2 before
  anything is uploaded. See "Notes" below.
- `box send PATH` takes PATH relative to the project folder first. When nothing is there, PATH is taken
  relative to the current folder (or as an absolute path): a file or folder inside the project keeps its
  path in the project, and a FILE outside the project is sent under its file name (`box send
  ../probe_output.txt` sends `probe_output.txt`). Refused, with exit code 2 and one line that says what
  to do: a folder outside the project (copy it in first), a name that exists both in the project folder
  and in the current folder as different things (pass an absolute path), env files, the manifest name
  `_session.json`, paths the `box.exclude` rules match, and `--archive` with a file outside. A path that
  exists nowhere exits with 1. The name in the namespace never holds `..`.
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
  ],
  "note": "Run 3 with the new loss; compare its curves with run 2 before tuning further.",
  "notes": [
    {"at": "2026-11-17T10:39:58Z", "note": "Summary of runs 1 to 3; the baseline is run 1.",
     "paths": ["results/summary.csv"]},
    {"at": "2026-11-17T10:40:12Z", "note": "Run 3 with the new loss; compare its curves with run 2 before tuning further.",
     "paths": ["results/run-3"]}
  ]
}
```

  `machine` is null when the machine has no id yet; `git` values are null outside a git clone. `note`
  and `notes` are there only when a send gave a note (since 0.5.3). The manifest never holds environment
  values or absolute paths.
- `box sessions [--json] [--limit N]` (any runtime, read-only) lists the namespaces newest first by start
  time: id, label, runtime, branch and short commit, file count, total size, last update, and the
  session's note under it (up to 200 characters; `--json` has the whole note and the count of notes). It reads one
  listing of the sessions side and one manifest per listed session; older namespaces
  (`<runtime>-<YYYYMMDDTHHMMSSZ>`, before 0.5.0) are listed from their files as "no manifest". Without the
  transfer program it shows the summary of the last `box index`.
- `box fetch --session ID [PATH] [--into DIR]` downloads a session's files (all, or PATH inside it) into
  `<project>/_sessions/<session id>/` (`--into` is relative to the project folder; `--into .` writes over
  the project tree). It prints every note of the session first, with the paths each note was given for
  (`--dry-run` prints them and downloads nothing). ID is the id, a unique start of it, or a label that exactly one session has. A
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
  runtime, branch, commit, last update, file count, size and note of the newest 50 sessions, for offline
  use; `box sessions` stays the live source.

### Notes

A later session sees names and sizes in the box, not what the files are for. Notes are short free text
kept next to the records that already exist (the session manifest, `_owners.json`, the committed index);
there is no new file and no new git record.

- **Session notes.** `box send --note TEXT` sets the manifest's `note` (the latest note given) and adds
  `{"at", "note", "paths"}` to its `notes` list, so a later send with another note does not erase what an
  earlier one said. Consecutive sends with the same text share one entry; the list keeps the newest 20.
  On a cloud runtime a send without a note is refused (exit code 2, one line, nothing uploaded):

  ```
  error: refusing box send on claude-cloud without a note: pass --note "what these files are and why" (one to three sentences: what the files are, why they were made, what to do next; later sessions read it); nothing was uploaded
  ```

  On a developer machine the note is optional. `box sessions` shows it under each session, `box fetch
  --session` prints every note first, and `box index` copies the latest note into its session summary.
- **Folder notes.** `agent-mirror box note FOLDER TEXT` (or `--clear`) on a developer machine records a
  one-line note on a folder of the shared tree (a folder unit, as `box owners` names it) in that folder's
  `_owners.json` entry. Only the folder's owning machine sets it, with the same rule as `box release`:

  ```
  error: <folder> is owned by desk-b since 2026-11-01T09:00:00Z, not this machine; only its owner sets its note
  error: <folder> has no owner in <shared>/_owners.json yet; only its owning machine sets its note. Push it from here first (`agent-mirror box push --only <folder>` claims it), then set the note
  error: refusing box note on claude-cloud: only the folder's owning machine (a developer machine) sets folder notes
  ```

  `box push` keeps the note when it rewrites the entry, and so does a take-over (the note describes the
  folder, not the machine); `box release` removes the entry with its note. `box note` also writes the entry
  into the owners line of the local `box_index.jsonl` when it has one: commit the index (as after `box
  index`), and `box ls` and `box owners` show the note offline on every machine. `box index` copies it too.
- **Text rules.** A note is one line: control characters are dropped and whitespace (newlines included)
  is collapsed to single spaces. At most 500 characters. Text that matches the secret patterns of `sync`'s
  scan, or holds the value of an environment variable whose name looks secret, is refused; the message
  names the pattern, never the text.
- **At a cloud session start.** `session-start` on a cloud runtime ends with, per project of the session,
  the newest 5 sessions (label and id, age, file count, note) and the shared folders that have notes:

  ```
  box of my-project: the newest 5 of 12 sessions, from the box (fetch one with `agent-mirror box fetch --session ID --project my-project`):
    first-probe (claude-cloud-20261118T101500Z-a1b2c3), 2 h ago, 1 file; note: Report of agent-mirror probe --full from the first session in this environment; read it before changing the cloud setup.
    codex-cloud-20261117T090000Z-0f1e2d, 1 d ago, 14 files; note: Loss curves of run 7; compare them with run 8 before tuning further.
  box of my-project: shared folders with notes, from the committed index (`agent-mirror box ls` shows them too):
    data/raw: Raw sensor exports from the lab; read-only inputs of the training scripts.
  ```

  It makes the calls `box sessions` makes (one listing and one batched manifest read; azure-blob signs in
  first), all within `session-start --box-timeout SECONDS` (default 20; 0 skips it). The folder notes come
  from the committed index, so they cost no call. It never fails the session: when it cannot list, it
  prints one line (`box of my-project: could not list its sessions (...); see them later with ...`), and
  without the transfer program it shows the session summary of the committed index. The step is recorded
  in the session-start step log as `box notes (<project>)`. A developer machine prints nothing new.

A rule for your instructions file (the store's `AGENTS.md`), so that cloud agents use the notes:

```
- A cloud agent gives a note with every `agent-mirror box send` (`--note "what these files are, why they
  were made, what to do next"`) and reads the notes (`agent-mirror box sessions`, `agent-mirror box ls`)
  before using files from the box.
```

The keep or leave rule that goes with it (see "Unsent files" below; the template `AGENTS.md` has both):

```
- Before finishing a cloud session, run `agent-mirror box unsent`. Small new files that belong in git are
  committed as usual. Large or binary outputs, and anything in a git-ignored output folder, go to the box
  with `agent-mirror box send PATH --label NAME --note "what, why, what next"`, or are declared disposable
  with `agent-mirror box skip PATH... --reason "why it can go"`; `box unsent` shows which is which.
- KEEP results that took real time or money to produce, analysis outputs, generated reports and figures,
  anything a later session would otherwise have to redo. LEAVE caches, anything rebuildable in minutes,
  files already in git, secrets and env files.
```

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
| cloud `session-start` | the 2 of `box sessions` per project (azure-blob: plus `az account show`, and a sign-in when it is needed); the folder notes come from the committed index |
| `box unsent`, `hook stop-check`, the developer `session-start` push check | none: git and local files only |
| `box skip` | 2 for the first record of a session (manifest write, read back), 3 later (read, write, read back); azure-blob on a cloud runtime: plus `az account show` |

Fetching by full id is the cheapest lookup: it never looks at other sessions. `--limit` cuts what
`box sessions` reads: rclone downloads only the manifests it shows; azure-blob downloads every manifest in
the one batched call (one pattern) and the list is cut afterwards. A batched azure-blob download is one
`az storage blob download-batch` whose pattern is chosen from the listing; when every pattern would also
fetch more than 16 MiB of other files, it goes file by file instead.

### Unsent files (`box unsent`, `box skip` and the stop check)

A cloud machine is thrown away after the session, and memory is synced by hooks while box files are not:
a result the session made and never sent is lost. The program detects such files, the agent judges each
one, and on a Claude cloud machine a Stop hook makes sure the judging happens.

Small new files that git can hold are git's business: the vendor's own stop hook on Claude Code on the web
asks the session to commit and push untracked files, and the session commits them as usual. What git should
not hold (large or binary outputs, many small result files, anything in a git-ignored folder) is the box's
business. So `box unsent` sorts the files it finds into two classes, and the stop check asks only about the
files for the box: the two hooks never ask about the same file.

**`agent-mirror box unsent [--all] [--json] [--check] [--time-limit 5]`** looks at the files of the project
clone for which all of these hold:

- git does not track it (untracked or ignored); tracked files are git's business;
- its modification time is at or after the session baseline (2 seconds of slack for coarse file-system
  clocks);
- it is not left out by name or rule: env files (`.env`, `*.env`); links (never followed or listed); these
  folders at any depth, never entered: `.git`, `.agent-mirror`, `__pycache__`, `.venv`, `venv`,
  `node_modules`, `.cache`, `.pytest_cache`, `.mypy_cache`, `.ruff_cache`, `.tox`, `.nox`; a folder with its
  own `.git` (a nested clone or a worktree travels through its own git); the box's `mount_dir` and
  `receive_dir` inside the clone; the `box.exclude` patterns; and the optional `box.unsent_ignore` patterns.
  `dist` and `build` are deliberately not on the list, because some projects keep results under those names;
  add them to `box.unsent_ignore` when they are build output for you;
- it was not fetched from the box and left unchanged since (`.agent-mirror/fetched.json`, as `box evict`
  uses it);
- it was not sent in this session with the same size and modification time: `box send` records each file
  it uploaded in the local session record (`.agent-mirror/session.json`, key `sent`);
- it is not under a path declared disposable with `box skip` (key `skipped` of the same record).

Each such file is then **for the box** or **left to git**:

| Class | Which files | Reason (`--json`) | Tag in the listing |
| --- | --- | --- | --- |
| for the box | ignored by git (a `.gitignore` rule, `.git/info/exclude` or the global excludes), any size | `git_ignored` | `ignored by git` |
| for the box | untracked, bigger than `box.git_text_max_bytes` (default 1 MiB) | `large` | `large` |
| for the box | untracked, binary (a NUL byte in its first 8 KiB) and bigger than `box.git_binary_max_bytes` (default 100 KiB) | `binary` | `binary` |
| for the box | untracked and small, in a top-level group whose small untracked files together are bigger than `box.git_total_max_bytes` (default 20 MiB) | `many_small` | `many small files` |
| for the box | anything, when the clone is not a git repo | `not_git` | none |
| left to git | every other untracked file (small text, small binary) | `small` | none |

- A top-level group is a top-level folder of the clone, or a file at the top on its own. When the total rule
  applies, the whole group is for the box and is shown as one folder line: the deepest folder that holds its
  small files (the top-level folder, or for example `src/gen/` when only that part is new).
- The binary test reads the first 8 KiB of a file only when its size is between the two limits, so the walk
  reads no file content otherwise and makes no git call per file.
- A limit of `0` leaves nothing to git under its rule: `git_text_max_bytes: 0` makes every untracked file
  for the box, `git_binary_max_bytes: 0` every binary one, `git_total_max_bytes: 0` every small one. All
  three keys are optional; a value that is not a whole number of 0 or more is reported as a warning (here and
  in `doctor`) and its default is used.
- An ignored file inside a new untracked folder (`results/run.log` under a new `results/`) is for the box,
  as git reports it.

The listing shows the files for the box, grouped by top-level folder, largest first. A folder whose files are
all new and all for the box (inside what git reports as a wholly untracked or ignored folder) is one line,
`folder/ (N files, size)`; other files are listed one by one under `in folder/: N files, size`. At most 30
lines, then `and N more files, size`. One line follows for the files left to git; `--all` lists them in a
block of their own instead:

```
unsent in demo (/home/user/demo): 2404 files for the box, 35.8 MiB, changed since 2026-11-19T14:44:29Z (session start), in no git commit and not in the box:
  sweep/ (2400 files, 21.5 MiB; many small files)
  results/run-3/model.ckpt (12.0 MiB; large)
  out/ (1 file, 2.0 MiB; ignored by git)
  figures/ (1 file, 300.0 KiB; binary)
  run.log (400 B; ignored by git)
left to git: 3 small untracked files, 7.0 KiB (commit them; --all lists them)
left out: 1 older than the baseline, 2 folders left out by name
keep a file or folder: agent-mirror box send PATH --label NAME --note "what, why, what next"
leave it behind:       agent-mirror box skip PATH... --reason "why it can go"
```

With `--all`, the `left to git` line becomes `left to git: 3 small untracked files, 7.0 KiB (commit them as
usual):` followed by their listing, in the same form. When nothing is for the box, the first line reads
`nothing for the box in demo (...)` (or `nothing unsent in demo (...)` when nothing is left to git either).

A folder line counts the files `box unsent` considers; `box send` of that folder applies the box's exclude
rules and env-file rule too, but not the list of folders left out by name.

- It is read-only and offline: one `git status --ignored` (without the index lock, so it never gets in the
  way of a git command running at the same time), a walk of what git reports, and the local records. The
  whole check stops at `--time-limit` seconds (default 5) and then says `the check was cut short after 5 s`.
- `--json` prints the document: `baseline` (`source`: `session-start`, `clone` or `none`, `at`, `seconds`),
  `complete`, `in_git`, `files` and `bytes` (both classes), `for_the_box` and `left_to_git` (each
  `{"files", "bytes"}`), `limits` (the three limits in use), `checked`, `entries` (the lines above, files
  for the box first, each with `class` and `reasons`), `paths` (every file with its size, time, `class` and
  `reason`, the files for the box first, the first 2000, then `paths_more`), `left_out` (a count per reason:
  `before_baseline`, `fetched`, `sent`, `skipped`, `env`, `links`, `builtin`, `nested_git`, `managed`,
  `excluded`, `ignored`) and `warnings`.
- Exit code: 0. With `--check`: 3 when something is for the box or the check could not finish (cut short, or
  git failed), else 0 (files left to git never make it 3). 1 when no project matched or its folder is
  missing, 2 for bad arguments. A project whose box is not configured has nothing to send to: it says so and
  exits 0.
- It works on developer machines too, from the baseline of the last session start there (24 hours).

**`agent-mirror box skip PATH... --reason TEXT [--label TEXT]`** declares files or folders of the clone
disposable for this session. Each PATH is taken relative to the clone, then to the current folder (or as an
absolute path); it must exist and be inside the clone, else nothing is recorded (exit 2 for a path outside
the clone, 1 for one that does not exist). The reason follows the rules of a note: one line, at most 500
characters, refused when it looks like it holds a secret. It is recorded twice:

- in the local session record (`skipped`, a list of `{"at", "reason", "paths"}`), so `box unsent` and the stop
  check leave out every file under those paths for the rest of the session;
- in the session's manifest on the box, as an entry of its optional `skipped` list (the newest 20 entries,
  each with at most 50 paths and the count of the others in `more`), so a later session sees what was left
  behind and why: `box sessions` prints `(N paths left behind: reason)` under the session, and
  `box fetch --session` prints `left behind (time): paths; reason: ...`. A skip before any send starts the
  session (a manifest with no files). When the box is not configured or cannot be reached (no transfer
  program, no sign-in, a failed write), it says so in one line, keeps the local record and exits 0.

**The local git exclude after a send or a skip** (cloud runtimes only). A file that was sent to the box, or
declared disposable, should not then be committed because the vendor's hook still sees it as untracked. So on
a cloud runtime:

- `box send PATH` adds PATH to the clone's local git exclude file (`.git/info/exclude`, the file
  `git rev-parse --git-path info/exclude` names; never `.gitignore`, never committed) when git reports it as
  untracked and all of its files are for the box by the rules above (applied to every file under it, whatever
  the session records say). When PATH also holds files left to git, only its files that are for the box are
  added: a small sent file may still be committed, so it stays visible to git. A path git reports as ignored
  is already out of sight; a tracked one is git's.
- `box skip PATH...` adds every untracked path at or under the given paths, small or large: it was declared
  disposable, so nothing should ask to commit it.
- Each line is anchored to the exact path: a leading `/`, a trailing `/` for a folder, and git's special
  characters (`*`, `?`, `[`, `\`, trailing spaces) escaped, so `/results/run-3/model.ckpt` hides that file and
  nothing else. A line already there is not added again. At most 100 paths per command; above that nothing is
  added and it says so. The command prints one line when it added something:

  ```
  added /results/run-3/model.ckpt to this clone's local git exclude file (.git/info/exclude, never committed): it is in the box now, so a stop hook that asks to commit untracked files leaves it alone
  added /sweep/ to this clone's local git exclude file (.git/info/exclude, never committed): it is declared disposable, so a stop hook that asks to commit untracked files leaves it alone
  ```

- The sent and skipped records keep those files out of `box unsent` although git now ignores them. A sent
  file that changes afterwards comes back, as ignored by git, so for the box.
- Developer machines never touch the exclude file for a send or a skip.
- UNVERIFIED: this relies on the vendor's hook looking at untracked files as `git status` reports them
  (so that what the local exclude file hides is not asked for). If it lists files some other way, the lines
  do no harm, and the stop check's log (below) shows which hook asked.

**The stop check** (Claude Code only): on a cloud runtime, `hooks install` (and so `cloud bootstrap`) adds a
fourth hook, `Stop: agent-mirror hook stop-check` with a timeout of 30 s. Developer machines never get it, so
it adds no time to their turns, and `hooks install` there removes one of ours if it finds it. When the agent
ends its turn, the hook:

- exits 0 at once and prints nothing anywhere but a cloud runtime;
- runs the check of `box unsent` for each project clone of the session that has a box (the projects
  `session-start` finds from `CLAUDE_PROJECT_DIR`, else the `cwd` of the hook input), within 8 seconds in all
  (`--time-limit`), and a hard stop with exit 0 twelve seconds after that;
- when files are for the box, prints `{"decision": "block", "reason": "..."}`, which keeps the agent working
  with the reason as its instruction. The reason starts with a fixed line that names the tool and its version,
  lists only the files for the box, and says in one line that small untracked files are left to git and that
  committing a listed file is not the remedy:

  ```
  agent-mirror 0.5.7 stop check (Claude Code Stop hook: agent-mirror hook stop-check)
  This cloud machine is thrown away after the session, and these files of the project clone (large, binary, many small files or ignored by git) are in no git commit and not in the box:
  demo (/home/user/demo): 2404 files for the box
    sweep/ (2400 files, 21.5 MiB; many small files)
    results/run-3/model.ckpt (12.0 MiB; large)
    out/ (1 file, 2.0 MiB; ignored by git)
    figures/ (1 file, 300.0 KiB; binary)
    run.log (400 B; ignored by git)
  Small untracked files are left to git (commit them as usual); committing a file listed here is not the remedy: send it or skip it.
  For each one, either send it: agent-mirror box send PATH --label NAME --note "what, why, what next"
  or declare it disposable: agent-mirror box skip PATH... --reason "why it can go"
  KEEP results that took real time or money to produce, analysis outputs, generated reports and figures, anything a later session would otherwise have to redo. LEAVE caches, anything rebuildable in minutes, files already in git, secrets and env files.
  ```

- passes when only files left to git are there: those are the vendor hook's to ask about;
- never blocks twice on the same set: it keeps short digests of the files for the box it last blocked on
  (never their names) in `<cache>/stop-check.json`, per `session_id` of the hook input, and blocks again only
  when that set gained a file. Files left to git never count, so a change among them never makes it block
  again. The set shrinking (files sent or skipped) never blocks. `stop_hook_active` (the agent is already
  continuing because of a stop hook) is honoured by the same rule: it blocks only on files that were not in
  the set it last blocked on. At most 3 blocks per session; Claude Code itself also caps consecutive
  continuations;
- never blocks when its input cannot be read, on any error, or when a check was cut short or git failed; it
  never prints file contents;
- adds one line per run to `<cache>/stop-check.log` and keeps the newest 200: the time, the agent-mirror
  version, the `session_id`, the decision (`block`, `pass` or `error`), how many files were for the box and
  left to git (`-` when the check did not finish), and the outcome in brackets; never a file name. For an
  error it records the error's type only. `agent-mirror probe` and `probe --full` print whether the Stop hook
  is installed (and whether this runtime should have it) and the last five lines:

  ```
  stop check hook (Stop: agent-mirror hook stop-check, claude-cloud): installed
  stop check log (~/.cache/agent-mirror/stop-check.log), the last 2 of 2 runs kept:
    2026-11-19T15:50:10Z agent-mirror 0.5.7 session=0f3c9a8e decision=block for_the_box=2404 left_to_git=3 (blocked (2404 files for the box))
    2026-11-19T15:50:41Z agent-mirror 0.5.7 session=0f3c9a8e decision=pass for_the_box=2 left_to_git=3 (passed (no new files for the box))
  ```

  On a developer machine `probe` prints these lines only when there is something to see (a Stop hook of ours
  in the settings, or a log).

The contract it relies on is Claude Code's hooks reference (the `Stop` event: stdin JSON with `session_id`,
`cwd`, `hook_event_name` and `stop_hook_active`; exit 0 with `{"decision": "block", "reason": ...}` on stdout
prevents the stop and gives Claude the reason). UNVERIFIED on a real cloud machine: that Claude Code on the
web runs user-level Stop hooks from `~/.claude/settings.json` (see the cloud README's notes), how it combines
ours with the vendor's own stop hook (which asks to commit and push untracked files; this version stays out of
its way for small files and hides what was sent or skipped from it, see above), and that a hook added during a
running session is used before the next session. Codex has no stop hook here: its instructions say to run
`box unsent` before finishing.

**Machines built before 0.5.6.** A cloud machine set up by an older version has three hooks. Its session
start adds the missing Stop hook (see "Cloud setup and drift repair" above), but the vendor's cached machine
keeps the old settings: raise the `# revision:` line of the pasted setup script once, so that the next build
runs `cloud bootstrap` with this version (pin 0.5.6 or later in `store.json` `tool.ref`; `cloud render`
warns about an older pin) and the machine has all four hooks from the start. Old pasted scripts keep working
unchanged.

**On a developer machine**, `session-start` ends with one line per project whose box has folders owned by
this machine that changed since this machine last pushed them:

```
box of demo: 2 owned folders changed since the last push (results, ws-a); upload them with: agent-mirror box push --project demo
```

It prints nothing when nothing changed, nothing is owned here, or the machine's box role is `reader` or
`none`. It makes no remote call: the owning machine and the fingerprint come from this clone's record of its
own pushes (`.agent-mirror/pushed.json`, written by `box push` since 0.5.6 and cleared for a folder by
`box release`) and from the owners line of the committed box index, whichever is newer; a folder the index
gives to another machine is not reported. Each owned folder is compared by its fingerprint (as `box owners`
compares, so a copy only fetched from the box is not a change). All of it stops at `--push-check-timeout`
seconds (default 5; 0 skips it). When the time runs out, a folder still counts as changed when a file seen
so far is newer than its last push (a sound but partial signal: it misses deletions); when none was found,
it prints one short line (`box of demo: the check of this machine's box folders stopped after 5 s; ...`).
The step is in the session-start log as `box push check (<project>)`.

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
