"""One shared in-process lock for every lake write of the BI app (ingest and motion).

The lake has a single writer (core/lake.py). The lock is taken without waiting: a second
writer in the same server process is told to try later. It does NOT cover the command line
or a second server process.
"""

import threading
from collections.abc import Iterator
from contextlib import contextmanager

LAKE_LOCK = threading.Lock()


@contextmanager
def try_lake_lock() -> Iterator[bool]:
    """Try to take the lake lock without waiting; yield True when taken, else False.

    The lock is released on exit only when this call took it, also after an exception.
    """
    acquired = LAKE_LOCK.acquire(blocking=False)
    try:
        yield acquired
    finally:
        if acquired:
            LAKE_LOCK.release()
