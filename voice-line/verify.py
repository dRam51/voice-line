"""End-to-end verification of the voice line.

Drives the real modules against the real servers. Audio playback is
captured rather than played, so this can run without a mic or speakers.
"""

from __future__ import annotations

import asyncio
import sys
import time
import warnings

import numpy as np

warnings.filterwarnings("ignore")

import httpx  # noqa: E402
import sounddevice as _sd  # noqa: E402


# ---------------------------------------------------------------- silence
# The suite used to synthesise and PLAY every test sentence through the real
# speakers. Run it a few times while someone is working and their machine
# appears to be talking to itself every couple of minutes — which is exactly
# what happened. A test harness must not make noise in someone's room.
#
# We stub the output stream instead of skipping playback, and the stub still
# consumes real time proportional to the audio, so the interrupt timing test
# stays meaningful. Pass --audible to actually hear it.
AUDIBLE = "--audible" in sys.argv


class _SilentStream:
    def __init__(self, samplerate=24000, blocksize=1024, **_kw):
        self.samplerate = samplerate or 24000
        self._open = True

    def start(self):
        pass

    def write(self, block):
        # Mimic the pacing of real playback so timing assertions hold.
        time.sleep(len(block) / float(self.samplerate))

    def stop(self):
        self._open = False

    def abort(self):
        self._open = False

    def close(self):
        self._open = False


if not AUDIBLE:
    _sd.OutputStream = _SilentStream

import signals  # noqa: E402
from brain import Brain  # noqa: E402
from config import KOKORO_URL, WHISPER_URL  # noqa: E402
from ears import Ears  # noqa: E402
from mouth import Mouth  # noqa: E402

PASS, FAIL = "  PASS", "  FAIL"
results = []


def check(name, ok, detail=""):
    results.append(ok)
    print(f"{PASS if ok else FAIL}  {name}" + (f"  ({detail})" if detail else ""))
    return ok


async def t1_services():
    print("\n[1] both local servers respond")
    async with httpx.AsyncClient(timeout=10) as c:
        try:
            r = await c.get("http://127.0.0.1:2022/health")
            check("whisper :2022 health", r.status_code == 200, f"HTTP {r.status_code}")
        except Exception as e:
            check("whisper :2022 health", False, str(e))
        try:
            r = await c.get("http://127.0.0.1:8880/health")
            check("kokoro :8880 health", r.status_code == 200, f"HTTP {r.status_code}")
        except Exception as e:
            check("kokoro :8880 health", False, str(e))

        # The spec's exact route must be the one that works.
        try:
            r = await c.post(
                KOKORO_URL,
                json={"model": "kokoro", "input": "Route check.",
                      "voice": "bm_lewis", "response_format": "pcm"},
            )
            pcm = np.frombuffer(r.content, dtype=np.int16)
            check("kokoro /v1/audio/speech returns int16 pcm",
                  r.status_code == 200 and pcm.size > 1000,
                  f"{pcm.size} samples @24k = {pcm.size/24000:.2f}s")
        except Exception as e:
            check("kokoro /v1/audio/speech", False, str(e))


