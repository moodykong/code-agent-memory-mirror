#!/bin/bash
# agent-mirror setup script for Codex cloud (the legacy container product).
# Project: @@PROJECT@@ (@@PROJECT_REPO@@)
# Rendered by `agent-mirror cloud render` from projects/@@PROJECT@@/env.json; re-render instead of editing.
# Store a fine-grained token (read and write, the store repo only) as a setup-only secret
# named STORE_REPO_TOKEN. The script removes the token from the clone's remote at the end,
# so the agent never sees it; as a result `agent-mirror sync` cannot push from the agent phase.
# Every step is allowed to fail; the script always exits 0.
STORE="$HOME/agent-mirror-store"
@@PRELUDE@@

# the store (private): clone it with the setup-only secret, then drop the token
if [ ! -d "$STORE/.git" ]; then
  timeout 120 git clone "@@STORE_ENV_TOKEN_URL@@" "$STORE" || true
  git -C "$STORE" remote set-url origin "@@STORE_PLAIN_URL@@" || true
fi
note "store cloned: $( [ -d "$STORE/.git" ] && echo yes || echo NO )"

@@TOOL_STEP@@

@@PYTHON_STEP@@

@@SETUP_COMMANDS_STEP@@

# links into the store, then the session start (this product has no start hook)
"$AM" init-store "$STORE" --adopt || true
"$AM" --store "$STORE" install --agents codex || true
"$AM" --store "$STORE" session-start || true
note "setup finished"
exit 0
