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


class LogDirTestCase(unittest.TestCase):
    """logs/ in a temp dir, and the root logger restored after each test."""

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


class MainLogsFailuresTest(LogDirTestCase):
    """Under pythonw.exe there is no console, so failures must reach the log."""

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
        """status and setup read args.config themselves, not via load_config."""
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

    def test_an_unknown_home_in_c_is_a_logged_error(self):
        """POSIX `-c ~nosuchuser/x`: an Error line and a log record, not a traceback."""
        def no_home(path):
            raise RuntimeError("Could not determine home directory.")

        with mock.patch.dict(os.environ, NO_CLR_ENV, clear=True), \
             mock.patch.object(Path, "expanduser", no_home):
            code, err = self.main("-c", "~nosuchuser/x.toml", "status")
        self.assertEqual(code, 1)
        self.assertIn("Error: -c ~nosuchuser/x.toml", err)
        self.assertIn("~nosuchuser/x.toml", self.log_text())

    def test_an_empty_c_is_refused_not_read_as_the_default(self):
        """`-c "$UNSET"` names no file; using the default config instead would hide it."""
        for argv, command in (
            (["status"], "cmd_status"),
            (["usage"], "cmd_usage"),
            (["run"], "cmd_run"),
            (["setup"], "cmd_setup"),
            (["service", "install"], "cmd_service"),
        ):
            for empty in ("", "  "):
                with self.subTest(argv=argv, empty=empty), \
                     mock.patch.dict(os.environ, NO_CLR_ENV, clear=True), \
                     mock.patch.object(cli, command) as run:
                    code, err = self.main("-c", empty, *argv)
                    self.assertEqual(code, 1)
                    self.assertIn("-c was given an empty path", err)
                    run.assert_not_called()
        self.assertIn("run failed: -c was given an empty path", self.log_text())

    def test_an_unknown_home_in_the_environment_stops_every_command(self):
        """CLR_HOME / CLR_CONFIG that cannot expand are reported once logging is up."""
        error = ConfigError("CLR_HOME=~nosuchuser: no user's home directory matches that ~")
        with mock.patch.object(cli, "PATH_ERROR", error), \
             mock.patch.object(cli, "cmd_status") as status:
            code, err = self.main("status")
        self.assertEqual(code, 1)
        status.assert_not_called()
        self.assertIn("CLR_HOME=~nosuchuser", err)
        self.assertIn("status failed: CLR_HOME=~nosuchuser", self.log_text())

    def test_a_bad_clr_config_stops_only_commands_that_read_it(self):
        """-c, `login`, `service status` and `uninstall` never read CLR_CONFIG."""
        error = ConfigError("CLR_CONFIG=~nosuchuser/c.toml: no user's home directory matches that ~")
        config = self.dir / "real.toml"
        config.write_text("", encoding="utf-8")
        for argv, command in (
            (["-c", str(config), "status"], "cmd_status"),
            (["-c", str(config), "usage"], "cmd_usage"),
            (["login"], "cmd_login"),
            (["service", "status"], "cmd_service"),
            (["service", "uninstall"], "cmd_service"),
        ):
            with self.subTest(argv=argv), \
                 mock.patch.object(cli, "CONFIG_PATH_ERROR", error), \
                 mock.patch.object(cli, command, return_value=0) as run:
                self.assertEqual(self.main(*argv)[0], 0)
                run.assert_called_once()
        for argv, command in (
            (["status"], "cmd_status"),
            (["run"], "cmd_run"),
            (["service", "install"], "cmd_service"),
            (["service", "show"], "cmd_service"),
            (["setup"], "cmd_setup"),
        ):
            with self.subTest(argv=argv), \
                 mock.patch.object(cli, "CONFIG_PATH_ERROR", error), \
                 mock.patch.object(cli, command, return_value=0) as run:
                code, err = self.main(*argv)
                self.assertEqual(code, 1)
                self.assertIn("CLR_CONFIG=~nosuchuser", err)
                run.assert_not_called()

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
        """Only `run` starts a bot, so only `run` checks for its token."""
        path = self.config('[discord]\nenabled = true\ntoken = ""\nallowed_user_ids = [1]\n')
        with mock.patch.dict(os.environ, NO_CLR_ENV, clear=True), \
             mock.patch.object(cli, "fetch_usage"), \
             mock.patch.object(cli, "render_plain", return_value="report"), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.cmd_usage(types.SimpleNamespace(config=str(path))), 0)

    def test_install_refuses_a_missing_config(self):
        """No service is installed for a mistyped -c path."""
        missing = Path(tempfile.mkdtemp()) / "typo.toml"
        args = types.SimpleNamespace(action="install", config=str(missing), system=False)
        with mock.patch("clr.service.backend", return_value="systemd"), \
             mock.patch("clr.service.install") as install:
            with self.assertRaises(ConfigError) as ctx:
                cli.cmd_service(args)
        install.assert_not_called()
        # The -c file itself was checked, not the default config.
        self.assertIn(str(missing), str(ctx.exception))

    def test_install_without_a_service_manager_says_so_first(self):
        """No config advice on a host that has nothing to install into."""
        from clr.service import ServiceError

        missing = Path(tempfile.mkdtemp()) / "typo.toml"
        args = types.SimpleNamespace(action="install", config=str(missing), system=False)
        with mock.patch("clr.service.backend", return_value=""), \
             mock.patch.object(cli, "check_install") as check:
            with self.assertRaises(ServiceError) as ctx:
                cli.cmd_service(args)
        self.assertIn("no supported service manager", str(ctx.exception))
        check.assert_not_called()


