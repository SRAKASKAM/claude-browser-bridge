#!/usr/bin/env bash
#
# install-extension.sh — load the bridge extension into a Chrome profile by
# driving the UI, because no API can do it.
#
#   ./install-extension.sh "BIM"            # profile's DISPLAY name, as in Chrome's Profiles menu
#   ./install-extension.sh "BIM" --reload   # just reload it (picks up manifest/icon changes)
#   ./install-extension.sh --list           # show the profile names you can pass
#
# WHY UI AUTOMATION: Chrome forbids every extension and every script from
# touching chrome:// pages — a deliberate security boundary, not bypassed here.
# This drives the same keyboard a human would, via System Events, which needs
# Accessibility granted to the controlling app.
#
# WHY THE FOCUS GUARD: synthetic keystrokes go to whatever app is frontmost. If
# the user clicks another window mid-run, the script would type into THEIR app —
# this has happened (a URL landed in a chat box). Every keystroke burst is
# therefore preceded by an assertion that Chrome is still frontmost, and the
# script aborts loudly instead of typing blind.
#
# WHY NOTHING IS TYPED AS TEXT: `keystroke "chrome://extensions"` is interpreted
# through the CURRENT keyboard layout. On a Russian layout it produced
# "фффффф://фффффффффф" and searched Google for it. So: URLs go through Chrome's
# own AppleScript API, and the file-dialog path goes through the clipboard.
# Only modifier shortcuts are pressed, and those are layout-independent.
#
# Profile targeting goes through Chrome's native "Profiles" menu: `open
# --profile-directory` is unreliable once Chrome runs, and native menus are
# readable by System Events while chrome:// web content is not.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
EXT_DIR="$(cd "$HERE/.." && pwd)/extension"

if ! osascript -e 'tell application "System Events" to tell process "Finder" to return name' >/dev/null 2>&1; then
  cat >&2 <<'MSG'
ERROR: no assistive access.
System Settings -> Privacy & Security -> Accessibility, tick BOTH rows if present:
  "claude"  (com.anthropic.claude-code)   <- the one that runs these commands
  "Claude"  (com.anthropic.claudefordesktop)
MSG
  exit 77
fi

if [ "${1:-}" = "--list" ]; then
  osascript -e 'tell application "Google Chrome" to activate' >/dev/null
  sleep 1
  osascript -e 'tell application "System Events" to tell process "Google Chrome" to return name of every menu item of menu 1 of menu bar item "Profiles" of menu bar 1'
  exit 0
fi

PROFILE="${1:?usage: install-extension.sh \"<Profile display name>\" [--reload] | --list}"
MODE="${2:-install}"
[ -f "$EXT_DIR/manifest.json" ] || { echo "no manifest at $EXT_DIR" >&2; exit 1; }

echo "→ profile: $PROFILE   mode: $MODE"

# --here: the caller has already put the right profile's window in front and
# verified it. Needed because Chrome's Profiles menu shows DISPLAY names, and
# two different accounts can share one ("Stanislau (cone.red)" is both
# stas.s@ and stanislau.r@) — clicking by name would pick whichever comes first.
if [ "$PROFILE" = "--here" ]; then
  osascript -e 'tell application "Google Chrome" to activate' >/dev/null
  sleep 1
  osascript -e 'tell application "Google Chrome" to set URL of active tab of front window to "chrome://extensions"' >/dev/null
  sleep 3
else

osascript <<AS
on assertChrome()
  tell application "System Events"
    set fa to name of first application process whose frontmost is true
    if fa is not "Google Chrome" then error "focus left Chrome (now: " & fa & ") — aborting before typing"
  end tell
end assertChrome

tell application "Google Chrome" to activate
delay 1.5
my assertChrome()

-- switch profile through the native menu
tell application "System Events" to tell process "Google Chrome"
  click menu item "$PROFILE" of menu 1 of menu bar item "Profiles" of menu bar 1
