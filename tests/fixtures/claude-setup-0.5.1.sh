#!/bin/bash
# agent-mirror setup script for Claude Code on the web.
# Project: demo (Example/demo)
# Rendered by `agent-mirror cloud render` from projects/demo/env.json; re-render instead of editing.
# Paste the whole file into the environment's "Setup script" box, then replace
# <STORE_REPO_TOKEN> with a fine-grained token that can READ ONLY the store repo (Contents: read-only).
# It is used once, for the clone below. In a session, sync pushes memory to an inbox branch through the
# vendor's GitHub access for the repos selected in that session, so select the store repo too.
# Every step is allowed to fail; the script always exits 0.
# Setup runs before the vendor sets its own session markers, so tell agent-mirror where it is:
# a cloud machine never adopts local content and never publishes a machine profile.
export AGENT_RUNTIME=claude-cloud
STORE="$HOME/agent-mirror-store"
TOOL="$HOME/code-agent-memory-mirror"
AM="$TOOL/plugin/bin/agent-mirror"
NOTE="$HOME/agent_setup_probe.txt"
START=$(date +%s)
note() { echo "$* at $(( $(date +%s) - START )) s" >> "$NOTE" 2>/dev/null || true; }
{
  echo "setup ran: $(date -u +%FT%TZ) as $(whoami), HOME=$HOME, pwd=$(pwd)"
  echo "git: $(command -v git || echo missing)   uv: $(command -v uv || echo missing)"
} > "$NOTE" 2>&1 || true

# the store (private): clone it with the token
if [ -d "$STORE/.git" ]; then
  timeout 60 git -C "$STORE" pull --ff-only || true
else
  timeout 120 git clone "https://x-access-token:<STORE_REPO_TOKEN>@github.com/example-org/my-agent-store.git" "$STORE" || true
fi
note "store cloned: $( [ -d "$STORE/.git" ] && echo yes || echo NO )"

# agent-mirror (public repo, no token): the ref pinned in the store's store.json
mkdir -p "$TOOL" || true
git -C "$TOOL" init -q 2>/dev/null || true
git -C "$TOOL" remote add origin "https://github.com/example-org/agent-mirror-tool" 2>/dev/null || git -C "$TOOL" remote set-url origin "https://github.com/example-org/agent-mirror-tool" 2>/dev/null || true
timeout 120 git -C "$TOOL" fetch -q --depth 1 origin "main" && git -C "$TOOL" checkout -q --force FETCH_HEAD || true
chmod +x "$AM" 2>/dev/null || true
ln -sf "$AM" /usr/local/bin/agent-mirror 2>/dev/null || { mkdir -p "$HOME/.local/bin" && ln -sf "$AM" "$HOME/.local/bin/agent-mirror"; } || true
note "agent-mirror at ref main ($(git -C "$TOOL" rev-parse --short HEAD 2>/dev/null || echo missing)), on PATH: $(command -v agent-mirror || echo no)"

# no companion_repos in projects/demo/env.json

# no python version in the project config

# the owner's setup_commands from projects/demo/env.json
( echo "setup ran" >> "$HOME/setup_marker.txt" ) || true
note "setup command 1 of 1 done"

# links into the store, and the session hooks in ~/.claude/settings.json
"$AM" init-store "$STORE" --adopt || true
"$AM" --store "$STORE" install --agents claude || true
"$AM" --store "$STORE" hooks install --agent claude || true
note "install and hooks done"
note "setup finished (keep it under about 300 s so the environment can be cached; UNVERIFIED limit)"
exit 0
