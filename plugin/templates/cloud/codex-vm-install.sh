#!/bin/bash
# agent-mirror install script (a fixed bootstrap) for Codex cloud (the newer VM product).
# revision: 1
# Projects: @@PROJECTS@@
# Select BOTH repos in the environment: the project and the store (@@STORE_REPO_PATH@@).
# Codex checks the store out with its own access, so this script needs no token.
# If the search below does not find the store checkout, set AGENT_MIRROR_STORE on the
# settings page to its folder.
@@FIXED_NOTE@@
# Every step is allowed to fail; the script always exits 0.
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

# the rest comes from the store: companion repos, python, setup_commands and links
# (Codex has no hooks; the start step runs session-start)
if [ -n "$STORE" ] && "$AM" cloud bootstrap --help >/dev/null 2>&1; then
  "$AM" --store "$STORE" cloud bootstrap --vendor codex @@PROJECT_FLAGS@@ --note "$NOTE" || true
elif [ -n "$STORE" ]; then
  # the store pins an agent-mirror older than 0.5.2 (no cloud bootstrap): links only
  "$AM" init-store "$STORE" --adopt || true
  "$AM" --store "$STORE" install --agents codex || true
  note "no cloud bootstrap in this agent-mirror: links only"
fi
note "setup finished"
exit 0
