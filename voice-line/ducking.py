"""Spotify ducking (macOS, AppleScript).

While the assistant speaks, drop Spotify to max(FLOOR, current * FACTOR).
Restore on a debounce so back-to-back sentence chunks do not yo-yo the
volume.

Never launches Spotify. We check System Events for a running process first,
because `tell application "Spotify"` alone would start it.

Every call is best-effort. If Spotify is absent, closed, or AppleScript is
unavailable, this silently does nothing.
"""

from __future__ import annotations

import asyncio
import subprocess

from config import DUCK_FACTOR, DUCK_FLOOR, DUCK_RESTORE_DEBOUNCE, DUCK_SPOTIFY

_IS_RUNNING = (
    'tell application "System Events" to return (name of processes) contains "Spotify"'
)
_GET = """
tell application "System Events"
    if not ((name of processes) contains "Spotify") then return "no"
end tell
tell application "Spotify"
    if player state is not playing then return "no"
    return (sound volume as text)
end tell
"""
_SET = """
tell application "System Events"
    if not ((name of processes) contains "Spotify") then return
end tell
tell application "Spotify" to set sound volume to {vol}
"""


async def _osascript(script: str) -> str:
    try:
        proc = await asyncio.create_subprocess_exec(
            "osascript", "-e", script,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=3.0)
        return out.decode().strip()
    except Exception:
        return ""


class Ducker:
    def __init__(self, enabled: bool = DUCK_SPOTIFY) -> None:
        self.enabled = enabled
        self._original: int | None = None
        self._restore_task: asyncio.Task | None = None

    async def duck(self) -> None:
        """Lower Spotify if it is playing above the floor."""
        if not self.enabled:
            return
        # A restore was pending — cancel it, we are speaking again.
        if self._restore_task and not self._restore_task.done():
            self._restore_task.cancel()
            self._restore_task = None

        if self._original is not None:
            return  # already ducked

        raw = await _osascript(_GET)
        if not raw or raw == "no":
            return
        try:
            current = int(float(raw))
        except ValueError:
            return
        if current <= DUCK_FLOOR:
            return

        self._original = current
        target = max(DUCK_FLOOR, int(current * DUCK_FACTOR))
        await _osascript(_SET.format(vol=target))

    def restore_soon(self) -> None:
        """Schedule a debounced restore."""
        if not self.enabled or self._original is None:
            return
        if self._restore_task and not self._restore_task.done():
            self._restore_task.cancel()
        self._restore_task = asyncio.create_task(self._restore_after_delay())

    async def _restore_after_delay(self) -> None:
        try:
            await asyncio.sleep(DUCK_RESTORE_DEBOUNCE)
        except asyncio.CancelledError:
            return
        await self.restore_now()

    async def restore_now(self) -> None:
        if self._original is None:
            return
        vol, self._original = self._original, None
        await _osascript(_SET.format(vol=vol))
