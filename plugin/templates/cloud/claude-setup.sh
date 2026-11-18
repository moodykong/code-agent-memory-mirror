#!/bin/bash
# agent-mirror setup script (a fixed bootstrap) for Claude Code on the web.
# revision: 1
# Projects: @@PROJECTS@@
# Paste it once per environment into the "Setup script" box, then replace <STORE_REPO_TOKEN> with a
# fine-grained token that can READ ONLY the store repo (Contents: read-only). It is used once, for the
# clone below. In a session, pulls and the push of memory to an inbox branch go through the vendor's
# GitHub access for the repos selected in that session, so select the store repo in every session.
@@FIXED_NOTE@@
# Every step is allowed to fail; the script always exits 0.
# Setup runs before the vendor sets its own session markers, so tell agent-mirror where it is:
# a cloud machine never adopts local content and never publishes a machine profile.
export AGENT_RUNTIME=claude-cloud
STORE="$HOME/agent-mirror-store"
@@PRELUDE@@

# the store (private): clone it with the read-only token
if [ -d "$STORE/.git" ]; then
  timeout 60 git -C "$STORE" pull --ff-only || true
else
  timeout 120 git clone "@@STORE_TOKEN_URL@@" "$STORE" || true
fi
note "store cloned: $( [ -d "$STORE/.git" ] && echo yes || echo NO )"

@@TOOL_STEP@@

# the rest comes from the store: companion repos, python, setup_commands, links and the session hooks
if "$AM" cloud bootstrap --help >/dev/null 2>&1; then
  "$AM" --store "$STORE" cloud bootstrap --vendor claude @@PROJECT_FLAGS@@ --note "$NOTE" || true
else
  # the store pins an agent-mirror older than 0.5.2 (no cloud bootstrap): links and hooks only
  "$AM" init-store "$STORE" --adopt || true
  "$AM" --store "$STORE" install --agents claude || true
  "$AM" --store "$STORE" hooks install --agent claude || true
  note "no cloud bootstrap in this agent-mirror: links and hooks only"
fi
note "setup finished (keep it under about 300 s so the environment can be cached; UNVERIFIED limit)"
exit 0
