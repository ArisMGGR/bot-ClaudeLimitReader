"""Tests for the /authenticate flow: who may use it, and what it does.

These drive the command callbacks against stub interactions; nothing here
connects to Discord.
"""

import asyncio
import datetime as dt
import logging
import socket
import sys
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from clr.auth import AuthError, Tokens  # noqa: E402
from clr.config import Config, ConfigError, DiscordConfig, TelegramConfig  # noqa: E402

try:
    from clr.bots import discord_bot
except ImportError:  # discord.py is optional; skip rather than fail the suite.
    discord_bot = None

UTC = dt.timezone.utc
OWNER = 4242
STRANGER = 9999


def cfg() -> Config:
    return Config(
        discord=DiscordConfig(enabled=True, token="t", allowed_user_ids=[OWNER])
    )


class StubResponse:
    def __init__(self):
        self.messages = []
        self.modals = []
        self.deferred = False
        # A deferred reply's visibility is set here, not by the followup's own
        # ephemeral flag, so tests of privacy must look at these.
        self.defer_kwargs = None

    async def send_message(self, content=None, **kwargs):
        self.messages.append((content, kwargs))

    async def send_modal(self, modal):
        self.modals.append(modal)

    async def defer(self, **kwargs):
        self.deferred = True
        self.defer_kwargs = kwargs


class StubFollowup:
    def __init__(self):
        self.messages = []

    async def send(self, content=None, **kwargs):
        self.messages.append((content, kwargs))


class StubInteraction:
    def __init__(self, user_id):
        self.user = mock.Mock(id=user_id)
        self.response = StubResponse()
        self.followup = StubFollowup()


def tokens(email="owner@example.com", expires_at=None) -> Tokens:
    if expires_at is None:
        expires_at = (dt.datetime.now(UTC) + dt.timedelta(hours=8)).timestamp()
    return Tokens(
        access_token="access-token-secret",
        refresh_token="refresh-token-secret",
        expires_at=expires_at,
        scope="user:inference",
        account_email=email,
    )


@unittest.skipIf(discord_bot is None, "discord.py is not installed")
class AuthenticateCommandTest(unittest.IsolatedAsyncioTestCase):
    async def invoke(self, user_id):
        command = discord_bot._make_authenticate_command(cfg())
        interaction = StubInteraction(user_id)
        await command.callback(interaction)
        return interaction

    async def test_stranger_is_refused_and_logged(self):
        with self.assertLogs(discord_bot.log, level="WARNING") as logs:
            interaction = await self.invoke(STRANGER)
        content, kwargs = interaction.response.messages[0]
        self.assertEqual(content, "Not authorized.")
        self.assertTrue(kwargs["ephemeral"])
        self.assertTrue(any(str(STRANGER) in line for line in logs.output), logs.output)

    async def test_owner_gets_the_link_privately(self):
        interaction = await self.invoke(OWNER)
        content, kwargs = interaction.response.messages[0]
        self.assertIn("https://", content)
        self.assertTrue(kwargs["ephemeral"])
        self.assertIsInstance(kwargs["view"], discord_bot._LoginView)

    async def test_each_run_is_its_own_pkce_attempt(self):
        first = await self.invoke(OWNER)
        second = await self.invoke(OWNER)
        a = first.response.messages[0][1]["view"].pending
        b = second.response.messages[0][1]["view"].pending
        self.assertNotEqual(a.code_verifier, b.code_verifier)
        self.assertNotEqual(a.state, b.state)


@unittest.skipIf(discord_bot is None, "discord.py is not installed")
class UsageCommandTest(unittest.IsolatedAsyncioTestCase):
    """Only allowed_user_ids see the quota report."""

    async def invoke(self, user_id):
        command = discord_bot._make_usage_command(cfg())
        interaction = StubInteraction(user_id)
        with mock.patch.object(discord_bot, "build_embed", return_value="embed") as build:
            await command.callback(interaction)
        return interaction, build

    async def test_stranger_is_refused_and_logged(self):
        with self.assertLogs(discord_bot.log, level="WARNING") as logs:
            interaction, build = await self.invoke(STRANGER)
        build.assert_not_called()
        self.assertFalse(interaction.response.deferred)
        self.assertEqual(interaction.followup.messages, [])
        content, kwargs = interaction.response.messages[0]
        self.assertEqual(content, "Not authorized.")
        self.assertTrue(kwargs["ephemeral"])
        self.assertTrue(any(str(STRANGER) in line for line in logs.output), logs.output)

    async def test_owner_gets_the_report_privately(self):
        interaction, build = await self.invoke(OWNER)
        build.assert_called_once()
        self.assertTrue(interaction.response.defer_kwargs["ephemeral"])
        content, kwargs = interaction.followup.messages[0]
        self.assertEqual(kwargs["embed"], "embed")
        self.assertTrue(kwargs["ephemeral"])