class InstallPreflightTest(unittest.TestCase):
    """`service install` refuses whatever `run` would refuse."""

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
             mock.patch("clr.service.backend", return_value="systemd"), \
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
        # real ~/.claudelimitreader/config.toml would stand in for it.
        nowhere = Path("/nonexistent/config.toml")
        with mock.patch("clr.config.DEFAULT_CONFIG_PATH", nowhere), \
             mock.patch.object(cli, "DEFAULT_CONFIG_PATH", nowhere), \
             mock.patch.object(cli, "load_config", wraps=cli.load_config) as load:
            install, error = self.install(self.ENABLED, clr_home=False)
        self.assertIsNone(error)
        install.assert_called_once()
        load.assert_called_once_with(self.app / "config.toml")


class RunStartupTest(unittest.TestCase):
    """What `run` does between its preflight and starting the bots."""

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
    """A network that is not up yet at logon must not end the bot."""

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

    async def failing_start(self, failures, exc=OSError("no route")):
        """Run retry_startup through `failures` transient errors on a fake clock."""
        from clr import bots

        clock, delays = [1_000_000.0], []
        outcomes = [exc] * failures + ["connected"]

        async def attempt():
            outcome = outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        async def sleep(seconds):
            delays.append(seconds)
            clock[0] += seconds

        with mock.patch.object(bots.asyncio, "sleep", sleep), \
             mock.patch.object(logs.time, "time", lambda: clock[0]), \
             self.assertLogs("clr", level="INFO") as cm:
            await bots.retry_startup("test", attempt, (OSError,))
        return delays, cm.records

    async def test_backoff_stops_at_the_ceiling(self):
        """5s doubling to at most 300s."""
        from clr import bots

        delays, _ = await self.failing_start(10)
        self.assertEqual(delays, [5, 10, 20, 40, 80, 160, 300, 300, 300, 300])
        self.assertEqual(max(delays), bots.MAX_RETRY_DELAY)

    async def test_startup_failures_are_logged_like_an_outage(self):
        """A cause for message-less errors, one ERROR past 15 minutes, one INFO at the end."""
        _, records = await self.failing_start(10, TimeoutError())
        levels = [r.levelname for r in records]
        self.assertEqual(levels.count("ERROR"), 1, levels)
        self.assertEqual(levels[-1], "INFO")
        self.assertIn("back after", records[-1].getMessage())
        warning = records[0].getMessage()
        self.assertIn("TimeoutError", warning)
        self.assertNotIn("()", warning)


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


