#!/usr/bin/env bash
# Install or upgrade the AI Usage plasmoid for the current user.
set -euo pipefail
cd "$(dirname "$0")"
ID=org.kde.plasma.aiusage
chmod +x package/contents/tools/aiusage.py
if kpackagetool6 -t Plasma/Applet -l 2>/dev/null | grep -qx "$ID"; then
    kpackagetool6 -t Plasma/Applet --upgrade package
else
    kpackagetool6 -t Plasma/Applet --install package
fi
echo "Installed $ID. Add it via right-click panel -> Add Widgets -> 'AI Usage'."
