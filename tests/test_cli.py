"""Tests for the command line entry point and the Telegram front-end wiring."""

import asyncio
import contextlib
import io
import logging
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from clr import cli, logs  # noqa: E402
from clr.config import Config, ConfigError, DiscordConfig, TelegramConfig  # noqa: E402

NO_CLR_ENV = {k: v for k, v in os.environ.items() if not k.startswith("CLR_")}


class MainLogsFailuresTest(unittest.TestCase):
    """Under pythonw.exe there is no console, so failures must reach the log (F-006)."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        root = logging.getLogger()
        self.handlers_before = list(root.handlers)
        self.level_before = root.level
        self.addCleanup(self._restore_logging)
        patcher = mock.patch.object(logs, "LOG_DIR", self.dir / "logs")
        patcher.start()
        self.addCleanup(patcher.stop)

    def _restore_logging(self):
        root = logging.getLogger()
        for handler in list(root.handlers):
            if handler not in self.handlers_before:
                root.removeHandler(handler)
                handler.close()
        root.setLevel(self.level_before)

    def main(self, *argv):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            code = cli.main(list(argv))
        return code, err.getvalue()

    def log_text(self):
        return "".join(p.read_text(encoding="utf-8") for p in (self.dir / "logs").glob("*.log"))

    def test_config_error_is_written_to_the_log_file(self):
        config = self.dir / "config.toml"
        config.write_text('[discord]\nenabled = true\ntoken = ""\nallowed_user_ids = [1]\n')
        with mock.patch.dict(os.environ, NO_CLR_ENV, clear=True):
            code, err = self.main("-c", str(config), "run")
        self.assertEqual(code, 1)
        self.assertIn("no token is set", err)
        self.assertIn("run failed", self.log_text())
        self.assertIn("no token is set", self.log_text())

    def test_the_console_shows_the_error_once(self):
        config = self.dir / "config.toml"
        config.write_text('[discord]\nenabled = "yes"\n')
        with mock.patch.dict(os.environ, NO_CLR_ENV, clear=True):
            _, err = self.main("-c", str(config), "run")
        self.assertEqual(err.count("discord.enabled"), 1, err)

    def test_nothing_enabled_is_logged_too(self):
        config = self.dir / "config.toml"
        config.write_text("")
        with mock.patch.dict(os.environ, NO_CLR_ENV, clear=True):
            code, _ = self.main("-c", str(config), "run")
        self.assertEqual(code, 1)
        self.assertIn("No platform enabled", self.log_text())

    def test_a_tilde_config_path_is_expanded_for_every_command(self):
        """status and setup read args.config themselves, not via load_config (F-058)."""
        home = self.dir / "home"
        home.mkdir()
        (home / "clr.toml").write_text("", encoding="utf-8")
        tokens = types.SimpleNamespace(expires_at=0, expired=True, account_email="", scope="")
        out = io.StringIO()
        env = {**NO_CLR_ENV, "HOME": str(home), "USERPROFILE": str(home)}
        with mock.patch.dict(os.environ, env, clear=True), \
             mock.patch.object(cli, "load_tokens", return_value=tokens), \
             contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(["-c", "~/clr.toml", "status"]), 0)
        line = next(ln for ln in out.getvalue().splitlines() if ln.startswith("Config"))
        self.assertIn(str(home / "clr.toml"), line)
        self.assertNotIn("[missing]", line)

    def test_unexpected_exceptions_are_logged_and_raised(self):
        def boom(args):
            raise RuntimeError("kaboom")

        with mock.patch.object(cli, "cmd_status", boom):
            with self.assertRaises(RuntimeError):
                self.main("status")
        self.assertIn("kaboom", self.log_text())


class StatusTest(unittest.TestCase):
    def test_reports_the_config_given_with_c(self):
        tokens = types.SimpleNamespace(
            expires_at=0, expired=True, account_email="", scope=""
        )
        other = Path(tempfile.mkdtemp()) / "other.toml"
        other.write_text("")
        out = io.StringIO()
        with mock.patch.object(cli, "load_tokens", return_value=tokens), \
             contextlib.redirect_stdout(out):
            cli.cmd_status(types.SimpleNamespace(config=str(other)))
        line = next(ln for ln in out.getvalue().splitlines() if ln.startswith("Config"))
        self.assertIn(str(other), line)
        self.assertNotIn("[missing]", line)


class ConfigUseTest(unittest.TestCase):
    def config(self, text):
        path = Path(tempfile.mkdtemp()) / "config.toml"
        path.write_text(text, encoding="utf-8")
        return path

    def test_usage_needs_no_bot_token(self):
        """Only `run` starts a bot, so only `run` checks for its token (F-033)."""
        path = self.config('[discord]\nenabled = true\ntoken = ""\nallowed_user_ids = [1]\n')
        with mock.patch.dict(os.environ, NO_CLR_ENV, clear=True), \
             mock.patch.object(cli, "fetch_usage"), \
             mock.patch.object(cli, "render_plain", return_value="report"), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.cmd_usage(types.SimpleNamespace(config=str(path))), 0)

    def test_install_refuses_a_missing_config(self):
        """No service is installed for a mistyped -c path (F-034)."""
        missing = Path(tempfile.mkdtemp()) / "typo.toml"
        args = types.SimpleNamespace(action="install", config=str(missing), system=False)
        with mock.patch("clr.service.install") as install:
            with self.assertRaises(ConfigError):
                cli.cmd_service(args)
        install.assert_not_called()


class InstallPreflightTest(unittest.TestCase):
    """`service install` refuses whatever `run` would refuse (F-044)."""

    ENABLED = '[discord]\nenabled = true\ntoken = "t"\nallowed_user_ids = [1]\n'

    def install(self, config_text, *, logged_in=True, library=True, clr_home=True):
        app = self.app = Path(tempfile.mkdtemp())
        config = app / "config.toml"
        config.write_text(config_text, encoding="utf-8")
        if logged_in:
            (app / "tokens.json").write_text("{}", encoding="utf-8")
        args = types.SimpleNamespace(action="install", config=None, system=False)
        spec = object() if library else None
        env = {**NO_CLR_ENV, **({"CLR_HOME": str(app)} if clr_home else {})}
        with mock.patch.dict(os.environ, env, clear=True), \
             mock.patch("clr.service.service_app_dir", return_value=app), \
             mock.patch("importlib.util.find_spec", return_value=spec), \
             mock.patch("clr.service.install", return_value="installed") as install, \
             contextlib.redirect_stdout(io.StringIO()):
            try:
                cli.cmd_service(args)
            except ConfigError as e:
                return install, str(e)
        return install, None

    def test_everything_in_place_installs(self):
        install, error = self.install(self.ENABLED)
        self.assertIsNone(error)
        install.assert_called_once()

    def test_not_logged_in_is_refused(self):
        install, error = self.install(self.ENABLED, logged_in=False)
        self.assertIn("Not logged in", error)
        install.assert_not_called()

    def test_missing_library_is_refused(self):
        install, error = self.install(self.ENABLED, library=False)
        self.assertIn("not installed", error)
        install.assert_not_called()

    def test_nothing_enabled_is_refused(self):
        install, error = self.install("")
        self.assertIn("No platform enabled", error)
        install.assert_not_called()

    def test_reads_the_config_in_the_services_app_dir(self):
        """Under sudo the service's files are SUDO_USER's, not root's."""
        # As under sudo with HOME=/root: this shell's default config is empty or
        # absent, and only the service's app dir holds the real one. cli binds
        # its own DEFAULT_CONFIG_PATH at import, so patch that name too; else a
        # real ~/.claudelimitreader/config.toml would stand in for it (F-047).
        nowhere = Path("/nonexistent/config.toml")
        with mock.patch("clr.config.DEFAULT_CONFIG_PATH", nowhere), \
             mock.patch.object(cli, "DEFAULT_CONFIG_PATH", nowhere), \
             mock.patch.object(cli, "load_config", wraps=cli.load_config) as load:
            install, error = self.install(self.ENABLED, clr_home=False)
        self.assertIsNone(error)
        install.assert_called_once()
        load.assert_called_once_with(self.app / "config.toml")


