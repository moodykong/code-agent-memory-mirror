#!/bin/bash
# agent-mirror maintenance script for Codex cloud (the legacy container product).
# Runs when a cached container is resumed. The token is not available here, so the
# pull of the private store may fail; that only warns. Always exits 0.
AM="$HOME/code-agent-memory-mirror/plugin/bin/agent-mirror"
"$AM" install --agents codex || true
"$AM" session-start || true
exit 0
