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
6. **Fake tools only.** `az`, `rclone` and clipboard tools are fakes written by `helpers.install_fake`
   (a Python script, plus a `.cmd` shim on Windows). Nothing reaches a network, a cloud account, a real
   scheduler or a real clipboard.

## Running the tests

```bash
python3 -m unittest discover -s tests          # Linux, macOS
python -m unittest discover -s tests           # Windows
```

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
