#!/usr/bin/env bash
# Install the local-memory kit into a target repository.
#
#   ./install.sh                 # install into the current directory
#   ./install.sh /path/to/repo   # install into a given repo
#   ./install.sh --force         # overwrite existing files
#
# Copies (never moves) the kit into the target repo:
#   mcp/memory_server.py
#   .github/copilot-instructions.md
#   .github/instructions/memory.instructions.md
#   AGENTS.md
#   .vscode/mcp.json
#   .copilot/memory/MEMORY.md
#
# Existing files are left untouched unless --force is given. Nothing is
# deleted by this script.

set -euo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)"
TARGET="$(pwd)"
FORCE="no"

for arg in "$@"; do
  case "$arg" in
    --force) FORCE="yes" ;;
    -h|--help)
      # Print the leading comment block; no hard-coded line range to drift.
      awk 'NR>1 && /^#/ { sub(/^# ?/, ""); print; next } NR>1 { exit }' "$0"
      exit 0
      ;;
    -*)
      # A typo like --forc must not be swallowed as a target directory.
      echo "unknown option: $arg (try --help)" >&2
      exit 2
      ;;
    *) TARGET="$arg" ;;
  esac
done

if [ ! -d "$TARGET" ]; then
  echo "target does not exist: $TARGET" >&2
  exit 1
fi

# Resolve the interpreter to embed in .vscode/mcp.json. Prefer a stable
# absolute path over bare "python3", because VS Code launches MCP servers
# with a minimal environment where PATH may not include the right python.
PY=""
for cand in \
  /opt/homebrew/bin/python3 \
  /usr/local/bin/python3 \
  "$(command -v python3 2>/dev/null || true)" \
  /usr/bin/python3
do
  if [ -n "$cand" ] && [ -x "$cand" ]; then
    if "$cand" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' 2>/dev/null; then
      PY="$cand"
      break
    fi
  fi
done

if [ -z "$PY" ]; then
  echo "no python3 >= 3.8 found; edit .vscode/mcp.json by hand afterwards" >&2
  PY="python3"
fi

install_file() {
  local rel="$1"
  local src="$SRC/$rel"
  local dst="$TARGET/$rel"

  if [ ! -f "$src" ]; then
    echo "  skip   $rel (not in kit)"
    return 0
  fi

  mkdir -p "$(dirname "$dst")"

  if [ -e "$dst" ] && [ "$FORCE" != "yes" ]; then
    echo "  keep   $rel (exists; use --force to overwrite)"
    return 0
  fi

  cp "$src" "$dst"
  echo "  write  $rel"
}

# Same as install_file but with an explicit source path.
install_from() {
  local src="$1"
  local rel="$2"
  local dst="$TARGET/$rel"

  if [ ! -f "$src" ]; then
    echo "  skip   $rel (not in kit)"
    return 0
  fi

  mkdir -p "$(dirname "$dst")"

  if [ -e "$dst" ] && [ "$FORCE" != "yes" ]; then
    echo "  keep   $rel (exists; use --force to overwrite)"
    return 0
  fi

  cp "$src" "$dst"
  echo "  write  $rel"
}

echo "installing memory kit"
echo "  from:  $SRC"
echo "  into:  $TARGET"
echo "  python: $PY"
echo

install_file "mcp/memory_server.py"
install_file ".github/copilot-instructions.md"
install_file ".github/instructions/memory.instructions.md"
install_file "AGENTS.md"
install_from "$SRC/memory/MEMORY.md" ".copilot/memory/MEMORY.md"

# .vscode/mcp.json is generated, not copied: the interpreter path is
# machine-specific.
mkdir -p "$TARGET/.vscode"
if [ -f "$TARGET/.vscode/mcp.json" ] && [ "$FORCE" != "yes" ]; then
  echo "  keep   .vscode/mcp.json (exists; use --force to overwrite)"
else
  cat > "$TARGET/.vscode/mcp.json" <<JSON
{
  "servers": {
    "memory": {
      "type": "stdio",
      "command": "$PY",
      "args": [
        "\${workspaceFolder}/mcp/memory_server.py"
      ],
      "env": {
        "MEMORY_ROOT": "\${workspaceFolder}/.copilot/memory",
        "MEMORY_LAYOUT": "flat",
        "MEMORY_ORGANIZE": "dated"
      }
    }
  }
}
JSON
  echo "  write  .vscode/mcp.json"
fi

chmod +x "$TARGET/mcp/memory_server.py" 2>/dev/null || true

echo
echo "done. next:"
echo "  1. open the repo in VS Code"
echo "  2. run 'MCP: List Servers' from the command palette and start 'memory'"
echo "  3. in Copilot Chat, ask: 'what do you remember about this project?'"
