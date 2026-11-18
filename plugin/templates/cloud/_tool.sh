# agent-mirror (public repo, no token) at the ref that store.json on the store's default branch pins
REF=$( { git -C "$STORE" show origin/HEAD:store.json 2>/dev/null || git -C "$STORE" show HEAD:store.json 2>/dev/null; } | python3 -c '
import json, re, sys
try:
    ref = str((json.load(sys.stdin).get("tool") or {}).get("ref") or "main")
except Exception:
    ref = "main"
print(ref if re.match(r"^[A-Za-z0-9._][A-Za-z0-9._/-]*$", ref) and ".." not in ref else "main")
' 2>/dev/null )
[ -n "$REF" ] || REF=main
mkdir -p "$TOOL" || true
git -C "$TOOL" init -q 2>/dev/null || true
git -C "$TOOL" remote add origin "@@TOOL_REPO@@" 2>/dev/null || git -C "$TOOL" remote set-url origin "@@TOOL_REPO@@" 2>/dev/null || true
timeout 120 git -C "$TOOL" fetch -q --depth 1 origin "$REF" && git -C "$TOOL" checkout -q --force FETCH_HEAD || true
chmod +x "$AM" 2>/dev/null || true
ln -sf "$AM" /usr/local/bin/agent-mirror 2>/dev/null || { mkdir -p "$HOME/.local/bin" && ln -sf "$AM" "$HOME/.local/bin/agent-mirror"; } || true
note "agent-mirror at ref $REF ($(git -C "$TOOL" rev-parse --short HEAD 2>/dev/null || echo missing)), on PATH: $(command -v agent-mirror || echo no)"
