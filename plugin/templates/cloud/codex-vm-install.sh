#!/bin/bash
# agent-mirror install script for Codex cloud (the newer VM product).
# Project: @@PROJECT@@ (@@PROJECT_REPO@@)
# Rendered by `agent-mirror cloud render` from projects/@@PROJECT@@/env.json; re-render instead of editing.
# Select BOTH repos in the environment: the project and the store (@@STORE_REPO_PATH@@).
# Codex checks the store out with its own access, so this script needs no token.
# If the search below does not find the store checkout, set AGENT_MIRROR_STORE on the
# settings page to its folder. Every step is allowed to fail; the script always exits 0.
# Setup runs before the vendor sets its own session markers, so tell agent-mirror where it is:
# a cloud machine never adopts local content and never publishes a machine profile.
export AGENT_RUNTIME=codex-cloud
@@PRELUDE@@

# find the store checkout (UNVERIFIED: where Codex checks out a second repo)
STORE="${AGENT_MIRROR_STORE:-}"
if [ -z "$STORE" ]; then
  STORE=$(find / -maxdepth 4 -type d -name "@@STORE_NAME@@" -not -path '/proc/*' 2>/dev/null | head -n 1)
fi
note "store checkout: ${STORE:-NOT FOUND}"

@@TOOL_STEP@@

@@COMPANIONS_STEP@@

@@PYTHON_STEP@@

@@SETUP_COMMANDS_STEP@@

# links into the store (Codex has no hooks; the start step runs session-start)
if [ -n "$STORE" ]; then
  "$AM" init-store "$STORE" --adopt || true
  "$AM" --store "$STORE" install --agents codex || true
fi
note "setup finished"
exit 0
