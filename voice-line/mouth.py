"""TTS queue and playback.

Sentences arrive one at a time, get synthesized, and play back to back.
Everything is cancellable mid-stream: an interrupt clears the queue and
stops the current block immediately.

Two engines:

  kokoro      free, local, $0 forever. Raw PCM int16 24k mono straight from
              the server into sounddevice.

  piper       free, local, IN-PROCESS. No server, no HTTP hop, ~25x realtime
              — about 3x Kokoro. The model is loaded once and cached; loading
              costs ~0.6s and must not happen per sentence.

  elevenlabs  higher quality, needs ELEVENLABS_API_KEY. Hard-won doctrine:
              - fetch mp3_44100_128 and decode locally with ffmpeg. Raw PCM
                at 44.1k needs their Pro tier, and the mp3 decode hides
                inside the network wait anyway, so it costs nothing.
              - turbo model. NOT multilingual for English — it makes
                delivery slow and dull.
              - style stays at 0 for the same reason.
              - their website voice previews are mastered demo clips. Raw
                API output never matches them, so we master locally.
              On ANY failure it falls back to Kokoro, so the voice degrades
              instead of going mute.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import time
import subprocess

import httpx
import numpy as np
import sounddevice as sd

import signals
from config import (
    ELEVEN_MASTER_FILTER,
    ELEVEN_MODEL,
    ELEVEN_SAMPLE_RATE,
    ELEVEN_SIMILARITY,
    ELEVEN_STABILITY,
    ELEVEN_STYLE,
    ELEVEN_VOICE_ID,
    KOKORO_SAMPLE_RATE,
    KOKORO_URL,
    KOKORO_VOICE,
    PIPER_ESPEAK_DATA,
    PIPER_MODEL,
    PIPER_SAMPLE_RATE,
    PIPER_SPEAKER,
    SPEECH_SPEED,
    TTS_ENGINE,
)

_ELEVEN_URL = "https://api.elevenlabs.io/v1/text-to-speech/{vid}/stream"
_PLAY_BLOCK = 1024  # frames per write — small enough to stop fast


class Mouth:
    def __init__(self, engine: str | None = None) -> None:
        self.engine = (engine or TTS_ENGINE).lower()
        self._queue: asyncio.Queue = asyncio.Queue()
        self._client = httpx.AsyncClient(timeout=30.0)
        # Generation counter, NOT a cancel flag. The flag version latched:
        # interrupt() set it, _run's early `continue` skipped straight past
        # the only line that cleared it (inside _play), and the mouth went
        # mute FOREVER while the terminal kept printing sentences. A counter
        # cannot get stuck — a sentence is stale iff its gen != the current
        # gen, which self-heals the moment a new turn starts.
        self._gen = 0
        self._worker: asyncio.Task | None = None
        self._speaking = False
        self._eleven_dead = False  # set after a failure, stops retry storms
        self._stream = None        # live OutputStream, so interrupt() can abort it
        self._api_key = os.environ.get("ELEVENLABS_API_KEY", "").strip()
        self.on_idle = None  # optional callback fired when the queue drains

        self._piper = None          # lazily loaded, then cached
        self._piper_dead = False

        if self.engine == "piper" and not PIPER_MODEL.exists():
            print(f"  [piper model missing at {PIPER_MODEL} — using Kokoro]")
            self.engine = "kokoro"
        if self.engine == "elevenlabs" and not self._api_key:
            print("  [no ELEVENLABS_API_KEY set — using Kokoro]")
            self.engine = "kokoro"
        if self.engine == "elevenlabs" and not shutil.which("ffmpeg"):
            print("  [ffmpeg not found — ElevenLabs needs it to decode; using Kokoro]")
            self.engine = "kokoro"

    # -- public ---------------------------------------------------------

    @property
    def speaking(self) -> bool:
        return self._speaking

    def start(self) -> None:
        self._worker = asyncio.create_task(self._run())

    def warm(self) -> float:
        """Load the TTS model now so the first sentence does not pay for it.

        Piper loads in ~0.6s but the first synthesise call measured 2.2s cold
        versus 0.15s warm. Left unwarmed that lands squarely on the first
        thing the person hears — the same cold-start trap as the microphone.
        Called at launch, off-thread, behind the spoken greeting.
        """
        t0 = time.monotonic()
        if self.engine == "piper":
            try:
                self._load_piper()
            except Exception as exc:
                print(f"  [piper preload failed: {exc} — using Kokoro]")
                self.engine = "kokoro"
        return time.monotonic() - t0

    def say(self, text: str) -> None:
        """Queue a sentence. Returns immediately."""
        text = text.strip()
        if text:
            self._queue.put_nowait((self._gen, text))

    def interrupt(self) -> None:
        """Clear the queue and stop playback NOW."""
        self._gen += 1          # everything queued before this is now stale
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
                self._queue.task_done()
            except asyncio.QueueEmpty:
                break
        # Abort OUR stream directly. sd.stop() only stops streams started by
        # sd.play() — it does nothing to an OutputStream we opened ourselves,
        # so relying on it left audio draining for ~400ms after the key press,
        # which sounds like the interrupt did not work. abort() drops the
        # buffered frames immediately instead of playing them out.
        stream = self._stream
        if stream is not None:
            try:
                stream.abort()
            except Exception:
                pass

    async def wait_until_done(self) -> None:
        await self._queue.join()

    async def aclose(self) -> None:
        self.interrupt()
        if self._worker:
            self._worker.cancel()
            try:
                await self._worker
            except (asyncio.CancelledError, Exception):
                pass
        await self._client.aclose()

    # -- worker ---------------------------------------------------------

    async def _run(self) -> None:
        while True:
            gen, text = await self._queue.get()
            try:
                if gen != self._gen:
                    continue  # superseded by an interrupt; drop it
                pcm, rate = await self._synth(text)
                if pcm is not None and gen == self._gen:
                    await self._play(pcm, rate, gen)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                print(f"\n  [tts error: {exc}]")
            finally:
                self._queue.task_done()
                if self._queue.empty() and self._speaking:
                    self._speaking = False
                    signals.set_state(signals.IDLE)
                    if self.on_idle:
                        try:
                            self.on_idle()
                        except Exception:
                            pass

    # -- synthesis ------------------------------------------------------

    async def _synth(self, text: str) -> tuple[np.ndarray | None, int]:
        if self.engine == "piper" and not self._piper_dead:
            pcm = await asyncio.to_thread(self._synth_piper, text)
            if pcm is not None:
                return pcm, PIPER_SAMPLE_RATE
            print("  [piper failed — falling back to kokoro]")
            self._piper_dead = True

        if self.engine == "elevenlabs" and not self._eleven_dead:
            pcm = await self._synth_eleven(text)
            if pcm is not None:
                return pcm, ELEVEN_SAMPLE_RATE
            # fall through to Kokoro so the voice degrades, never goes mute
            print("  [elevenlabs failed — falling back to kokoro]")
            self._eleven_dead = True
        return await self._synth_kokoro(text), KOKORO_SAMPLE_RATE

    def _load_piper(self):
        """Load once, cache forever. Loading costs ~0.6s."""
        if self._piper is not None:
            return self._piper
        # Must be set BEFORE the import: piper resolves espeak data at load
        # time from a path baked on its build machine, which does not exist
        # here. See PIPER_ESPEAK_DATA in config.py.
        if os.path.isdir(PIPER_ESPEAK_DATA):
            os.environ.setdefault("ESPEAK_DATA_PATH", PIPER_ESPEAK_DATA)
        from piper import PiperVoice
        self._piper = PiperVoice.load(
            str(PIPER_MODEL), config_path=str(PIPER_MODEL) + ".json"
        )
        return self._piper

    def _synth_piper(self, text: str) -> np.ndarray | None:
        """Synchronous — callers wrap it in a thread so the loop keeps running."""
        try:
            from piper import SynthesisConfig
            voice = self._load_piper()
            # length_scale is the INVERSE of speed: smaller is faster.
            cfg = SynthesisConfig(length_scale=1.0 / max(0.25, SPEECH_SPEED))
            if PIPER_SPEAKER is not None:
                cfg = SynthesisConfig(
                    length_scale=1.0 / max(0.25, SPEECH_SPEED),
                    speaker_id=self._piper_speaker_id(),
                )
            chunks = [np.frombuffer(c.audio_int16_bytes, dtype=np.int16)
                      for c in voice.synthesize(text, syn_config=cfg)]
            if not chunks:
                return None
            return np.concatenate(chunks)
        except Exception as exc:
            print(f"\n  [piper error: {exc}]")
            return None

    def _piper_speaker_id(self):
        import json
        m = json.loads((str(PIPER_MODEL) + ".json") and
                       open(str(PIPER_MODEL) + ".json").read())
        smap = m.get("speaker_id_map") or {}
        if isinstance(PIPER_SPEAKER, int):
            return PIPER_SPEAKER
        return smap.get(PIPER_SPEAKER, 0)

    async def _synth_kokoro(self, text: str) -> np.ndarray | None:
        try:
            resp = await self._client.post(
                KOKORO_URL,
                json={
                    "model": "kokoro",
                    "input": text,
                    "voice": KOKORO_VOICE,
                    "response_format": "pcm",
                    "speed": SPEECH_SPEED,
                },
            )
            resp.raise_for_status()
            return np.frombuffer(resp.content, dtype=np.int16)
        except Exception as exc:
            print(f"\n  [kokoro error: {exc}]")
            return None

    async def _synth_eleven(self, text: str) -> np.ndarray | None:
        try:
            resp = await self._client.post(
                _ELEVEN_URL.format(vid=ELEVEN_VOICE_ID),
                headers={
                    "xi-api-key": self._api_key,
                    "Content-Type": "application/json",
                },
                params={"output_format": "mp3_44100_128"},
                json={
                    "text": text,
                    "model_id": ELEVEN_MODEL,
                    "voice_settings": {
                        "stability": ELEVEN_STABILITY,
                        "similarity_boost": ELEVEN_SIMILARITY,
                        "style": ELEVEN_STYLE,
                        "use_speaker_boost": True,
                    },
                },
            )
            if resp.status_code == 401:
                print("  [elevenlabs: key rejected]")
                return None
            if resp.status_code == 429:
                print("  [elevenlabs: out of credits or rate limited]")
                return None
            resp.raise_for_status()
            return await asyncio.to_thread(self._decode_and_master, resp.content)
        except Exception as exc:
            print(f"\n  [elevenlabs error: {exc}]")
            return None

    @staticmethod
    def _decode_and_master(mp3: bytes) -> np.ndarray | None:
        """mp3 -> mastered int16 PCM, via one ffmpeg pass."""
        # atempo changes tempo without shifting pitch, so the voice speeds up
        # without turning into a chipmunk. Chained if the factor is large,
        # since a single atempo stage only accepts 0.5-2.0.
        speed_af = ""
        sp = SPEECH_SPEED
        while sp > 2.0:
            speed_af += "atempo=2.0,"
            sp /= 2.0
        if abs(sp - 1.0) > 0.01:
            speed_af += f"atempo={sp:.3f},"
        proc = subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error",
                "-i", "pipe:0",
                "-af", speed_af + ELEVEN_MASTER_FILTER,
                "-f", "s16le", "-acodec", "pcm_s16le",
                "-ar", str(ELEVEN_SAMPLE_RATE), "-ac", "1",
                "pipe:1",
            ],
            input=mp3, capture_output=True,
        )
        if proc.returncode != 0 or not proc.stdout:
            return None
        return np.frombuffer(proc.stdout, dtype=np.int16)

    # -- playback -------------------------------------------------------

    async def _play(self, pcm: np.ndarray, rate: int, gen: int) -> None:
        """Play PCM, feeding the signal bus, stoppable between blocks."""
        if pcm.size == 0:
            return
        if not self._speaking:
            self._speaking = True
        signals.set_state(signals.SPEAKING)

        stream = sd.OutputStream(
            samplerate=rate, channels=1, dtype="int16", blocksize=_PLAY_BLOCK
        )
        stream.start()
        self._stream = stream
        try:
            pos = 0
            total = pcm.size
            while pos < total:
                if gen != self._gen:
                    break
                block = pcm[pos : pos + _PLAY_BLOCK]
                pos += _PLAY_BLOCK
                # write() blocks until the device has room; off-thread so the
                # event loop keeps servicing the key listener (interrupts).
                try:
                    await asyncio.to_thread(stream.write, block)
                except Exception:
                    # An interrupt aborts the stream out from under an
                    # in-flight write, which PortAudio reports as an error.
                    # That is expected here, not a failure — swallow it so a
                    # normal interrupt does not print scary noise.
                    if gen != self._gen:
                        break
                    raise
                signals.write_waveform(block)
        finally:
            try:
                if gen != self._gen:
                    stream.abort()
                else:
                    stream.stop()
                stream.close()
            except Exception:
                pass
            self._stream = None
