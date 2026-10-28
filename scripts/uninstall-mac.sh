#!/bin/bash
# Codex Overload Fix - macOS uninstaller.
#
# Unloads the LaunchAgent and removes the plist. The relay itself is a plain
# file in the repo; nothing else is installed, so there is no package to remove.
set -euo pipefail

LABEL="com.codex-overload-fix.relay"
PLIST_PATH="$HOME/Library/LaunchAgents/$LABEL.plist"
STATE_DIR="$HOME/Library/Application Support/codex-overload-fix"
PURGE=0

say()  { printf '  %s\n' "$*"; }
ok()   { printf '  [ok] %s\n' "$*"; }
die()  { printf 'error: %s\n' "$*" >&2; exit 1; }

while [ $# -gt 0 ]; do
    case "$1" in
        --purge)  PURGE=1; shift ;;
        -h|--help)
            cat <<'EOF'
usage: uninstall-mac.sh [--purge]

  --purge   also delete the log and the state directory
EOF
            exit 0 ;;
        *) die "unknown option: $1 (try --help)" ;;
    esac
done

if launchctl bootout "gui/$UID/$LABEL" >/dev/null 2>&1; then
    ok "launchctl bootout gui/$UID/$LABEL"
elif launchctl unload -w "$PLIST_PATH" >/dev/null 2>&1; then
    ok "launchctl unload -w"
else
    say "agent was not loaded"
fi

if [ -f "$PLIST_PATH" ]; then
    rm -f "$PLIST_PATH"
    ok "removed $PLIST_PATH"
else
    say "no plist at $PLIST_PATH"
fi

if [ "$PURGE" -eq 1 ] && [ -d "$STATE_DIR" ]; then
    rm -rf "$STATE_DIR"
    ok "removed $STATE_DIR"
fi

cat <<'EOF'

The relay is stopped and will not start at login again.
If the installer was run with --write-config, also restore the config:

    python3 tools/configure_codex.py --restore

(or delete the model_provider line and the [model_providers.*retry*] block
from ~/.codex/config.toml yourself). Direct mode then goes straight to
chatgpt.com; gateway mode should point its base_url back at the gateway.
EOF