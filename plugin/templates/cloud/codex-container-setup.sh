#!/bin/bash
# agent-mirror setup script (a fixed bootstrap) for Codex cloud (the legacy container product).
# revision: 1
# Projects: @@PROJECTS@@
# Store a fine-grained token (read-only, the store repo only) as a setup-only secret
# named STORE_REPO_TOKEN. The script removes the token from the clone's remote at the end,
# so the agent never sees it; as a result `agent-mirror sync` cannot push from the agent phase.
@@FIXED_NOTE@@
# Every step is allowed to fail; the script always exits 0.
# Setup runs before the vendor sets its own session markers, so tell agent-mirror where it is:
# a cloud machine never adopts local content and never publishes a machine profile.
export AGENT_RUNTIME=codex-cloud
STORE="$HOME/agent-mirror-store"
@@PRELUDE@@

# the store (private): clone it with the setup-only secret, then drop the token
if [ ! -d "$STORE/.git" ]; then
  timeout 120 git clone "@@STORE_ENV_TOKEN_URL@@" "$STORE" || true
  git -C "$STORE" remote set-url origin "@@STORE_PLAIN_URL@@" || true
fi
note "store cloned: $( [ -d "$STORE/.git" ] && echo yes || echo NO )"

@@TOOL_STEP@@

# the rest comes from the store: companion repos, python, setup_commands and links,
# then the session start (this product has no start hook)
if "$AM" cloud bootstrap --help >/dev/null 2>&1; then
  "$AM" --store "$STORE" cloud bootstrap --vendor codex @@PROJECT_FLAGS@@ --note "$NOTE" || true
else
  # the store pins an agent-mirror older than 0.5.2 (no cloud bootstrap): links only
  "$AM" init-store "$STORE" --adopt || true
  "$AM" --store "$STORE" install --agents codex || true
  note "no cloud bootstrap in this agent-mirror: links only"
fi
"$AM" --store "$STORE" session-start || true
note "setup finished"
exit 0
