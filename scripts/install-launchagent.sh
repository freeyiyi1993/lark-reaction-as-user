#!/usr/bin/env bash
# Install lark-reaction-as-user as a macOS LaunchAgent.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LABEL="${LARK_REACTION_LABEL:-com.local.lark-reaction-as-user}"
PLIST="$HOME/Library/LaunchAgents/${LABEL}.plist"
TEMPLATE="$ROOT/launchagent/com.local.lark-reaction-as-user.plist.template"

usage() {
  cat <<'USAGE'
Usage:
  scripts/install-launchagent.sh

Environment:
  LARK_REACTION_LABEL    LaunchAgent label (default: com.local.lark-reaction-as-user)
USAGE
}

case "${1:-}" in
  -h|--help)
    usage
    exit 0
    ;;
  "")
    ;;
  *)
    echo "unknown arg: $1" >&2
    usage >&2
    exit 2
    ;;
esac

mkdir -p "$HOME/Library/LaunchAgents" "$HOME/.local/state/lark-reaction-as-user/logs"

sed \
  -e "s|REPLACE_LABEL|$LABEL|g" \
  -e "s|REPLACE_HOME|$HOME|g" \
  -e "s|REPLACE_REPO|$ROOT|g" \
  "$TEMPLATE" > "$PLIST"

chmod 600 "$PLIST"
launchctl bootout "gui/$(id -u)" "$PLIST" >/dev/null 2>&1 || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
launchctl kickstart -k "gui/$(id -u)/${LABEL}"

echo "installed $LABEL"
echo "plist: $PLIST"
echo "health: $ROOT/scripts/health.sh"
