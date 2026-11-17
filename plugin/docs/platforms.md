# Platforms

agent-mirror is plain Python 3.8+ with the standard library only. It never runs a shell string:
every program it starts gets a list of arguments, and every path goes through `os.path` or `pathlib`.
Everything that differs between operating systems lives in one module,
`plugin/agent_mirror/platforms.py`, plus the scheduler content in `plugin/agent_mirror/timer.py`.
Both can be unit-tested on any OS by passing a platform name and an environment mapping.

## What is verified

| | Linux (including WSL 2) | macOS | Windows (native) |
| --- | --- | --- | --- |
| Test suite | runs and passes (Python 3.9 and 3.10) | UNVERIFIED | UNVERIFIED |
| Folder mapping (homes, config, cache) | verified | unit-tested only | Claude home verified; the rest unit-tested only |
| Claude memory folder name (slug) | verified | UNVERIFIED | verified (see below) |
| Links | symlinks, verified | symlinks, UNVERIFIED | symlink, junction or managed copy, UNVERIFIED |
| Scheduler (`timer`) | systemd user timer, file content unit-tested, enabling UNVERIFIED in tests | launchd, content unit-tested only | schtasks, command unit-tested only |
| Hook commands | verified format | UNVERIFIED | UNVERIFIED |
| Launcher | `plugin/bin/agent-mirror` | same file | `plugin/bin/agent-mirror.cmd` |

"Verified" means it ran on a Linux machine (WSL 2, Ubuntu), except for the Windows facts listed under
"Verified on Windows", which were read from the Claude home of a real Windows 11 machine. agent-mirror
itself has not run on a real Mac or a real Windows machine yet. Each assumption below that is marked
UNVERIFIED should be checked there before anyone relies on it.

## Verified on Windows (Windows 11, read from a real Claude home)

- Claude Code's home is `%USERPROFILE%\.claude`.
- The memory folder for project `C:\Users\Name\my-project` is
  `%USERPROFILE%\.claude\projects\C--Users-Name-my-project\memory`: every character that is not a
  letter or digit becomes `-`, including the colon, the backslashes and spaces. A path containing
  ` - ` (space, dash, space) becomes `---`.
- The drive letter keeps the case it has in the path (upper case in the paths Windows reports).

agent-mirror's slug code (`claude_slug` in `platforms.py`) applies exactly this rule, and the tests cover
these cases. Everything else about Windows below is still UNVERIFIED.

## Folders

| What | Linux and macOS | Windows |
| --- | --- | --- |
| home (`~`) | `$HOME` | `%USERPROFILE%` (or `%HOMEDRIVE%%HOMEPATH%`) |
| Claude Code home | `~/.claude`, or `$CLAUDE_CONFIG_DIR` | `%USERPROFILE%\.claude`, or `%CLAUDE_CONFIG_DIR%` |
| Codex home | `~/.codex`, or `$CODEX_HOME` | `%USERPROFILE%\.codex`, or `%CODEX_HOME%` |
| shared skills | `~/.agents/skills` | `%USERPROFILE%\.agents\skills` |
| agent-mirror config | `${XDG_CONFIG_HOME:-~/.config}/agent-mirror/config.json` | `%APPDATA%\agent-mirror\config.json` |
| hook log | Linux `${XDG_CACHE_HOME:-~/.cache}/agent-mirror/sync.log`, macOS `~/Library/Caches/agent-mirror/sync.log` | `%LOCALAPPDATA%\agent-mirror\sync.log` |
| backups | `~/.agent-mirror-backups/<timestamp>/` | `%USERPROFILE%\.agent-mirror-backups\<timestamp>\` |

UNVERIFIED (macOS): Claude Code and Codex use `~/.claude` and `~/.codex` there too.
UNVERIFIED (Windows): Codex uses `%USERPROFILE%\.codex` and reads `AGENTS.md` and `~/.agents/skills`
from the same places as on Linux. (Claude's `%USERPROFILE%\.claude` is verified, see above.)

## Links

`install` always tries a symlink first.

- Linux and macOS: symlinks always work for a normal user.
- Windows: a symlink needs Developer Mode or admin rights. When the symlink fails, agent-mirror uses:
  - a directory junction (`cmd /c mklink /J`) for folders (skills, the memory folder). Junctions need
    no special rights, but only work within local NTFS drives.
  - a managed copy for single files (`CLAUDE.md`, `AGENTS.md`). The copy is recorded in the backup
    manifest. `doctor` shows it as `COPY (copy, re-run install after editing)`. Edit the store copy,
    then run `install` again: it refreshes the copy and keeps the old one in the backup folder.
    `uninstall` removes a managed copy only when it still matches the store copy.
- `install` prints which method it used for each link, and the manifest records it.

UNVERIFIED (Windows): junction creation through `cmd /c mklink /J`, junction detection
(`os.path.isjunction` on Python 3.12+, reparse tags before that), removal of junctions with `os.rmdir`,
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
  (`az` is `az.cmd`), using `shutil.which`.
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