class LibraryLogTest(LogDirTestCase):
    """What the platform libraries log that must not reach logs/."""

    def setUp(self):
        super().setUp()
        telegram_log = logging.getLogger("telegram")
        self.addCleanup(telegram_log.setLevel, telegram_log.level)
        self.addCleanup(logging.getLogger("discord.client").removeFilter, cli._not_voice_warning)

    @unittest.skipIf(telegram_bot is None, "python-telegram-bot is not installed")
    def test_debug_logging_keeps_the_telegram_token_out(self):
        """-v: python-telegram-bot logs its API URL, token included, at DEBUG."""
        cfg = Config(telegram=TelegramConfig(enabled=True, token="123456:sekrit-7f3a",
                                             allowed_user_ids=[1]))
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            cli._setup_logging(verbose=True)
            telegram_bot.build_application(cfg)
        self.assertNotIn("sekrit-7f3a", self.log_text())
        self.assertNotIn("sekrit-7f3a", err.getvalue())

    def test_the_missing_voice_library_warning_is_dropped(self):
        """The bot never joins voice; other discord.client warnings still get through."""
        client_log = logging.getLogger("discord.client")
        with contextlib.redirect_stderr(io.StringIO()):
            cli._setup_logging(verbose=False)
            # What discord.py's Client.__init__ logs without PyNaCl or davey.
            client_log.warning("PyNaCl is not installed, voice will NOT be supported")
            client_log.warning("davey is not installed, voice will NOT be supported")
            client_log.warning("Shard ID None heartbeat blocked for more than 10 seconds.")
        text = self.log_text()
        self.assertNotIn("voice will NOT be supported", text)
        self.assertIn("heartbeat blocked", text)


@unittest.skipIf(telegram_bot is None, "python-telegram-bot is not installed")
class TelegramLifecycleTest(unittest.TestCase):
    def test_post_init_registers_commands(self):
        """initialize()/start() alone never call post_init."""
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
        """No network at logon: shut the attempt down and try again."""
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

    def test_a_rejected_token_is_a_config_error_without_the_token(self):
        """The library quotes the token in its message, which would reach the log."""
        from telegram.error import InvalidToken

        app = FakeApp()

        async def rejected(*args, **kwargs):
            raise InvalidToken("The token `123456:sekrit-7f3a` was rejected by the server.")

        app.initialize = rejected
        with mock.patch.object(telegram_bot, "build_application", return_value=app):
            with self.assertRaises(ConfigError) as ctx:
                asyncio.run(cli._run_telegram(Config()))
        self.assertIn("rejected the bot token", str(ctx.exception))
        self.assertNotIn("sekrit-7f3a", str(ctx.exception))
        # Nor in a traceback that chains the original exception.
        self.assertTrue(ctx.exception.__suppress_context__)
        self.assertIn("shutdown", app.calls)

    def polling_failures(self, spacing, count):
        """Feed `count` failures, `spacing` seconds apart, to the polling error callback."""
        from telegram.error import NetworkError

        app = FakeApp()
        callbacks = []

        async def start_polling(**kwargs):
            callbacks.append(kwargs.get("error_callback"))

        app.updater.start_polling = start_polling

        async def run():
            task = asyncio.create_task(cli._run_telegram(Config()))
            await asyncio.sleep(0.05)
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

        with mock.patch.object(telegram_bot, "build_application", return_value=app):
            asyncio.run(run())
        on_error = callbacks[0]
        self.assertIsNotNone(on_error, "start_polling got no error_callback")

        clock = [1_000_000.0]
        with mock.patch.object(logs.time, "time", lambda: clock[0]), \
             self.assertLogs("clr.telegram", level="WARNING") as cm:
            for _ in range(count):
                on_error(NetworkError("httpx.ReadError: connection lost"))
                clock[0] += spacing
        return cm.records

    def test_polling_failures_minutes_apart_are_separate_outages(self):
        """Polling worked in between, so this is never "still down"."""
        records = self.polling_failures(spacing=4 * 60, count=5)
        self.assertEqual({r.levelname for r in records}, {"WARNING"})
        self.assertTrue(all("attempt 1," in r.getMessage() for r in records), records)

    def test_a_continuous_polling_outage_is_still_escalated(self):
        records = self.polling_failures(spacing=30, count=31)  # 15 minutes of retries
        self.assertEqual([r.levelname for r in records].count("ERROR"), 1)
        self.assertIn("still down after 15m", records[-1].getMessage())


@unittest.skipIf(telegram_bot is None, "python-telegram-bot is not installed")
class TelegramEditedMessageTest(unittest.IsolatedAsyncioTestCase):
    async def test_edited_command_still_gets_a_reply(self):
        """An edited "/usage" has update.message None."""
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
        """Unlisted users get no reply and no typing indicator."""
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