async def t2_full_turn():
    """Round trip: synthesize speech -> transcribe it -> ask brain -> speak."""
    print("\n[2] full turn end to end")
    ears = Ears()
    spoken = "What is two plus two? Answer with just the number."

    # Make real audio with Kokoro, then feed it to whisper. This exercises
    # the actual transcription path without needing a microphone.
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(KOKORO_URL, json={
            "model": "kokoro", "input": spoken,
            "voice": "bm_lewis", "response_format": "pcm"})
    pcm24 = np.frombuffer(r.content, dtype=np.int16)
    # 24k -> 16k for whisper
    idx = (np.arange(int(pcm24.size * 16000 / 24000)) * 24000 / 16000).astype(int)
    pcm16 = pcm24[np.clip(idx, 0, pcm24.size - 1)]

    t0 = time.time()
    heard = await ears.transcribe(pcm16)
    stt_ms = (time.time() - t0) * 1000
    # Whisper renders numerals inconsistently run to run: "two plus two",
    # "2 plus 2", "2 + 2". Assert on meaning, not on one exact spelling.
    low = heard.lower()
    normalized = low.replace("+", " plus ").replace("two", "2")
    normalized = " ".join(normalized.split())
    got_it = "2 plus 2" in normalized
    check("whisper transcribed synthesized speech", got_it,
          f"{stt_ms:.0f}ms -> {heard!r}")

    brain = Brain()
    await brain.start()
    # main.py warms the session at launch behind the spoken greeting, so a
    # cold first turn never happens in real use. Warm it here too, otherwise
    # this measures the prompt-cache toll instead of real turn latency.
    await brain.warmup()
    mouth = Mouth(engine="kokoro")
    mouth.start()

    sentences, timings = [], []
    t0 = time.time()

    def on_sentence(s):
        timings.append(time.time() - t0)
        sentences.append(s)
        mouth.say(s)

    reply = await brain.ask(heard or spoken, on_sentence)
    await mouth.wait_until_done()

    check("brain produced a reply", bool(reply.strip()), repr(reply[:60]))
    check("reply was chunked into sentences", len(sentences) >= 1,
          f"{len(sentences)} chunk(s)")
    if timings:
        # Reported, not asserted at a tight bound. This measures how long the
        # MODEL takes to produce its first sentence, which swings between
        # ~1.7s and ~3.8s run to run and is not something this codebase
        # controls. The guarantee that actually matters — audio on the
        # speakers within the filler deadline regardless of model speed — is
        # enforced by the app and tested in [4]. A generous ceiling here still
        # catches genuine breakage.
        print(f"        (model first sentence: {timings[0]:.2f}s)")
        check("first sentence within a sane bound", timings[0] < 15.0,
              f"{timings[0]:.2f}s")
    check("answer contains 4", "4" in reply or "four" in reply.lower(), repr(reply[:40]))

    await mouth.aclose()
    await brain.aclose()
    await ears.aclose()
    return brain, sentences


async def t3_interrupt():
    print("\n[3] interrupt stops playback mid-reply")
    mouth = Mouth(engine="kokoro")
    mouth.start()
    for s in ["This is the first fairly long sentence that should be cut off partway.",
              "This second sentence must never be heard at all.",
              "And neither should this third one."]:
        mouth.say(s)

    # Wait until it is genuinely playing. Synthesis of a long sentence can
    # take a couple of seconds, so a fixed sleep here races the TTS.
    waited = 0.0
    while not mouth.speaking and waited < 15.0:
        await asyncio.sleep(0.1)
        waited += 0.1
    was_speaking = mouth.speaking
    mouth.interrupt()
    await asyncio.sleep(0.35)

    check("was speaking before interrupt", was_speaking, f"began at {waited:.1f}s")
    check("interrupt stopped playback", not mouth.speaking)
    check("queue drained by interrupt", mouth._queue.qsize() == 0,
          f"qsize={mouth._queue.qsize()}")
    await mouth.aclose()


async def t4_tool_turn():
    """A tool-using turn must put audio on the speakers early.

    This drives VoiceLine.handle(), not brain.ask() directly, because the
    guarantee belongs to the APP, not the model: main.py speaks a filler line
    at FILLER_AFTER_SEC if nothing has been said yet. Testing brain.ask alone
    measured only whether the model felt like emitting filler, which varies
    run to run and made this check flaky.
    """
    print("\n[4] tool-using turn puts audio out early")
    import argparse
    from config import FILLER_AFTER_SEC
    from main import VoiceLine

    args = argparse.Namespace(open_mic=False, voice="kokoro", cwd=None, no_duck=True)
    app = VoiceLine(args)
    await app.brain.start()
    await app.brain.warmup()
    app.mouth.start()

    stamps = []
    real_say = app.mouth.say
    t0 = time.time()

    def spy(sentence):
        stamps.append(time.time() - t0)
        real_say(sentence)

    app.mouth.say = spy
    await app.handle(
        "Read the file config.py in the current working directory and tell me what TTS_ENGINE is set to."
    )
    total = time.time() - t0

    if stamps:
        check("first audio within the filler deadline",
              stamps[0] <= FILLER_AFTER_SEC + 1.5,
              f"first at {stamps[0]:.2f}s (deadline {FILLER_AFTER_SEC + 1.5:.1f}s), "
              f"turn took {total:.2f}s")
        check("more than one chunk reached the mouth", len(stamps) >= 2,
              f"{len(stamps)} chunks at {[f'{x:.1f}s' for x in stamps]}")
    else:
        check("first audio within the filler deadline", False, "nothing reached the mouth")
        check("more than one chunk reached the mouth", False, "")

    await app.mouth.aclose()
    await app.brain.aclose()


