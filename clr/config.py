"""Configuration loading and on-disk paths."""

from __future__ import annotations

import math
import os
import tempfile
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


class ConfigError(Exception):
    """Raised when a config file named explicitly is missing, or any is malformed."""


def expand_path(raw, what: str) -> Path:
    """Path(raw).expanduser(), with an unknown ~user as a ConfigError.

    On POSIX, `~name` for an account that does not exist raises RuntimeError,
    which would otherwise end the command in a traceback.
    """
    try:
        return Path(raw).expanduser()
    except RuntimeError:
        raise ConfigError(f"{what}{raw}: no user's home directory matches that ~") from None


# The error for each of CLR_HOME / CLR_CONFIG that cannot be expanded. This
# module is imported before logging starts, so main() raises it once the log
# file is open.
_PATH_ERRORS: dict[str, ConfigError] = {}


def _env_path(name: str, default: Path) -> Path:
    # An empty variable counts as unset, as it does for the tokens; Path("")
    # would otherwise mean the current directory.
    raw = os.environ.get(name) or default
    try:
        path = expand_path(raw, f"{name}=")
    except ConfigError as e:
        _PATH_ERRORS[name] = e
        path = Path(raw)  # a placeholder only; main() refuses to use it
    # Absolute: a service runs from another working directory, and systemd
    # ignores relative paths.
    return path.absolute()


APP_DIR = _env_path("CLR_HOME", Path.home() / ".claudelimitreader")
TOKEN_PATH = APP_DIR / "tokens.json"
DEFAULT_CONFIG_PATH = _env_path("CLR_CONFIG", APP_DIR / "config.toml")

# Every command uses the app dir, so a bad CLR_HOME stops them all; a bad
# CLR_CONFIG stops only those that read the default config.
PATH_ERROR = _PATH_ERRORS.get("CLR_HOME")
CONFIG_PATH_ERROR = _PATH_ERRORS.get("CLR_CONFIG")

# OAuth client identity used by Claude Code. The bot performs its own
# authorization against this client so it never shares a refresh-token lineage
# with a local Claude Code install.
CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
SCOPES = "user:profile user:inference"

API_BASE = "https://api.anthropic.com"
USER_AGENT = "claudelimitreader/1.0"

# Refreshed access tokens live ~8h; renew a little early to avoid edge races.
REFRESH_SKEW_SECONDS = 300


@dataclass
class DiscordConfig:
    enabled: bool = False
    token: str = ""
    allowed_user_ids: list[int] = field(default_factory=list)
    # Optional. When set, slash commands are registered to just this guild,
    # which takes effect immediately instead of the ~1h global propagation.
    guild_id: int | None = None


@dataclass
class TelegramConfig:
    enabled: bool = False
    token: str = ""
    allowed_user_ids: list[int] = field(default_factory=list)


@dataclass
class PaceConfig:
    # A window is flagged as overspending once used% exceeds elapsed% by more
    # than this many percentage points.
    tolerance_points: float = 5.0
    # Ratio at which the verdict escalates from "ahead" to "critical".
    critical_ratio: float = 1.5


@dataclass
class Config:
    discord: DiscordConfig = field(default_factory=DiscordConfig)
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    pace: PaceConfig = field(default_factory=PaceConfig)
    # The file this was read from, for status reports; None when built in code.
    path: Path | None = None

    @property
    def any_platform_enabled(self) -> bool:
        return self.discord.enabled or self.telegram.enabled


def _int(item, where: str) -> int:
    """An integer ID, or a string of digits. Booleans and fractions are refused."""
    if isinstance(item, bool) or (isinstance(item, float) and not item.is_integer()):
        raise ConfigError(f"{where} contains a non-integer entry: {item!r}")
    try:
        return int(item)
    except (TypeError, ValueError):
        raise ConfigError(f"{where} contains a non-integer entry: {item!r}") from None


def _as_int_list(raw, where: str) -> list[int]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ConfigError(f"{where} must be a list of integer user IDs")
    return [_int(item, where) for item in raw]


def _section(data: dict, name: str) -> dict:
    section = data.get(name, {})
    if not isinstance(section, dict):
        raise ConfigError(f"[{name}] must be a table")
    return section


def _bool(section: dict, key: str, where: str) -> bool:
    value = section.get(key, False)
    if not isinstance(value, bool):
        # bool("false") is True; insist on a real TOML boolean instead.
        raise ConfigError(f"{where} must be true or false (unquoted), not {value!r}")
    return value


def _str(section: dict, key: str, where: str) -> str:
    value = section.get(key, "")
    if not isinstance(value, str):
        raise ConfigError(f"{where} must be a quoted string")
    return value


