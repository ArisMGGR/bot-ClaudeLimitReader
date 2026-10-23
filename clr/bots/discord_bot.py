"""Discord front-end: /usage, /status, and /authenticate."""

from __future__ import annotations

import asyncio
import datetime as dt
import logging

import discord
from discord import app_commands

from ..auth import (
    AuthError,
    PendingLogin,
    finish_login,
    load_tokens,
    save_tokens,
    start_login,
)
from ..config import DEFAULT_CONFIG_PATH, TOKEN_PATH, Config, ConfigError
from ..logs import Outage
from ..pace import humanize
from ..render import (
    _severity_icon,
    build_paces,
    overall_color,
    pace_block,
    window_line,
)
from ..usage import UsageError, fetch_usage
from . import retry_startup

log = logging.getLogger("clr.discord")


def build_embed(cfg: Config) -> discord.Embed:
    report = fetch_usage()
    embed = discord.Embed(
        title="Claude usage",
        color=overall_color(report, cfg.pace),
    )

    for window, pace in build_paces(report, cfg.pace):
        icon = _severity_icon(window)
        embed.add_field(
            name=f"{icon} {window.label}",
            value=f"```{window_line(window, pace)}```",
            inline=False,
        )

    embed.add_field(name="Pace", value=pace_block(report, cfg.pace), inline=False)

    footer = f"Checked {report.fetched_at:%Y-%m-%d %H:%M} UTC"
    if report.account_email:
        footer = f"{report.account_email} · {footer}"
    embed.set_footer(text=footer)
    return embed


class _ReconnectFilter(logging.Filter):
    """Swaps discord.py's per-retry traceback for the outage's one-liner.

    ``Client.connect`` logs every failed gateway reconnect with
    ``log.exception("Attempting a reconnect in %.2fs")``: some 60 lines of
    aiohttp/asyncio frames per attempt, a dozen attempts for a short network
    drop. The cause and the retry delay are all that line carries.
    """

    def __init__(self, outage: Outage) -> None:
        super().__init__()
        self.outage = outage

    def filter(self, record: logging.LogRecord) -> bool:
        exc = record.exc_info[1] if record.exc_info else None
        if exc is None or not str(record.msg).startswith("Attempting a reconnect"):
            return True
        retry = record.args[0] if isinstance(record.args, tuple) and record.args else None
        self.outage.failed(exc, retry_in=retry, now=record.created)
        return False


class UsageClient(discord.Client):
    def __init__(self, cfg: Config, outage: Outage | None = None):
        super().__init__(intents=discord.Intents.default())
        self.cfg = cfg
        self.tree = app_commands.CommandTree(self)
        self.started_at = dt.datetime.now(dt.timezone.utc)  # for /status
        self.outage = outage or Outage(log, "Discord gateway")

    async def setup_hook(self) -> None:
        self.tree.add_command(_make_usage_command(self.cfg))
        self.tree.add_command(_make_status_command(self.cfg))
        self.tree.add_command(_make_authenticate_command(self.cfg))

        guild_id = self.cfg.discord.guild_id
        if guild_id:
            # Guild-scoped commands appear at once; global ones can take ~1h.
            guild = discord.Object(id=guild_id)
            self.tree.copy_global_to(guild=guild)
            try:
                await self.tree.sync(guild=guild)
            except discord.DiscordServerError:
                raise  # transient; retry_startup tries again
            except discord.HTTPException as e:
                # A server the bot is not in (403) or no such server (404) is a
                # config mistake: say so and stay up, rather than end the bot
                # and the Telegram one beside it.
                log.error(
                    "could not register slash commands in guild %s (%s); check "
                    "discord.guild_id and that the bot was invited to that server "
                    "with the applications.commands scope, then restart",
                    guild_id, e,
                )
                return
            log.info("slash commands synced to guild %s", guild_id)
        else:
            await self.tree.sync()
            log.info("slash commands synced globally (may take up to an hour)")

    async def on_ready(self) -> None:
        self.outage.restored()
        log.info("connected as %s", self.user)

    async def on_resumed(self) -> None:
        # A successful RESUME after a drop fires this instead of on_ready.
        self.outage.restored()