async def t5_no_double_play():
    """AssistantMessage arrives alongside deltas; we must not speak both."""
    print("\n[5] nothing plays twice")
    brain = Brain()
    await brain.start()
    sentences = []
    reply = await brain.ask("Say exactly: The sky is blue. Nothing else.",
                            sentences.append)
    await brain.aclose()

    joined = " ".join(sentences)
    # If we were speaking both the deltas and the final AssistantMessage,
    # the spoken text would be about twice the reply length.
    ratio = len(joined) / max(len(reply.strip()), 1)
    check("spoken text is not duplicated", ratio < 1.5,
          f"spoken/reply length ratio {ratio:.2f}")
    check("no sentence repeated verbatim",
          len(sentences) == len(set(sentences)) or len(sentences) <= 1,
          f"{len(sentences)} chunks, {len(set(sentences))} unique")


async def t7_piper():
    """The configured engine must actually synthesise, and fall back safely."""
    print("\n[7] piper engine")
    from config import PIPER_MODEL, PIPER_SAMPLE_RATE, TTS_ENGINE
    check("piper model present", PIPER_MODEL.exists(), str(PIPER_MODEL.name))

    m = Mouth(engine="piper")
    check("piper selected (did not silently downgrade)", m.engine == "piper", m.engine)
    t0 = time.time()
    await asyncio.to_thread(m.warm)
    warm_s = time.time() - t0
    check("model preloads in under 3s", warm_s < 3.0, f"{warm_s:.2f}s")

    t0 = time.time()
    pcm, rate = await m._synth("Paper trading is still within the measurement window.")
    dt = time.time() - t0
    ok = pcm is not None and pcm.size > 1000
    check("piper synthesised audio", ok,
          f"{(pcm.size/rate if ok else 0):.2f}s audio in {dt:.2f}s "
          f"({(pcm.size/rate/dt if ok and dt else 0):.0f}x realtime)")
    check("returns piper's sample rate", rate == PIPER_SAMPLE_RATE, str(rate))

    # A missing model must degrade to Kokoro, never go mute.
    import config as _cfg
    real = _cfg.PIPER_MODEL
    try:
        _cfg.PIPER_MODEL = real.parent / "does_not_exist.onnx"
        import importlib, mouth as _mouth
        importlib.reload(_mouth)
        fallback = _mouth.Mouth(engine="piper")
        check("missing model falls back to kokoro", fallback.engine == "kokoro",
              fallback.engine)
        await fallback._client.aclose()
    finally:
        _cfg.PIPER_MODEL = real
        import importlib, mouth as _mouth
        importlib.reload(_mouth)
    await m._client.aclose()


async def t6_signal_bus():
    print("\n[6] signal bus")
    from config import STATE_FILE, WAVEFORM_FILE
    import json

    signals.set_state(signals.LISTENING)
    check("state file written", STATE_FILE.read_text() == "listening")

    # The self-heal rule: a waveform write must force state back to speaking.
    signals.set_state(signals.IDLE)
    signals._last_waveform_write = 0.0
    signals.write_waveform(np.random.randint(-8000, 8000, 4096).astype(np.int16))
    data = json.loads(WAVEFORM_FILE.read_text())
    check("waveform has 64 points", len(data["samples"]) == 64, str(len(data["samples"])))
    check("waveform self-heals state to speaking",
          STATE_FILE.read_text() == "speaking",
          f"state={STATE_FILE.read_text()!r}")

    # Bus must never raise, even on garbage.
    try:
        signals.write_waveform(np.zeros(0, dtype=np.int16))
        signals.set_state("idle")
        check("bus survives empty input", True)
    except Exception as e:
        check("bus survives empty input", False, str(e))
    signals.reset()


async def main():
    print("=" * 60)
    print("  voice-line verification")
    print("=" * 60)
    await t1_services()
    await t2_full_turn()
    await t3_interrupt()
    await t4_tool_turn()
    await t5_no_double_play()
    await t6_signal_bus()
    await t7_piper()

    print("\n" + "=" * 60)
    passed, total = sum(results), len(results)
    print(f"  {passed}/{total} checks passed")
    print("=" * 60)
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    asyncio.run(main())
