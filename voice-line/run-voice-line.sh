#!/bin/bash
# voice-line launcher.
#
# Starts the two local services if they are not already up, then runs the
# voice line in the FOREGROUND of this terminal.
#
# macOS note: run this from Terminal (or iTerm), never from launchd. Mic
# permission is granted to the hosting terminal app, and Input Monitoring
# (System Settings > Privacy & Security > Input Monitoring) must be enabled
# for that same app or the hold-to-talk key will never fire. Running this as
# a background daemon would mean a 24/7 open mic — don't.

set -uo pipefail

VOICE_LINE_DIR="$HOME/voice-line"
WHISPER_DIR="$HOME/whisper.cpp"
KOKORO_DIR="$HOME/Kokoro-FastAPI"
WHISPER_MODEL="models/ggml-small.en.bin"

up() { curl -s -m 2 "$1" -o /dev/null 2>/dev/null; }

# --- whisper on 2022 ---------------------------------------------------
if up http://127.0.0.1:2022/health; then
  echo "  whisper  already up on 2022"
else
  echo "  whisper  starting..."
  ( cd "$WHISPER_DIR" && nohup ./build/bin/whisper-server \
      -m "$WHISPER_MODEL" \
      --host 127.0.0.1 --port 2022 \
      --inference-path /v1/audio/transcriptions \
      -t 4 --convert > "$WHISPER_DIR/server.log" 2>&1 & )
  for _ in $(seq 1 40); do up http://127.0.0.1:2022/health && break; sleep 1; done
  up http://127.0.0.1:2022/health && echo "  whisper  up" \
    || { echo "  whisper FAILED — see $WHISPER_DIR/server.log"; exit 1; }
fi

# --- kokoro on 8880 ----------------------------------------------------
if up http://127.0.0.1:8880/health; then
  echo "  kokoro   already up on 8880"
else
  echo "  kokoro   starting (first boot takes ~60s)..."
  ( cd "$KOKORO_DIR" && nohup ./start-voiceline.sh > "$KOKORO_DIR/server.log" 2>&1 & )
  for _ in $(seq 1 120); do up http://127.0.0.1:8880/health && break; sleep 1; done
  up http://127.0.0.1:8880/health && echo "  kokoro   up" \
    || { echo "  kokoro FAILED — see $KOKORO_DIR/server.log"; exit 1; }
fi

# --- the voice line ----------------------------------------------------
cd "$VOICE_LINE_DIR" || exit 1
exec uv run python main.py "$@"
