#!/bin/sh
# Nova on macOS (Apple Silicon): deps, models, local LLM, Chrome bridge, doctor.
# Usage: sh scripts/setup_mac.sh
set -e
cd "$(dirname "$0")/.."

command -v brew >/dev/null || { echo "Install Homebrew first: https://brew.sh"; exit 1; }
command -v uv >/dev/null || brew install uv
command -v ollama >/dev/null || brew install ollama
brew services start ollama >/dev/null 2>&1 || true

# iCloud Drive ("Desktop & Documents") marks files inside dot-folders hidden, and Python 3.12 skips hidden .pth
# files - then `import nova` fails. Keep the venv in a *.nosync folder iCloud ignores, linked as .venv for uv.
if [ -d .venv ] && [ ! -L .venv ]; then
    rm -rf .venv.nosync
    mv .venv .venv.nosync
fi
[ -e .venv.nosync ] || mkdir .venv.nosync
[ -L .venv ] || ln -s .venv.nosync .venv
uv sync
chflags -R nohidden .venv.nosync

uv run python scripts/get_models.py

RAM_GB=$(( $(sysctl -n hw.memsize) / 1073741824 ))
ollama pull qwen3.5:2b-q4_K_M
if [ "$RAM_GB" -ge 16 ]; then
    ollama pull qwen3.5:4b
else
    echo "(${RAM_GB} GB RAM: using qwen3.5:2b on the Apple GPU; pull qwen3.5:4b on a 16 GB+ Mac for better plans)"
fi

uv run nova chrome --install
uv run nova doctor || true
cat <<'EOF'

Two one-time steps:
  1. System Settings > Privacy & Security > Accessibility: turn on the terminal app you run Nova from
     (Terminal / iTerm / VS Code). Needed for the Ctrl+Option+N chord, window moves and typing.
  2. Chrome: chrome://extensions > Developer mode > Load unpacked > this repo's extension/ folder.
Then: uv run nova   (menu-bar app; hold Ctrl+Option+N to talk, or say "Nova, ...")
EOF
