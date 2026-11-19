# Changelog

## 0.5.7

The stop check no longer fights the vendor's own stop hook. On Claude Code on the web, the vendor's hook asks the
session to commit and push untracked files; for a small new file both hooks asked at once. Now small files that git
can hold are left to git (the vendor's hook asks for them, and the session commits them as usual), and the stop check
here asks only about files that do not belong in git: large or binary outputs, many small result files, and
anything in a git-ignored folder, which it asks to send to the box or skip.

- **Two classes in `box unsent`.** Every unsent file is either for the box (ignored by git, any size; or untracked
  and above `box.git_text_max_bytes`, default 1 MiB; or binary, a NUL byte in its first 8 KiB, and above
  `box.git_binary_max_bytes`, default 100 KiB; or one of the small untracked files of a top-level folder whose
  small untracked files together exceed `box.git_total_max_bytes`, default 20 MiB, shown as one folder line) or
  left to git (every other untracked file). Outside a git repo every file is for the box, as before. The three
  keys are new and optional (whole numbers of bytes, 0 or more; 0 leaves nothing to git under its rule; a bad
  value is a warning and its default is used). The binary test reads 8 KiB only of files between the two limits,
  so the walk stays as fast as before.
- **The listing.** `box unsent` lists the files for the box, each line with a short reason where it helps
  (`ignored by git`, `large`, `binary`, `many small files`), then one line for the rest:
  `left to git: 3 small untracked files, 41.2 KiB (commit them; --all lists them)`. The new `--all` lists those
  too, in their own block. `--json` gives each path and entry its `class` (`for_the_box` or `left_to_git`) and
  reason, both totals (`for_the_box`, `left_to_git`; `files` and `bytes` still count both), `limits` and
  `in_git`. `--check` exits 3 only when something is for the box (or the check could not finish).
- **The stop check** blocks only on files for the box, and its "never twice for the same set" memory counts
  only those: a change among the files left to git never makes it block again. Its reason starts with a fixed
  line, `agent-mirror 0.5.7 stop check (Claude Code Stop hook: agent-mirror hook stop-check)`, names only the
  files for the box, and says in one line that small untracked files are left to git and that committing a
  listed file is not the remedy: send it or skip it.
- **The stop check's log.** Every run on a cloud runtime adds one line to `<cache>/stop-check.log` (the time, the
  version, the session id, the decision `block`, `pass` or `error`, the counts of files for the box and left to
  git, the outcome; never a file name), keeping the newest 200. `probe` and `probe --full` print the last five
  and whether the Stop hook is installed (and expected on this runtime), so a test on a real machine can tell
  which hook asked what.
