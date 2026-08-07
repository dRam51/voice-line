"""Shared debug log. Terminal output overwrites itself by design, so this
file is the record that survives a failed run."""

from __future__ import annotations

import os
import time

DEBUG_LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "debug.log")


def reset() -> None:
    try:
        open(DEBUG_LOG, "w").close()
    except Exception:
        pass


def log(msg: str) -> None:
    try:
        with open(DEBUG_LOG, "a") as fh:
            fh.write(f"{time.strftime('%H:%M:%S')}.{int(time.time() % 1 * 1000):03d} {msg}\n")
            fh.flush()
    except Exception:
        pass
