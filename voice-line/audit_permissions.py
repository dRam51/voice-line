"""Permission audit for the voice line. Run it any time.

Answers one question with evidence: what can this thing actually do?

It enumerates the tools the live session really holds — not what the config
claims — and exercises the gate against every one of them. Written because a
config check alone gave a WRONG answer once: ~/.claude.json showed "no MCP
servers" while the session was holding 60 of them, including
place_equity_order. Read the session, not the config.

    cd ~/Documents/home-lab/ai-voice-assistant/voice-line && uv run python audit_permissions.py
"""

from __future__ import annotations

import asyncio
import sys
import warnings

warnings.filterwarnings("ignore")

from brain import Brain            # noqa: E402
from config import RH_PREFIX       # noqa: E402
from gate import decide            # noqa: E402

DANGEROUS_MARKERS = ("place_", "cancel_", "exercise_", "create_", "update_",
                     "add_", "remove_", "follow_", "unfollow_", "review_")


async def main() -> int:
    brain = Brain()
    await brain.start()
    client = brain._client

    # Wait for connectors — they attach a second or two after connect().
    for _ in range(30):
        st = await client.get_mcp_status()
        if all(s.get("status") in ("connected", "needs-auth", "failed")
               for s in st.get("mcpServers", [])):
            break
        await asyncio.sleep(1)

    print("\n  MCP CONNECTORS")
    for s in st.get("mcpServers", []):
        print(f"    {s.get('name','?'):26s} {s.get('status','?')}")

    await client.query("say ok")
    tools: list[str] = []
    async for msg in client.receive_response():
        d = getattr(msg, "data", None) or {}
        if isinstance(d, dict) and "tools" in d:
            tools = d["tools"]
            break
    await brain.aclose()

    builtins = sorted(t for t in tools if not t.startswith("mcp__"))
    mcp = sorted(t for t in tools if t.startswith("mcp__"))

    print(f"\n  BUILT-IN TOOLS IN SESSION ({len(builtins)})")
    for t in builtins:
        print(f"    {'ALLOW' if decide(t)[0] else 'DENY '}  {t}")

    allowed_mcp = [t for t in mcp if decide(t)[0]]
    denied_mcp = [t for t in mcp if not decide(t)[0]]
    print(f"\n  MCP TOOLS IN SESSION ({len(mcp)})   allowed={len(allowed_mcp)} denied={len(denied_mcp)}")
    for t in allowed_mcp:
        print(f"    ALLOW  {t}")

    # The assertions that matter.
    failures = []
    for t in builtins:
        if t in ("Bash", "Write", "Edit", "NotebookEdit", "Task"):
            failures.append(f"write/exec tool present: {t}")
    for t in allowed_mcp:
        short = t[len(RH_PREFIX):] if t.startswith(RH_PREFIX) else t
        if any(m in short for m in DANGEROUS_MARKERS):
            failures.append(f"mutating MCP tool ALLOWED: {t}")
    if not any(t.startswith(RH_PREFIX) and decide(t)[0] for t in mcp):
        failures.append("no Robinhood read tool allowed — read access is broken")

    print("\n  " + "=" * 58)
    if failures:
        for f in failures:
            print(f"  FAIL  {f}")
        print(f"  {len(failures)} problem(s)")
        return 1
    print("  PASS  read-only: no shell, no writes, no trading")
    print("  PASS  Robinhood read access intact")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