- **Out of the vendor's sight once dealt with.** On a cloud runtime, `box send` adds what it sent and what is for
  the box to the clone's local git exclude file (`.git/info/exclude`, never `.gitignore`, never committed): the
  path when all of it is for the box, else only its files that are (a small sent file may still be committed,
  so it stays visible). `box skip` adds every untracked path it declares disposable. Each line is anchored to
  the exact path (a leading slash, a trailing slash for a folder, git's special characters escaped), never
  added twice, and the command prints one line saying so. The sent and skipped records keep those files out
  of `box unsent`; a sent file that changes comes back as ignored by git. Developer machines never touch the
  file. This relies on an assumption that is UNVERIFIED: that the vendor's hook looks at untracked files as
  `git status` reports them.
- **The suggested rule** in the template `AGENTS.md`, the README and the docs: small new files that belong in
  git are committed as usual; large or binary outputs and anything in a git-ignored output folder go to the box
  with a note, or are skipped with a reason; `box unsent` shows which is which. A store still holding the
  template of 0.5.6 is still recognised as the template by `plan`.

Compatibility: only optional keys are new (`box.git_text_max_bytes`, `box.git_binary_max_bytes`,
`box.git_total_max_bytes`, and new keys in `box unsent --json`); the records of 0.5.2 to 0.5.6 are read and
written as before. The transfer calls of every box command are unchanged. On a developer machine only the output
of `box unsent` changes.

## 0.5.6

Files a cloud session made are no longer lost by being forgotten: the program detects them, the agent judges
each one, and a hook makes sure the judging happens.

- **`box unsent [--json] [--check] [--time-limit 5]`.** Lists the files of the project clone that are untracked
  or ignored by git, changed at or after the session baseline, and persisted nowhere: not fetched from the box
  unchanged, not sent in this session unchanged, not declared disposable. Left out by name or rule: env files,
  links, the folders `.git`, `.agent-mirror`, `__pycache__`, `.venv`, `venv`, `node_modules`, `.cache`,
  `.pytest_cache`, `.mypy_cache`, `.ruff_cache`, `.tox` and `.nox` at any depth (not `dist` or `build`, which
  some projects use for results), nested git clones, the box's mount and receive folders, `box.exclude` and
  the new optional `box.unsent_ignore` patterns. Grouped by top-level folder, largest first, a folder that
  holds only new files on one line, at most 30 lines. Offline and read-only (one `git status` without the
  index lock), within a time limit. Exit 0; `--check` exits 3 when something is unsent or the check could not
  finish.
- **The session baseline.** `session-start` records, for each project clone that has a box, the time its
  setup work finished, in `<clone>/.agent-mirror/baseline.json`, and keeps it for the same session (on a
  cloud runtime the same machine, on a developer runtime 24 hours, as `box send` decides its session).
  Without one, `box unsent` says so and counts from the time the clone was made (the first entry of git's
  HEAD log).
- **`box send` remembers what it sent** in the local session record (`sent`: size and modification time per
  file), so those files leave the list until they change.
- **`box skip PATH... --reason TEXT`.** Declares files or folders of the clone disposable for this session:
  recorded locally and in the session manifest as the new optional `skipped` list (`{"at", "reason",
  "paths"}`, the newest 20, the reason under the note rules). `box sessions` prints `(N paths left behind:
  reason)`, `box fetch --session` prints what was left behind and why, and the index summary carries the
  count. When the box cannot be reached it records locally and says so; a path outside the clone is refused.
- **The stop check (Claude Code, cloud runtimes only).** `hooks install` adds a fourth hook on a cloud runtime,
  `Stop: agent-mirror hook stop-check` (developer machines never get it, and install removes a stray one
  there; `hooks status` and `doctor` count 3 or 4 by runtime). When the agent ends its turn with unsent files,
  the hook prints `{"decision": "block", "reason": ...}`: the list, the `box send` and `box skip` commands and
  the keep or leave criteria. Never twice for the same set (short digests in `<cache>/stop-check.json`; again
  only when the set gained a file, which also covers `stop_hook_active`), at most 3 times a session, never on
  an error, on unreadable input or when its check runs out of time (8 s, a hard stop 12 s later). Silent
  anywhere but a cloud runtime. Codex has no stop hook: its instructions say to run `box unsent`.
- **Old cloud machines get the hook.** A Claude cloud session start checks Claude's settings and adds the
  agent-mirror hooks the machine lacks when some are there (step `hooks check`). A hook added during a
  session may only be used from a later session, and a cached machine starts each session without it: raise
  the `# revision:` line of the pasted setup script once (and pin 0.5.6 or later; `cloud render` now warns
  about an older pin). Old pasted scripts and snapshots keep working.
- **Keep or leave criteria.** The template `AGENTS.md`, the README and the docs carry the rule: KEEP results
  that took real time or money to produce, analysis outputs, generated reports and figures, anything a later
  session would otherwise have to redo; LEAVE caches, anything rebuildable in minutes, files already in git,
  secrets and env files. A store still holding the template of 0.5.3 to 0.5.5 is still recognised as the
  template by `plan`.
- **Developer machines: owned folders changed since the last push.** `session-start` prints one line per
  project, `box of demo: 2 owned folders changed since the last push (results, ws-a); upload them with:
  agent-mirror box push --project demo`, and nothing when nothing changed or nothing is owned here. Offline:
  `box push` now records what it uploaded of each folder in `<clone>/.agent-mirror/pushed.json` (`box release`
  clears it), and the owners line of the committed index counts too, whichever is newer. Bounded by
  `--push-check-timeout` (default 5 s, 0 skips it), recorded in the step log as `box push check (<project>)`;
  the rest of a developer session start's output is unchanged.

Compatibility: every new key is optional (`skipped` in the manifest, `sent` and `skipped` in the local
session record, `box.unsent_ignore`), so 0.5.2 to 0.5.5 read what 0.5.6 writes and the reverse. The transfer
calls of the existing box commands are unchanged.

## 0.5.5

- `doctor` on a cloud machine checks the links of that machine's own agent only. A Claude cloud machine is
  set up with `install --agents claude`, so the Codex instructions link was reported as `MISSING` there.

## 0.5.4

- `probe --full` shortens the `doctor` output to its last 40 lines; a link line that is not OK (MISSING,
  ELSEWHERE, NOT LINKED, COPY) or a PROBLEM above that cut is now printed too. Before, the summary could say
  `1 MISSING` with the line itself cut away.
- The runbook's first-session prompt shows the probe report and removes it after sending it. A report left in
  the project clone made the vendor's stop hook ask the session to commit and push it.

Faster tests; no change to the program otherwise.

- **`python3 tests/run_parallel.py`** runs the suite in parallel: every test class in its own Python process
  (`--jobs N`, default the CPU count; `--split file`; `--timeout SECONDS`), each with its own temp folder,
  `AGENT_MIRROR_HOME` and `AGENT_MIRROR_TEST_SANDBOX`, and the sandbox guard in every worker. It prints the
  call-count tables, the full output of what failed, the skipped tests with their reasons and one summary
  line. CONTRIBUTING.md says how to use it and that new tests must be safe to run in parallel. The serial
  `python3 -m unittest discover -s tests` works as before.
- **WSL: the tests leave the Windows folders (`/mnt/c/...`) out of PATH** when that changes no lookup of git,
  bash, sh, timeout, az or rclone. Every program lookup there is slow: a real rclone start spent about a
  second in them, which made the real-rclone tests take most of the run.
- **No real `az` or `rclone --version` from `probe --full` tests.** Three of them found the real programs on
  PATH (and the real `az --version` checks online for updates); they now put fakes in front, as the other
  probe tests do. Their assertions are unchanged.
- Measured on a 20-CPU WSL 2 machine: the serial run went from about 380 s to about 120 s, and the parallel
  run takes about 25 s (about 40 s with `--jobs 4`). Elsewhere only the parallel gain applies.

## 0.5.3

Notes that travel with the box's files, and two fixes from a real Windows 11 machine.

- **`box send --note TEXT`.** One to three sentences: what the files are, why they were made, what to do
  next. The session manifest gets two optional keys: `note` (the latest note given) and `notes`, one
  `{"at", "note", "paths"}` entry per note with the paths it was given for (consecutive sends with the same
  text share one entry; the newest 20 are kept), so a later send with another note does not erase the
  earlier context. `schema` stays 1. On a cloud runtime a send without a note is refused with exit code 2
  and one line, before anything is uploaded: `error: refusing box send on claude-cloud without a note: pass
  --note "what these files are and why" (...)`. On a developer machine the note stays optional. `probe
  --full` sends its test file with a note.
- **Note text rules.** One line (control characters dropped, whitespace and newlines collapsed), at most
  500 characters, and text that matches `sync`'s secret patterns or holds the value of a secret-looking
  environment variable is refused; the message names the pattern, never the text.
- **Notes are shown.** `box sessions` prints the session's note under each line (up to 200 characters;
  `--json` has the whole note and the count of notes). `box fetch --session ID` prints every note of the
  session first (`--dry-run` prints them and downloads nothing). The `_sessions` summary of `box index`
  carries each session's note, so `box sessions` without the transfer program shows it too.
