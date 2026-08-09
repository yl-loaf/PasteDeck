#!/bin/bash
# Double-click this file on macOS to install PasteDeck dependencies.
# It will open a Terminal window and run the installer.

cd "$(dirname "$0")"
chmod +x ./install_pastedeck.sh 2>/dev/null || true
./install_pastedeck.sh
echo ""
echo "Press any key to close this window..."
read -n 1 -s