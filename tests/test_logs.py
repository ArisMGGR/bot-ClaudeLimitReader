"""Tests for the daily log files. Run with: python -m unittest discover tests"""

import asyncio
import contextlib
import io
import logging
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from clr import logs  # noqa: E402
from clr.logs import build_handler, heartbeat  # noqa: E402


class DailyLogTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.handler = build_handler(self.dir)
        self.log = logging.getLogger(f"clr.test.{id(self)}")
        self.log.setLevel(logging.INFO)
        self.log.propagate = False
        self.log.addHandler(self.handler)
        self.addCleanup(self.handler.close)

    def _record(self, msg, created):
        rec = self.log.makeRecord(self.log.name, logging.INFO, "f", 1, msg, (), None)
        rec.created = created
        return rec

    def test_file_is_named_for_the_day_and_starts_with_the_date(self):
        now = time.time()
        self.handler.emit(self._record("hello", now))
        day = time.strftime("%Y-%m-%d", time.localtime(now))
        path = self.dir / f"{day}.log"
        self.assertTrue(path.exists())
        lines = path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(lines[0], f"# {day}")
        self.assertIn("hello", lines[-1])

    def test_rolls_over_to_a_new_file_the_next_day(self):
        now = time.time()
        self.handler.emit(self._record("today", now))
        self.handler.emit(self._record("tomorrow", now + 86400))
        names = sorted(p.name for p in self.dir.iterdir())
        self.assertEqual(len(names), 2, names)
        first, second = (self.dir / n for n in names)
        self.assertIn("today", first.read_text(encoding="utf-8"))
        self.assertIn("tomorrow", second.read_text(encoding="utf-8"))

    def test_header_is_written_once_per_file(self):
        now = time.time()
        self.handler.emit(self._record("one", now))
        self.handler.close()
        again = build_handler(self.dir)
        self.addCleanup(again.close)
        again.emit(self._record("two", now))
        day = time.strftime("%Y-%m-%d", time.localtime(now))
        text = (self.dir / f"{day}.log").read_text(encoding="utf-8")
        self.assertEqual(text.count(f"# {day}"), 1)
        self.assertIn("one", text)
        self.assertIn("two", text)


    def test_a_deleted_directory_is_recreated_at_rollover(self):
        """Removing logs/ while the bot runs must not break logging (F-041)."""
        now = time.time()
        self.handler.emit(self._record("today", now))
        self.handler.close()  # Windows cannot delete an open file
        shutil.rmtree(self.dir)
        self.handler.emit(self._record("tomorrow", now + 86400))
        day = time.strftime("%Y-%m-%d", time.localtime(now + 86400))
        self.assertIn("tomorrow", (self.dir / f"{day}.log").read_text(encoding="utf-8"))

    def test_an_unopenable_file_disables_logging_quietly(self):
        """Callers of the logger never see the OSError; stderr gets one warning."""
        now = time.time()
        self.handler.emit(self._record("today", now))
        self.handler.close()
        shutil.rmtree(self.dir)
        self.dir.write_text("a file where the directory was")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            for n in range(3):
                self.handler.handle(self._record(f"tomorrow {n}", now + 86400))
        self.assertEqual(err.getvalue().count("file logging disabled"), 1, err.getvalue())


class SudoOwnershipTest(unittest.TestCase):
    """Files made under `sudo ... service install` go back to the user (F-003)."""

    def build(self, directory, euid, env):
        chowned = []
        with mock.patch.object(logs.os, "geteuid", lambda: euid, create=True), \
             mock.patch.object(logs.os, "chown", lambda p, u, g: chowned.append((Path(p), u, g)),
                               create=True), \
             mock.patch.dict(logs.os.environ, env):
            handler = build_handler(directory)
            handler.close()
        return chowned

    def test_new_directory_and_file_are_handed_back(self):
        directory = Path(tempfile.mkdtemp()) / "logs"
        chowned = self.build(directory, 0, {"SUDO_UID": "1000", "SUDO_GID": "1000"})
        paths = [p for p, _, _ in chowned]
        self.assertIn(directory, paths)
        self.assertEqual(len([p for p in paths if p.suffix == ".log"]), 1)
        self.assertTrue(all((u, g) == (1000, 1000) for _, u, g in chowned))

    def test_nothing_changes_without_sudo(self):
        directory = Path(tempfile.mkdtemp()) / "logs"
        env = {"SUDO_UID": "", "SUDO_GID": ""}
        self.assertEqual(self.build(directory, 1000, env), [])
        self.assertEqual(self.build(directory / "again", 0, env), [])


class HeartbeatTest(unittest.TestCase):
    def setUp(self):
        self.records = []
        self.catch = logging.Handler()
        self.catch.emit = self.records.append
        clr_log = logging.getLogger("clr")
        clr_log.addHandler(self.catch)
        self.addCleanup(clr_log.removeHandler, self.catch)
        self.old_level = clr_log.level
        clr_log.setLevel(logging.INFO)
        self.addCleanup(clr_log.setLevel, self.old_level)

    def _beats(self):
        return [r for r in self.records if "All good" in r.getMessage()]

    def _run(self, body, interval=0.1):
        async def main():
            task = asyncio.create_task(heartbeat(interval=interval))
            await body()
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

        asyncio.run(main())

    def test_logs_all_good_when_quiet(self):
        self._run(lambda: asyncio.sleep(0.25))
        self.assertGreaterEqual(len(self._beats()), 2)

    def test_skips_windows_with_an_error(self):
        async def body():
            await asyncio.sleep(0.02)
            logging.getLogger("clr.test").error("boom")
            await asyncio.sleep(0.13)  # past the first window, not the second

        self._run(body)
        self.assertEqual(self._beats(), [])

    def test_watch_is_removed_when_cancelled(self):
        before = list(logging.getLogger().handlers)
        self._run(lambda: asyncio.sleep(0.01))
        self.assertEqual(logging.getLogger().handlers, before)


if __name__ == "__main__":
    unittest.main()
