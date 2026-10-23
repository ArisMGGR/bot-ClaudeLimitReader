"""Daily log files under ``<project>/logs``, next to ``run.py``.

One file per calendar day, named ``YYYY-MM-DD.log``, each starting with a
header line carrying that date. A long-running ``python run.py run`` rolls over to the
next file by itself at midnight local time.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import os
import sys
import time
from pathlib import Path

LOG_DIR = Path(__file__).resolve().parent.parent / "logs"


def _hand_to_sudo_user(path: Path) -> None:
    """Give a file this process created back to the user who ran sudo.

    `sudo ... service install --system` logs like any other command. Left
    root-owned, today's file (or the directory itself) would be unwritable for
    the service, which runs as that user, and file logging would silently stop.
    """
    geteuid = getattr(os, "geteuid", None)
    uid, gid = os.environ.get("SUDO_UID"), os.environ.get("SUDO_GID")
    if geteuid is None or geteuid() != 0 or not uid or not gid:
        return
    try:
        os.chown(path, int(uid), int(gid))
    except (OSError, ValueError):
        pass  # best effort; the service reports "file logging disabled" if it matters


class DailyFileHandler(logging.FileHandler):
    """A file handler that writes into ``<dir>/<today>.log``.

    ``logging.handlers.TimedRotatingFileHandler`` keeps the live file under a
    fixed name and only dates it once rotated; here the date *is* the name from
    the first record onwards, so today's file is always obvious on disk.
    """

    def __init__(self, directory: Path, encoding: str = "utf-8") -> None:
        self.directory = Path(directory)
        self._ensure_directory()
        self._day = dt.date.today()
        # Set when a later day's file cannot be opened; cleared at the next day.
        self._failed = False
        super().__init__(self._path_for(self._day), encoding=encoding, delay=False)

    def _ensure_directory(self) -> None:
        if not self.directory.exists():
            self.directory.mkdir(parents=True, exist_ok=True)
            _hand_to_sudo_user(self.directory)

    def _path_for(self, day: dt.date) -> Path:
        return self.directory / f"{day.isoformat()}.log"

    def _open(self):
        path = Path(self.baseFilename)
        fresh = not path.exists() or path.stat().st_size == 0
        stream = super()._open()
        if fresh:
            _hand_to_sudo_user(path)
            day = path.stem
            stream.write(f"# {day}\n\n")
            stream.flush()
        return stream

    def emit(self, record: logging.LogRecord) -> None:
        day = dt.date.fromtimestamp(record.created)
        if day != self._day:
            self._day = day
            self._failed = False  # a new day gets a fresh attempt
            self.close()
            self.baseFilename = str(self._path_for(day))
        if self._failed:
            return
        if self.stream is None:
            # An exception here would reach whatever called the logger, so a
            # file that cannot be opened disables file logging for the day.
            try:
                self._ensure_directory()  # it may have been deleted meanwhile
                self.stream = self._open()
            except OSError as e:
                self._failed = True
                if sys.stderr is not None:
                    sys.stderr.write(f"file logging disabled: {e}\n")
                return
        super().emit(record)


def describe_error(exc: BaseException) -> str:
    """One line for a connection failure, instead of a traceback.

    aiohttp and httpx already name the host and the cause in the message
    ("Cannot connect to host gateway…:443 ssl:default [Temporary failure in
    name resolution]"); the frames through library internals add nothing. A
    message-less exception (asyncio.TimeoutError) borrows its cause's text.
    """
    name = type(exc).__name__
    seen = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        text = str(cur).strip()
        if text:
            return f"{name}: {text}" if cur is exc else f"{name}: {type(cur).__name__}: {text}"
        cur = cur.__cause__ or cur.__context__
    return name


def _duration(seconds: float) -> str:
    seconds = int(max(seconds, 0))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


class Outage:
    """Folds a run of connection failures into short, countable log lines.

    Every failed attempt gets one WARNING line with its cause. An outage still
    going after ``escalate_after`` seconds gets one ERROR, and ``restored()``
    closes it with an INFO line giving its length and attempt count. Without a
    ``restored()`` signal, ``quiet_gap`` (seconds without a failure) ends it.
    """

    ESCALATE_AFTER = 15 * 60

    def __init__(
        self,
        log: logging.Logger,
        what: str,
        escalate_after: float = ESCALATE_AFTER,
        quiet_gap: float | None = None,
    ) -> None:
        self.log = log
        self.what = what
        self.escalate_after = escalate_after
        self.quiet_gap = quiet_gap
        self.down_since: float | None = None
        self.last_failure = 0.0
        self.failures = 0
        self.escalated = False
        self.cause = ""

    def failed(self, exc: BaseException, retry_in: float | None = None, now: float | None = None) -> None:
        now = time.time() if now is None else now
        if self.down_since is not None and self.quiet_gap is not None \
                and now - self.last_failure > self.quiet_gap:
            self.down_since = None  # recovered unannounced in between
        if self.down_since is None:
            self.down_since, self.failures, self.escalated = now, 0, False
        self.failures += 1
        self.last_failure = now
        self.cause = describe_error(exc)
        down = _duration(now - self.down_since)
        retry = f"; retrying in {retry_in:.0f}s" if retry_in is not None else ""
        if not self.escalated and now - self.down_since >= self.escalate_after:
            self.escalated = True
            self.log.error(
                "%s still down after %s (%d failed attempts): %s%s",
                self.what, down, self.failures, self.cause, retry,
            )
        else:
            self.log.warning(
                "%s unreachable (attempt %d, down %s): %s%s",
                self.what, self.failures, down, self.cause, retry,
            )

    def restored(self, now: float | None = None) -> None:
        if self.down_since is None:
            return
        now = time.time() if now is None else now
        self.log.info(
            "%s back after %s (%d failed attempt(s); last error: %s)",
            self.what, _duration(now - self.down_since), self.failures, self.cause,
        )
        self.down_since = None


class ErrorWatch(logging.Handler):
    """Remembers when the last WARNING (or worse) record went through."""

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.last_error: float | None = None

    def emit(self, record: logging.LogRecord) -> None:
        self.last_error = record.created

    def quiet_since(self, since: float) -> bool:
        return self.last_error is None or self.last_error < since


async def heartbeat(interval: float = 30 * 60) -> None:
    """Log an "all good" line every ``interval`` seconds without problems.

    Keeps the daily log from going silent for hours on a healthy bot, so a
    quiet file still proves the process was alive. A window that saw a warning
    or error (a dropped connection, say) gets no heartbeat: that line is
    already in the log.
    """
    log = logging.getLogger("clr")
    watch = ErrorWatch()
    root = logging.getLogger()
    root.addHandler(watch)
    minutes = round(interval / 60)
    try:
        while True:
            window_start = time.time()
            await asyncio.sleep(interval)
            if watch.quiet_since(window_start):
                log.info("All good — no warnings or errors in the last %d minutes.", minutes)
    finally:
        root.removeHandler(watch)


def build_handler(directory: Path | None = None) -> DailyFileHandler:
    """Create the daily file handler, formatted with full timestamps."""
    handler = DailyFileHandler(directory or LOG_DIR)
    handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    return handler
