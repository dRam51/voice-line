"""The permission gate — the last thing between a misheard sentence and a trade.

An ALLOWLIST. Anything not explicitly named is denied, including tools that
do not exist yet. A denylist would fail open the day the Robinhood connector
gains a new mutating tool, and "fails open toward a brokerage account" is not
an acceptable failure mode for something triggered by a microphone.

Order of checks matters: the write-list is consulted before the read-list, so
a name appearing in both would still be denied.
"""

from __future__ import annotations

from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

from config import (
    RH_PREFIX,
    ROBINHOOD_READ_TOOLS,
    ROBINHOOD_WRITE_TOOLS,
    VOICE_TOOLS,
)
from dbg import log


def decide(tool_name: str) -> tuple[bool, str]:
    """-> (allowed, reason). Pure function so it can be tested exhaustively."""
    if tool_name in VOICE_TOOLS:
        return True, "built-in read/web tool"

    if tool_name.startswith(RH_PREFIX):
        short = tool_name[len(RH_PREFIX):]
        # Deny-first: a name in both lists is denied.
        if short in ROBINHOOD_WRITE_TOOLS:
            return False, "Robinhood tool that mutates money, positions or saved state"
        if short in ROBINHOOD_READ_TOOLS:
            return True, "Robinhood read-only tool"
        return False, "unrecognised Robinhood tool — allowlist denies by default"

    return False, "not on the voice allowlist"


async def can_use_tool(tool_name: str, _input: dict, _ctx) -> object:
    allowed, reason = decide(tool_name)
    if allowed:
        return PermissionResultAllow()
    log(f"GATE DENIED {tool_name} ({reason})")
    return PermissionResultDeny(
        message=(
            f"'{tool_name}' is not available on the voice line ({reason}). "
            "This session is read-only: it can read files, browse the web, and "
            "read Robinhood account data. It cannot trade, cancel, or modify "
            "anything. Tell the person you can look it up but not act on it."
        )
    )
