# Platforms

> **WARNING: run the tests only with the sandbox guard.** An early test run on native Windows isolated
> HOME while the program read USERPROFILE and APPDATA there, so it changed the real user profile; the
> guard below (`AGENT_MIRROR_HOME` plus `AGENT_MIRROR_TEST_SANDBOX`, see CONTRIBUTING.md) now makes that
> impossible, and the test suite refuses to start without it.

agent-mirror is plain Python 3.8+ with the standard library only. It never runs a shell string:
every program it starts gets a list of arguments, and every path goes through `os.path` or `pathlib`.
Everything that differs between operating systems lives in one module,
`plugin/agent_mirror/platforms.py`, plus the scheduler content in `plugin/agent_mirror/timer.py`.
Both can be unit-tested on any OS by passing a platform name and an environment mapping.

## What is verified

| | Linux (including WSL 2) | macOS | Windows (native) |
| --- | --- | --- | --- |
| Test suite | runs and passes (Python 3.9 and 3.10) | UNVERIFIED | runs and passes natively (Python 3.11, Windows 11), under the sandbox guard, from a copy in the temp folder; one POSIX-shell test skipped; the real profile's before and after fingerprints were identical (0.3.0, and again for 0.4.0) |
| Folder mapping (homes, config, cache) | verified | unit-tested only | verified under `AGENT_MIRROR_HOME`; the real-profile mapping (USERPROFILE, APPDATA) unit-tested only; Claude home verified |
| Claude memory folder name (slug) | verified | UNVERIFIED | verified (see below) |
| Links | symlinks, verified | symlinks, UNVERIFIED | symlinks verified (Developer Mode on); real junctions verified by test; managed copies verified with a simulated symlink failure only |
| Scheduler (`timer`) | systemd user timer, file content unit-tested, enabling UNVERIFIED in tests | launchd, content unit-tested only | schtasks dry-run command verified by test; enabling UNVERIFIED |
| Hook commands | verified format | UNVERIFIED | written and recognised by test; UNVERIFIED that Claude Code on Windows runs them |
| Launcher | `plugin/bin/agent-mirror` | same file | `plugin/bin/agent-mirror.cmd` (tests run the Python launcher directly) |
| Clipboard (`clip`) | fake tools only | fake tools only | fake tools only (real clipboards are never touched by tests) |

"Verified" means the test suite exercised it on Linux (WSL 2, Ubuntu) or natively on Windows 11 with
Python 3.11, inside the test sandbox, or (for the Claude home and folder names) that it was read from the
Claude home of a real Windows 11 machine. Nothing has run on a real Mac. Each assumption below that is
marked UNVERIFIED should be checked before anyone relies on it.

## AGENT_MIRROR_HOME and the test sandbox

One module, `plugin/agent_mirror/platforms.py`, turns the environment into every home-like location: the
user home, the Claude and Codex homes, the shared skills folder, the companion repos folder, the user
config folder, the cache, the backups folder, the systemd user folder, the LaunchAgents folder and the
temp folder used for staging.
Nothing else in the program reads HOME, USERPROFILE, APPDATA, LOCALAPPDATA or XDG_* or calls
`os.path.expanduser`. `agent-mirror debug paths` prints every resolved location as JSON.

- `AGENT_MIRROR_HOME=<dir>` replaces the user home on every platform. Every location is derived from it,
  and the config is `<dir>/.config/agent-mirror/config.json` on Linux, macOS and Windows alike. HOME,
  USERPROFILE, APPDATA, XDG_CONFIG_HOME, CLAUDE_CONFIG_DIR, CODEX_HOME and TEMP/TMP/TMPDIR are ignored
  unless they point inside `<dir>`. Useful for tests, and for keeping a second, separate setup.
- `AGENT_MIRROR_TEST_SANDBOX=<dir>` is a tripwire. Before any command does anything, every location is
  resolved and compared with `<dir>` (real paths, case-insensitively on Windows); if one is outside, the
  command exits with code 97 and changes nothing. In that mode the program also never starts a real
  scheduler (systemctl, launchctl, schtasks) or clipboard tool (clip, PowerShell, pbcopy, pbpaste,
  wl-copy, wl-paste, xclip, xsel) unless the program found on PATH is itself inside `<dir>`.

## Secret files on Windows

Windows ignores the 0600 permission bits. Where agent-mirror writes or reads a secret file
(`cloud secrets --out`, `box rclone-env --out`, the `local-file` recipe), "private" means owner-only
(0600) on Linux and macOS, and **inside the user profile** on Windows, whose own permissions keep other
accounts out. The messages say which: a file written outside the profile on Windows is reported as
readable by other accounts, a `local-file` source outside the profile becomes "needs you: move it inside
your user profile", and `--replace` refuses a file outside the profile.

