"""Global hold-to-talk key listener.

THE GOTCHA THIS MODULE EXISTS FOR: the OS fires on_press repeatedly while a
key is held down (key repeat). Without a held-state flag, every repeat looks
like a fresh press, which re-triggers the turn and kills every reply before
it can speak. `self._held` is that flag. Do not remove it.

Second defence, for using a modifier as PTT: if any OTHER key goes down
while the PTT key is held, this is a keyboard shortcut (Cmd+C, Cmd+Tab),
not speech. We emit CANCEL and the turn loop throws the audio away.

macOS: the hosting terminal needs ACCESSIBILITY permission
(System Settings > Privacy & Security > Accessibility). Not Input
Monitoring — pynput uses a CGEventTap, which macOS gates behind
Accessibility. Without it the listener starts, prints "This process is not
trusted!", and then silently never fires. Grant it to the terminal app, not
to python, and restart the terminal afterwards.

Mic permission comes from launching in Terminal, not launchd. Never run this
as a daemon — nobody wants a 24/7 open mic.
"""

from __future__ import annotations

import asyncio
import ctypes
import ctypes.util
import sys
import time

from pynput import keyboard

from config import CHORD_CANCEL, MIN_HOLD_SEC, PTT_KEY_NAME

PRESS = "press"
RELEASE = "release"
CANCEL = "cancel"


def is_trusted() -> bool | None:
    """Is this process allowed to tap keyboard events?

    Returns True/False on macOS, None on other platforms (nothing to check).
    Calls AXIsProcessTrusted() through ctypes so we need no extra dependency.
    """
    if sys.platform != "darwin":
        return None
    try:
        path = ctypes.util.find_library("ApplicationServices")
        lib = ctypes.cdll.LoadLibrary(path)
        lib.AXIsProcessTrusted.restype = ctypes.c_bool
        return bool(lib.AXIsProcessTrusted())
    except Exception:
        return None


PERMISSION_HELP = """
  ┌─ HOLD-TO-TALK IS BLOCKED ─────────────────────────────────────────┐
  │ This terminal lacks Accessibility permission, so the key listener │
  │ will never fire. (Input Monitoring is NOT the one that matters —  │
  │ pynput uses a CGEventTap, which macOS gates on Accessibility.)    │
  │                                                                    │
  │   System Settings > Privacy & Security > Accessibility            │
  │   -> enable your terminal app, then FULLY QUIT and reopen it      │
  │                                                                    │
  │ Typing still works in the meantime — replies are spoken aloud.    │
  └────────────────────────────────────────────────────────────────────┘
"""


def _resolve_key():
    key = getattr(keyboard.Key, PTT_KEY_NAME, None)
    if key is None:
        raise SystemExit(
            f"config.PTT_KEY_NAME={PTT_KEY_NAME!r} is not a pynput Key. "
            "Try 'cmd_r', 'alt_r', 'shift_r', or 'ctrl_r'."
        )
    return key


class PushToTalk:
    """Emits (event, duration) tuples onto an asyncio queue."""

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        self._key = _resolve_key()
        self.queue: asyncio.Queue = asyncio.Queue()

        self._held = False        # <-- the key-repeat filter. Critical.
        self._cancelled = False
        self._down_at = 0.0
        self._listener: keyboard.Listener | None = None

    # -- listener callbacks (run on pynput's thread) --------------------

    def _emit(self, event: str, duration: float = 0.0) -> None:
        self._loop.call_soon_threadsafe(self.queue.put_nowait, (event, duration))

    def _on_press(self, key) -> None:
        if key == self._key:
            if self._held:
                return  # key repeat — ignore it, this is the whole ballgame
            self._held = True
            self._cancelled = False
            self._down_at = time.monotonic()
            self._emit(PRESS)
            return

        # A different key went down. If PTT is held, this is a shortcut.
        if self._held and CHORD_CANCEL and not self._cancelled:
            self._cancelled = True
            self._emit(CANCEL)

    def _on_release(self, key) -> None:
        if key != self._key or not self._held:
            return
        self._held = False
        held_for = time.monotonic() - self._down_at

        if self._cancelled:
            self._cancelled = False
            return  # already cancelled by the chord rule

        if held_for < MIN_HOLD_SEC:
            self._emit(CANCEL, held_for)  # too short to be speech
            return

        self._emit(RELEASE, held_for)

    # -- lifecycle ------------------------------------------------------

    def start(self) -> None:
        self._listener = keyboard.Listener(
            on_press=self._on_press, on_release=self._on_release
        )
        self._listener.daemon = True
        self._listener.start()

    def stop(self) -> None:
        if self._listener is not None:
            try:
                self._listener.stop()
            except Exception:
                pass
            self._listener = None

    @property
    def key_label(self) -> str:
        pretty = {
            "cmd_r": "Right Command", "cmd_l": "Left Command",
            "alt_r": "Right Option", "alt_l": "Left Option",
            "shift_r": "Right Shift", "ctrl_r": "Right Control",
        }
        return pretty.get(PTT_KEY_NAME, PTT_KEY_NAME)
