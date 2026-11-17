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

## Style

- Python 3.8+ standard library only; every program is started with an argument list, never a shell
  string.
- Plain, simple English in messages and docs; no em dashes.
- Nothing private in this public repo: no real names, user names, hosts, accounts or tokens.
  `tests/test_public_safety.py` checks it.