def _make_usage_command(cfg: Config) -> app_commands.Command:
    allowed = set(cfg.discord.allowed_user_ids)

    @app_commands.command(name="usage", description="Show remaining Claude quota and burn rate")
    # Installable to a user account as well as a guild, and usable in DMs and
    # group DMs. A personal install needs no guild and therefore no permissions.
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def usage(interaction: discord.Interaction) -> None:
        if interaction.user.id not in allowed:
            log.warning("denied /usage for user id %s", interaction.user.id)
            await interaction.response.send_message(
                "Not authorized.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            # The usage fetch is blocking urllib; keep it off the event loop.
            embed = await asyncio.get_running_loop().run_in_executor(
                None, build_embed, cfg
            )
        except AuthError as e:
            await interaction.followup.send(f"⚠️ Auth problem\n```{e}```", ephemeral=True)
            return
        except UsageError as e:
            await interaction.followup.send(f"⚠️ Could not read usage\n```{e}```", ephemeral=True)
            return
        except Exception:
            log.exception("unexpected failure building usage embed")
            await interaction.followup.send(
                "⚠️ Unexpected error — check the bot logs.", ephemeral=True
            )
            return

        await interaction.followup.send(embed=embed, ephemeral=True)

    return usage


# ------------------------------------------------------------------- /status


def build_status_embed(cfg: Config, client: discord.Client) -> discord.Embed:
    """Report credential and bot health. Reads no secrets out of the token file."""
    tokens = load_tokens()
    now = dt.datetime.now(dt.timezone.utc)

    if tokens is None:
        embed = discord.Embed(
            title="Not logged in",
            description=(
                f"No credentials at `{TOKEN_PATH}`.\n"
                "Run `/authenticate` to authorize this bot."
            ),
            color=0xE03131,
        )
        return embed

    expires = dt.datetime.fromtimestamp(tokens.expires_at, dt.timezone.utc)
    if tokens.expired:
        # Normal and self-healing: the refresh token mints a new access token.
        token_state = "expired — renews on the next request"
        color = 0xF08C00
    else:
        token_state = f"valid for {humanize(expires - now)}"
        color = 0x2F9E44

    embed = discord.Embed(title="ClaudeLimitReader status", color=color)
    embed.add_field(
        name="Claude account",
        value=(
            f"**{tokens.account_email or 'unknown account'}**\n"
            f"Access token {token_state}\n"
            f"-# Scope: {tokens.scope or 'unknown'}"
        ),
        inline=False,
    )

    platforms = []
    if cfg.discord.enabled:
        scope = f"guild `{cfg.discord.guild_id}`" if cfg.discord.guild_id else "global"
        platforms.append(
            f"Discord — {len(cfg.discord.allowed_user_ids)} allowed user(s), {scope}"
        )
    if cfg.telegram.enabled:
        platforms.append(
            f"Telegram — {len(cfg.telegram.allowed_user_ids)} allowed user(s)"
        )
    # An empty field value is a 400 from Discord, so never leave one blank.
    embed.add_field(
        name="Platforms",
        value="\n".join(platforms) or "None enabled",
        inline=False,
    )

    started = getattr(client, "started_at", None)
    latency = client.latency  # seconds, or nan before the first heartbeat
    bot_lines = [f"Connected as **{client.user}**" if client.user else "Connecting"]
    if started is not None:
        bot_lines.append(f"Up for {humanize(now - started)}")
    if latency == latency:  # nan fails this, and formats as 'nan ms' otherwise
        bot_lines.append(f"Gateway latency {latency * 1000:.0f} ms")
    embed.add_field(name="Bot", value="\n".join(bot_lines), inline=False)

    embed.add_field(
        name="Files",
        value=f"-# Tokens `{TOKEN_PATH}`\n-# Config `{cfg.path or DEFAULT_CONFIG_PATH}`",
        inline=False,
    )
    embed.set_footer(text=f"Checked {now:%Y-%m-%d %H:%M} UTC")
    return embed


def _make_status_command(cfg: Config) -> app_commands.Command:
    allowed = set(cfg.discord.allowed_user_ids)

    @app_commands.command(
        name="status",
        description="Show credential and bot health",
    )
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def status(interaction: discord.Interaction) -> None:
        if interaction.user.id not in allowed:
            log.warning("denied /status for user id %s", interaction.user.id)
            await interaction.response.send_message("Not authorized.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            # Off the event loop: load_tokens waits on the token lock, which a
            # refresh holds for as long as the token endpoint takes to answer.
            embed = await asyncio.to_thread(build_status_embed, cfg, interaction.client)
        except AuthError as e:
            await interaction.followup.send(f"⚠️ Auth problem\n```{e}```", ephemeral=True)
            return
        except Exception:
            log.exception("unexpected failure building status embed")
            await interaction.followup.send(
                "⚠️ Unexpected error — check the bot logs.", ephemeral=True
            )
            return

        await interaction.followup.send(embed=embed, ephemeral=True)

    return status


# ------------------------------------------------------------- /authenticate
#
# The same authorization-code + PKCE login `python run.py login` runs, driven from
# Discord so a host you cannot easily get a terminal on can be re-authorized
# when its refresh token lapses. The authorization code is a credential: it
# arrives through a modal, every reply here is ephemeral, and it is never
# logged.

LOGIN_TTL = dt.timedelta(minutes=10)


class _CodeModal(discord.ui.Modal, title="Finish the Claude login"):
    code: discord.ui.TextInput = discord.ui.TextInput(
        label="Authorization code",
        placeholder="Paste the code from the approval page",
        style=discord.TextStyle.short,
        max_length=512,
        required=True,
    )

    def __init__(self, pending: PendingLogin, started_at: dt.datetime):
        super().__init__()
        self.pending = pending
        self.started_at = started_at

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)

        if dt.datetime.now(dt.timezone.utc) - self.started_at > LOGIN_TTL:
            await interaction.followup.send(
                "⌛ That login attempt has expired. Run `/authenticate` again.",
                ephemeral=True,
            )
            return

        try:
            # Blocking urllib; keep the token exchange off the event loop.
            tokens = await asyncio.to_thread(
                finish_login, self.pending, self.code.value
            )
            await asyncio.to_thread(save_tokens, tokens)
        except AuthError as e:
            log.warning("discord login failed: %s", e)
            await interaction.followup.send(f"⚠️ {e}", ephemeral=True)
            return
        except Exception:
            log.exception("unexpected failure finishing the login")
            await interaction.followup.send(
                "⚠️ Unexpected error — check the bot logs.", ephemeral=True
            )
            return

        who = f" as **{tokens.account_email}**" if tokens.account_email else ""
        log.info("re-authorized via discord%s", who and f" ({tokens.account_email})")
        await interaction.followup.send(
            f"✅ Logged in{who}. Tokens stored at `{TOKEN_PATH}`.\n"
            "`/usage` works again straight away — no restart needed.",
            ephemeral=True,
        )

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        log.exception("modal error", exc_info=error)


class _LoginView(discord.ui.View):
    """The button that opens the code modal, usable only by who asked for it."""

    def __init__(self, pending: PendingLogin, user_id: int):
        super().__init__(timeout=LOGIN_TTL.total_seconds())
        self.pending = pending
        self.user_id = user_id
        self.started_at = dt.datetime.now(dt.timezone.utc)

    @discord.ui.button(label="Enter code", style=discord.ButtonStyle.primary)
    async def enter_code(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("Not authorized.", ephemeral=True)
            return
        await interaction.response.send_modal(
            _CodeModal(self.pending, self.started_at)
        )


def _make_authenticate_command(cfg: Config) -> app_commands.Command:
    allowed = set(cfg.discord.allowed_user_ids)

    @app_commands.command(
        name="authenticate",
        description="Re-authorize this bot against your Claude account",
    )
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def authenticate(interaction: discord.Interaction) -> None:
        if interaction.user.id not in allowed:
            log.warning("denied /authenticate for user id %s", interaction.user.id)
            await interaction.response.send_message("Not authorized.", ephemeral=True)
            return

        pending = start_login()  # local PKCE work only, nothing to await
        await interaction.response.send_message(
            "**Authorize this bot**\n"
            f"1. Open this link and approve:\n<{pending.url}>\n"
            "2. Copy the code the page shows.\n"
            "3. Press **Enter code** below and paste it.\n\n"
            "-# Only you can see this. The link expires in 10 minutes, and "
            "finishing replaces the credentials the bot is using now.",
            view=_LoginView(pending, interaction.user.id),
            ephemeral=True,
            suppress_embeds=True,
        )

    return authenticate


async def run(cfg: Config) -> None:
    outage = Outage(log, "Discord gateway")

    async def login() -> UsageClient:
        # A fresh client per attempt; login() also runs setup_hook's sync.
        client = UsageClient(cfg, outage)
        try:
            await client.login(cfg.discord.token)
        except BaseException:
            await client.close()
            raise
        return client

    # OSError covers aiohttp's connection errors. connect() then handles its
    # own reconnects; a bad token (LoginFailure) is not retried.
    try:
        client = await retry_startup(
            "discord", login, (OSError, discord.DiscordServerError, discord.GatewayNotFound)
        )
    except discord.LoginFailure:
        # An "Error:" line like Telegram's rejected token, not a traceback.
        raise ConfigError(
            "Discord rejected the bot token; check discord.token (or "
            "CLR_DISCORD_TOKEN), and reset it on the Developer Portal's Bot tab if it was revoked"
        ) from None
    reconnects = _ReconnectFilter(outage)
    client_log = logging.getLogger("discord.client")
    client_log.addFilter(reconnects)
    try:
        await client.connect(reconnect=True)
    finally:
        client_log.removeFilter(reconnects)
        await client.close()
