#!/usr/bin/env python3
"""voice-visualizer server — the only bridge to the voice line.

Two jobs, nothing else:
  1. serve index.html (and anything in assets/)
  2. serve /state as JSON, read from the voice line's signal bus

STRICTLY READ-ONLY on the bus. This process never writes .voice_state,
.voice_waveform or .voice_alert. Two writers on one bus is chaos; the voice
line owns those files, we only look at them.

    python3 server.py            real bus,  http://127.0.0.1:8777
    python3 server.py --mock     scripted loop, http://127.0.0.1:8778

Standard library only. No packages, no venv.
"""

from __future__ import annotations

import argparse
import http.server
import json
import math
import os
import socketserver
import time
from pathlib import Path

# ------------------------------------------------------------------ config
HERE = Path(__file__).resolve().parent
# The voice line writes the bus here. Derived from THIS file's location so
# the pair can be moved together without editing paths — it used to be
# hardcoded to ~/voice-line, which broke the moment the project moved.
BUS_DIR = HERE.parent / "voice-line"
STATE_FILE = BUS_DIR / ".voice_state"
WAVEFORM_FILE = BUS_DIR / ".voice_waveform"
ALERT_FILE = BUS_DIR / ".voice_alert"

PORT = 8777
MOCK_PORT = 8778

# A waveform older than this is not "live" any more.
WAVEFORM_FRESH_SEC = 2.0

# Raw int16 magnitudes come off the bus; this is what we call "full scale".
# Kokoro output peaks around 12-15k, so 9000 puts normal speech near the top
# without clipping every syllable to 1.0.
LEVEL_FULL_SCALE = 9000.0

VALID_STATES = ("idle", "listening", "thinking", "speaking")


# -------------------------------------------------------------- bus reading
def read_state_file() -> str:
    try:
        s = STATE_FILE.read_text(errors="ignore").strip().lower()
        return s if s in VALID_STATES else "idle"
    except Exception:
        return "idle"


def read_waveform():
    """-> (level 0..1, samples list|None, fresh bool). Never raises."""
    try:
        raw = WAVEFORM_FILE.read_text(errors="ignore")
        data = json.loads(raw)
        ts = float(data.get("ts", 0.0))
        samples = data.get("samples") or []
        fresh = (time.time() - ts) <= WAVEFORM_FRESH_SEC
        if not samples:
            return 0.0, None, fresh
        mean_abs = sum(abs(float(x)) for x in samples) / len(samples)
        level = max(0.0, min(1.0, mean_abs / LEVEL_FULL_SCALE))
        return level, samples, fresh
    except Exception:
        return 0.0, None, False


def read_alert() -> bool:
    try:
        return ALERT_FILE.exists()
    except Exception:
        return False


def build_state() -> dict:
    state = read_state_file()
    level, samples, fresh = read_waveform()

    # STOMP TOLERANCE. A live waveform means audio is genuinely playing right
    # now, so we report speaking no matter what the state file claims. Any
    # stray process that overwrites .voice_state mid-sentence cannot break the
    # show — the waveform is ground truth while it is fresh.
    if fresh:
        state = "speaking"
    elif state == "speaking":
        # State file says speaking but no fresh waveform: playback ended and
        # nobody reset it. Don't leave the scene stuck at full energy.
        state = "idle"
        level = 0.0

    if not fresh:
        level = 0.0
        samples = None

    out = {"state": state, "level": round(level, 4), "alert": read_alert()}
    if samples:
        # Optional passthrough for scenes that want a real oscilloscope.
        out["samples"] = [round(float(x), 1) for x in samples]
    return out


# ------------------------------------------------------------------- mock
MOCK_SCRIPT = [
    ("idle", 3.0),
    ("listening", 3.0),
    ("thinking", 3.5),
    ("speaking", 7.0),
    ("alert", 2.5),
    ("idle", 2.5),
]
MOCK_TOTAL = sum(d for _, d in MOCK_SCRIPT)
_MOCK_T0 = time.time()


def build_mock_state() -> dict:
    t = (time.time() - _MOCK_T0) % MOCK_TOTAL
    acc = 0.0
    cur, elapsed = "idle", 0.0
    for name, dur in MOCK_SCRIPT:
        if t < acc + dur:
            cur, elapsed = name, t - acc
            break
        acc += dur

    alert = cur == "alert"
    state = "idle" if alert else cur
    level = 0.0
    samples = None

    if cur == "speaking":
        # Synthetic breathing voice: a couple of beating envelopes plus a
        # word-gap gate, so it reads like speech and not a sine wave.
        env = 0.55 + 0.45 * math.sin(elapsed * 3.1)
        gate = 1.0 if (math.sin(elapsed * 1.7) > -0.45) else 0.25
        level = max(0.0, min(1.0, env * gate * (0.6 + 0.4 * math.sin(elapsed * 11.0))))
        samples = [
            abs(math.sin(i * 0.7 + elapsed * 6.0) * 0.5
                + math.sin(i * 1.9 - elapsed * 4.0) * 0.5)
            * level * LEVEL_FULL_SCALE
            for i in range(64)
        ]
        state = "speaking"

    out = {"state": state, "level": round(level, 4), "alert": alert}
    if samples:
        out["samples"] = [round(x, 1) for x in samples]
    return out


# ----------------------------------------------------------------- handler
class Handler(http.server.SimpleHTTPRequestHandler):
    mock = False

    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(HERE), **kw)

    def _json(self, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path == "/state":
            try:
                self._json(build_mock_state() if self.mock else build_state())
            except Exception as exc:
                self._json({"state": "idle", "level": 0.0, "alert": False,
                            "error": str(exc)})
            return
        if path in ("/", ""):
            self.path = "/index.html"
        return super().do_GET()

    def log_message(self, *_a):
        pass  # keep the console clean; the scene is the output


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main() -> None:
    ap = argparse.ArgumentParser(prog="server.py")
    ap.add_argument("--mock", action="store_true",
                    help="serve a scripted state loop on the mock port; "
                         "never touches the real bus")
    ap.add_argument("--port", type=int, default=None)
    args = ap.parse_args()

    Handler.mock = args.mock
    port = args.port or (MOCK_PORT if args.mock else PORT)

    with Server(("127.0.0.1", port), Handler) as httpd:
        mode = "MOCK (scripted loop)" if args.mock else f"LIVE bus at {BUS_DIR}"
        print(f"  voice-visualizer  ·  {mode}")
        print(f"  http://127.0.0.1:{port}/")
        if not args.mock and not BUS_DIR.is_dir():
            print(f"  [warn] {BUS_DIR} does not exist — /state will read idle")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n  down.")


if __name__ == "__main__":
    main()
