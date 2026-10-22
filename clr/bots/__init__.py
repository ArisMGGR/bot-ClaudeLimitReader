"""Chat platform front-ends."""

from __future__ import annotations

import asyncio
import logging

from ..logs import Outage

log = logging.getLogger("clr")

# Backoff between attempts to reach a platform at startup, in seconds.
FIRST_RETRY_DELAY = 5
MAX_RETRY_DELAY = 300


async def retry_startup(name: str, attempt, transient: tuple[type[BaseException], ...]):
    """Await `attempt()` until it gets past a transient network failure.

    A bot started at logon often runs before the network is up. Exiting then
    would leave it down until the next logon (Windows has no restart-on-failure),
    so keep trying; anything not listed as transient (a bad token) still fails.
    Failures are logged like a dropped connection: one line each with its
    cause, and one ERROR once the outage passes 15 minutes.
    """
    outage = Outage(log, f"{name} first connection")
    delay = FIRST_RETRY_DELAY
    while True:
        try:
            result = await attempt()
        except transient as e:
            outage.failed(e, retry_in=delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, MAX_RETRY_DELAY)
        else:
            outage.restored()
            return result
