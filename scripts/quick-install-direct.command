#!/bin/bash
# ===========================================================================
#  Codex Overload Fix - one-click install, DIRECT mode (your own account).
#  For non-technical users: double-click this file in Finder. No key needed -
#  Codex keeps using the ChatGPT login you already have.
#
#  If macOS refuses to open it (downloaded from the internet): right-click
#  the file -> Open -> Open, or run
#      xattr -d com.apple.quarantine quick-install-direct.command
# ===========================================================================
cd "$(dirname "$0")" || exit 1

bash ./install-mac.sh --write-config

echo
echo "============================================================"
echo " If you see 'All done' above: fully quit Codex from the"
echo " tray icon and start it again. That is the last step."
echo "============================================================"
read -n 1 -s -r -p "Press any key to close."
