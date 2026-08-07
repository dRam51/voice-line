"""voice-line — hold a key, talk, hear the reply.

    mic -> ears (sounddevice) -> whisper :2022 -> warm Claude session
        -> mouth (sentence-chunked TTS) -> speakers

Half-duplex: the mic is only ever open while the key is held, so the system
cannot hear itself. No barge-in on open speakers, by design.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
import termios
import tty

import time

import numpy as np

import signals
from brain import Brain
from dbg import log, reset as reset_log
from config import (
    FILLER_AFTER_SEC,
    FILLER_LINE,
    GREETING,
    MIC_SAMPLE_RATE,
    QUIT_PHRASES,
    RELEASE_TAIL_SEC,
    SESSION_CWD,
    TTS_ENGINE,
)
from ducking import Ducker
from ears import Ears
from mouth import Mouth
from ptt import CANCEL, PERMISSION_HELP, PRESS, RELEASE, PushToTalk, is_trusted

# ---------------------------------------------------------------- terminal


class RawTerminal:
    """cbreak + kernel echo off + bracketed paste on, restored on exit.

    Canonical mode cannot host paste-aware input: the kernel hands you a
    line at a time and you can neither see paste boundaries nor suppress
    the echo of a 4000-character dump. So we take the terminal raw and run
    a tiny line editor ourselves.
    """

    def __init__(self) -> None:
        self.fd = sys.stdin.fileno()
        self.saved = None

    def __enter__(self):
        try:
            self.saved = termios.tcgetattr(self.fd)
            tty.setcbreak(self.fd)
            attrs = termios.tcgetattr(self.fd)
            attrs[3] &= ~termios.ECHO  # lflag: kernel echo off, we echo
            termios.tcsetattr(self.fd, termios.TCSANOW, attrs)
            sys.stdout.write("\x1b[?2004h")  # bracketed paste on
            sys.stdout.flush()
        except Exception:
            self.saved = None
        return self

    def __exit__(self, *_exc):
        try:
            sys.stdout.write("\x1b[?2004l")
            sys.stdout.flush()
            if self.saved is not None:
                termios.tcsetattr(self.fd, termios.TCSADRAIN, self.saved)
        except Exception:
            pass


_PASTE_START = "\x1b[200~"
_PASTE_END = "\x1b[201~"

# Gutter junk that rides along when you copy from a terminal or editor:
# box-drawing rules, line-number columns, diff markers.
_GUTTER = re.compile(r"^\s*(\d+\s*[│|\t]|[│|>»]+\s?)", re.MULTILINE)
_BOXCHARS = re.compile(r"[│─┌┐└┘├┤┬┴┼║═╔╗╚╝╠╣╦╩╬]")


def scrub_paste(text: str) -> str:
    """Strip gutter glyphs and undo hard wraps, into one clean line."""
    text = _GUTTER.sub("", text)
    text = _BOXCHARS.sub(" ", text)
    text = text.replace("\r", "\n")
    # Paragraph breaks become sentence breaks; hard wraps become spaces.
    # Only add a full stop when the paragraph does not already end in one,
    # otherwise pasted prose comes out with ".." at every break.
    text = re.sub(r"([^.!?:;])\s*\n\s*\n+", r"\1. ", text)
    text = re.sub(r"\n\s*\n+", " ", text)
    text = re.sub(r"\s*\n\s*", " ", text)
    text = re.sub(r"\s{2,}", " ", text)
    return text.strip()


class LineEditor:
    """Feeds completed lines into a queue. Handles bracketed paste."""

    def __init__(self, queue: asyncio.Queue) -> None:
        self.queue = queue
        self.buf = ""
        self.pending = ""      # raw bytes not yet parsed
        self.paste_buf = None  # not None while inside a paste

    def feed(self, data: str) -> None:
        self.pending += data
        while self.pending:
            if self.paste_buf is not None:
                end = self.pending.find(_PASTE_END)
                if end == -1:
                    self.paste_buf += self.pending
                    self.pending = ""
                    return
                self.paste_buf += self.pending[:end]
                self.pending = self.pending[end + len(_PASTE_END):]
                self._finish_paste()
                continue

            start = self.pending.find(_PASTE_START)
            if start == 0:
                self.paste_buf = ""
                self.pending = self.pending[len(_PASTE_START):]
                continue

            chunk = self.pending if start == -1 else self.pending[:start]
            self.pending = "" if start == -1 else self.pending[start:]
            for ch in chunk:
                self._char(ch)

    def _finish_paste(self) -> None:
        cleaned = scrub_paste(self.paste_buf or "")
        self.paste_buf = None
        if not cleaned:
            return
        self.buf += (" " if self.buf and not self.buf.endswith(" ") else "") + cleaned
        # Echo a count, never the text — a long paste must not flood the pane.
        if len(cleaned) > 80:
            sys.stdout.write(f"\r\x1b[2K> [pasted {len(cleaned)} chars]")
        else:
            sys.stdout.write(f"\r\x1b[2K> {self.buf}")
        sys.stdout.flush()

    def _char(self, ch: str) -> None:
        if ch in ("\r", "\n"):
            line, self.buf = self.buf.strip(), ""
            sys.stdout.write("\r\x1b[2K")
            sys.stdout.flush()
            if line:
                self.queue.put_nowait(line)
            return
        if ch in ("\x7f", "\b"):
            if self.buf:
                self.buf = self.buf[:-1]
                sys.stdout.write(f"\r\x1b[2K> {self.buf}")
                sys.stdout.flush()
            return
        if ch == "\x03":  # Ctrl-C
            raise KeyboardInterrupt
        if ch == "\x04":  # Ctrl-D
            self.queue.put_nowait("__EOF__")
            return
        if ch < " " and ch != "\t":
            return  # swallow other control bytes
        self.buf += ch
        sys.stdout.write(f"\r\x1b[2K> {self.buf}")
        sys.stdout.flush()


# ------------------------------------------------------------------- app


class VoiceLine:
    def __init__(self, args) -> None:
        self.args = args
        self.ears = Ears()
        self.mouth = Mouth(engine=args.voice)
        self.brain = Brain(cwd=args.cwd)
        self.ducker = Ducker(enabled=not args.no_duck)
        self.ptt: PushToTalk | None = None
        self.typed: asyncio.Queue = asyncio.Queue()
        self.running = True
        self.turn_lock = asyncio.Lock()
        # A turn runs as a BACKGROUND task. If it ran inline in the race
        # loop, that loop would sit blocked inside mouth.wait_until_done()
        # for the whole reply — so the interrupt keypress would land in the
        # queue with nobody reading it until the thing it was meant to
        # interrupt had already finished. Dispatch must never block.
        self._turn_task: asyncio.Task | None = None
        # Bumped on every interrupt. Sentences from a superseded turn check
        # this and drop themselves instead of reaching the mouth.
        self._turn_seq = 0

    # -- a turn ---------------------------------------------------------

    def _is_quit(self, text: str) -> bool:
        low = text.lower().strip().strip(".!?,")
        return any(p in low for p in QUIT_PHRASES)

    async def handle(self, text: str) -> None:
        """One turn. Speech and typing both land here."""
        if self._is_quit(text):
            self.running = False
            self.mouth.interrupt()
            self.mouth.say("Goodbye.")
            await self.mouth.wait_until_done()
            return

        log(f"handle() entered with {text!r}; acquiring turn_lock")
        async with self.turn_lock:
            log("  turn_lock acquired")
            print(f"\r\x1b[2K  you: {text}")
            signals.set_state(signals.THINKING)
            await self.ducker.duck()
            log("  ducker.duck() returned; calling brain.ask()")

            first = {"done": False}
            my_turn = self._turn_seq

            t_turn = time.monotonic()

            async def filler_if_slow() -> None:
                """Never let the line go silent for long.

                If the model needs tools before it says anything, the person
                hears nothing and assumes the app is broken. A short spoken
                acknowledgement costs ~0.2s to synthesize and buys us all the
                patience we need. Cancelled the moment a real sentence lands.
                """
                try:
                    await asyncio.sleep(FILLER_AFTER_SEC)
                    if not first["done"] and my_turn == self._turn_seq:
                        log(f"  no sentence after {FILLER_AFTER_SEC}s — speaking filler")
                        self.mouth.say(FILLER_LINE)
                except asyncio.CancelledError:
                    pass

            filler_task = asyncio.create_task(filler_if_slow())

            def on_sentence(sentence: str) -> None:
                if my_turn != self._turn_seq:
                    log(f"  dropping sentence from interrupted turn: {sentence[:40]!r}")
                    return
                if not first["done"]:
                    first["done"] = True
                    filler_task.cancel()
                    print(f"\r\x1b[2K  {'-' * 40}")
                log(f"  sentence @{time.monotonic()-t_turn:.2f}s: {sentence!r}")
                print(f"  {sentence}")
                self.mouth.say(sentence)

            try:
                await self.brain.ask(text, on_sentence)
            except Exception as exc:
                log(f"  BRAIN ERROR: {type(exc).__name__}: {exc}")
                print(f"\n  [brain error: {exc}]")
                self.mouth.say("Something went wrong on my end.")

            filler_task.cancel()
            await self.mouth.wait_until_done()
            log(f"  turn complete in {time.monotonic()-t_turn:.2f}s")
            signals.set_state(signals.IDLE)
            self.ducker.restore_soon()
            self._prompt()

    def interrupt_turn(self) -> str:
        """Stop the current reply NOW. Safe to call any time."""
        stopped = []
        if self.mouth.speaking or not self.mouth._queue.empty():
            stopped.append("playback")
        if self._turn_task is not None and not self._turn_task.done():
            stopped.append("generation")
        if not stopped:
            return ""
        self._turn_seq += 1          # orphan any in-flight sentences
        self.mouth.interrupt()       # clear queue + stop audio immediately
        # Stop generation cleanly via the SDK rather than cancelling the
        # coroutine, which would corrupt the response stream.
        asyncio.create_task(self.brain.interrupt())
        asyncio.create_task(self.ducker.restore_now())
        log(f"interrupt: stopped {'+'.join(stopped)}")
        return "+".join(stopped)

    def _prompt(self) -> None:
        sys.stdout.write("\r\x1b[2K> ")
        sys.stdout.flush()

    # -- push to talk ---------------------------------------------------

    async def on_ptt(self, event: str) -> None:
        log(f"PTT event: {event}")
        if event == PRESS:
            # Pressing while it talks = interrupt. Speaker-safe, no headphones.
            if self.interrupt_turn():
                print("\r\x1b[2K  [interrupted]")
            signals.set_state(signals.LISTENING)
            sys.stdout.write("\r\x1b[2K  [listening]")
            sys.stdout.flush()
            try:
                t0 = time.monotonic()
                self.ears.open()
                log(f"  mic opened in {(time.monotonic()-t0)*1000:.0f}ms")
            except Exception as exc:
                # Denied mic permission surfaces here on some macOS versions.
                log(f"  MIC OPEN FAILED: {exc!r}")
                print(f"\r\x1b[2K  [mic failed to open: {exc}]")
                signals.set_state(signals.IDLE)
                self._prompt()

        elif event == CANCEL:
            self.ears.abort()
            signals.set_state(signals.IDLE)
            self._prompt()

        elif event == RELEASE:
            # Spawn, never await. Awaiting the whole turn here is what made
            # the interrupt key dead: this coroutine IS the dispatcher.
            self._turn_task = asyncio.create_task(self._run_turn())

    async def _run_turn(self) -> None:
        """Tail, close the mic, transcribe, then take the turn."""
        try:
            # Tail so the last word survives the key coming up.
            await asyncio.sleep(RELEASE_TAIL_SEC)
            audio = self.ears.close()
            peak0 = int(np.abs(audio).max()) if audio.size else 0
            log(f"  captured {audio.size} samples "
                f"({audio.size/MIC_SAMPLE_RATE:.2f}s) peak={peak0}")
            signals.set_state(signals.THINKING)
            sys.stdout.write("\r\x1b[2K  [transcribing]")
            sys.stdout.flush()
            t0 = time.monotonic()
            text = await self.ears.transcribe(audio)
            log(f"  transcribed in {(time.monotonic()-t0)*1000:.0f}ms -> {text!r}")
            if not text:
                # Say WHY nothing happened. A silent return here makes a
                # blocked mic look identical to a working app that simply
                # heard nothing, which is impossible to debug.
                signals.set_state(signals.IDLE)
                secs = audio.size / MIC_SAMPLE_RATE
                if audio.size == 0:
                    why = "no audio captured — mic never opened (permission?)"
                elif peak0 < 50:
                    why = f"mic silent ({secs:.1f}s, peak {peak0}) — check mic permission/device"
                elif secs < 0.3:
                    why = f"too short ({secs:.2f}s)"
                else:
                    why = f"heard {secs:.1f}s (peak {peak0}) but no words came back"
                print(f"\r\x1b[2K  [nothing to send: {why}]")
                self._prompt()
                return
            await self.handle(text)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            log(f"  TURN ERROR: {type(exc).__name__}: {exc}")
            print(f"\r\x1b[2K  [turn error: {exc}]")
            self._prompt()

    # -- open mic (legacy) ----------------------------------------------

    async def open_mic_loop(self) -> None:
        print("  [open-mic mode — speak when ready]")
        while self.running:
            if self.mouth.speaking:
                await asyncio.sleep(0.1)
                continue
            signals.set_state(signals.LISTENING)
            audio = await self.ears.listen_vad(lambda: not self.running)
            if audio.size == 0:
                continue
            signals.set_state(signals.THINKING)
            text = await self.ears.transcribe(audio)
            if text:
                await self.handle(text)

    # -- main loop ------------------------------------------------------

    async def run(self) -> None:
        loop = asyncio.get_running_loop()

        reset_log()
        log("=== launch ===")
        log(f"trusted(Accessibility)={is_trusted()}  engine={self.mouth.engine}")

        await self.brain.start()
        log("brain connected")
        self.mouth.start()

        engine = self.mouth.engine
        print(f"\n  voice-line  |  {engine}  |  cwd: {self.args.cwd or SESSION_CWD}")

        if not self.args.open_mic:
            self.ptt = PushToTalk(loop)
            # Fail loudly. Without Accessibility the listener starts fine and
            # then silently never fires, which looks like a broken app.
            if is_trusted() is False:
                print(PERMISSION_HELP)
            self.ptt.start()
            print(f"  hold {self.ptt.key_label} to talk  ·  type + Enter also works")
            print("  say 'goodbye' or Ctrl-C to hang up\n")
        else:
            print("  open-mic mode\n")

        # Greet immediately, warm the session behind it. The first turn pays
        # a prompt-cache toll of several seconds; the greeting covers it.
        self.mouth.say(GREETING)
        log(f"greeting queued: {GREETING!r}")
        warm = asyncio.create_task(self.brain.warmup())
        warm.add_done_callback(
            lambda t: log(f"warmup finished (exc={t.exception() if not t.cancelled() else 'cancelled'})")
        )

        # Pay the CoreAudio cold-open cost now, not on the first key press.
        # The first input open in a process can take 3+ seconds; doing it
        # here (off-thread, behind the greeting) means the first real press
        # opens in ~90ms like every other one. See Ears.warm().
        mic_ms = await asyncio.to_thread(self.ears.warm)
        log(f"mic warmed in {mic_ms*1000:.0f}ms")
        # Same trap as the mic: an unloaded TTS model puts its whole load cost
        # on the first sentence the person hears. Pay it here instead.
        tts_ms = await asyncio.to_thread(self.mouth.warm)
        log(f"tts model warmed in {tts_ms*1000:.0f}ms (engine={self.mouth.engine})")
        if mic_ms > 1.0:
            print(f"  [mic device warmed in {mic_ms:.1f}s]")
        log("entering race loop — ready for input")

        # stdin -> line editor -> typed queue
        editor = LineEditor(self.typed)
        stdin_ok = False
        try:
            loop.add_reader(
                sys.stdin.fileno(),
                lambda: editor.feed(os.read(sys.stdin.fileno(), 4096).decode(errors="ignore")),
            )
            stdin_ok = True
        except Exception:
            pass  # not a tty (piped input) — voice still works

        self._prompt()

        try:
            if self.args.open_mic:
                await self.open_mic_loop()
            else:
                await self._race_loop()
        finally:
            warm.cancel()
            if stdin_ok:
                try:
                    loop.remove_reader(sys.stdin.fileno())
                except Exception:
                    pass

    async def _race_loop(self) -> None:
        """Race the key listener against typed lines.

        THE GOTCHA: asyncio.wait(FIRST_COMPLETED) returns pending futures
        that are still live. You must keep those exact future objects and
        re-await them next iteration. Re-creating them each pass drops
        whatever they had already consumed — typed input goes missing and
        key events get eaten.
        """
        assert self.ptt is not None
        ptt_fut = asyncio.ensure_future(self.ptt.queue.get())
        typed_fut = asyncio.ensure_future(self.typed.get())

        try:
            while self.running:
                done, _pending = await asyncio.wait(
                    {ptt_fut, typed_fut}, return_when=asyncio.FIRST_COMPLETED
                )

                if ptt_fut in done:
                    event, _dur = ptt_fut.result()
                    ptt_fut = asyncio.ensure_future(self.ptt.queue.get())  # only this one
                    await self.on_ptt(event)

                if typed_fut in done:
                    line = typed_fut.result()
                    typed_fut = asyncio.ensure_future(self.typed.get())    # only this one
                    if line == "__EOF__":
                        self.running = False
                        break
                    # Typing while it talks interrupts, same as the key.
                    if self.interrupt_turn():
                        print("\r\x1b[2K  [interrupted]")
                    self._turn_task = asyncio.create_task(self.handle(line))
        finally:
            for fut in (ptt_fut, typed_fut):
                if not fut.done():
                    fut.cancel()

    async def shutdown(self) -> None:
        self.running = False
        if self.ptt:
            self.ptt.stop()
        try:
            await self.ducker.restore_now()
        except Exception:
            pass
        await self.mouth.aclose()
        await self.ears.aclose()
        await self.brain.aclose()
        signals.reset()


def parse_args():
    p = argparse.ArgumentParser(prog="voice-line")
    p.add_argument("--open-mic", action="store_true",
                   help="legacy hands-free VAD mode instead of hold-to-talk")
    p.add_argument("--voice", default=TTS_ENGINE,
                   choices=["piper", "kokoro", "elevenlabs"],
                   help="TTS engine; all fall back to kokoro on failure")
    p.add_argument("--cwd", default=None,
                   help="session working directory (defaults to config.SESSION_CWD)")
    p.add_argument("--no-duck", action="store_true", help="disable Spotify ducking")
    return p.parse_args()


async def amain() -> None:
    app = VoiceLine(parse_args())
    try:
        await app.run()
    finally:
        await app.shutdown()


def main() -> None:
    signals.set_state(signals.IDLE)
    with RawTerminal():
        try:
            asyncio.run(amain())
        except KeyboardInterrupt:
            pass
    print("\r\x1b[2K  voice-line down.")


if __name__ == "__main__":
    main()
