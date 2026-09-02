#!/usr/bin/env bash
# Open the user's ORDINARY Chrome — no debugging port, no automation switches,
# no throwaway profile. That is the whole point of the bridge: the browser must
# look exactly like the one the user browses with, because anti-bot checks read
# those switches. Control arrives later, through the extension.
#
#   ./open-chrome.sh                          # just open Chrome
#   ./open-chrome.sh https://www.vinted.pl    # open Chrome at a URL
#   ./open-chrome.sh --profile-dir "Profile 1" https://www.vinted.pl
#
# --profile-dir takes Chrome's own directory name (see chrome://version ->
# "Profile Path"), not the bridge label. The bridge label is set in the
# extension popup, per profile.
set -euo pipefail

APP="Google Chrome"
PROFILE_DIR=""
URL=""

while [ $# -gt 0 ]; do
  case "$1" in
    --profile-dir) PROFILE_DIR="${2:?--profile-dir needs a value}"; shift 2 ;;
    --app) APP="${2:?--app needs a value}"; shift 2 ;;
    -h|--help) sed -n '2,14p' "$0"; exit 0 ;;
    *) URL="$1"; shift ;;
  esac
done

if [ ! -d "/Applications/$APP.app" ]; then
  echo "not found: /Applications/$APP.app" >&2
  exit 1
fi

if [ -n "$PROFILE_DIR" ]; then
  # -n forces a new instance so --profile-directory is honoured even when
  # Chrome is already running.
  open -na "$APP" --args --profile-directory="$PROFILE_DIR" ${URL:+"$URL"}
else
  if [ -n "$URL" ]; then
    open -a "$APP" "$URL"
  else
    open -a "$APP"
  fi
fi

echo "opened $APP${PROFILE_DIR:+ (profile dir: $PROFILE_DIR)}${URL:+ at $URL}"
echo "next: click the Claude Bridge extension icon and set this profile's label"
