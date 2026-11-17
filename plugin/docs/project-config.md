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

The box moves untracked bulk files (results, figures, slides) between the developer machine and agents.
Two sides: **inbound** (the developer machine uploads with `box push`; agents read with `box index` and
`box fetch`) and **outbound** (agents upload results with `box send`; the developer pulls them by hand).

Common keys:

- `backend`: `"azure-blob"`, `"rclone"` or `"none"`. Missing or `"none"`: every box command prints
  "box not configured" and exits 0.
- `include`: patterns of files that belong in the box, relative to the project folder.
- `exclude`: patterns that never go in the box. Env files (`.env`, `*.env`) are always excluded and
  `box send` refuses them, whatever the config says.

Settings for on-demand retrieval (all optional):

| Key | Default | Meaning |
| --- | --- | --- |
| `archives` | none | top-level folder names or globs (for example `["ws-*", "decompile"]`) sent as one bundle each; see "Archive mode" |
| `archive_large_file_bytes` | `52428800` (50 MiB) | in archived folders, files this big or bigger travel on their own instead of inside the bundle; `0` keeps everything in the bundle |
| `toc_inline_bytes` | `5000000` | a table of contents bigger than this goes into its own `<name>.toc.json.gz` instead of the sidecar |
| `min_free_bytes` | `2147483648` (2 GiB) | free disk that every fetch and receive must leave; `--force` overrides |
| `receive_dir` | `_agent_outbound` | where `box receive` puts results (relative to the project folder, or absolute) |
| `mount_dir` | `_box` | where `box mount` mounts the inbound side (relative to the project folder, or absolute) |

Patterns: `**` crosses folders, `*` and `?` do not, and `**/` also matches no folder. A pattern without
`/` matches a file name at any depth.

### `"backend": "azure-blob"`

Uses the Azure CLI (`az`) with Azure AD sign-in only (`--auth-mode login`, never account keys or SAS).

| Key | Meaning |
| --- | --- |
| `storage_account` | the storage account name, e.g. `"mystorageaccount"`. While empty, box commands do nothing. |
| `inbound_container` | container the developer machine writes |
| `outbound_container` | container agents write |
| `prefix` | folder inside both containers, e.g. the project name |

- `include` entries must be `folder/**` (no wildcards); `exclude` entries must be `folder/**`,
  `**/name`, `name` or `**/folder-name/**`, because `az storage blob sync` can only exclude path
  prefixes and file names. Other shapes are refused with a clear message.
- `box push` runs one `az storage blob sync` per include folder with `--delete-destination false`
  (pass `--delete` to delete remote files whose local file is gone). It is refused on a cloud runtime.
  One machine writes each box: see "One writer per box" below.
- `box fetch` downloads through a staging folder and never overwrites a local file on a local runtime.
- `box send` uploads to `<outbound_container>/<prefix>/<runtime>-<UTC time>/<path>`, never overwriting.
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
| `inbound_remote` | `"remote:path"` as rclone names it, e.g. `"onedrive:agent-box/inbound/my-project"` |
| `outbound_remote` | `"remote:path"` for results, e.g. `"onedrive:agent-box/outbound/my-project"` |

- `box push`: `rclone copy <project> <inbound_remote>` with `--filter` rules built from `include` and
  `exclude` (env files first, then excludes, then includes, then `- **`). `rclone sync` only with `--delete`.
- `box index`: `rclone lsjson -R --files-only` into `projects/<name>/box_index.jsonl`.
- `box fetch`: `rclone copyto` (a file) or `rclone copy` (a folder), with `--ignore-existing` on a
  local runtime so local files are never overwritten.
- `box send`: `rclone copyto`, or `rclone copy --files-from <list>` for a folder (the list leaves out
  env files and excluded paths), into `<outbound_remote>/<runtime>-<UTC time>/<path>`.
- Credentials stay in rclone's own config (`rclone config`), or, in a cloud session, in
  `RCLONE_CONFIG_<REMOTE>_*` environment variables listed in `env_var_names`.

### Archive mode

For folders with very many small files on slow backends (a consumer cloud drive, say), set `box.archives`.
Each top-level project folder whose name matches is left out of the file-by-file push and travels as:

