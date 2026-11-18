# Contributing

agent-mirror changes real home folders: it links agent homes into a store, edits Claude Code's
settings, writes a user config and installs scheduled tasks. The tests must never be able to do that to
the machine they run on.

## The rule: tests run only inside the sandbox guard

Once, a test run on native Windows isolated HOME while the program read USERPROFILE and APPDATA there,
and it changed the real user profile. Since then:

1. **One resolver.** Every home-like location (user home, Claude and Codex homes, shared skills, user
   config, cache, backups, systemd user folder, LaunchAgents, temp folder) comes from
   `plugin/agent_mirror/platforms.py`. Nothing else may call `os.path.expanduser` or `Path.home`, or read
   HOME, USERPROFILE, APPDATA, LOCALAPPDATA or XDG_* directly. Use `util.expand`, `util.tilde`,
   `platforms.current()` and `Platform.mkdtemp`.
2. **`AGENT_MIRROR_HOME`.** When set, every location is derived from it on every platform.
3. **The tripwire.** With `AGENT_MIRROR_TEST_SANDBOX=<dir>`, every command first resolves every location
   and exits with code 97 if one is outside `<dir>`. Real schedulers and clipboard tools are never started
   in that mode unless the program found on PATH is itself inside `<dir>`.
4. **The harness.** `tests/helpers.py` gives every subprocess both variables plus HOME, USERPROFILE,
   APPDATA, LOCALAPPDATA, XDG_CONFIG_HOME, XDG_CACHE_HOME and TEMP/TMP/TMPDIR inside the sandbox, and
   removes CLAUDE_CONFIG_DIR and CODEX_HOME. Before any test runs, importing `helpers` starts
   `agent-mirror debug paths` in a sandbox and stops the whole test process (exit code 97) if a single
   location is outside it. Every test module also imports `setUpModule` from `helpers`.
5. **In-process tests** build a `Platform` from an injected environment (or rely on the sandboxed one
   `helpers` activates) and call `helpers.reactivate_sandbox()` afterwards; they never read the real
   environment's home.
6. **Fake tools, and a real `rclone` only against local folders.** `az`, `rclone` and clipboard tools are
   fakes written by `helpers.install_fake` (a Python script, plus a `.cmd` shim on Windows). The one
   exception: the `Rclone*Real*` and `RcloneLocalArchiveTest` classes run a real `rclone`, when one is
   installed, against folders in the sandbox with a sandboxed `RCLONE_CONFIG`; they are skipped without it. Nothing reaches a network, a cloud account, a real
   scheduler or a real clipboard. A cloud `session-start` lists the box only with a transfer program
   inside the sandbox (the fakes): a real `az` or `rclone` elsewhere on PATH is never started by it in a
   test run, and it reads the committed index instead.

## Running the tests

```bash
python3 tests/run_parallel.py                  # fast: in parallel (Linux, macOS)
python tests\run_parallel.py                   # fast: in parallel (Windows)
python3 -m unittest discover -s tests          # serial, in one process (Linux, macOS)
python -m unittest discover -s tests           # serial, in one process (Windows)
```

`tests/run_parallel.py` (standard library only) runs every test class in its own Python process, at most
`--jobs N` at a time (default: the CPU count; `--jobs 1` runs one class after the other). `--split file`
makes one process per test file instead, `--timeout SECONDS` (default 900) stops a process that hangs, `-v`
prints every process's full output, and file names (or patterns) run only those files:
`python3 tests/run_parallel.py test_box_notes.py`. It prints one line per class as it finishes (with what
the tests print, such as the call-count tables, and the full output of a class that failed), one line per
file, the skipped tests with their reasons, and one summary line, `Ran N tests in S s with --jobs J: OK`
or `FAILED (...)`. The exit code is 0 when every collected test ran and passed, 97 when the sandbox guard
stopped a worker, and 1 otherwise. Every worker gets its own temp folder (TMPDIR, TEMP and TMP), with its
own `AGENT_MIRROR_HOME` and `AGENT_MIRROR_TEST_SANDBOX` inside it, and imports `helpers`, so the sandbox
guard runs in every worker before any test; the run's temp folder is removed at the end.