class StubClient:
    def __init__(self, user="clr#0001", latency=0.042, started_at=None):
        self.user = user
        self.latency = latency
        if started_at is not None:
            self.started_at = started_at


@unittest.skipIf(discord_bot is None, "discord.py is not installed")
class StatusCommandTest(unittest.IsolatedAsyncioTestCase):
    async def invoke(self, user_id, stored=None):
        command = discord_bot._make_status_command(cfg())
        interaction = StubInteraction(user_id)
        interaction.client = StubClient(
            started_at=dt.datetime.now(UTC) - dt.timedelta(hours=3)
        )
        with mock.patch.object(discord_bot, "load_tokens", return_value=stored):
            await command.callback(interaction)
        return interaction

    async def test_stranger_is_refused_privately_and_logged(self):
        with self.assertLogs(discord_bot.log, level="WARNING") as logs:
            interaction = await self.invoke(STRANGER, stored=tokens())
        content, kwargs = interaction.response.messages[0]
        self.assertEqual(content, "Not authorized.")
        self.assertTrue(kwargs["ephemeral"])
        self.assertFalse(interaction.response.deferred)
        self.assertTrue(any(str(STRANGER) in line for line in logs.output), logs.output)

    async def test_reply_is_ephemeral(self):
        interaction = await self.invoke(OWNER, stored=tokens())
        self.assertTrue(interaction.response.defer_kwargs["ephemeral"])
        self.assertTrue(interaction.followup.messages[0][1]["ephemeral"])

    async def test_logged_out_points_at_authenticate(self):
        interaction = await self.invoke(OWNER, stored=None)
        embed = interaction.followup.messages[0][1]["embed"]
        self.assertEqual(embed.title, "Not logged in")
        self.assertIn("/authenticate", embed.description)

    async def test_tokens_are_read_off_the_event_loop(self):
        """A refresh holds the token lock; waiting for it must not stall the bot."""
        threads = []

        def load_tokens():
            threads.append(threading.get_ident())
            return tokens()

        command = discord_bot._make_status_command(cfg())
        interaction = StubInteraction(OWNER)
        interaction.client = StubClient()
        with mock.patch.object(discord_bot, "load_tokens", load_tokens):
            await command.callback(interaction)
        self.assertEqual(len(threads), 1)
        self.assertNotEqual(threads[0], threading.get_ident())


