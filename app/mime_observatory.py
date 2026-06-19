"""In-memory MIME capture for testing Exchange header behaviour."""
import time
from collections import deque

_captures: deque = deque(maxlen=20)


def capture(raw_bytes: bytes, label: str = "") -> None:
    _captures.appendleft({
        "ts": time.time(),
        "label": label,
        "raw": raw_bytes.decode("utf-8", errors="replace"),
    })


def get_captures() -> list[dict]:
    return list(_captures)


def clear() -> None:
    _captures.clear()