def _token(section: dict, name: str) -> str:
    """A bot token: CLR_<NAME>_TOKEN if set, else the file's, without surrounding whitespace.

    python-telegram-bot puts the token in the request URL verbatim, so a stray
    space or a CR from a CRLF file would otherwise reach Telegram.
    """
    env = os.environ.get(f"CLR_{name.upper()}_TOKEN", "").strip()
    return env or _str(section, "token", f"{name}.token").strip()


def _number(section: dict, key: str, default: float, where: str) -> float:
    value = section.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{where} must be a number, not {value!r}")
    try:
        number = float(value)
    except OverflowError:  # an integer too large for a float
        raise ConfigError(f"{where} is too large to be a number") from None
    if not math.isfinite(number):
        raise ConfigError(f"{where} must be a finite number, not {value!r}")
    return number


EMPTY_CONFIG_PATH = "-c was given an empty path; name a config file, or leave -c out"


def load_config(path: Path | None = None, *, check_platforms: bool = True) -> Config:
    """Read config.toml, applying environment overrides for the bot tokens.

    A missing default file reads as an empty config; a path given explicitly
    (-c) must exist. check_platforms=False skips the token and allowed-user
    checks, for commands that never start a bot.
    """
    explicit = path is not None
    if explicit and not str(path).strip():
        # `-c "$UNSET"` must not quietly fall back to the default config.
        raise ConfigError(EMPTY_CONFIG_PATH)
    # expanduser, as for CLR_CONFIG: a quoted -c "~/x.toml" reaches us unexpanded.
    path = expand_path(path, "-c ") if explicit else DEFAULT_CONFIG_PATH
    if explicit and not path.exists():
        raise ConfigError(f"{path} does not exist")

    data: dict = {}
    if path.exists():
        try:
            raw = path.read_bytes()
        except OSError as e:  # a directory, or no permission
            raise ConfigError(f"cannot read {path}: {e}") from e
        try:
            # Some Windows editors prefix UTF-8 with a BOM, which TOML forbids.
            text = raw.removeprefix(b"\xef\xbb\xbf").decode("utf-8")
        except UnicodeDecodeError as e:
            raise ConfigError(f"{path} is not UTF-8 text ({e}); save it as UTF-8") from e
        try:
            data = tomllib.loads(text)
        except tomllib.TOMLDecodeError as e:
            raise ConfigError(f"{path} is not valid TOML: {e}") from e

    d = _section(data, "discord")
    t = _section(data, "telegram")
    p = _section(data, "pace")

    guild_id = _int(d["guild_id"], "discord.guild_id") if d.get("guild_id") else None

    discord = DiscordConfig(
        enabled=_bool(d, "enabled", "discord.enabled"),
        token=_token(d, "discord"),
        allowed_user_ids=_as_int_list(d.get("allowed_user_ids"), "discord.allowed_user_ids"),
        guild_id=guild_id,
    )
    telegram = TelegramConfig(
        enabled=_bool(t, "enabled", "telegram.enabled"),
        token=_token(t, "telegram"),
        allowed_user_ids=_as_int_list(t.get("allowed_user_ids"), "telegram.allowed_user_ids"),
    )
    pace = PaceConfig(
        tolerance_points=_number(p, "tolerance_points", 5.0, "pace.tolerance_points"),
        critical_ratio=_number(p, "critical_ratio", 1.5, "pace.critical_ratio"),
    )
    if pace.tolerance_points < 0:
        raise ConfigError("pace.tolerance_points must not be negative")
    if pace.critical_ratio <= 0:
        raise ConfigError("pace.critical_ratio must be greater than 0")

    cfg = Config(discord=discord, telegram=telegram, pace=pace, path=path)

    # Fail loudly rather than starting a bot that silently answers nobody.
    for name, section in (("discord", cfg.discord), ("telegram", cfg.telegram)):
        if section.enabled and check_platforms:
            if not section.token:
                raise ConfigError(
                    f"{name}.enabled is true but no token is set "
                    f"(config file or CLR_{name.upper()}_TOKEN)"
                )
            if not section.allowed_user_ids:
                raise ConfigError(
                    f"{name}.enabled is true but {name}.allowed_user_ids is empty; "
                    "refusing to expose account usage to everyone"
                )

    return cfg


def write_private(path: Path, data: bytes) -> None:
    """Write a file atomically and owner-only from the moment it exists.

    For anything holding a secret: tokens.json, config.toml and its backup, the
    service's environment file. A unique temp name keeps concurrent writers
    (the service and a CLI command) from sharing a half-written file.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f"{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        try:
            os.chmod(tmp, 0o600)
        except (OSError, NotImplementedError):
            pass  # Windows ACLs; best effort only.
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def ensure_app_dir() -> Path:
    """Create the app directory with owner-only permissions where supported."""
    APP_DIR.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(APP_DIR, 0o700)
    except (OSError, NotImplementedError):
        pass  # Windows ACLs; best effort only.
    return APP_DIR