@unittest.skipIf(discord_bot is None, "discord.py is not installed")
class StatusEmbedTest(unittest.TestCase):
    def build(self, stored, **client_kwargs):
        client = StubClient(**client_kwargs)
        with mock.patch.object(discord_bot, "load_tokens", return_value=stored):
            return discord_bot.build_status_embed(cfg(), client)

    def fields(self, embed):
        return {f.name: f.value for f in embed.fields}

    def test_live_token_shows_how_long_it_lasts(self):
        soon = dt.datetime.now(UTC) + dt.timedelta(hours=7, minutes=30)
        embed = self.build(tokens(expires_at=soon.timestamp()))
        self.assertIn("valid for 7h 29m", self.fields(embed)["Claude account"])

    def test_expired_token_is_explained_as_self_healing(self):
        embed = self.build(tokens(expires_at=0))
        account = self.fields(embed)["Claude account"]
        self.assertIn("expired", account)
        self.assertIn("renews", account)

    def test_never_prints_the_tokens_themselves(self):
        stored = tokens()
        embed = self.build(stored)
        blob = repr(embed.to_dict())
        self.assertNotIn(stored.access_token, blob)
        self.assertNotIn(stored.refresh_token, blob)

    def test_lists_both_platforms(self):
        client = StubClient()
        both = Config(
            discord=DiscordConfig(enabled=True, token="t", allowed_user_ids=[OWNER],
                                  guild_id=7),
            telegram=TelegramConfig(enabled=True, token="t", allowed_user_ids=[1, 2]),
        )
        with mock.patch.object(discord_bot, "load_tokens", return_value=tokens()):
            embed = discord_bot.build_status_embed(both, client)
        platforms = self.fields(embed)["Platforms"]
        self.assertIn("Discord", platforms)
        self.assertIn("guild `7`", platforms)
        self.assertIn("Telegram", platforms)

    def test_no_field_is_ever_empty(self):
        """Discord answers 400 on a blank field value."""
        for label, conf in (
            ("both", cfg()),
            ("neither platform enabled", Config()),
        ):
            with self.subTest(label):
                with mock.patch.object(discord_bot, "load_tokens", return_value=tokens()):
                    embed = discord_bot.build_status_embed(conf, StubClient())
                for field in embed.to_dict().get("fields", []):
                    self.assertTrue(field["value"], f'{label}: {field["name"]} is empty')

    def test_uptime_and_latency_when_known(self):
        embed = self.build(
            tokens(), started_at=dt.datetime.now(UTC) - dt.timedelta(minutes=90)
        )
        bot = self.fields(embed)["Bot"]
        self.assertIn("Up for 1h 30m", bot)
        self.assertIn("42 ms", bot)

    def test_files_field_shows_the_config_actually_loaded(self):
        conf = cfg()
        conf.path = Path("/srv/clr/elsewhere.toml")
        with mock.patch.object(discord_bot, "load_tokens", return_value=tokens()):
            embed = discord_bot.build_status_embed(conf, StubClient())
        self.assertIn(str(conf.path), self.fields(embed)["Files"])

    def test_latency_before_the_first_heartbeat_is_omitted(self):
        embed = self.build(tokens(), latency=float("nan"))
        self.assertNotIn("nan", self.fields(embed)["Bot"])

    def test_uptime_omitted_when_the_client_has_none(self):
        embed = self.build(tokens())  # StubClient without started_at
        self.assertNotIn("Up for", self.fields(embed)["Bot"])


@unittest.skipIf(discord_bot is None, "discord.py is not installed")
class LoginViewTest(unittest.IsolatedAsyncioTestCase):
    def view(self):
        return discord_bot._LoginView(discord_bot.start_login(), OWNER)

    async def test_only_the_requester_may_open_the_modal(self):
        view = self.view()
        interaction = StubInteraction(STRANGER)
        await view.enter_code.callback(interaction)
        content, kwargs = interaction.response.messages[0]
        self.assertEqual(content, "Not authorized.")
        self.assertTrue(kwargs["ephemeral"])
        self.assertEqual(interaction.response.modals, [])

    async def test_requester_gets_the_modal(self):
        view = self.view()
        interaction = StubInteraction(OWNER)
        await view.enter_code.callback(interaction)
        self.assertIsInstance(interaction.response.modals[0], discord_bot._CodeModal)


