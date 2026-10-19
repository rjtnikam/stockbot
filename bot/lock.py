"""One browser at a time: the 6 am run and Telegram commands share this lock.

The server has ~1 GB RAM; two Chromium browsers at once can freeze it.
"""
from __future__ import annotations

import fcntl
import time
from contextlib import contextmanager

from .config import DATA

LOCK = DATA / "browser.lock"
PAUSE = DATA / "paused"


@contextmanager
def browser_lock(wait_seconds: int = 3600):
    LOCK.touch(exist_ok=True)
    f = open(LOCK, "w")
    end = time.time() + wait_seconds
    while True:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except BlockingIOError:
            if time.time() > end:
                f.close()
                raise TimeoutError("another bot job is still using the browser")
            time.sleep(10)
    try:
        yield
    finally:
        fcntl.flock(f, fcntl.LOCK_UN)
        f.close()


def browser_busy() -> bool:
    LOCK.touch(exist_ok=True)
    with open(LOCK, "w") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(f, fcntl.LOCK_UN)
            return False
        except BlockingIOError:
            return True
