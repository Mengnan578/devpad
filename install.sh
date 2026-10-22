#!/bin/sh
# Install devpad's skill for agent hosts that read a local skills directory.
#
#   ./install.sh              # link the skill into every known agent root
#   ./install.sh --cli        # also add a `devpad` command on PATH
#   ./install.sh --dry-run    # show what would happen
#
# Claude Code users have a simpler path: /plugin marketplace add Mengnan578/devpad
# This script exists for everything else (DeepSeek Harness, Codex, plain skills
# directories), where a symlink is the whole installation.
set -eu

ROOT=$(cd "$(dirname "$0")" && pwd)
SKILL="$ROOT/skills/devpad"

DRY=0
WITH_CLI=0
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY=1 ;;
    --cli) WITH_CLI=1 ;;
    -h|--help) sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown argument: $arg" >&2; exit 2 ;;
  esac
done

say() { printf '\033[1m%s\033[0m\n' "$*"; }
warn() { printf '\033[33m%s\033[0m\n' "$*"; }

[ -f "$SKILL/SKILL.md" ] || { echo "missing $SKILL/SKILL.md — is this the devpad package root?" >&2; exit 1; }

say "skill source: $SKILL"
say "agent skill roots:"

# Only touch roots that already exist, or that the user clearly has: creating a
# directory for an agent someone does not use is noise, not help.
for base in "$HOME/.dsh/skills" "$HOME/.claude/skills" "$HOME/.agents/skills" "$HOME/.config/agents/skills"; do
  parent=$(dirname "$base")
  if [ ! -d "$parent" ]; then
    echo "  skip  $base  (parent $parent does not exist)"
    continue
  fi
  target="$base/devpad"
  if [ -e "$target" ] && [ ! -L "$target" ]; then
    warn "  keep  $target  (a real directory is already there; move it aside to let install.sh manage it)"
    continue
  fi
  if [ "$DRY" -eq 1 ]; then
    echo "  plan  $target -> $SKILL"
  else
    mkdir -p "$base"
    ln -sfn "$SKILL" "$target"
    echo "  link  $target -> $SKILL"
  fi
done

if [ "$WITH_CLI" -eq 1 ]; then
  say "command line:"
  BIN="$HOME/.local/bin"
  if [ "$DRY" -eq 1 ]; then
    echo "  plan  $BIN/devpad"
  else
    mkdir -p "$BIN"
    cat > "$BIN/devpad" <<EOF
#!/bin/sh
# devpad — installed by install.sh; points at $ROOT
exec python3 "$ROOT/scripts/devpad.py" "\$@"
EOF
    chmod +x "$BIN/devpad"
    echo "  write $BIN/devpad"
    case ":$PATH:" in
      *":$BIN:"*) ;;
      *) warn "  note  $BIN is not on PATH; add it or use the full path" ;;
    esac
  fi
fi

echo
say "next"
echo "  cd <your project>"
if [ "$WITH_CLI" -eq 1 ]; then
  echo "  devpad doctor"
  echo "  devpad up --terminal"
else
  echo "  python3 $ROOT/scripts/devpad.py doctor"
  echo "  python3 $ROOT/scripts/devpad.py up --terminal"
fi
echo
echo "  restart the agent, then ask it to 'restart the backend'."
