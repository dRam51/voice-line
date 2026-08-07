"""The warm Claude Agent SDK session.

One ClaudeSDKClient per voice session, created at launch and kept warm.
Warm turns are fast; the FIRST turn pays a prompt-cache toll of several
seconds, so main.py fires a warmup query at startup and hides the toll
behind the spoken greeting.

Sentence chunking is the whole latency trick: we stream partial messages
and hand each completed sentence to the mouth the instant it is done, so
audio starts while the rest of the reply is still being written.

THE FLUSH GOTCHA: you must flush the sentence buffer when a content block
STOPS, not only on sentence-ending punctuation. Pre-tool filler like
"On it, checking now" often arrives as its own block; if you wait for
punctuation-plus-more-text, that filler sits silent through the entire tool
run and then plays glued to the answer. Flush on block stop.
"""

from __future__ import annotations

import asyncio
import re

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient

from config import (
    BLOCKED_TOOLS,
    BREATH_SENTENCES,
    SESSION_CWD,
    SPOKEN_DISCIPLINE,
    VOICE_TOOLS,
)
from gate import can_use_tool
from dbg import log

# A sentence ends at . ! ? … possibly followed by a closing quote/bracket.
# The lookahead requires whitespace or end-of-string so "3.5" and "e.g."
# do not get split mid-number.
_SENTENCE_END = re.compile(r'([.!?…]+["\')\]]?)(\s+|$)')

# Never speak these — they are markdown scaffolding, not words.
# NOTE: \d is deliberately NOT in this class. It used to be, and it silently
# ate every purely numeric answer — ask "what is two plus two" and the reply
# "4" was dropped and the assistant said nothing. Digits are real answers.
_STRIP_MARKDOWN = re.compile(r"^[#>\-\*\+\s\.\)\(\|`~_]+$")


def _tidy(sentence: str) -> str:
    """Strip markdown residue that would be read aloud as noise."""
    s = sentence.strip()
    if not s or _STRIP_MARKDOWN.match(s):
        return ""
    s = re.sub(r"\*\*(.+?)\*\*", r"\1", s)      # bold
    s = re.sub(r"(?<!\w)\*(.+?)\*(?!\w)", r"\1", s)  # italic
    s = re.sub(r"`([^`]+)`", r"\1", s)          # inline code
    s = re.sub(r"^#{1,6}\s*", "", s)            # headers
    s = re.sub(r"^[\-\*\+]\s+", "", s)          # bullets
    s = re.sub(r"\s+", " ", s).strip()
    return s


