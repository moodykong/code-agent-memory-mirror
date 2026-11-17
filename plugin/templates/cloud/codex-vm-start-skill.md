---
name: agent-mirror-start
description: Run first in every Codex cloud session to load the user's context and set up the project.
---
1. From the project folder, run `agent-mirror session-start`
   (or `~/code-agent-memory-mirror/plugin/bin/agent-mirror session-start` if it is not on PATH).
2. Read the memory index file whose path it prints, before any other work.
3. Follow `~/.codex/AGENTS.md`, including the shared memory and cloud session rules.
4. Before you finish, run `agent-mirror sync` so new memory reaches the store.
