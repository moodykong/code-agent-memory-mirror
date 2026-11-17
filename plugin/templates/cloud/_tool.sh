# agent-mirror (public repo, no token): the ref pinned in the store's store.json
mkdir -p "$TOOL" || true
git -C "$TOOL" init -q 2>/dev/null || true
git -C "$TOOL" remote add origin "@@TOOL_REPO@@" 2>/dev/null || git -C "$TOOL" remote set-url origin "@@TOOL_REPO@@" 2>/dev/null || true
timeout 120 git -C "$TOOL" fetch -q --depth 1 origin "@@TOOL_REF@@" && git -C "$TOOL" checkout -q --force FETCH_HEAD || true
chmod +x "$AM" 2>/dev/null || true
ln -sf "$AM" /usr/local/bin/agent-mirror 2>/dev/null || { mkdir -p "$HOME/.local/bin" && ln -sf "$AM" "$HOME/.local/bin/agent-mirror"; } || true
note "agent-mirror at ref @@TOOL_REF@@ ($(git -C "$TOOL" rev-parse --short HEAD 2>/dev/null || echo missing)), on PATH: $(command -v agent-mirror || echo no)"