class RunStartupTest(unittest.TestCase):
    """What `run` does between its preflight and starting the bots (F-056)."""

    def run_cmd(self, load_tokens=None):
        calls = []

        def started(coro):
            coro.close()  # the bots never start here
            calls.append("asyncio.run")

        cfg = Config(discord=DiscordConfig(enabled=True, token="t", allowed_user_ids=[1]))
        with mock.patch.object(cli, "load_config", return_value=cfg), \
             mock.patch.object(cli, "_preflight"), \
             mock.patch.object(cli, "load_tokens",
                               side_effect=load_tokens or (lambda: calls.append("load_tokens"))), \
             mock.patch.object(cli, "record_running_instance",
                               side_effect=lambda: calls.append("record_running_instance")), \
             mock.patch.object(cli.asyncio, "run", side_effect=started):
            try:
                cli.cmd_run(types.SimpleNamespace(config=None))
            finally:
                self.calls = calls
        return calls

    def test_tokens_are_read_and_the_instance_recorded_before_the_bots_start(self):
        self.assertEqual(self.run_cmd(), ["load_tokens", "record_running_instance", "asyncio.run"])

    def test_a_corrupt_token_file_stops_run_before_anything_starts(self):
        from clr.auth import AuthError

        with self.assertRaises(AuthError):
            self.run_cmd(load_tokens=AuthError("corrupt"))
        self.assertEqual(self.calls, [])


