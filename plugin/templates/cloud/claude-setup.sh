#!/bin/bash
# agent-mirror setup script for Claude Code on the web.
# Project: @@PROJECT@@ (@@PROJECT_REPO@@)
# Rendered by `agent-mirror cloud render` from projects/@@PROJECT@@/env.json; re-render instead of editing.
# Paste the whole file into the environment's "Setup script" box, then replace
# <STORE_REPO_TOKEN> with a fine-grained token that can read and write ONLY the store repo
# (write access lets `agent-mirror sync` push memory to an inbox branch; it never pushes to main).
# Every step is allowed to fail; the script always exits 0.
# Setup runs before the vendor sets its own session markers, so tell agent-mirror where it is:
# a cloud machine never adopts local content and never publishes a machine profile.
export AGENT_RUNTIME=claude-cloud
STORE="$HOME/agent-mirror-store"
@@PRELUDE@@

# the store (private): clone it with the token
if [ -d "$STORE/.git" ]; then
  timeout 60 git -C "$STORE" pull --ff-only || true
else
  timeout 120 git clone "@@STORE_TOKEN_URL@@" "$STORE" || true
fi
note "store cloned: $( [ -d "$STORE/.git" ] && echo yes || echo NO )"

@@TOOL_STEP@@

@@COMPANIONS_STEP@@

@@PYTHON_STEP@@

@@SETUP_COMMANDS_STEP@@

# links into the store, and the session hooks in ~/.claude/settings.json
"$AM" init-store "$STORE" --adopt || true
"$AM" --store "$STORE" install --agents claude || true
"$AM" --store "$STORE" hooks install --agent claude || true
note "install and hooks done"
note "setup finished (keep it under about 300 s so the environment can be cached; UNVERIFIED limit)"
exit 0
