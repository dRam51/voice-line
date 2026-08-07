#!/bin/bash
# Double-click me.
#
# Starts server.py in the background if port 8777 isn't already answering,
# then opens Chrome in kiosk mode with a throwaway profile so no tabs,
# extensions or logins ride along. Quitting Chrome (Cmd-Q) leaves the
# server warm for next time.

set -uo pipefail
cd "$(dirname "$0")" || exit 1

PORT=8777
URL="http://127.0.0.1:${PORT}/"
LOG="${TMPDIR:-/tmp}/voice-visualizer.log"
PROFILE="${TMPDIR:-/tmp}/voice-visualizer-chrome-profile"
CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

up() { curl -s -m 2 "http://127.0.0.1:${PORT}/state" -o /dev/null 2>/dev/null; }

if up; then
  echo "  server already up on ${PORT}"
else
  echo "  starting server (log: ${LOG})"
  nohup python3 server.py > "$LOG" 2>&1 &
  for _ in $(seq 1 30); do up && break; sleep 0.2; done
  if up; then echo "  server up"; else
    echo "  server FAILED to start — see ${LOG}"; tail -20 "$LOG"; exit 1
  fi
fi

if [ -x "$CHROME" ]; then
  echo "  opening kiosk…"
  # Fresh throwaway profile each launch: no tabs, no extensions, no history.
  rm -rf "$PROFILE" 2>/dev/null
  "$CHROME" \
    --kiosk \
    --user-data-dir="$PROFILE" \
    --no-first-run \
    --no-default-browser-check \
    --disable-extensions \
    --disable-translate \
    --disable-features=Translate,TranslateUI \
    --autoplay-policy=no-user-gesture-required \
    --app="$URL" >/dev/null 2>&1
  echo "  kiosk closed. Server still running on ${PORT}."
else
  echo "  Chrome not found — opening your default browser."
  echo "  Press Ctrl-Cmd-F (or use the View menu) to go fullscreen."
  open "$URL"
fi