end tell
delay 3
tell application "Google Chrome" to activate
delay 1
my assertChrome()

-- navigate via Chrome's own API, never by typing (see layout note above)
tell application "Google Chrome"
  if (count of windows) is 0 then make new window
  set URL of active tab of front window to "chrome://extensions"
end tell
delay 3
my assertChrome()
AS
rc=$?
[ $rc -ne 0 ] && { echo "aborted before the extensions page (see error above)" >&2; exit $rc; }

fi

echo "→ on chrome://extensions (do not touch keyboard/mouse until this finishes)"

# Both controls are anchored to the window frame, not to screen coordinates, so
# the window can be any size or position. Offsets measured against Chrome 151.
#   Developer mode toggle: top-right of the page header
#   Load unpacked:         left end of the toolbar row that dev mode reveals
COORDS=$(osascript <<'AS'
tell application "System Events" to tell process "Google Chrome"
  set {wx, wy} to position of front window
  set {ww, wh} to size of front window
end tell
-- (`&` on integers builds a list in AppleScript, so coerce to text first)
set devX to ((wx + ww - 28) as integer) as text
set devY to ((wy + 169) as integer) as text
set btnX to ((wx + 76) as integer) as text
set btnY to ((wy + 225) as integer) as text
return devX & "," & devY & "," & btnX & "," & btnY
AS
)
IFS=',' read -r DEV_X DEV_Y BTN_X BTN_Y <<<"$COORDS"
echo "  dev-mode toggle at ${DEV_X},${DEV_Y}; 'Load unpacked' at ${BTN_X},${BTN_Y}"

# Path via clipboard: layout-independent, unlike keystroke.
OLD_CLIP="$(pbpaste 2>/dev/null || true)"
printf '%s' "$EXT_DIR" | pbcopy

CLICK="uv run --quiet --with pyobjc-framework-Quartz python $HERE/uiclick.py"

# Developer mode gates the whole toolbar row — without it there is no Load
# unpacked button at all. Clicking blind is not idempotent (a second run would
# switch it back off), so read the toggle's colour first: blue = on, grey = off.
RGB=$($CLICK probe "$DEV_X" "$DEV_Y" 2>/dev/null | tail -1)
R=${RGB%%,*}; B=${RGB##*,}
if [ $(( B - R )) -lt 30 ]; then
  echo "  developer mode is off → enabling"
  $CLICK "$DEV_X" "$DEV_Y" >/dev/null
  sleep 2
else
  echo "  developer mode already on"
fi

echo "  clicking Load unpacked"
$CLICK "$BTN_X" "$BTN_Y" >/dev/null
sleep 2.5

# Quoted heredoc: the AppleScript below must reach osascript verbatim. Unquoted,
# bash would expand backticks and $ inside it (that bug cost a run).
osascript <<'AS'
on assertChrome()
  tell application "System Events"
    set fa to name of first application process whose frontmost is true
    if fa is not "Google Chrome" then error "focus left Chrome (now: " & fa & ") - aborting"
  end tell
end assertChrome

my assertChrome()
-- Native "Go to folder" sheet, then paste the path.
-- KEY CODES, not keystroke: keystroke resolves the CHARACTER through the
-- current layout, and a Russian layout has no "v" - the paste silently did
-- nothing and the dialog opened whatever was selected in /Applications.
-- Key codes are physical positions: 5 = G, 9 = V, 36 = Return.
tell application "System Events" to tell process "Google Chrome"
  key code 5 using {command down, shift down}
  delay 1.4
  key code 9 using {command down}
  delay 1.0
  key code 36
  delay 1.8
  key code 36
end tell
delay 2.5
AS
rc=$?

# restore whatever the user had on the clipboard
printf '%s' "$OLD_CLIP" | pbcopy

if [ $rc -ne 0 ]; then
  echo "aborted during the file dialog (see error above)" >&2
  exit $rc
fi
echo "→ done; verify with: bb.py health"
