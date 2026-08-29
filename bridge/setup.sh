#!/usr/bin/env bash
# One-time (and re-runnable) setup for the browser bridge.
#
#   - creates the state directory outside the plugin, so nothing here is lost
#     when the plugin updates
#   - generates the pairing token shared by the server and every extension
#   - copies the extension to a STABLE path, which matters more than it looks:
#     Chrome derives an unpacked extension's identity from its folder path, so
#     loading it straight out of the versioned plugin directory would give you a
#     brand-new, unconfigured extension after every update.
#
#   ./setup.sh                # set up, keep the existing token
#   ./setup.sh --rotate       # new token (re-paste it into every profile popup)
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOME_DIR="${BROWSER_BRIDGE_HOME:-$HOME/.claude-browser-bridge}"
TOKEN_FILE="$HOME_DIR/token"
EXT_DIR="$HOME_DIR/extension"

mkdir -p "$HOME_DIR" "$EXT_DIR"
chmod 700 "$HOME_DIR"

if [ -f "$TOKEN_FILE" ] && [ "${1:-}" != "--rotate" ]; then
  echo "token: already set ($TOKEN_FILE)"
else
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -hex 24 > "$TOKEN_FILE"
  else
    head -c 24 /dev/urandom | od -An -tx1 | tr -d ' \n' > "$TOKEN_FILE"
  fi
  chmod 600 "$TOKEN_FILE"
  echo "token: generated ($TOKEN_FILE)"
fi

# Mirror rather than symlink: Chrome refuses to load some symlinked extension
# trees, and a copy also survives the plugin being uninstalled mid-session.
rm -rf "$EXT_DIR"
mkdir -p "$EXT_DIR"
cp "$HERE/extension/"* "$EXT_DIR/"
echo "extension: synced to $EXT_DIR"

if command -v clip.exe >/dev/null 2>&1; then
  tr -d '\n' < "$TOKEN_FILE" | clip.exe && echo "token: copied to clipboard"
elif command -v pbcopy >/dev/null 2>&1; then
  tr -d '\n' < "$TOKEN_FILE" | pbcopy && echo "token: copied to clipboard"
elif command -v wl-copy >/dev/null 2>&1; then
  tr -d '\n' < "$TOKEN_FILE" | wl-copy && echo "token: copied to clipboard"
elif command -v xclip >/dev/null 2>&1; then
  tr -d '\n' < "$TOKEN_FILE" | xclip -selection clipboard && echo "token: copied to clipboard"
fi

cat <<TXT

Next, in the Chrome profile you want Claude to drive:
  1. open  chrome://extensions  and turn on Developer mode
  2. Load unpacked -> $EXT_DIR
  3. click the Claude Bridge toolbar icon, paste the token, give this profile a
     label (e.g. "private"), Save

Repeat 2-3 for every Chrome profile you want to expose. Same token everywhere,
one distinct label each.
TXT
