"""60-second PTT smoke test. Logs every real key event it receives.

Isolates the global hotkey from the mic, whisper, the model, and TTS.
"""
import asyncio
import sys
import time
import warnings

warnings.filterwarnings("ignore")

from ptt import CANCEL, PRESS, RELEASE, PushToTalk  # noqa: E402


async def main():
    loop = asyncio.get_running_loop()
    p = PushToTalk(loop)
    try:
        p.start()
    except Exception as exc:
        print(f"LISTENER FAILED TO START: {exc}", flush=True)
        sys.exit(2)

    print(f"listening for {p.key_label} for 60s...", flush=True)
    t0 = time.time()
    seen = []
    while time.time() - t0 < 60:
        try:
            event, dur = await asyncio.wait_for(p.queue.get(), timeout=1.0)
        except asyncio.TimeoutError:
            continue
        stamp = time.time() - t0
        seen.append(event)
        if event == PRESS:
            print(f"  [{stamp:5.1f}s] PRESS    mic would open", flush=True)
        elif event == RELEASE:
            print(f"  [{stamp:5.1f}s] RELEASE  held {dur:.2f}s -> would transcribe", flush=True)
        elif event == CANCEL:
            why = "too short" if dur else "chord (other key pressed)"
            print(f"  [{stamp:5.1f}s] CANCEL   {why} -> audio discarded", flush=True)
    p.stop()

    print(f"\nRESULT: {len(seen)} events", flush=True)
    if not seen:
        print("NO EVENTS RECEIVED", flush=True)
    else:
        holds = seen.count(RELEASE)
        print(f"valid holds: {holds}   cancels: {seen.count(CANCEL)}", flush=True)


asyncio.run(main())