@unittest.skipIf(discord_bot is None, "discord.py is not installed")
class CodeModalTest(unittest.IsolatedAsyncioTestCase):
    def modal(self, age=dt.timedelta(0), code="the-code#state"):
        started = dt.datetime.now(UTC) - age
        modal = discord_bot._CodeModal(discord_bot.start_login(), started)
        modal.code._value = code
        return modal

    async def test_successful_exchange_is_saved(self):
        modal = self.modal()
        interaction = StubInteraction(OWNER)
        with mock.patch.object(discord_bot, "finish_login", return_value=tokens()) as fin:
            with mock.patch.object(discord_bot, "save_tokens") as save:
                await modal.on_submit(interaction)

        fin.assert_called_once()
        self.assertEqual(fin.call_args.args[1], "the-code#state")
        save.assert_called_once()
        content, kwargs = interaction.followup.messages[0]
        self.assertIn("Logged in", content)
        self.assertIn("owner@example.com", content)
        self.assertTrue(kwargs["ephemeral"])

    async def test_expired_attempt_is_rejected_before_any_exchange(self):
        modal = self.modal(age=discord_bot.LOGIN_TTL + dt.timedelta(minutes=1))
        interaction = StubInteraction(OWNER)
        with mock.patch.object(discord_bot, "finish_login") as fin:
            with mock.patch.object(discord_bot, "save_tokens") as save:
                await modal.on_submit(interaction)

        fin.assert_not_called()
        save.assert_not_called()
        self.assertIn("expired", interaction.followup.messages[0][0])

    async def test_bad_code_reports_the_error_and_saves_nothing(self):
        modal = self.modal(code="nonsense")
        interaction = StubInteraction(OWNER)
        with mock.patch.object(
            discord_bot, "finish_login", side_effect=AuthError("Could not exchange")
        ):
            with mock.patch.object(discord_bot, "save_tokens") as save:
                await modal.on_submit(interaction)

        save.assert_not_called()
        self.assertIn("Could not exchange", interaction.followup.messages[0][0])

    async def test_reply_is_always_ephemeral(self):
        for code, patch in (
            ("good", mock.patch.object(discord_bot, "finish_login", return_value=tokens())),
            ("bad", mock.patch.object(discord_bot, "finish_login", side_effect=AuthError("no"))),
        ):
            with self.subTest(code=code):
                interaction = StubInteraction(OWNER)
                with patch, mock.patch.object(discord_bot, "save_tokens"):
                    await self.modal(code=code).on_submit(interaction)
                self.assertTrue(interaction.response.defer_kwargs["ephemeral"])
                self.assertTrue(interaction.followup.messages[0][1]["ephemeral"])

    async def test_the_code_never_reaches_the_log(self):
        """Neither when the code is accepted nor when it is rejected."""
        for outcome, patch in (
            ("accepted", mock.patch.object(discord_bot, "finish_login", return_value=tokens())),
            ("rejected", mock.patch.object(discord_bot, "finish_login",
                                           side_effect=AuthError("rejected"))),
        ):
            with self.subTest(outcome):
                modal = self.modal(code="secret-code#state")
                interaction = StubInteraction(OWNER)
                with patch, mock.patch.object(discord_bot, "save_tokens"):
                    # The root logger: a record on any logger, not just clr.discord.
                    with self.assertLogs(level="DEBUG") as logs:
                        await modal.on_submit(interaction)
                self.assertNotIn("secret-code", "\n".join(logs.output))


@unittest.skipIf(discord_bot is None, "discord.py is not installed")
class StartupRetryTest(unittest.IsolatedAsyncioTestCase):
    """No network at logon must not end the bot; a bad token still does."""

    def fake_client(self, outcomes, log):
        class FakeClient:
            def __init__(self, cfg, outage=None):
                log.append("new")

            async def login(self, token):
                outcome = outcomes.pop(0)
                if outcome is not None:
                    raise outcome

            async def connect(self, reconnect=True):
                log.append("connect")

            async def close(self):
                log.append("close")

        return FakeClient

    async def test_a_connection_error_is_retried_with_a_fresh_client(self):
        from clr import bots

        log = []
        fake = self.fake_client([OSError("no route"), None], log)
        with mock.patch.object(discord_bot, "UsageClient", fake), \
             mock.patch.object(bots, "FIRST_RETRY_DELAY", 0), \
             self.assertLogs("clr", level="WARNING"):
            await discord_bot.run(cfg())
        self.assertEqual(log, ["new", "close", "new", "connect", "close"])

    async def test_a_rejected_token_is_not_retried(self):
        """It ends the run as a ConfigError (an Error line), not a traceback."""
        import discord

        log = []
        fake = self.fake_client([discord.LoginFailure("Improper token")], log)
        with mock.patch.object(discord_bot, "UsageClient", fake):
            with self.assertRaises(ConfigError) as ctx:
                await discord_bot.run(cfg())
        self.assertEqual(log, ["new", "close"])
        self.assertIn("discord.token", str(ctx.exception))
        self.assertTrue(ctx.exception.__suppress_context__)

    async def test_reconnects_during_connect_are_one_line_warnings(self):
        """run() itself installs the reconnect filter, and removes it after."""
        client_log = logging.getLogger("discord.client")
        filters_before = list(client_log.filters)

        class FakeClient:
            def __init__(self, cfg, outage=None):
                pass

            async def login(self, token):
                pass

            async def connect(self, reconnect=True):
                # What discord.py's Client.connect logs for each failed reconnect.
                try:
                    raise OSError("Cannot connect to host gateway.discord.gg:443")
                except OSError:
                    client_log.exception("Attempting a reconnect in %.2fs", 2.5)

            async def close(self):
                pass

        with mock.patch.object(discord_bot, "UsageClient", FakeClient), \
             self.assertLogs("clr.discord", level="WARNING") as clr_logs, \
             self.assertNoLogs("discord.client", level="DEBUG"):
            await discord_bot.run(cfg())
        self.assertEqual(len(clr_logs.records), 1)
        record = clr_logs.records[0]
        self.assertIn("Discord gateway unreachable", record.getMessage())
        self.assertIn("retrying in 2s", record.getMessage())
        self.assertIsNone(record.exc_info)
        self.assertEqual(client_log.filters, filters_before)