class StartupRetryTest(unittest.IsolatedAsyncioTestCase):
    """A network that is not up yet at logon must not end the bot (F-042)."""

    async def test_transient_failures_are_retried_with_backoff(self):
        from clr import bots

        outcomes = [OSError("no route"), OSError("no route"), "connected"]
        delays = []

        async def attempt():
            outcome = outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        async def sleep(seconds):
            delays.append(seconds)

        with mock.patch.object(bots.asyncio, "sleep", sleep), \
             self.assertLogs("clr", level="WARNING"):
            result = await bots.retry_startup("test", attempt, (OSError,))
        self.assertEqual(result, "connected")
        self.assertEqual(delays, [bots.FIRST_RETRY_DELAY, bots.FIRST_RETRY_DELAY * 2])

    async def test_other_errors_are_not_retried(self):
        from clr import bots

        async def attempt():
            raise ValueError("bad token")

        with self.assertRaises(ValueError):
            await bots.retry_startup("test", attempt, (OSError,))


class LibraryCheckTest(unittest.TestCase):
    def test_missing_library_is_a_config_error(self):
        cfg = Config(discord=DiscordConfig(enabled=True, token="t", allowed_user_ids=[1]))
        with mock.patch("importlib.util.find_spec", return_value=None):
            with self.assertRaises(ConfigError) as ctx:
                cli._check_libraries(cfg)
        self.assertIn("discord.py is not installed", str(ctx.exception))

    def test_disabled_platform_needs_no_library(self):
        cfg = Config(telegram=TelegramConfig(enabled=False))
        with mock.patch("importlib.util.find_spec", return_value=None):
            cli._check_libraries(cfg)  # no error


class FakeApp:
    def __init__(self):
        self.calls = []
        self.updater = types.SimpleNamespace(
            start_polling=self._record("start_polling"), stop=self._record("updater.stop")
        )

        async def post_init(app):
            self.calls.append("post_init")

        self.post_init = post_init
        self.initialize = self._record("initialize")
        self.start = self._record("start")
        self.stop = self._record("stop")
        self.shutdown = self._record("shutdown")

    def _record(self, name):
        async def call(*args, **kwargs):
            self.calls.append(name)

        return call


