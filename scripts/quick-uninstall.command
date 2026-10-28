#!/bin/bash
# ===========================================================================
#  Codex Overload Fix - one-click full rollback, for non-technical users.
#  Double-click this file to undo everything the installer did:
#    1. stops the relay and removes its autostart entry
#    2. restores ~/.codex/config.toml from the pre-install backup
#  Afterwards you can simply delete this folder.
#
#  If macOS refuses to open it (downloaded from the internet): right-click
#  the file -> Open -> Open, or run
#      xattr -d com.apple.quarantine quick-uninstall.command
# ===========================================================================
cd "$(dirname "$0")/.." || exit 1

echo "== 1/2 stopping the relay and removing autostart =="
bash scripts/uninstall-mac.sh --purge

echo
echo "== 2/2 restoring ~/.codex/config.toml from the pre-install backup =="
PY="$(command -v python3 || true)"
if [ -n "$PY" ]; then
    if "$PY" tools/configure_codex.py --restore; then
        :
    else
        echo "(no backup found - config.toml was never modified, nothing to do)"
    fi
else
    echo "python3 not found; to restore the config later run:"
    echo "    python3 tools/configure_codex.py --restore"
fi

echo
echo "============================================================"
echo " Rollback complete. You can now delete this folder"
echo " (drag it to the Trash) - nothing else is left behind."
echo "============================================================"
read -n 1 -s -r -p "Press any key to close."
