#!/bin/bash
# ===========================================================================
#  Codex Overload Fix - one-click install, GATEWAY mode (sub2api etc.)
#  For non-technical users: double-click this file in Finder. You will only
#  be asked for the gateway API key.
#
#  IT / the person distributing this zip: set GATEWAY_URL below before
#  sending it out. Leave the placeholder to have the user paste it instead.
#
#  If macOS refuses to open it (downloaded from the internet): right-click
#  the file -> Open -> Open, or run
#      xattr -d com.apple.quarantine quick-install-gateway.command
# ===========================================================================
GATEWAY_URL="EDIT-THIS"

cd "$(dirname "$0")" || exit 1

if [ "$GATEWAY_URL" = "EDIT-THIS" ]; then
    echo "Where is the gateway? Paste the address IT gave you"
    echo "(for example http://127.0.0.1:8888) and press Enter:"
    printf '> '
    read -r GATEWAY_URL
    if [ -z "$GATEWAY_URL" ]; then
        echo "No gateway address given - exiting without changing anything."
        read -n 1 -s -r -p "Press any key to close."
        exit 1
    fi
fi

bash ./install-mac.sh --mode gateway --upstream "$GATEWAY_URL" --write-config

echo
echo "============================================================"
echo " If you see 'All done' above: fully quit Codex from the"
echo " tray icon and start it again. That is the last step."
echo "============================================================"
read -n 1 -s -r -p "Press any key to close."