- **`box note FOLDER TEXT` and `box note FOLDER --clear`.** A one-line note on a folder of the shared tree,
  stored in the folder's `_owners.json` entry (`note`, `note_updated`). Only the owning machine sets it, on
  a developer machine, with the messages of `box release`; refused on a cloud runtime. It goes through the
  same read, merge, write, read back as every `_owners.json` change (and converts a box that still has the
  old `_writer.json` marker the way `box release` does). `box push` keeps an entry's note (and any key it
  does not set itself) when it rewrites the entry, and so does `--take-over`; `box release` removes the
  entry with its note. `box note` also writes the entry into the owners line of the local
  `box_index.jsonl`; commit it as after `box index`. `box owners` (`--json` too) and `box ls` show the
  note, offline from the committed index.
- **At a cloud session start**, `session-start` ends with, per project of the session, the newest 5
  sessions (label and id, age, file count, note) and the shared folders that have notes. It makes the calls
  `box sessions` makes (one listing and one batched manifest read; azure-blob signs in first), all within
  the new `session-start --box-timeout SECONDS` (default 20; 0 skips it; the az sign-in is bounded by it
  too). The folder notes come from the committed index. It is the step `box notes (<project>)` of the step
  log, never fails the session, prints one line when it cannot list, and shows the committed index's
  session summary when the transfer program is missing. A developer machine prints nothing new. In a test
  sandbox it never starts a transfer program from outside the sandbox.
- **Call counts are unchanged** for `box sessions`, `send`, `fetch`, `push`, `owners` and `index`; a new test
  checks that a cloud session start makes the same number of calls with 3 and 30 sessions (rclone 2,
  azure-blob 3). `box note` makes 4, like `box release`.
