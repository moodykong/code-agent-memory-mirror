TOOL="$HOME/code-agent-memory-mirror"
AM="$TOOL/plugin/bin/agent-mirror"
NOTE="$HOME/agent_setup_probe.txt"
START=$(date +%s)
note() { echo "$* at $(( $(date +%s) - START )) s" >> "$NOTE" 2>/dev/null || true; }
{
  echo "setup ran: $(date -u +%FT%TZ) as $(whoami), HOME=$HOME, pwd=$(pwd)"
  echo "git: $(command -v git || echo missing)   uv: $(command -v uv || echo missing)"
} > "$NOTE" 2>&1 || true
