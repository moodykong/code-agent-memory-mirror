"""Which agent runtime is this: claude-local, claude-cloud, codex-local, codex-cloud or unknown."""

import os

from agent_mirror.util import warn

RUNTIMES = ("claude-local", "claude-cloud", "codex-local", "codex-cloud", "unknown")
CLOUD_RUNTIMES = ("claude-cloud", "codex-cloud")
AGENT_NAMES = ("claude", "codex")


def detect_runtime(env=None):
    env = os.environ if env is None else env
    forced = env.get("AGENT_RUNTIME", "").strip()
    if forced:
        if forced in RUNTIMES:
            return forced
        warn("AGENT_RUNTIME=%r is not one of %s; ignoring it" % (forced, ", ".join(RUNTIMES)))
    if env.get("CLAUDE_CODE_REMOTE", "").strip().lower() == "true":
        return "claude-cloud"
    if "CLAUDECODE" in env:
        return "claude-local"
    if any(k in env for k in ("CODEX_THREAD_ID", "CODEX_SESSION_ID", "CODEX_CI")):
        return "codex-local"
    return "unknown"


def agent_for_runtime(runtime):
    if runtime.startswith("claude"):
        return "claude"
    if runtime.startswith("codex"):
        return "codex"
    return None


def parse_agents(text):
    agents = [a.strip() for a in (text or "").split(",") if a.strip()]
    bad = [a for a in agents if a not in AGENT_NAMES]
    if bad or not agents:
        raise SystemExit("error: --agents takes a comma list of %s (got %r)" % ("/".join(AGENT_NAMES), text))
    return agents
