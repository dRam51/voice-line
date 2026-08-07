"""Mic capture and transcription.

Two modes:
  - hold-to-talk (default): the mic is opened on key press and fully closed
    on release, so room audio and music never leak into the transcriber.
  - open-mic (--open-mic): webrtcvad endpointing, for the legacy hands-free
    behaviour.

Half-duplex: nothing here ever runs while the mouth is speaking. The turn
loop gates it. That is what stops the system hearing itself.
"""

from __future__ import annotations

import asyncio
import io
import re
import time
import wave

import httpx
import numpy as np
import sounddevice as sd
import webrtcvad

from config import (
    MAX_GAIN,
    MIC_BLOCK,
    MIC_SAMPLE_RATE,
    MIN_SPEECH_SEC,
    VAD_AGGRESSIVENESS,
    TARGET_PEAK,
    VAD_SILENCE_SEC,
    WHISPER_URL,
)

# Whisper emits these when it hears non-speech. They are not words.
_BRACKETED = re.compile(r"[\[\(\*][^\]\)\*]{0,40}[\]\)\*]")

# Things whisper hallucinates onto silence.
_JUNK = {
    "", "you", "thank you", "thanks for watching", "bye", ".", "!", "?",
    "thank you.", "you.", "bye.", "so", "uh", "um",
}


class Ears:
    def __init__(self) -> None:
        self._stream: sd.InputStream | None = None
        self._frames: list[np.ndarray] = []
        self._client = httpx.AsyncClient(timeout=30.0)

    # -- raw capture ----------------------------------------------------

    @staticmethod
    def warm() -> float:
        """Open and immediately close the mic once, to pay the cold cost.

        THE BUG THIS FIXES: the first CoreAudio input open in a process can
        take over 3 SECONDS. open() is called synchronously on key press, so
        that first press stalls the event loop, the release event queues up
        behind it, and by the time the stream is live the person has already
        stopped talking. You capture the 0.18s tail of silence, whisper
        returns nothing, and the app fails silently.

        Warm opens cost ~90ms. Paying the toll at launch (behind the spoken
        greeting) makes the first real press behave like every other one.

        The mic is still fully closed between holds — this opens it once at
        startup and shuts it immediately.
        """
        t0 = time.monotonic()
        try:
            s = sd.InputStream(
                samplerate=MIC_SAMPLE_RATE, channels=1, dtype="int16",
                blocksize=MIC_BLOCK, callback=lambda *a: None,
            )
            s.start()
            s.stop()
            s.close()
        except Exception:
            pass
        return time.monotonic() - t0

    def open(self) -> None:
        """Open the mic and start collecting. Idempotent."""
        if self._stream is not None:
            return
        self._frames = []

        def _cb(indata, _frames, _time, status):
            if status:
                pass  # overflows are survivable, don't spam the console
            self._frames.append(indata.copy().reshape(-1))

        self._stream = sd.InputStream(
            samplerate=MIC_SAMPLE_RATE,
            channels=1,
            dtype="int16",
            blocksize=MIC_BLOCK,
            callback=_cb,
        )
        self._stream.start()

    def close(self) -> np.ndarray:
        """Close the mic completely and return what was captured."""
        if self._stream is None:
            return np.zeros(0, dtype=np.int16)
        try:
            self._stream.stop()
            self._stream.close()
        except Exception:
            pass
        self._stream = None
        if not self._frames:
            return np.zeros(0, dtype=np.int16)
        audio = np.concatenate(self._frames)
        self._frames = []
        return audio

    def abort(self) -> None:
        """Close the mic and throw away the audio (chord cancel)."""
        self.close()

    # -- transcription --------------------------------------------------

    @staticmethod
    def _to_wav(audio: np.ndarray) -> bytes:
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(MIC_SAMPLE_RATE)
            wf.writeframes(audio.tobytes())
        return buf.getvalue()

    @staticmethod
    def _clean(text: str) -> str:
        text = _BRACKETED.sub(" ", text)
        text = re.sub(r"\s+", " ", text).strip()
        if text.strip().lower().strip(".!?,") in _JUNK:
            return ""
        return text

    @staticmethod
    def _normalize(audio: np.ndarray) -> tuple[np.ndarray, float]:
        """Bring quiet speech up to a level whisper can actually read.

        Mic level varies enormously with how far you are from the laptop and
        how loudly you talk. A turn at peak 9797 transcribed perfectly; the
        same words at peak 665 came back as an empty string. Whisper is not
        good at very quiet audio, and the fix belongs here rather than in the
        person's posture.

        Scales toward a target peak, capped so we never take near-silence and
        amplify it into hallucinated words.
        """
        if audio.size == 0:
            return audio, 1.0
        peak = float(np.abs(audio).max())
        if peak < 40:
            return audio, 1.0        # genuine silence — leave it alone
        gain = min(MAX_GAIN, TARGET_PEAK / peak)
        if gain <= 1.05:
            return audio, 1.0        # already loud enough
        boosted = np.clip(audio.astype(np.float32) * gain, -32768, 32767)
        return boosted.astype(np.int16), gain

    async def transcribe(self, audio: np.ndarray) -> str:
        """Send audio to the local whisper server. Returns cleaned text."""
        if audio.size < MIC_SAMPLE_RATE * 0.2:
            return ""
        audio, gain = self._normalize(audio)
        if gain > 1.05:
            print(f"\r\x1b[2K  [quiet mic — boosted {gain:.1f}x]")
        try:
            resp = await self._client.post(
                WHISPER_URL,
                files={"file": ("speech.wav", self._to_wav(audio), "audio/wav")},
                data={"response_format": "json", "temperature": "0"},
            )
            resp.raise_for_status()
            return self._clean(resp.json().get("text", ""))
        except Exception as exc:
            print(f"\n  [transcribe failed: {exc}]")
            return ""

    # -- open-mic mode --------------------------------------------------

    async def listen_vad(self, should_stop) -> np.ndarray:
        """Legacy hands-free capture. Blocks until an utterance ends.

        Discards anything with less than MIN_SPEECH_SEC of actual speech, so
        a door closing or a cough does not become a turn.
        """
        vad = webrtcvad.Vad(VAD_AGGRESSIVENESS)
        collected: list[np.ndarray] = []
        speech_frames = 0
        silence_frames = 0
        started = False
        frame_sec = MIC_BLOCK / MIC_SAMPLE_RATE
        silence_limit = int(VAD_SILENCE_SEC / frame_sec)

        queue: asyncio.Queue = asyncio.Queue()
        loop = asyncio.get_running_loop()

        def _cb(indata, _frames, _time, _status):
            loop.call_soon_threadsafe(queue.put_nowait, indata.copy().reshape(-1))

        with sd.InputStream(
            samplerate=MIC_SAMPLE_RATE, channels=1, dtype="int16",
            blocksize=MIC_BLOCK, callback=_cb,
        ):
            while not should_stop():
                try:
                    frame = await asyncio.wait_for(queue.get(), timeout=0.5)
                except asyncio.TimeoutError:
                    continue

                try:
                    voiced = vad.is_speech(frame.tobytes(), MIC_SAMPLE_RATE)
                except Exception:
                    voiced = False

                if voiced:
                    started = True
                    speech_frames += 1
                    silence_frames = 0
                    collected.append(frame)
                elif started:
                    silence_frames += 1
                    collected.append(frame)
                    if silence_frames >= silence_limit:
                        break

        if not collected or speech_frames * frame_sec < MIN_SPEECH_SEC:
            return np.zeros(0, dtype=np.int16)
        return np.concatenate(collected)

    async def aclose(self) -> None:
        await self._client.aclose()