**New tests must be safe to run in parallel.** Classes run at the same time, in any order, each in its own
process:

- make every folder with `tempfile` (ToolTest's `self.base` is one) and never use a fixed path, file name,
  port or other shared resource outside it;
- never change anything another process could see: the repo and its git state, a git config outside the
  sandbox, a global cache, another class's files;
- sharing within one class (`setUpClass`, a class attribute) is fine; between classes or files it is not;
- a test that measures time keeps a generous bound, since the workers compete for the CPU.

On WSL, `helpers` leaves the Windows folders that WSL appends to PATH (`/mnt/c/...`) out of the PATH the
tests use, unless that would change where git, bash, sh, timeout, az or rclone is found: every program
lookup there is slow, and a real rclone start spent about a second looking through them.

On a machine where you are unsure, run the guard tests alone first and read the paths:

```bash
python3 -m unittest discover -s tests -p test_sandbox_guard.py
```

New tests that start the program go through `ToolTest.run_tool` (or `helpers.sandbox_env`); never build
an environment by hand. A test that only makes sense on one platform is skipped with a stated reason.
The fake `rclone` keeps uploads when `FAKE_RCLONE_ROOT` is set (a folder standing in for every remote),
like the fake `az` with `FAKE_AZ_ROOT`.

The program never fetches into or checks out its own clone during a test run: the cloud self-update in
`session-start` refuses a tool clone outside `AGENT_MIRROR_TEST_SANDBOX` (tests of the self-update use a
copy of the plugin inside the sandbox, with a local bare repo as its origin).

## Proving the real profile is untouched (native Windows, or any machine you are unsure about)

Run the suite from a copy of the repo in a temp folder, never from a folder you work in, and fingerprint
the real profile before and after. `tests/profile_fingerprint.py` only reads: names, kinds, link
targets, sizes, modification times and SHA-256 of small files in every place agent-mirror could change
(the Claude and Codex instruction files, Claude's settings and skills, every Claude memory folder, the
shared skills folder, the agent-mirror config and cache folders, the backups and companions folders, and
on Windows a read-only `schtasks /Query` for the timer task).

```bat
rem a copy of the repo under the Windows temp folder
robocopy C:\path\to\code-agent-memory-mirror %TEMP%\am-check /E /XD .git __pycache__
cd /d %TEMP%\am-check
rem 1. prove the guard: every path must be inside the sandbox folder
mkdir %TEMP%\am-guard\home
set AGENT_MIRROR_HOME=%TEMP%\am-guard\home
set AGENT_MIRROR_TEST_SANDBOX=%TEMP%\am-guard
python plugin\bin\agent-mirror debug paths
set AGENT_MIRROR_HOME=
set AGENT_MIRROR_TEST_SANDBOX=
python -m unittest discover -s tests -p test_sandbox_guard.py
rem 2. the before-fingerprint, the full suite, the after-fingerprint
python tests\profile_fingerprint.py > %TEMP%\am-before.json
python -m unittest discover -s tests
python tests\profile_fingerprint.py > %TEMP%\am-after.json
python tests\profile_fingerprint.py --compare %TEMP%\am-before.json %TEMP%\am-after.json
rem 3. clean up: the copy, the guard folder, the fingerprints and any agent-mirror-* temp folders
```

The compare must print `identical`. If anything differs, stop and find out why before running anything
else. (A Claude Code session running at the same time on that machine can change its own memory
folders; close it during the check.)

## Style

- Python 3.8+ standard library only; every program is started with an argument list, never a shell
  string.
- Plain, simple English in messages and docs; no em dashes.
- Nothing private in this public repo: no real names, user names, hosts, accounts or tokens.
  `tests/test_public_safety.py` checks it.