```
<inbound>/_archives/<name>.tar.gz          the bundle: every file below archive_large_file_bytes
<inbound>/_archives/<name>.large/<path>    each larger file, on its own
<inbound>/_archives/<name>.json            the sidecar (uploaded last)
<inbound>/_archives/<name>.toc.json.gz     the table of contents, only when it is too big for the sidecar
```

The sidecar holds the file count, total bytes, newest modification time, the archive size, the
agent-mirror version, a fingerprint (made from the sorted relative paths, sizes and modification times),
and the table of contents: every file's relative path and size, marked `"in": "bundle"` or `"in": "large"`.

- `box push` packs with the `tar` program when present (Python's `tarfile` otherwise), applies the exclude
  rules, always leaves out env files, and never follows or packs symbolic links. It checks free disk in
  the temp folder first (set `TMPDIR` to use another disk). It skips the upload when the remote sidecar's
  fingerprint matches, re-sends the bundle only when a small file changed, sends only the large files that
  changed, and removes the temp archive afterwards. Remote large files that no longer exist locally stay.
- `box index` lists each archive as one entry (`"archive": true`, file count, bytes, large files) and
  writes its table of contents to `projects/<name>/box_toc/<folder>.json` (gzipped when big) in the store.
  Commit those files with the index so a cloud agent can browse without downloading anything.
- `box fetch <archived folder>` downloads the bundle, extracts it safely (absolute paths, `..` members,
  devices and links pointing outside are rejected) and prints the large files it did not fetch, with their
  sizes and the exact command for each. `box fetch <path to a large file>` fetches that one file.
  `box fetch <folder> --all` fetches the bundle and every large file. `box fetch <sub-path>` extracts only
  that part of the bundle and lists the large files under it. A local runtime never overwrites existing
  files; a cloud runtime does.
- `box send <folder> --archive` sends a folder to the outbound side as one `.tar.gz`.

### One writer per box

`box push` (both backends) writes `<inbound>/_writer.json` on the first push for a project: the
machine id, machine name, date and tool version. A later push from a machine with another id is refused
before any transfer, and the message names the current writer; `box push --take-over` makes this machine
the writer. A machine whose recorded role for the project (`agent-mirror apply`, unit `box:<project>`)
is `reader` or `none` is refused before anything else. `--dry-run` reads the marker and says what a push
would do. `box index` records the marker in the index, so `agent-mirror plan` names the writer offline.
On azure-blob the marker is `<inbound_container>/<prefix>/_writer.json`; on rclone it is
`<inbound_remote>/_writer.json`. Neither `--delete` mode removes it.

### What could go in the box (`box candidates`)

`agent-mirror box candidates --project NAME --project-path PATH [--json] [--min-bytes N]` is a
read-only helper: it lists the project's entries that git ignores or does not track (every top-level
entry when the folder is not a git repo) with sizes, file counts, large files, newest change, a kind
guess (`rebuildable`, `secret`, `worktree`, `candidate`) and whether `box.include` or `box.archives`
already cover them. Details: reconciliation.md.

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

### `box receive`

`box receive [--into DIR]` downloads everything on the outbound side into `DIR` (default `receive_dir`,
else `<project>/_agent_outbound`), never overwriting existing local files, after the same disk check.
It is refused on a cloud runtime: results are received on the developer machine.

### `box mount` (rclone only)

`box mount [--cache-max-size 10G]` runs `rclone mount <inbound_remote> <mount_dir> --read-only
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
  `inbound_remote` and `outbound_remote`, and use only letters, digits and `_` in remote names.
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
    "inbound_container": "agent-box-inbound",
    "outbound_container": "agent-box-outbound",
    "prefix": "my-project",
    "include": ["results/**", "docs/slides/**", "figures/**"],
    "exclude": ["results/scratch/**", "**/__pycache__/**", "**/.env", "**/*.env"],
    "archives": ["ws-*"],
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
  "inbound_remote": "onedrive:agent-box/inbound/my-project",
  "outbound_remote": "onedrive:agent-box/outbound/my-project",
  "include": ["results/**", "figures/**"],
  "exclude": ["**/__pycache__/**"],
  "archives": ["decompile"],
  "receive_dir": "_agent_outbound",
  "mount_dir": "_box"
}
```
