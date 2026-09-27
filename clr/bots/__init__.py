"""Chat platform front-ends."""

from __future__ import annotations

import asyncio
import logging

log = logging.getLogger("clr")

# Backoff between attempts to reach a platform at startup, in seconds.
FIRST_RETRY_DELAY = 5
MAX_RETRY_DELAY = 300


async def retry_startup(name: str, attempt, transient: tuple[type[BaseException], ...]):
    """Await `attempt()` until it gets past a transient network failure.

    A bot started at logon often runs before the network is up. Exiting then
    would leave it down until the next logon (Windows has no restart-on-failure),
    so keep trying; anything not listed as transient (a bad token) still fails.
    """
    delay = FIRST_RETRY_DELAY
    while True:
        try:
            return await attempt()
        except transient as e:
            log.warning("%s: could not connect (%s); retrying in %ds", name, e, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, MAX_RETRY_DELAY)
