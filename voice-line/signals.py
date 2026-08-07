"""The visualizer signal bus.

Contract is just files in the project root, so anything can watch them:

  .voice_state       plain text: idle | listening | thinking | speaking
  .voice_waveform    JSON {"ts": <unix float>, "samples": [64 floats]}
  .voice_loading_pid exists while an optional thinking sound is playing

Every write is wrapped in try/except. The bus must NEVER crash the voice
line — a visualizer is a nice-to-have, the conversation is not.

We never write .voice_alert. That file belongs to any OTHER process on the
machine that wants the visualizer's attention.
"""

from __future__ import annotations

import json
import os
import time

import numpy as np

from config import (
    LOADING_PID_FILE,
    STATE_FILE,
    WAVEFORM_FILE,
    WAVEFORM_HZ,
    WAVEFORM_POINTS,
)

IDLE = "idle"
LISTENING = "listening"
THINKING = "thinking"
SPEAKING = "speaking"

_last_waveform_write = 0.0


def _atomic_write(path, data: str) -> None:
    """Write via temp + rename so a reader never sees a half-written file."""
    tmp = f"{path}.tmp{os.getpid()}"
    with open(tmp, "w") as fh:
        fh.write(data)
    os.replace(tmp, path)


def set_state(state: str) -> None:
    """Write the current state. Never raises."""
    try:
        _atomic_write(STATE_FILE, state)
    except Exception:
        pass


def write_waveform(pcm: np.ndarray) -> None:
    """Downsample a PCM block to 64 points and publish it.

    Rate-limited to WAVEFORM_HZ. Raw int16 magnitudes are fine — the
    visualizer normalizes.

    THE SELF-HEAL RULE: every waveform write also re-writes the state to
    "speaking". This function only runs while audio is audibly playing, so
    any stray process that stomps the state file gets corrected within
    ~70ms. This one rule fixed a bug that took a whole evening to find.
    """
    global _last_waveform_write
    try:
        now = time.time()
        if now - _last_waveform_write < 1.0 / WAVEFORM_HZ:
            return
        _last_waveform_write = now

        block = np.abs(np.asarray(pcm, dtype=np.float32).reshape(-1))
        if block.size == 0:
            return

        # Bucket into WAVEFORM_POINTS bins, take the peak of each so the
        # shape stays lively instead of averaging down to a flat line.
        if block.size >= WAVEFORM_POINTS:
            trim = block.size - (block.size % WAVEFORM_POINTS)
            buckets = block[:trim].reshape(WAVEFORM_POINTS, -1)
            samples = buckets.max(axis=1)
        else:
            samples = np.pad(block, (0, WAVEFORM_POINTS - block.size))

        _atomic_write(
            WAVEFORM_FILE,
            json.dumps({"ts": now, "samples": [round(float(x), 2) for x in samples]}),
        )
        # Self-heal: we are provably speaking right now.
        _atomic_write(STATE_FILE, SPEAKING)
    except Exception:
        pass


def set_loading(active: bool) -> None:
    """Create/remove the loading-pid file for an optional thinking sound."""
    try:
        if active:
            _atomic_write(LOADING_PID_FILE, str(os.getpid()))
        elif os.path.exists(LOADING_PID_FILE):
            os.remove(LOADING_PID_FILE)
    except Exception:
        pass


def reset() -> None:
    """Clean shutdown: back to idle, drop the loading marker."""
    set_state(IDLE)
    set_loading(False)