@unittest.skipIf(discord_bot is None, "discord.py is not installed")
class GuildSyncTest(unittest.TestCase):
    """A wrong guild_id is logged, not fatal; a Discord outage is still retried."""

    def sync_with(self, error, status):
        conf = cfg()
        conf.discord.guild_id = 1234
        client = discord_bot.UsageClient(conf)
        response = mock.Mock(status=status, reason="")
        with mock.patch.object(client.tree, "sync",
                               side_effect=error(response, {"code": 0, "message": "x"})):
            asyncio.run(client.setup_hook())

    def test_a_guild_the_bot_cannot_reach_is_an_error_line(self):
        import discord

        for error, status in ((discord.Forbidden, 403), (discord.NotFound, 404)):
            with self.subTest(status), self.assertLogs(discord_bot.log, level="ERROR") as logs:
                self.sync_with(error, status)
            self.assertIn("1234", logs.output[0])
            self.assertIn("discord.guild_id", logs.output[0])

    def test_a_server_error_still_reaches_the_startup_retry(self):
        import discord

        with self.assertRaises(discord.DiscordServerError):
            self.sync_with(discord.DiscordServerError, 503)


@unittest.skipIf(discord_bot is None, "discord.py is not installed")
class ReconnectLogTest(unittest.TestCase):
    """A gateway reconnect logs one line, not discord.py's traceback."""

    def setUp(self):
        from clr.logs import Outage

        self.outage = Outage(discord_bot.log, "Discord gateway")
        self.filter = discord_bot._ReconnectFilter(self.outage)
        self.client_log = logging.getLogger("discord.client")
        self.client_log.addFilter(self.filter)
        self.addCleanup(self.client_log.removeFilter, self.filter)
        self.old_level = self.client_log.level
        self.client_log.setLevel(logging.DEBUG)
        self.addCleanup(self.client_log.setLevel, self.old_level)

    def reconnect(self):
        try:
            try:
                raise socket.gaierror(-3, "Temporary failure in name resolution")
            except OSError as e:
                raise OSError(
                    "Cannot connect to host gateway.discord.gg:443 ssl:default "
                    "[Temporary failure in name resolution]"
                ) from e
        except OSError:
            self.client_log.exception("Attempting a reconnect in %.2fs", 1.21)

    def test_reconnect_becomes_a_one_line_warning(self):
        with self.assertLogs("clr.discord", level="DEBUG") as clr_logs, \
             self.assertNoLogs("discord.client", level="DEBUG"):
            self.reconnect()
        self.assertEqual(len(clr_logs.records), 1)
        record = clr_logs.records[0]
        self.assertEqual(record.levelname, "WARNING")
        self.assertIsNone(record.exc_info)
        self.assertIn("name resolution", record.getMessage())
        self.assertIn("retrying in 1s", record.getMessage())

    def test_other_discord_client_records_pass_through(self):
        with self.assertLogs("discord.client", level="ERROR"):
            try:
                raise ValueError("bug")
            except ValueError:
                self.client_log.exception("Ignoring exception in on_message")

    def back_lines(self, event):
        """Two failed reconnects, then `event` fired twice: the outage closes once."""
        client = discord_bot.UsageClient(cfg(), self.outage)
        with self.assertLogs("clr.discord", level="INFO") as clr_logs:
            self.reconnect()
            self.reconnect()
            asyncio.run(getattr(client, event)())
            asyncio.run(getattr(client, event)())
        return [r.getMessage() for r in clr_logs.records if "back after" in r.getMessage()]

    def test_resumed_reports_the_outage_once(self):
        back = self.back_lines("on_resumed")
        self.assertEqual(len(back), 1, back)
        self.assertIn("2 failed attempt(s)", back[0])

    def test_ready_reports_the_outage_once(self):
        """A session Discord will not resume ends in READY instead."""
        back = self.back_lines("on_ready")
        self.assertEqual(len(back), 1, back)
        self.assertIn("2 failed attempt(s)", back[0])


if __name__ == "__main__":
    unittest.main()
