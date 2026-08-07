"""Stage-by-stage diagnostic. Run in the terminal you launch voice-line from.

Tests output and input separately so we know which half is broken.
"""
from __future__ import annotations

import asyncio
import sys
import time
import warnings

warnings.filterwarnings("ignore")

import httpx  # noqa: E402
import numpy as np  # noqa: E402
import sounddevice as sd  # noqa: E402

from config import KOKORO_URL, MIC_SAMPLE_RATE  # noqa: E402
from ears import Ears  # noqa: E402


def hdr(n):
    print(f"\n{'=' * 58}\n  {n}\n{'=' * 58}", flush=True)


async def main():
    hdr("1. AUDIO DEVICES")
    try:
        din, dout = sd.default.device
        devs = sd.query_devices()
        print(f"  default INPUT : [{din}] {devs[din]['name']}", flush=True)
        print(f"  default OUTPUT: [{dout}] {devs[dout]['name']}", flush=True)
        print("\n  all outputs:", flush=True)
        for i, d in enumerate(devs):
            if d["max_output_channels"] > 0:
                mark = " <-- default" if i == dout else ""
                print(f"    [{i}] {d['name']}{mark}", flush=True)
        print("  all inputs:", flush=True)
        for i, d in enumerate(devs):
            if d["max_input_channels"] > 0:
                mark = " <-- default" if i == din else ""
                print(f"    [{i}] {d['name']}{mark}", flush=True)
    except Exception as e:
        print(f"  ERROR: {e}", flush=True)

    hdr("2. OUTPUT — can you hear this?")
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(KOKORO_URL, json={
                "model": "kokoro",
                "input": "If you can hear this sentence, audio output is working.",
                "voice": "bm_lewis", "response_format": "pcm"})
        pcm = np.frombuffer(r.content, dtype=np.int16)
        print(f"  kokoro returned {pcm.size} samples ({pcm.size/24000:.1f}s), "
              f"peak={np.abs(pcm).max()}", flush=True)
        print("  PLAYING NOW — listen...", flush=True)
        sd.play(pcm, 24000, blocking=True)
        print("  ...done. Did you hear it?", flush=True)
    except Exception as e:
        print(f"  OUTPUT FAILED: {type(e).__name__}: {e}", flush=True)

    hdr("3. INPUT — say something for 4 seconds")
    print("  recording in 1s...", flush=True)
    await asyncio.sleep(1)
    print("  >>> TALK NOW <<<", flush=True)
    try:
        rec = sd.rec(int(4 * MIC_SAMPLE_RATE), samplerate=MIC_SAMPLE_RATE,
                     channels=1, dtype="int16")
        sd.wait()
        audio = rec.reshape(-1)
        peak = int(np.abs(audio).max())
        rms = float(np.sqrt(np.mean(audio.astype(np.float64) ** 2)))
        print(f"  captured {audio.size} samples, peak={peak}, rms={rms:.1f}", flush=True)
        if peak < 50:
            print("  >>> MIC IS SILENT. Either permission is denied, or the", flush=True)
            print("  >>> default input is a device with no signal.", flush=True)
        elif peak < 500:
            print("  >>> very quiet — mic may be the wrong device", flush=True)
        else:
            print("  >>> mic level looks good", flush=True)
    except Exception as e:
        print(f"  INPUT FAILED: {type(e).__name__}: {e}", flush=True)
        return

    hdr("4. TRANSCRIPTION")
    ears = Ears()
    t0 = time.time()
    text = await ears.transcribe(audio)
    print(f"  whisper took {(time.time()-t0)*1000:.0f}ms", flush=True)
    print(f"  heard: {text!r}", flush=True)
    if not text:
        print("  >>> empty. Either silence, or the junk filter ate it.", flush=True)
        raw = await raw_transcribe(audio)
        print(f"  raw (unfiltered): {raw!r}", flush=True)
    await ears.aclose()

    hdr("SUMMARY")
    print("  Tell me: (a) did you hear step 2, (b) what step 3 peak was,", flush=True)
    print("  (c) what step 4 heard.", flush=True)


async def raw_transcribe(audio):
    """Bypass the junk filter so we can see what whisper really returned."""
    from config import WHISPER_URL
    ears = Ears()
    wav = ears._to_wav(audio)
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(WHISPER_URL,
                             files={"file": ("s.wav", wav, "audio/wav")},
                             data={"response_format": "json", "temperature": "0"})
        return r.json().get("text", "")
    except Exception as e:
        return f"<error {e}>"
    finally:
        await ears.aclose()


if __name__ == "__main__":
    if sys.platform == "darwin":
        print("Run this in the SAME terminal you launch voice-line from.")
    asyncio.run(main())
