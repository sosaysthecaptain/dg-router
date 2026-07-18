#!/usr/bin/env bash
# Install (symlink) the dg-router plugin into KiCad's scripting/plugins dir.
# Re-run after adding files; changes are picked up via KiCad's "Refresh Plugins".
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PKG="$REPO/dg_router_plugin"

# Pick the newest KiCad scripting/plugins dir under ~/Documents/KiCad.
PLUGINS_DIR="$(ls -d "$HOME"/Documents/KiCad/*/scripting/plugins 2>/dev/null | sort -V | tail -1)"
if [ -z "${PLUGINS_DIR:-}" ]; then
  echo "No KiCad scripting/plugins dir found under ~/Documents/KiCad/*/" >&2
  exit 1
fi

LINK="$PLUGINS_DIR/dg_router_plugin"
rm -rf "$LINK"
ln -s "$PKG" "$LINK"
echo "Linked: $LINK -> $PKG"
echo
echo "Next: in KiCad's PCB Editor, Tools > External Plugins > Refresh Plugins"
echo "Then look for the 'dg-router' button on the top toolbar."
