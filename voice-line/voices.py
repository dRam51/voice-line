"""Browse, audition and set the voice.

    uv run python voices.py                 # list every installed voice
    uv run python voices.py --en            # just the English ones
    uv run python voices.py --try bm_george # hear one voice
    uv run python voices.py --audition      # hear all British/American males
    uv run python voices.py --audition --en # hear every English voice
    uv run python voices.py --set bm_george # write it into config.py

Only makes sound when you ask it to.
"""

from __future__ import annotations

import argparse
import re
import sys
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")

import httpx  # noqa: E402
import numpy as np  # noqa: E402
import sounddevice as sd  # noqa: E402

from config import KOKORO_URL, KOKORO_VOICE, SPEECH_SPEED  # noqa: E402

SAMPLE = ("Paper trading is still in the measurement window, "
          "so nothing has changed yet.")

# Kokoro's prefix convention: first letter = accent, second = gender.
ACCENTS = {
    "a": "American", "b": "British", "e": "Spanish", "f": "French",
    "h": "Hindi", "i": "Italian", "j": "Japanese", "p": "Portuguese",
    "z": "Chinese",
}
VOICE_DIR = Path.home() / "Kokoro-FastAPI" / "api" / "src" / "voices" / "v1_0"


def all_voices() -> list[str]:
    if VOICE_DIR.is_dir():
        return sorted(p.stem for p in VOICE_DIR.glob("*.pt"))
    try:
        r = httpx.get("http://127.0.0.1:8880/v1/audio/voices", timeout=5)
        d = r.json()
        return sorted(d.get("voices") or d)
    except Exception:
        return []


def label(v: str) -> str:
    acc = ACCENTS.get(v[:1], "?")
    gen = {"f": "female", "m": "male"}.get(v[1:2], "?")
    # v0* are the older first-generation voice packs.
    legacy = " (legacy v0)" if "_v0" in v else ""
    return f"{acc} {gen}{legacy}"


def is_english(v: str) -> bool:
    return v[:1] in ("a", "b")


def say(voice: str, text: str = SAMPLE) -> bool:
    try:
        r = httpx.post(KOKORO_URL, timeout=40, json={
            "model": "kokoro", "input": text, "voice": voice,
            "response_format": "pcm", "speed": SPEECH_SPEED})
        r.raise_for_status()
        pcm = np.frombuffer(r.content, dtype=np.int16)
        if pcm.size == 0:
            print("    (no audio returned)")
            return False
        sd.play(pcm, 24000, blocking=True)
        return True
    except Exception as exc:
        print(f"    failed: {exc}")
        return False


def set_voice(voice: str) -> None:
    cfg = Path(__file__).parent / "config.py"
    src = cfg.read_text()
    new, n = re.subn(r'^KOKORO_VOICE = ".*"$',
                     f'KOKORO_VOICE = "{voice}"', src, count=1, flags=re.M)
    if not n:
        print("  could not find KOKORO_VOICE in config.py — change it by hand")
        return
    cfg.write_text(new)
    print(f"  KOKORO_VOICE = \"{voice}\"  written to config.py")
    print("  restart the voice line for it to take effect.")


def main() -> int:
    ap = argparse.ArgumentParser(prog="voices.py")
    ap.add_argument("--en", action="store_true", help="English voices only")
    ap.add_argument("--try", dest="try_", metavar="VOICE", help="hear one voice")
    ap.add_argument("--audition", action="store_true", help="hear several in a row")
    ap.add_argument("--set", dest="set_", metavar="VOICE", help="write it into config.py")
    ap.add_argument("--text", default=SAMPLE, help="what the sample should say")
    a = ap.parse_args()

    voices = all_voices()
    if not voices:
        print("  no voices found — is the Kokoro server installed?")
        return 1

    if a.set_:
        if a.set_ not in voices:
            print(f"  '{a.set_}' is not installed. Run without --set to list them.")
            return 1
        set_voice(a.set_)
        return 0

    if a.try_:
        if a.try_ not in voices:
            print(f"  '{a.try_}' is not installed. Run without arguments to list them.")
            return 1
        print(f"  {a.try_}  ({label(a.try_)})")
        say(a.try_, a.text)
        return 0

    pool = [v for v in voices if is_english(v)] if a.en else voices
    if a.audition:
        if not a.en:
            # Default shortlist: the accents closest to the current voice.
            pool = [v for v in voices if v[:2] in ("bm", "am") and "_v0" not in v]
        print(f"  auditioning {len(pool)} voices — Ctrl-C to stop\n")
        for v in pool:
            cur = "  <-- current" if v == KOKORO_VOICE else ""
            print(f"  {v:16s} {label(v):22s}{cur}", flush=True)
            say(v, a.text)
        print("\n  set one with:  uv run python voices.py --set <name>")
        return 0

    print(f"  {len(pool)} voices installed   (current: {KOKORO_VOICE})\n")
    group = None
    for v in pool:
        if v[:2] != group:
            group = v[:2]
            print(f"\n  {label(v)}")
        cur = "  <-- current" if v == KOKORO_VOICE else ""
        print(f"    {v}{cur}")
    print("\n  hear one:      uv run python voices.py --try af_bella")
    print("  hear several:  uv run python voices.py --audition")
    print("  choose one:    uv run python voices.py --set af_bella")
    return 0


if __name__ == "__main__":
    sys.exit(main())