class Brain:
    def __init__(self, cwd=None) -> None:
        self._cwd = str(cwd or SESSION_CWD)
        self._client: ClaudeSDKClient | None = None
        self._buf = ""
        self._pending = ""
        self._sentences_sent = 0
        # ONE query at a time. A ClaudeSDKClient has a single response
        # stream: if a second query() is issued while another coroutine is
        # still draining receive_response(), the two interleave and BOTH
        # get garbage. In practice the second caller saw ~8 fragment
        # messages, no text deltas at all, and spoke nothing — a totally
        # silent turn with no error anywhere. Every query+receive cycle
        # must hold this lock end to end.
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        options = ClaudeAgentOptions(
            cwd=self._cwd,
            system_prompt={
                "type": "preset",
                "preset": "claude_code",
                "append": SPOKEN_DISCIPLINE,
            },
            # HARD CAPABILITY LIMIT. `tools` sets which built-in tools exist
            # at all — Bash, Write and Edit are simply absent from this
            # session, not merely gated behind a prompt. That distinction
            # matters: `allowed_tools` only controls what runs WITHOUT being
            # prompted, so on its own it would not have removed shell access.
            #
            # This is a voice line. Commands arrive by speech, and whisper
            # mishears — it has returned '[clicking]' from room noise. A
            # misheard sentence must not be able to run a shell command or
            # modify a file, so those tools are not on the table.
            tools=VOICE_TOOLS,
            # Deliberately NOT pre-approving anything here. Entries in
            # allowed_tools are auto-approved and never reach can_use_tool,
            # which would punch a hole straight through the gate.
            # Belt and braces: even if the base set were widened by a future
            # edit or an inherited setting, these stay blocked.
            disallowed_tools=BLOCKED_TOOLS,
            # The real control. disallowed_tools is a denylist and would fail
            # open on any new connector tool; this callback is an allowlist and
            # denies by default. See gate.py.
            can_use_tool=can_use_tool,
            # No write tools exist, so there is nothing to auto-accept.
            permission_mode="default",
            include_partial_messages=True,
            setting_sources=["user", "project", "local"],
        )
        self._client = ClaudeSDKClient(options=options)
        await self._client.connect()

    async def warmup(self) -> None:
        """Pay the startup cost now, while the greeting is playing.

        This does NOT just prime the prompt cache. CLAUDE.md's startup
        sequence tells the assistant to read VAULT-INDEX.md, yesterday's
        daily note, and To-Do.md before doing anything. On a voice line
        that ran on the FIRST REAL TURN and produced ~9 seconds of dead
        silence before a single word was spoken — which reads as a broken
        app, not a thinking one.

        So we force that work to happen here, at launch, behind the spoken
        greeting. By the time the person presses the key, the vault is
        already loaded and turn one is as fast as every other turn.
        """
        if self._client is None:
            return
        try:
            async with self._lock:
                log("warmup: sending query (startup sequence)")
                await self._client.query(
                    "Silently run your startup sequence now: read the vault index, "
                    "check yesterday's daily note, and scan the open to-do list, so "
                    "you are oriented before we start talking. Do not summarize any "
                    "of it and do not greet me — I have already been greeted. "
                    "When you are done, reply with exactly: ready"
                )
                n = 0
                async for _ in self._client.receive_response():
                    n += 1
                log(f"warmup: stream ended after {n} messages")
        except Exception as exc:
            log(f"warmup: EXCEPTION {type(exc).__name__}: {exc}")

    async def interrupt(self) -> None:
        """Ask the CLI to stop generating the current turn.

        Use this rather than cancelling the ask() coroutine. Cancelling
        mid-stream would abandon receive_response() with the response
        half-drained, and the NEXT query would then pick up the leftovers —
        exactly the interleaving that made turns come back empty.
        interrupt() ends the stream cleanly so the client stays usable.
        """
        if self._client is None:
            return
        try:
            await self._client.interrupt()
            log("brain: interrupt sent")
        except Exception as exc:
            log(f"brain: interrupt failed ({type(exc).__name__}: {exc})")

    async def ask(self, text: str, on_sentence):
        """Stream a turn. Calls on_sentence(str) per completed sentence.

        Returns the full reply text.
        """
        if self._client is None:
            raise RuntimeError("Brain.start() was not called")

        self._buf = ""
        self._pending = ""
        self._sentences_sent = 0
        full: list[str] = []

        if self._lock.locked():
            log("ask: waiting for warmup/previous turn to release the client")
        async with self._lock:
            log(f"ask: sending query {text!r}")
            await self._client.query(text)
            log("ask: query sent, awaiting stream")
            _n = 0

            async for message in self._client.receive_response():
                _n += 1
                if _n <= 3 or _n % 25 == 0:
                    log(f"ask: msg #{_n} {type(message).__name__}")
                for chunk in self._extract(message):
                    if not chunk:
                        continue
                    full.append(chunk)
                    self._buf += chunk
                    self._drain(on_sentence)

                if self._is_block_stop(message):
                    # THE FLUSH. Do not remove.
                    self._flush(on_sentence)

            log(f"ask: stream ended after {_n} messages")
        self._flush(on_sentence)
        self.flush_pending(on_sentence)  # ship a half-formed breath
        return "".join(full)

    # -- streaming plumbing ---------------------------------------------

    @staticmethod
    def _extract(message) -> list[str]:
        """Pull text deltas out of whatever shape the message arrives in."""
        out: list[str] = []

        # Partial streaming events
        event = getattr(message, "event", None)
        if isinstance(event, dict):
            if event.get("type") == "content_block_delta":
                delta = event.get("delta") or {}
                if delta.get("type") == "text_delta":
                    out.append(delta.get("text", ""))
            return out

        # Fully-formed assistant messages: only used if partials are absent,
        # guarded by _seen_partial so we never speak the same text twice.
        return out

    @staticmethod
    def _is_block_stop(message) -> bool:
        event = getattr(message, "event", None)
        if isinstance(event, dict):
            return event.get("type") in ("content_block_stop", "message_stop")
        return False

    def _drain(self, on_sentence) -> None:
        """Emit every complete sentence sitting in the buffer."""
        while True:
            match = _SENTENCE_END.search(self._buf)
            if not match:
                return
            end = match.end(1)
            sentence, self._buf = self._buf[:end], self._buf[match.end():]
            self._emit(sentence, on_sentence)

    def _flush(self, on_sentence) -> None:
        if self._buf.strip():
            leftover, self._buf = self._buf, ""
            self._emit(leftover, on_sentence)

    def _emit(self, sentence: str, on_sentence) -> None:
        """Ship a sentence, batching per BREATH_SENTENCES after the first.

        The first sentence always goes out alone so audio starts as early as
        possible. After that, batching trades latency for smoothness:
        BREATH_SENTENCES=1 speaks each sentence the instant it completes,
        =2 waits for a pair so short lines do not land flat.
        """
        clean = _tidy(sentence)
        if not clean:
            return

        if self._sentences_sent == 0 or BREATH_SENTENCES <= 1:
            self._sentences_sent += 1
            on_sentence(clean)
            self._pending = ""
            return

        pending = getattr(self, "_pending", "")
        if pending:
            on_sentence(f"{pending} {clean}".strip())
            self._pending = ""
        else:
            self._pending = clean
        self._sentences_sent += 1

    def flush_pending(self, on_sentence) -> None:
        """Ship a half-formed breath at end of turn so nothing is lost."""
        pending = getattr(self, "_pending", "")
        if pending:
            self._pending = ""
            on_sentence(pending)

    async def aclose(self) -> None:
        if self._client is not None:
            try:
                await self._client.disconnect()
            except Exception:
                pass
