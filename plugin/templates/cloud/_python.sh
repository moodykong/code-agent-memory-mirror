# Python @@PYTHON@@ through uv (the project config sets "python")
command -v uv >/dev/null 2>&1 || (timeout 120 curl -LsSf https://astral.sh/uv/install.sh | sh) || true
export PATH="$HOME/.local/bin:$PATH"
timeout 600 uv python install "@@PYTHON@@" || true
note "python @@PYTHON@@ step done"