try:
    from clr.bots import telegram_bot
except ImportError:  # python-telegram-bot is optional
    telegram_bot = None


@unittest.skipIf(telegram_bot is None, "python-telegram-bot is not installed")
class TelegramLifecycleTest(unittest.TestCase):
    def test_post_init_registers_commands(self):
        """initialize()/start() alone never call post_init (F-010)."""
        app = FakeApp()

        async def run():
            task = asyncio.create_task(cli._run_telegram(Config()))
            await asyncio.sleep(0.05)
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

        with mock.patch.object(telegram_bot, "build_application", return_value=app):
            asyncio.run(run())
        self.assertLess(app.calls.index("initialize"), app.calls.index("post_init"))
        self.assertLess(app.calls.index("post_init"), app.calls.index("start"))

    def test_network_failure_at_initialize_is_retried(self):
        """No network at logon: shut the attempt down and try again (F-042)."""
        from telegram.error import NetworkError

        from clr import bots

        offline, online = FakeApp(), FakeApp()

        async def fail(*args, **kwargs):
            offline.calls.append("initialize")
            raise NetworkError("httpx.ConnectError")

        offline.initialize = fail

        async def run():
            task = asyncio.create_task(cli._run_telegram(Config()))
            await asyncio.sleep(0.05)
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

        with mock.patch.object(telegram_bot, "build_application",
                               side_effect=[offline, online]), \
             mock.patch.object(bots, "FIRST_RETRY_DELAY", 0), \
             self.assertLogs("clr", level="WARNING"):
            asyncio.run(run())
        self.assertEqual(offline.calls, ["initialize", "shutdown"])
        self.assertIn("start_polling", online.calls)


@unittest.skipIf(telegram_bot is None, "python-telegram-bot is not installed")
class TelegramEditedMessageTest(unittest.IsolatedAsyncioTestCase):
    async def test_edited_command_still_gets_a_reply(self):
        """An edited "/usage" has update.message None (F-011)."""
        replies = []

        async def reply_text(text, **kwargs):
            replies.append(text)

        async def send_chat_action(*args):
            pass

        update = types.SimpleNamespace(
            effective_user=types.SimpleNamespace(id=7),
            effective_chat=types.SimpleNamespace(id=1),
            message=None,
            effective_message=types.SimpleNamespace(reply_text=reply_text),
        )
        context = types.SimpleNamespace(bot=types.SimpleNamespace(send_chat_action=send_chat_action))
        cfg = Config(telegram=TelegramConfig(enabled=True, token="t", allowed_user_ids=[7]))
        handler = telegram_bot._make_handler(cfg)
        with mock.patch.object(telegram_bot, "build_message", return_value="report"):
            await handler(update, context)
        self.assertEqual(replies, ["report"])

    async def test_stranger_gets_silence_and_is_logged(self):
        """Unlisted users get no reply and no typing indicator (F-049)."""
        replies, actions = [], []

        async def reply_text(text, **kwargs):
            replies.append(text)

        async def send_chat_action(*args):
            actions.append(args)

        update = types.SimpleNamespace(
            effective_user=types.SimpleNamespace(id=9999),
            effective_chat=types.SimpleNamespace(id=1),
            effective_message=types.SimpleNamespace(reply_text=reply_text),
        )
        context = types.SimpleNamespace(bot=types.SimpleNamespace(send_chat_action=send_chat_action))
        cfg = Config(telegram=TelegramConfig(enabled=True, token="t", allowed_user_ids=[7]))
        handler = telegram_bot._make_handler(cfg)
        with mock.patch.object(telegram_bot, "build_message", return_value="report") as build, \
             self.assertLogs(telegram_bot.log, level="WARNING") as logs:
            await handler(update, context)
        self.assertEqual(replies, [])
        self.assertEqual(actions, [])
        build.assert_not_called()
        self.assertTrue(any("9999" in line for line in logs.output), logs.output)


if __name__ == "__main__":
    unittest.main()
