"""Hold the mic open for 15 seconds so you can watch the macOS indicator.

voice-line normally opens the mic for only 1-2 seconds per hold, which makes
the orange dot easy to miss. This keeps it open long enough to see clearly.

Look at the TOP-RIGHT of the menu bar, next to Control Center: a small solid
orange dot. Click Control Center and you will also see "Microphone" listed
with the app currently using it.
"""

import time

import numpy as np
import sounddevice as sd

from config import MIC_BLOCK, MIC_SAMPLE_RATE

HOLD_SEC = 15

frames = []


def cb(indata, *_):
    frames.append(indata.copy().reshape(-1))


print(f"\n  Opening the mic for {HOLD_SEC} seconds.")
print("  LOOK NOW at the top-right of your menu bar for a small ORANGE DOT.")
print("  (Also check Control Center — it lists which app is using the mic.)\n")

stream = sd.InputStream(
    samplerate=MIC_SAMPLE_RATE, channels=1, dtype="int16",
    blocksize=MIC_BLOCK, callback=cb,
)
stream.start()

for remaining in range(HOLD_SEC, 0, -1):
    live = np.abs(np.concatenate(frames)).max() if frames else 0
    print(f"\r  MIC IS OPEN — {remaining:2d}s left   (live level: {int(live):5d})",
          end="", flush=True)
    time.sleep(1)

stream.stop()
stream.close()

print("\n\n  Mic CLOSED. The orange dot should disappear within a second or two.")
print("  If you saw it appear and vanish, the indicator works and voice-line")
print("  is only holding the mic while you hold the key.\n")