- **Compatibility.** 0.5.2 reads manifests and `_owners.json` written by 0.5.3 (the new keys are optional
  and ignored), and 0.5.3 reads those of 0.5.2 (no notes). A 0.5.2 `box send` into a session keeps its
  notes, and a 0.5.2 `box push` of an unchanged folder keeps the folder note. One loss: a 0.5.2 `box push`
  by the owning machine of a folder whose content changed rebuilds that entry without the note; set it
  again with 0.5.3. A new test runs the 0.5.2 release against records of both versions.
- **Upgrade steps.** The runbook's first-session prompt now uses `--note`, so pin agent-mirror 0.5.3 or
  later in `store.json` before you render again (`cloud render` warns about an older pin). An instruction
  in your own `AGENTS.md` that tells cloud agents to run `box send` without `--note` now meets the refusal:
  add the note. The template `AGENTS.md` says so; a store that still holds the 0.5.0 to 0.5.2 template counts
  as having no instructions of its own. A rule worth adding: "a cloud agent gives a note with every `box
  send` and reads the notes (`box sessions`, `box ls`) before using files from the box".
- **Windows: git's "dubious ownership".** A store or agent-mirror clone made from an elevated shell belongs
  to the Administrators group, and git started without elevation (Task Scheduler, the agent app) refuses
  it. `sync` printed only "the store ... is not a git clone". Now `sync`, `session-start`, the cloud
  self-update, `cloud bootstrap`, `init-store` and `probe` name the refusal in one line with the exact
  remedy (`git config --global --add safe.directory <path with forward slashes>`, the path git names), and
  `doctor` reports it as a PROBLEM for the store and for the agent-mirror clone. The git config is never
  changed by agent-mirror. Tested with git's `GIT_TEST_ASSUME_DIFFERENT_OWNER` where git honours it.
- **Windows: the Microsoft Store Python.** It keeps the files it writes under `%LOCALAPPDATA%` and
  `%APPDATA%` in `%LOCALAPPDATA%\Packages\PythonSoftwareFoundation.Python.<version>_<id>\LocalCache\`, so a
  printed log path did not exist for other programs. agent-mirror recognises that Python (from
  `sys.executable` or `sys.base_prefix`), and `doctor`, `probe`, `timer install` and `sync --log` also print
  the real location, checked by which path exists. Nothing changes with any other Python. The computation is
  unit-tested with made-up paths.

## 0.5.2

Fixes from real Claude Code on the web probe sessions.

- **The setup script is a fixed bootstrap.** `cloud render` now writes setup scripts whose only
  project-specific text is the project name, the store URL, the agent-mirror URL and the runtime marker.
  They clone the store (a read-only token is enough), check agent-mirror out at the ref that `store.json`
  on the store's default branch pins, and run the new `agent-mirror cloud bootstrap --project NAME --vendor
  claude|codex`. That command reads `projects/<name>/env.json` from the default branch (with `git show`,
  never from the working tree, never from an `inbox/*` branch) and does what the setup scripts of 0.5.1 did
  themselves: companion repos, Python through uv, the `setup_commands` (each timed into
  `~/agent_setup_probe.txt`; STORE, TOOL, AM and NOTE are set for them as before), then `init-store
  --adopt`, `install` and (Claude) `hooks install`. It writes a stamp on the machine,
  `<config>/bootstrap-stamp.json` (never in the store): a hash of the setup inputs and the agent-mirror
  commit. Paste the script once per environment; a project settings change needs no new paste. The
  `# revision: 1` line is the manual fallback: raise it to make the vendor rebuild its cached machine.
  When the store pins an agent-mirror without `cloud bootstrap`, the script falls back to the 0.5.1 steps
  (links and hooks) and `cloud render` warns.
- **Drift repair at cloud session start.** After the store pull and the self-update to the pinned ref,
  `session-start` on a cloud runtime compares the stamp with the default branch and runs the setup steps
  again for a project whose `python`, `setup_commands` or `companion_repos` changed. It is bounded by the
  new `session-start --repair-timeout` (default 900 s; a command it has to stop is killed with everything
  it started), keeps the programs' output out of the session, never fails the session, and keeps the old
  record when the time ran out so that the next start tries again. A project that `cloud bootstrap` could
  not set up (env.json missing or invalid) is set up at the first session start after the store is fixed.
  A detached store checkout without `origin/HEAD` (a vendor's own checkout may be one) is read at its
  checked-out commit, as the setup script reads the tool ref there. Each new session starts from the
  vendor's cached machine, so the repair repeats at every session start until the machine is rebuilt.
  The self-update reads the pinned ref from the default branch too (from the working tree only when the
  default branch cannot be told, as before) and refuses a store checkout on an `inbox/*` branch.
- **Session-start timing.** Each step (store pull, tool update, drift check and each repaired setup step,
  each dependency sync, each `session_start_commands` entry, linking) is recorded with start, end, seconds
  and status in `<cache>/session-start.jsonl` (the newest 50 runs; names and times only, never output). A
  cloud session start prints a one-line summary; `probe` and `probe --full` print the last record.
- **`box send` from the folder above the clones.** A cloud session starts in the folder that holds the
  project clone and the store clone. `box send FILE --project NAME` there took that folder as the project
  folder, so it started a second session namespace with no git branch recorded. The box commands now find
  the clone of NAME (the current repo, then the clones in the child folders, as session-start does). A path
  is taken relative to the project folder first, then relative to the current folder: inside the clone it
  keeps its path, and a file outside it (`../probe2.txt`, which 0.5.1 refused with "without '..'") is sent
  under its file name. A folder outside, a name that exists both in the clone and in the current folder,
  env files, the manifest name and excluded paths are refused with one line that says what to do.
- **`doctor` and `probe` on a cloud session** require the box tools only of the projects in the session (on
  a developer machine: of the projects found or configured with a folder here). A tool only another
  project's box needs is an info line, not MISSING or a PROBLEM.
- **Nothing waits for a choice on a cloud machine.** A skill only the cloud machine has (the vendor
  preinstalls some, such as a session-start hook skill) is `external`; a box unit has no role to choose
  there; any other unit that would wait is left as it is and `apply` says so. Developer machines are
  unchanged.
- Inbox safety, confirmed by tests: a developer sync applies only plain paths under `memory/` (no empty,
  `.` or `..` part, no backslash or colon), refuses a memory path that resolves outside `memory/` through
  a link, and holds every change to `projects/*/env.json`, `projects/*/cloud/**` and `store.json` for
  review, also when a branch renames one into `memory/`.
- The runbook's first-session prompt writes the probe report inside the project clone and sends it to the
  box: `cd <clone> && agent-mirror probe --full > probe_output.txt 2>&1 && agent-mirror box send
  probe_output.txt --label first-probe`.

## 0.5.1

- Docs and rendered setup scripts now ask for a read-only store token. On Claude Code on the web the token is used only for the clone at setup time; in-session pulls and pushes go through the vendor GitHub access for the selected repos.

- The sessions commands make a fixed number of rclone or az calls, however many sessions the box holds.
  Real use on a consumer cloud drive, where every call costs seconds, showed 0.5.0 taking minutes for a
  fetch by label with 8 sessions and over two minutes to prune 5 small ones, because it read each manifest,
  listed each namespace without one and deleted each namespace with a call of its own.
  - `box sessions`: one recursive listing (it also gives the counts and sizes of namespaces without a
    manifest) and one batched manifest read: 2 calls. With `--limit`, rclone reads only the manifests shown;
    azure-blob reads them all in its one call and cuts the list afterwards.
  - `box fetch --session`: 3 calls for a full id (only that namespace is looked at), a unique start of an id
    or a label; the label lookup is not repeated within the command, and archives come in the same download.
  - `box prune --yes`: rclone deletes all selected namespaces with one `rclone delete` (an anchored
    `+ /<id>/**` filter per namespace, then `- **`) and one `rclone rmdirs` with the same filters for the
    emptied folders: 5 calls. azure-blob still needs one `delete-batch` per selected namespace, because one fnmatch pattern
    cannot name several prefixes without matching others.
  - `box index` reads its session summary the same way, and reads archive sidecars and separate tables of
    contents in batches. `box receive` downloads in one batch on azure-blob too.
  - azure-blob batches several files into one `az storage blob download-batch`, with a pattern chosen from
    the listing; when every pattern would also fetch more than 16 MiB of other files, it goes file by file.
  - `box send` stays at 3 calls for the first send of a session and 4 later; the manifest read before a
    write and the read back after it are both needed so that no other writer's change is lost.
- Tests count the calls with 3 and with 30 sessions (and 3 and 30 shared folders) on both fake backends and
  require the same count under a ceiling; a prune next to a look-alike name (a selected id that is the start
  of another namespace's name) deletes only the selected namespace, also with the real rclone.
- platforms.md records what the real cloud sessions showed; project-config.md has a "Cost on slow remotes"
  note.

## 0.5.0

- The two sides of the box are now called **shared** (was inbound) and **sessions** (was outbound) in
  messages, help texts and docs. New config keys: `shared_container` and `sessions_container`
  (azure-blob), `shared_remote` and `sessions_remote` (rclone). The older `inbound_*` and `outbound_*`
  keys keep working; `doctor` notes them in one line. `cloud render` notes and the store templates use the
  new names. Objects inside existing boxes stay where they are.
- Session namespaces. `box send` writes into `<sessions>/<session id>/`, one namespace per session, made
  at the first send and remembered in `<project>/.agent-mirror/session.json`. The id is
  `<runtime>-<YYYYMMDDTHHMMSSZ>-<6 hex>`. A cloud runtime starts a new session on a new machine; a developer
  runtime with `--new-session` or after 24 hours. `box send --label TEXT` names the session. A second send
  of a path in the same session replaces it (before, every send made a new folder and never overwrote).
- Each session has a manifest, `_session.json`: schema, id, label, runtime, machine id and name, project,
  repo, git branch and commit at the first send, start and update times, tool version, the files (path,
  bytes, modification time; folders sent as archives with `archive: true` and their file count) and what
  was promoted. It is updated after each send with read, merge, write and read back, and holds no
  environment values or absolute paths.
- `box sessions [--json] [--limit N]`: the sessions newest first, from one listing plus one manifest read
  per listed session; namespaces from older versions show as "no manifest". Any runtime. Falls back to the
  summary in the committed index when the transfer program is missing.
- `box fetch --session ID [PATH] [--into DIR]`: a session's files into `<project>/_sessions/<session id>/`
  by default (`--into .` writes over the project tree). ID can be a unique start of the id or a label.
  Never overwrites on a developer runtime; disk guard; archives extracted safely; `_sessions/` is
  git-excluded; `box status` lists fetched sessions and `box evict _sessions/<id>` removes them.
- `box promote --session ID PATH [--as FOLDER]` (developer machines): copies a session's file or folder
  into the shared tree through this machine's copy of the folder and the normal ownership of `box push`
  (claimed when unowned). Refused when the folder is owned elsewhere or not covered by `box.include` or
  `box.archives`. Recorded in the session manifest. `--dry-run`.
- `box prune [--older-than DAYS] [--session ID ...] [--keep-promoted] [--yes]` (developer machines):
  deletes session namespaces; always lists them with sizes first, deletes only with `--yes`, never touches
  the shared tree, refuses names that do not look like session ids.
- `box receive` downloads every session not received yet, one folder per session, as before; its help
  points at `box sessions` and `box fetch --session`. `box index` adds a compact session summary line.
  `probe --full` lists the newest sessions after its send test and checks that its own session is there.
- The store's AGENTS.md template tells cloud agents to look at `box sessions`, to send with a label, and
  that results worth keeping are promoted by the user's own machine. A store still holding the previous
  template counts as having no instructions of its own, as before.
- Fewer transfer calls: `box push` reads the sidecars of all archives it pushes in one call on rclone, the
  first manifest write of a new session skips a read, and a session found by label is not read twice.
- Tests: send, the manifest and its merge, listing, fetch by id, start and label, promote, prune and receive
  on azure-blob (fake az, older key names) and rclone (fake, new key names), one story with the real rclone,
  and two cloud sessions whose results a third lists and fetches.

## 0.4.0

- A box can have several writer machines, each owning its own folders. The unit of ownership is a folder
  unit: every archived folder and every include folder (on rclone, include patterns that do not start with
  a folder form the unit `.`). `<inbound>/_owners.json` records, per folder, the owning machine's id and
  name, since when, the fingerprint of its last upload and when it was updated. It replaces the single
  writer marker `_writer.json`.
- `box push` claims folders nobody owns (recorded after the upload succeeded), uploads the folders this
  machine owns, and skips folders another machine owns with a note such as
  `owned by desk-a since 2026-11-17T10:00:00Z; local copy different`. Skipping is not an error. A rclone
  push carries only the include patterns of the folders it pushes, so `--delete` never touches another
  machine's folder. `--only FOLDER` (repeatable) limits a push; `--take-over FOLDER` (repeatable) moves a
  folder to this machine and uploads it. `--dry-run` reads `_owners.json` and says what would be claimed,
  uploaded and skipped. A machine whose role is `reader` or `none` is still refused before any transfer.
- The remote has no locking: every change to `_owners.json` reads it again right before writing, changes
  only this machine's entries, writes, reads it back and checks them; on a mismatch it tries again (3
  times), then fails clearly. A folder another machine claimed in the meantime is reported, never taken.
- `agent-mirror box owners [--json]`: every folder, its owner, whether it is here, and the local copy
  against the owner's fingerprint (`same`, `different`, `absent`, `fetched`, `unknown`, `unowned`). Read-only; uses
  the committed index when the transfer program is missing.
- `agent-mirror box release FOLDER...`: the owning machine gives folders up so another machine's next
  push claims them. No data is deleted.
- `box.archives` entries can name nested folders: `"decompile/project*"` or `"decompile/binaries-*"`.
  Every matching folder is its own archive, named by its relative path
  (`_archives/decompile/project-cli.tar.gz`, `.json`, `.large/`). A folder inside an archived folder is
  never an archive of its own; a folder that matches two entries goes with the first, and `doctor` warns
  about both cases. Large files, tables of contents, the fingerprint skip, safe extraction, sub-path
  fetch, `box ls` and `box candidates` coverage all work for nested names. An archived folder inside an
  include folder is left out of that folder's sync.
- `box index` writes the owners as one line `{"path": "_owners.json", "schema": 1, "owners": {...}}`, and
  archive entries carry their fingerprint. `box ls` names the owner next to each folder. `plan` compares a
  box folder by folder: `reconcile` only when a folder owned by another machine is here with different
  content (listed as a conflict); its note also lists the folders owned here, owned elsewhere and the same
  or not here, and the folders the next push would claim. The default role is `writer` when this machine
  owns or would claim a folder.
- Compatibility: a box with only `_writer.json` reads as that machine owning every folder currently in
  the box. The first `_owners.json` written for it carries those folders too; `_writer.json` is never
  deleted. A bare `--take-over` keeps its old meaning only while there is no `_owners.json`. Update
  agent-mirror on every machine that pushes to a box: older versions read only `_writer.json`.
- "Same" means the same relative paths, sizes and modification times (whole seconds), the fingerprint
  archives already used. Copies with equal bytes but other times read as different. A folder whose every
  file here was fetched from the box and is unchanged since reads as `fetched`, never as a conflict.
- Tests: ownership on azure-blob (fake az), rclone (fake rclone, and the real rclone against local folders
  when installed), another machine writing `_owners.json` during a push (once, every time, and claiming the
  same folder), the old marker, nested archives, and a proof that `apply --dry-run` (and `plan`) change no
  path, size, time or link and create no machine id.

## 0.3.1

- Skill comparison reads CRLF as LF in text files, so a skill checked out on Windows and the same skill in the store (git keeps LF) no longer show as different. Binary files are still compared byte for byte. Found on the first real Windows plan, where 11 of 13 differing skills differed only in line endings.
- A folder in a skills folder that has files but no SKILL.md is listed as "not a skill" and left alone.

## 0.3.0

- Cloud setup scripts export AGENT_RUNTIME, because the vendor runs them before it sets its own session markers. Without it, install during setup looked like a developer machine and wrote a machine profile into the store, which every cloud sync then pushed and a developer machine held for review. Sync on a cloud runtime also leaves `machines/` out of its commits.

- Per-unit reconciliation. `agent-mirror plan` (read-only, `--json`) compares this machine with the store
  for every unit: the instruction file per agent, each skill by name (compared by content), each
  project's Claude memory folder and each project's box. Cases: `first-setup` (only this machine has it),
  `join` (only the store has it), `reconcile` (both, different), `identical`, `nothing`, `linked`, plus
  `external` (a skill linked into a project repo) and `legacy-folder-link`. It ends by saying whether the
  machine is fresh (nothing waits for a choice). See plugin/docs/reconciliation.md.
- `agent-mirror apply [--choices FILE] [--only UNIT_ID ...] [--yes-defaults] [--dry-run]` applies one
  action per unit: skills `adopt`, `keep-local`, `link`, `skip`, `take-store`, `keep-machine`;
  instructions `adopt`, `link`, `take-store`, `keep-local`; memory `adopt`, `link`, `merge`, `skip`; box
  `writer`, `reader`, `none` (a role only); `convert` for a legacy whole-folder skills link. Units that
  need a choice wait. Everything replaced is backed up and `uninstall` restores it. The secret scan runs
  before anything is copied into the store. Cloud runtimes never adopt.
- `install` is for fresh machines: it applies every unit that needs no decision, lists the units waiting
  for a choice and says to run `agent-mirror plan`. `--strict` exits with code 4 and changes nothing when
  the machine is not fresh. Skills are linked one by one into the shared skills folder; a whole skills
  folder is never replaced by one link, and the old whole-folder link is converted.
- Machine identity: a stable id and a name (default: the host name) in the user config, made by
  `init-store`, `install` and `apply`; `agent-mirror machine [--name NAME]`. Recorded choices live there
  too. After a real apply on a developer runtime a non-secret profile goes to `machines/<name>.json` in
  the store; `sync` commits `machines/`, and from a cloud session it is held for review.
- `sync` copies a real (unlinked) Claude memory folder into the store only for a project whose memory
  this machine decided to keep there (before, any folder whose name ended with a project name).
- One writer per box: the first `box push` writes `<inbound>/_writer.json`; pushes from another machine
  are refused until `box push --take-over`; a machine whose recorded role is `reader` or `none` is refused
  before any transfer. Both backends, and `--dry-run`. `box index` records the marker in the index.
- `agent-mirror box candidates`: a read-only list of untracked and ignored entries with sizes, large
  files, newest change, a kind guess (`rebuildable`, `secret`, `worktree`, `candidate`) and box coverage.
- `companion_repos` in env.json: public repos that a cloud session cannot attach. `cloud render` adds a
  token-free clone step into `~/companions/<dir>`; cloud `session-start` links them as `../<dir>` beside
  the project clone.
- Cloud `session-start` updates agent-mirror's own clone to the ref pinned in store.json (when its origin
  matches) and runs the new code once. In a test sandbox it never touches a clone outside the sandbox.
- `probe --full` tests a sub-path fetch from an archive when the index has no small loose file, then
  evicts it. `doctor` reports a missing `az` or `rclone` as a problem only when a project's box needs it,
  shows the machine and a plan summary, and marks links decided against as `DECIDED`.
- Tests: plan and apply for every case and unit kind, the writer marker on both backends, candidates,
  companions, the self-update with a local bare repo. `tests/profile_fingerprint.py` and CONTRIBUTING.md
  describe the read-only before and after check of a real profile.

## 0.2.1

- Verified on Claude Code on the web (2026-11): setup script timing, store clone by token at setup, user-level hooks written by the setup script, rclone box through the proxy (list, fetch, send), public companion repo cloned at setup, memory round trip through an inbox branch. Finding: inside a running session GitHub is reached only through the vendor proxy, so the store repo must be selected in the session; the Claude runbook now says so.

- Safety: one resolver (`platforms.py`) for every home-like location. `AGENT_MIRROR_HOME` moves them all
  into one folder on every platform. With `AGENT_MIRROR_TEST_SANDBOX` set, every command exits with code
  97 when any location is outside the sandbox, and real schedulers and clipboard tools are never started.
  `agent-mirror debug paths` prints the resolved locations. The test harness sets both for every run and
  refuses to start a test run that is not sandboxed. (An earlier Windows test run changed a real user
  profile because the tests isolated HOME only.)
- Windows: the test suite passes natively (Windows 11, Python 3.11). Fixed: mixed path separators when
  joining box paths, case-sensitive inside-folder checks, "owner-only" secret files (Windows ignores 0600;
  agent-mirror now checks and reports "inside the user profile" there). Fake tools work as `.cmd` shims.
- CONTRIBUTING.md with the test rules.

## 0.2.0

- Fix after release: the Windows and WSL clipboard copy left an invisible byte order mark before the first line; it now copies through PowerShell. `clip copy --secret FILE` works in either argument order. `clip clear` works on Windows.

- Secret recipes: `"secrets"` in a project's env.json names where each environment value comes from
  (`rclone-remote`, `local-file`, `env`, `literal`, `azure-keyvault`, `ask-user`), never the value. Other
  recipe types are rejected; no command from the store is ever run. `doctor` and `cloud render` warn about
  variables no recipe covers.
- `agent-mirror cloud secrets --check | --out FILE [--replace]`: produce the values on this machine, write
  them to an owner-only file outside the store and any git work tree, print names and lengths only.
- `cloud render` also writes `runbook.json` per vendor (network hosts, env values with their recipes, setup
  script placeholders, first-session prompt, cleanup); the README is generated from it.
  `agent-mirror cloud runbook [--json]` prints it.
- Inbox branches: with `"cloud_push": "inbox"` (the default for new and adopted stores) cloud sessions push
  to `inbox/<runtime>-<id>`, never to the main branch. A developer machine's `sync` applies memory changes
  (union-merged indexes, side files on conflicts, secret scan) and holds everything else for
  `agent-mirror review list | show | accept | discard`. `doctor` reports held branches. Cloud
  `session-start` pulls main with a rebase.
- `agent-mirror clip copy | check | clear`: clipboard hand-off without printing secrets (Windows and WSL,
  macOS, Wayland, X11).
- README: "Join a second machine".

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