## Verified on Windows (Windows 11, read from a real Claude home)

- Claude Code's home is `%USERPROFILE%\.claude`.
- The memory folder for project `C:\Users\Name\my-project` is
  `%USERPROFILE%\.claude\projects\C--Users-Name-my-project\memory`: every character that is not a
  letter or digit becomes `-`, including the colon, the backslashes and spaces. A path containing
  ` - ` (space, dash, space) becomes `---`.
- The drive letter keeps the case it has in the path (upper case in the paths Windows reports).

agent-mirror's slug code (`claude_slug` in `platforms.py`) applies exactly this rule, and the tests cover
these cases.

Verified by the test suite running natively on Windows 11 (Python 3.11), inside the sandbox:

- install, uninstall and the backups with real symlinks (this machine allows them), real directory
  junctions (`mklink /J`: made, detected, read and removed), and the junction or managed-copy fallback
  when a symlink fails (simulated failure);
- init-store create and adopt, and the refusal of a store inside a project repo, with case-insensitive
  path comparisons;
- hooks install and uninstall in Claude's settings (quoted, forward-slash launcher path to
  `agent-mirror.cmd`), the memory-write hook, sync and the inbox flow with Git for Windows;
- programs found as `.cmd` files through PATH (`az.cmd`, `rclone.cmd` fakes), run with argument lists;
- archive mode with the Windows `tar` (bsdtar) and Python's safe extraction; forward slashes in tables of
  contents; rendered shell scripts written with LF line endings;
- box folder owners (`_owners.json`) and nested archive names, with the fake `az` and `rclone` and with a
  real Windows rclone against local folders (0.4.0);
- multi-repo discovery, cloud render, runbooks, cloud secrets and the timer dry run (schtasks).

Everything else about Windows below is still UNVERIFIED, in particular how Claude Code on Windows runs hook
commands, enabling a scheduled task, and the real clipboard tools.

## Folders

| What | Linux and macOS | Windows |
| --- | --- | --- |
| home (`~`) | `$HOME` | `%USERPROFILE%` (or `%HOMEDRIVE%%HOMEPATH%`) |
| Claude Code home | `~/.claude`, or `$CLAUDE_CONFIG_DIR` | `%USERPROFILE%\.claude`, or `%CLAUDE_CONFIG_DIR%` |
| Codex home | `~/.codex`, or `$CODEX_HOME` | `%USERPROFILE%\.codex`, or `%CODEX_HOME%` |
| shared skills | `~/.agents/skills` | `%USERPROFILE%\.agents\skills` |
| Claude skills | `<claude home>/skills` | `<claude home>\skills` |
| companion repos (cloud) | `~/companions` | `%USERPROFILE%\companions` |
| agent-mirror config | `${XDG_CONFIG_HOME:-~/.config}/agent-mirror/config.json` | `%APPDATA%\agent-mirror\config.json` |
| hook log | Linux `${XDG_CACHE_HOME:-~/.cache}/agent-mirror/sync.log`, macOS `~/Library/Caches/agent-mirror/sync.log` | `%LOCALAPPDATA%\agent-mirror\sync.log` |
| backups | `~/.agent-mirror-backups/<timestamp>/` | `%USERPROFILE%\.agent-mirror-backups\<timestamp>\` |
| with `AGENT_MIRROR_HOME` set | everything under it; config `<dir>/.config/agent-mirror` | the same, on Windows too |

UNVERIFIED (macOS): Claude Code and Codex use `~/.claude` and `~/.codex` there too.
UNVERIFIED (Windows): Codex uses `%USERPROFILE%\.codex` and reads `AGENTS.md` and `~/.agents/skills`
from the same places as on Linux. (Claude's `%USERPROFILE%\.claude` is verified, see above.)

Skills folders: agent-mirror links store skills one by one into the shared folder (`~/.agents/skills`)
and into each selected agent's own folder. Only Claude has its own folder in this model; Codex reads the
shared one. UNVERIFIED: older Codex versions that read `~/.codex/skills` instead; agent-mirror neither
reads nor writes that folder.

## Links

`install` and `apply` always try a symlink first.

- Linux and macOS: symlinks always work for a normal user.
- Windows: a symlink needs Developer Mode or admin rights. When the symlink fails, agent-mirror uses:
  - a directory junction (`cmd /c mklink /J`) for folders (each skill, the memory folder). Junctions
    need no special rights, but only work within local NTFS drives.
  - a managed copy for single files (`CLAUDE.md`, `AGENTS.md`). The copy is recorded in the backup
    manifest. `doctor` shows it as `COPY (copy, re-run install after editing)`. Edit the store copy,
    then run `install` again: it refreshes the copy and keeps the old one in the backup folder.
    `uninstall` removes a managed copy only when it still matches the store copy. `plan` shows a managed
    copy as `linked`, and as out of date once the store's file changed; `install` and `apply` then
    refresh it.
- `install` prints which method it used for each link, and the manifest records it.

Verified by test on Windows 11 (Python 3.11): junction creation through `cmd /c mklink /J`, junction
detection (reparse tags on Python 3.11), reading the target (without the `\\?\` prefix Windows adds) and
removal with `os.rmdir`. UNVERIFIED: junction detection through `os.path.isjunction` on Python 3.12+,
and Claude Code following a junction for its memory folder.

## The Claude memory folder

Claude Code keeps per-project memory in `<claude home>/projects/<slug>/memory`, where `<slug>` is the
project path with every character that is not a letter or digit turned into `-`. agent-mirror does not
assume the name: it first looks for an existing folder under `<claude home>/projects/` that matches the
computed slug of the project's real path or its absolute path, and on Windows also the form with the
drive letter in the other case (`C--Users-...` and `c--Users-...`). Only when none exists does it
create the computed one, and it says so. `install --memory-dir PATH` overrides the whole search.

Verified (Windows): the slug rule and the drive letter's case, see "Verified on Windows". agent-mirror
still also tries the other drive-letter case when it looks for an existing folder, which is harmless.
UNVERIFIED (macOS): that Claude Code uses the resolved path (for example
`/private/var/...` rather than `/var/...`); agent-mirror tries both.

## Hooks

`hooks install` writes three hooks into `<claude home>/settings.json`, each with an absolute path to
the launcher for the current OS:

- Linux and macOS: `'/path/to/plugin/bin/agent-mirror' session-start` (shell quoting).
- Windows: `"C:/path/to/plugin/bin/agent-mirror.cmd" session-start` (double quotes, forward slashes).

UNVERIFIED (Windows): which shell Claude Code uses for hook commands on native Windows (it is
documented to need Git for Windows, so probably Git Bash), and that the quoted forward-slash path to a
`.cmd` file runs from it. UNVERIFIED (all platforms): the `async` key on the PostToolUse hook.

## Scheduler (`timer`)

- Linux: `~/.config/systemd/user/agent-mirror-sync.service` and `.timer`, enabled with
  `systemctl --user daemon-reload` and `systemctl --user enable --now agent-mirror-sync.timer`.
  On WSL, systemd must be on (`[boot] systemd=true` in `/etc/wsl.conf`); without it, the enable step
  fails and agent-mirror says so. The unit runs the current Python with the launcher script.
- macOS (UNVERIFIED): `~/Library/LaunchAgents/agent-mirror.sync.plist` with `StartInterval`,
  loaded with `launchctl bootstrap gui/<uid> <plist>` (after a `launchctl bootout` that may fail).
  The plist carries the `PATH` from install time so `git` is found. Output goes to
  `~/Library/Logs/agent-mirror-sync.log`.
- Windows (UNVERIFIED): `schtasks /Create /TN agent-mirror-sync /SC MINUTE /MO <minutes> /TR "<command>" /F`.
  The command uses `pythonw.exe` (next to the current `python.exe`) so no console window opens, and
  `sync --log` writes output to the hook log. schtasks limits `/TR` to 261 characters; agent-mirror
  warns when the command is longer.

`timer install --dry-run` prints the files and commands for the current OS without writing or running
anything.

## Running programs

- On Windows, programs are found through `PATH` with their `.exe`, `.cmd` or `.bat` suffix
  (`az` is `az.cmd`), using `shutil.which`, and started with an argument list (verified by test with
  `.cmd` fakes for az and rclone).
- Transfers (`az`, `rclone`) run in their own process group so a timeout also stops their children
  (azcopy under az): `killpg` on Linux and macOS, `taskkill /T` on Windows (UNVERIFIED).
- The sync lock checks whether the process holding it is alive. On Windows this uses
  `OpenProcess` through `ctypes` (never `os.kill`, which would end the process on Windows); if that
  fails, a lock older than 15 minutes is treated as stale.
- Commands shown by `--dry-run` use POSIX quoting on every OS, with secret values replaced by `***`.

## Mounting the box (`box mount`, rclone only)

- Linux: needs `/dev/fuse` and a `fusermount3` or `fusermount` program (package `fuse3`); agent-mirror
  checks both first and says which is missing. The mount runs with `--daemon`; `box unmount` uses
  `fusermount3 -u`. Verified only that the command is built correctly; UNVERIFIED on cloud machines,
  where containers often have no FUSE. `probe --full` reports whether mounting looks possible.
- macOS (UNVERIFIED): needs macFUSE (`/Library/Filesystems/macfuse.fs`); unmount with `umount`.
- Windows (UNVERIFIED): needs WinFsp; `rclone mount` cannot run as a daemon there, so agent-mirror
  prints the command to run in its own window.

## Clipboard (`clip`)

`agent-mirror clip copy FILE`, `clip check` and `clip clear` pick the first backend that is present:

| Where | Copy | Read back | Clear |
| --- | --- | --- | --- |
| Windows, and WSL when `clip.exe` and `powershell.exe` are on PATH | PowerShell `Set-Clipboard`, text read as UTF-8 from stdin | PowerShell `Get-Clipboard -Raw` (UTF-8 output) | PowerShell `Set-Clipboard -Value $null` |
| macOS | `pbcopy` | `pbpaste` | copy an empty text |
| Linux with `WAYLAND_DISPLAY` | `wl-copy` | `wl-paste --no-newline` | `wl-copy --clear` |
| Linux with `DISPLAY` | `xclip -selection clipboard` (then `xsel --clipboard`) | the same tool | empty text, or `xsel --clipboard --clear` |

WSL is detected by `microsoft` in `/proc/version`; without the Windows tools on PATH it falls back to the
Linux tools. With no backend, `clip` says what to install and exits with code 3.

Verified on a real clipboard: WSL on Windows 11 only (copy, read back and clear, with a host list, a shell
script and a values file; the clipboard text matched the source byte for byte). Copying goes through
PowerShell, not `clip.exe`, because `clip.exe` needs UTF-16 with a byte order mark and leaves that mark on
the clipboard as an invisible first character. Native Windows, macOS and Linux desktops are UNVERIFIED.

## Two homes on one Windows PC (Windows plus WSL)

A Windows PC with WSL has two separate home folders: the Windows one (`C:\Users\<you>`) used by
native Windows agents, and the Linux one (`/home/<you>`) inside WSL used by agents started there.
They do not share `~/.claude`, `~/.codex` or the agent-mirror config.

Treat them as two machines:

1. Clone the store separately in each home (for example `C:\Users\<you>\agent-store` and
   `~/agent-store` in WSL), both from the same store remote.
2. Run `agent-mirror init-store <that path> --adopt` and `agent-mirror install` in each.
3. `sync` in each one commits and pushes to the same remote; `MEMORY.md` merges by union lines.

Do not point a WSL install at the Windows clone through `/mnt/c/...` (or the reverse): file modes,
line endings and symlinks behave differently across that boundary, and two agents writing one clone
at once can collide.

The Claude memory folder slug also differs between the two homes, because the project path differs
(`C:\Users\<you>\code\app` becomes `C--Users-<you>-code-app`, while `/home/<you>/code/app` becomes
`-home-<you>-code-app` and `/mnt/c/Users/<you>/code/app` becomes `-mnt-c-Users-<you>-code-app`).
Each home's install links its own slug folder to the same `memory/<project>/` in its own store clone.

## Cloud self-update and companion repos

On a cloud runtime `session-start` brings agent-mirror's own clone to the ref pinned in the store's
`store.json` (only when the clone's origin is `tool.repo`), with `git fetch --depth 1` for the shallow
clone the setup scripts make (a full clone is fetched without `--depth`, so it stays full), and runs the
new code once (the `AGENT_MIRROR_SELF_UPDATED` variable stops a loop). With `AGENT_MIRROR_TEST_SANDBOX`
set it never fetches into or checks out a clone outside the sandbox. It also links each project's
companion repos from `~/companions/<dir>` to `../<dir>` beside the clone, in Python. Both are exercised by
the test suite on Linux and natively on Windows (a local bare repo as the tool's origin); on a real cloud
machine they are UNVERIFIED.

## Claude Code on the web: what a real session showed

Checked in real sessions with an rclone box (2026-11):

- The setup script runs as root with the project already cloned, has direct access to GitHub, and finished in seconds. It can clone a private store with a token and public repos without one.
- A user settings file written by the setup script is honoured: the session-start hook ran and linked the memory folder.
- Inside a running session all GitHub traffic goes through the vendor's proxy, which serves only the repos selected for that session. The token used at setup no longer applies. Select the store repo next to the project in every session, or the store cannot be pulled and memory cannot be pushed to the inbox branch.
- A public repo that the user does not own cannot be selected. Clone it in the setup script and link it at session start.
- The rclone box worked through the proxy: listing, fetching an archived folder, sending a file.
- `box mount` is not possible there (no FUSE helper). Fetch on demand is the method.
- With two repos selected the session starts in the folder above the clones; project discovery and the extra memory link for that folder worked.

