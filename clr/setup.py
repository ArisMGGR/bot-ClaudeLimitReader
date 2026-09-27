"""Interactive first-run setup: log in, write config.toml, verify it works.

Everything here is also doable by hand (`python run.py login` plus a copied
config.example.toml); this just walks through it in one pass and validates the
result before you find out at `python run.py run` time.
"""

from __future__ import annotations

import datetime as dt
import getpass
import math
import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

from .auth import (
    AuthError,
    finish_login,
    import_local_credentials,
    load_tokens,
    save_tokens,
    start_login,
)
from .config import (
    DEFAULT_CONFIG_PATH,
    TOKEN_PATH,
    ConfigError,
    ensure_app_dir,
    load_config,
    write_private,
)
from .service import SERVICE_NAME, ServiceError
from .service import backend as service_backend
from .service import install as install_service
from .service import status as service_status

# ------------------------------------------------------------------- prompting


class SetupAborted(ConfigError):
    """Raised when the wizard cannot continue — no tty, or the user backed out."""


class NoInput(SetupAborted):
    """stdin closed at one of setup's own prompts; cmd_setup names the config."""


def _no_tty_message(path: Path = DEFAULT_CONFIG_PATH) -> str:
    return (
        "setup needs an interactive terminal; configure "
        f"{path} by hand instead (see config.example.toml)"
    )


def _input(prompt: str, eof_message: str | None = None) -> str:
    try:
        return input(prompt)
    except EOFError:
        if eof_message:
            raise SetupAborted(eof_message) from None
        raise NoInput(_no_tty_message()) from None


# `login` reaches the code prompt without setup's tty check, so its EOF must
# not send the user off to edit config.toml.
LOGIN_NEEDS_INPUT = (
    "login needs you to paste the code the browser shows, but there is no input "
    "to read it from; run `python run.py login` in an interactive terminal "
    "(or `python run.py login --import` on a host with Claude Code)"
)


def _ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    answer = _input(f"{prompt}{suffix}: ").strip()
    return answer or default


def _ask_yes_no(prompt: str, default: bool = True) -> bool:
    suffix = " [Y/n]" if default else " [y/N]"
    while True:
        answer = _input(f"{prompt}{suffix}: ").strip().lower()
        if not answer:
            return default
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no"):
            return False
        print("  Please answer y or n.")


def _mask(secret: str) -> str:
    if not secret:
        return ""
    return "*" * max(len(secret) - 4, 0) + secret[-4:]


def _ask_secret(prompt: str, env_var: str) -> str:
    """Read a bot token without echoing it, or defer to the environment."""
    if os.environ.get(env_var):
        if _ask_yes_no(f"  {env_var} is set in the environment - use it?", True):
            return ""
        # The loader lets the variable win over the file, and the service
        # install copies it. Drop it here so the rest of setup, and the
        # service it installs, use the token typed below (F-061).
        del os.environ[env_var]
        print(
            f"  Ignoring {env_var} for the rest of setup. It still overrides\n"
            "  config.toml for commands run from this shell; unset it there."
        )
    while True:
        try:
            secret = getpass.getpass(f"{prompt} (input hidden): ").strip()
        except (EOFError, KeyboardInterrupt):
            raise SetupAborted("no token entered") from None
        if secret:
            print(f"  Got {_mask(secret)}")
            return secret
        print(f"  A token is required (or set {env_var} and re-run setup).")


def parse_ids(raw: str, where: str) -> list[int]:
    """Parse a comma- or space-separated list of numeric user IDs."""
    out: list[int] = []
    for chunk in raw.replace(",", " ").split():
        try:
            out.append(int(chunk))
        except ValueError:
            raise ConfigError(f"{where}: {chunk!r} is not a numeric ID") from None
    return out


def _ask_ids(prompt: str, where: str) -> list[int]:
    while True:
        raw = _input(f"{prompt}: ").strip()
        if not raw:
            print("  At least one ID is required - the bot refuses to answer everyone.")
            continue
        try:
            return parse_ids(raw, where)
        except ConfigError as e:
            print(f"  {e}")


def _ask_guild_id() -> int | None:
    """One server ID, or None for a global registration. Re-asks on a bad answer."""
    while True:
        raw = _ask("  Server (guild) ID - blank registers /usage globally, ~1h to appear")
        if not raw:
            return None
        try:
            ids = parse_ids(raw, "discord.guild_id")
        except ConfigError as e:
            print(f"  {e}")
            continue
        if len(ids) == 1:
            return ids[0]
        print("  Enter a single server ID.")


def _ask_float(prompt: str, default: float, *, minimum: float, inclusive: bool) -> float:
    """A finite number above `minimum` (or equal to it, when inclusive)."""
    bound = f">= {minimum}" if inclusive else f"> {minimum}"
    while True:
        raw = _ask(prompt, str(default))
        try:
            value = float(raw)
        except ValueError:
            print(f"  {raw!r} is not a number.")
            continue
        if math.isfinite(value) and (value >= minimum if inclusive else value > minimum):
            return value
        print(f"  Enter a number {bound}.")


# --------------------------------------------------------------- config output


def _toml_str(value: str) -> str:
    """A TOML basic string. Control characters are not allowed raw, so escape them."""
    out = []
    for ch in value:
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            out.append(f"\\u{ord(ch):04x}")
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def render_config_toml(
    *,
    discord_enabled: bool,
    discord_token: str,
    discord_ids: list[int],
    discord_guild_id: int | None,
    telegram_enabled: bool,
    telegram_token: str,
    telegram_ids: list[int],
    tolerance_points: float,
    critical_ratio: float,
) -> str:
    """Render a config.toml. Values come from the wizard, so no validation here."""
    lines = [
        "# ClaudeLimitReader configuration - written by `python run.py setup`.",
        "# Bot tokens may also come from CLR_DISCORD_TOKEN / CLR_TELEGRAM_TOKEN,",
        "# which take precedence over the values in this file.",
        "",
        "[discord]",
        f"enabled = {str(discord_enabled).lower()}",
        f"token = {_toml_str(discord_token)}",
        f"allowed_user_ids = [{', '.join(str(i) for i in discord_ids)}]",
    ]
    if discord_guild_id is not None:
        lines.append(f"guild_id = {discord_guild_id}")
    else:
        lines.append("# guild_id = 000000000000000000")
    lines += [
        "",
        "[telegram]",
        f"enabled = {str(telegram_enabled).lower()}",
        f"token = {_toml_str(telegram_token)}",
        f"allowed_user_ids = [{', '.join(str(i) for i in telegram_ids)}]",
        "",
        "[pace]",
        f"tolerance_points = {tolerance_points}",
        f"critical_ratio = {critical_ratio}",
        "",
    ]
    return "\n".join(lines)


def write_config(path: Path, text: str) -> None:
    """Write the config atomically, owner-only - it holds bot tokens."""
    write_private(path, text.encode("utf-8"))


# ------------------------------------------------------- virtualenv and deps

PROJECT_DIR = Path(__file__).resolve().parent.parent
VENV_DIR = PROJECT_DIR / "venv"
REQUIREMENTS = PROJECT_DIR / "requirements.txt"

# Set on the handed-off child so a failed venv cannot bounce us round forever.
HANDOFF_FLAG = "CLR_SETUP_IN_VENV"

# Import name -> distribution name, for the two optional platform libraries.
PLATFORM_DEPS = {"discord": "discord.py", "telegram": "python-telegram-bot"}


def _in_venv() -> bool:
    return sys.prefix != sys.base_prefix


def venv_python(venv_dir: Path) -> Path:
    """The interpreter inside a venv, whichever layout this platform uses."""
    if sys.platform == "win32":
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def missing_deps() -> list[str]:
    """Which platform libraries are not importable by the running interpreter."""
    import importlib.util

    return [
        dist
        for module, dist in PLATFORM_DEPS.items()
        if importlib.util.find_spec(module) is None
    ]


def _pip_install(python: Path) -> None:
    """Install requirements.txt, letting pip print its own progress."""
    if not REQUIREMENTS.exists():
        raise SetupAborted(f"cannot find {REQUIREMENTS}")
    print(f"\nInstalling dependencies into {python.parent.parent}...")
    proc = subprocess.run(
        [str(python), "-m", "pip", "install", "-q", "-r", str(REQUIREMENTS)]
    )
    if proc.returncode != 0:
        raise SetupAborted(
            f"pip install failed ({proc.returncode}); install "
            f"{REQUIREMENTS} yourself and re-run setup"
        )
    print("Dependencies installed.")


def _create_venv() -> Path:
    print(f"\nCreating a virtualenv at {VENV_DIR}...")
    proc = subprocess.run(
        [sys.executable, "-m", "venv", str(VENV_DIR)], capture_output=True, text=True
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise SetupAborted(
            f"could not create the virtualenv: {detail}\n"
            "On Debian or Ubuntu this usually means python3-venv is missing:\n"
            "    sudo apt install python3-venv"
        )
    python = venv_python(VENV_DIR)
    if not python.exists():
        raise SetupAborted(f"the virtualenv has no interpreter at {python}")
    print(f"Created {VENV_DIR}")
    return python


def _handoff(python: Path, args) -> int:
    """Re-run the wizard under the venv's interpreter and return its exit code.

    The rest of setup has to run there: the service records the interpreter it
    was installed with, and that must be the one holding the dependencies.
    """
    cmd = [str(python), str(PROJECT_DIR / "run.py")]
    if args.config:
        cmd += ["-c", str(args.config)]
    cmd.append("setup")

    print(f"\nContinuing under {python}\n")
    env = {**os.environ, HANDOFF_FLAG: "1"}
    return subprocess.run(cmd, env=env).returncode


def _step_env(args) -> int | None:
    """Step 1. Returns an exit code if the wizard handed off, else None."""
    print("\n--- 1/5  Python environment --------------------------------------")

    if _in_venv():
        print(f"Running in a virtualenv: {sys.prefix}")
    elif os.environ.get(HANDOFF_FLAG):
        # The handoff already happened and still landed outside a venv.
        print("Running outside a virtualenv; continuing with the interpreter given.")
    else:
        print("Not running in a virtualenv.")
        print(
            "A virtualenv keeps discord.py and python-telegram-bot out of your\n"
            "system Python, and the service will record whichever interpreter\n"
            "setup finishes in."
        )

        python = None
        if VENV_DIR.exists():
            if _ask_yes_no(f"Use the existing virtualenv at {VENV_DIR}?", True):
                python = venv_python(VENV_DIR)
                if not python.exists():
                    raise SetupAborted(
                        f"{VENV_DIR} exists but has no interpreter at {python}; "
                        "remove it and re-run setup"
                    )
        elif _ask_yes_no(f"Create one at {VENV_DIR}?", True):
            python = _create_venv()

        if python is not None:
            _pip_install(python)
            return _handoff(python, args)

        print("Continuing with the current interpreter.")

    missing = missing_deps()
    if not missing:
        print("Both platform libraries are installed.")
        return None

    print(f"Not installed: {', '.join(missing)}")
    print("You only need the library for the platforms you enable.")
    if _ask_yes_no("Install the dependencies now?", True):
        _pip_install(Path(sys.executable))
    else:
        print("Skipped - enabling a platform whose library is missing will fail.")
    return None


# ------------------------------------------------------------------- the steps


def interactive_login(import_local: bool = False) -> None:
    """Run a login and store the tokens. Shared with `python run.py login`."""
    if import_local:
        print("Adopting credentials from the local Claude Code install...")
        print(
            "Note: this takes over that token's lineage. Your Claude Code CLI\n"
            "      will need `claude /login` again after the bot next refreshes.\n"
        )
        tokens = import_local_credentials()
        save_tokens(tokens)
        print(f"Logged in. Tokens stored at {TOKEN_PATH}")
        return

    pending = start_login()

    print()
    print("1. Open this URL in any browser (it does not have to be on this host):")
    print()
    print(f"   {pending.url}")
    print()
    print("2. Approve the request, then copy the code shown on the page.")
    print()

    pasted = _input("Paste the code here: ", LOGIN_NEEDS_INPUT)
    tokens = finish_login(pending, pasted)
    save_tokens(tokens)

    who = f" as {tokens.account_email}" if tokens.account_email else ""
    print(f"\nLogged in{who}. Tokens stored at {TOKEN_PATH}")


def _step_login() -> None:
    print("\n--- 2/5  Claude account ------------------------------------------")

    tokens = load_tokens()
    if tokens is not None:
        who = tokens.account_email or "unknown account"
        expires = dt.datetime.fromtimestamp(tokens.expires_at, dt.timezone.utc)
        # An expired access token is normal - the refresh token renews it.
        state = "expired" if tokens.expired else f"expires {expires:%Y-%m-%d %H:%M} UTC"
        print(f"Already logged in: {who} (access token {state})")
        if not _ask_yes_no("Log in again?", False):
            return

    print(
        "\nA browser login gives the bot its own refresh-token lineage, so it\n"
        "never competes with a local Claude Code install. Prefer it if you can."
    )
    use_import = _ask_yes_no(
        "Adopt this host's existing Claude Code credentials instead?", False
    )
    interactive_login(import_local=use_import)


def _step_platforms() -> dict:
    print("\n--- 3/5  Platforms -----------------------------------------------")

    answers: dict = {
        "discord_enabled": False,
        "discord_token": "",
        "discord_ids": [],
        "discord_guild_id": None,
        "telegram_enabled": False,
        "telegram_token": "",
        "telegram_ids": [],
    }

    while True:
        answers["discord_enabled"] = _ask_yes_no("Enable Discord?", True)
        answers["telegram_enabled"] = _ask_yes_no("Enable Telegram?", False)
        if answers["discord_enabled"] or answers["telegram_enabled"]:
            break
        print("  Enable at least one - there is nothing to run otherwise.")

    if answers["discord_enabled"]:
        print(
            "\nDiscord: create an app at https://discord.com/developers/applications,\n"
            "add a bot user, and copy its token (Bot tab -> Reset Token).\n"
            "For the IDs, enable Developer Mode in Discord settings, then\n"
            "right-click your name / the server icon -> Copy ID."
        )
        answers["discord_token"] = _ask_secret("  Bot token", "CLR_DISCORD_TOKEN")
        answers["discord_ids"] = _ask_ids(
            "  Your Discord user ID(s), comma separated", "discord.allowed_user_ids"
        )
        answers["discord_guild_id"] = _ask_guild_id()

    if answers["telegram_enabled"]:
        print(
            "\nTelegram: talk to @BotFather (/newbot) for the token, and to\n"
            "@userinfobot for your numeric user ID."
        )
        answers["telegram_token"] = _ask_secret("  Bot token", "CLR_TELEGRAM_TOKEN")
        answers["telegram_ids"] = _ask_ids(
            "  Your Telegram user ID(s), comma separated", "telegram.allowed_user_ids"
        )

    if _ask_yes_no("\nCustomize the pace thresholds?", False):
        answers["tolerance_points"] = _ask_float(
            "  Overspend tolerated, in percentage points", 5.0,
            minimum=0.0, inclusive=True,
        )
        answers["critical_ratio"] = _ask_float(
            "  Even-pace multiple that escalates the warning", 1.5,
            minimum=0.0, inclusive=False,
        )
    else:
        answers["tolerance_points"] = 5.0
        answers["critical_ratio"] = 1.5

    return answers


def _step_write(path: Path, answers: dict) -> bool:
    print("\n--- 4/5  Write config --------------------------------------------")

    text = render_config_toml(**answers)
    try:
        tomllib.loads(text)  # never replace a working config with a broken one
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"the rendered config is not valid TOML ({e}); nothing was written") from e

    if path.exists():
        print(f"{path} already exists.")
        if not _ask_yes_no("Overwrite it? (a .bak copy is kept)", False):
            print("Left the existing config alone. Nothing was written.")
            return False
        backup = path.with_name(path.name + ".bak")
        write_private(backup, path.read_bytes())  # it holds the old tokens
        print(f"Backed up to {backup}")

    write_config(path, text)
    print(f"Wrote {path}")
    return True


def _ask_scope() -> bool:
    """Ask whether to install the unit system-wide. True means system scope."""
    if os.geteuid() == 0:
        return _ask_yes_no(
            "Running as root - install system-wide (starts at boot)?", True
        )

    print(
        "\nA system unit starts at boot and is managed with plain `systemctl`,\n"
        "but writing it needs root. A user unit needs no root, and is managed\n"
        "with `systemctl --user`, but stops when your last session ends unless\n"
        "you run `loginctl enable-linger`."
    )
    if not _ask_yes_no("Install system-wide? (you will be asked for sudo)", True):
        return False

    if shutil.which("sudo") is None:
        print("  No sudo on this host - installing a user unit instead.")
        return False
    return True


def _sudo_install(path: Path) -> int:
    """Run `service install --system` under sudo. Returns its exit code.

    -E and the explicit interpreter keep the virtualenv: sudo otherwise resets
    the environment and the unit would record a Python without the deps.
    """
    cmd = ["sudo", "-E", sys.executable, str(PROJECT_DIR / "run.py")]
    if path != DEFAULT_CONFIG_PATH:
        cmd += ["-c", str(path)]
    cmd += ["service", "install", "--system"]

    print("Running: " + " ".join(cmd))
    return subprocess.run(cmd).returncode


def _step_service(path: Path) -> bool:
    """Offer to install the background service. Returns True if one is running."""
    print("\n--- 5/5  Background service --------------------------------------")

    kind = service_backend()
    if not kind:
        print(
            "No systemd and no Task Scheduler here, so there is nothing to\n"
            "install into. Run the bot with `python run.py run`."
        )
        return False

    if kind == "systemd":
        print("The bot can run in the background as a systemd unit,")
        print(f"{SERVICE_NAME}, restarted on failure instead of")
        print("held open by a terminal.")
    else:
        # Which of the two Windows mechanisms is used depends on elevation.
        print("The bot can run in the background as claude-limit-reader-bot,")
        print("started at logon instead of held open by a terminal.")

    if not _ask_yes_no("Install it now?", True):
        print("Skipped. You can install it later with: python run.py service install")
        return False

    # Under sudo, this wizard's login and config belong to root, but the unit
    # would run as SUDO_USER, who cannot read them (F-045).
    geteuid = getattr(os, "geteuid", None)
    sudo_user = os.environ.get("SUDO_USER")
    if geteuid is not None and geteuid() == 0 and sudo_user:
        print(
            "\nNot installing the service: setup ran under sudo, so its login and\n"
            f"config are root's, but the service would run as {sudo_user}.\n"
            "Re-run `python run.py setup` without sudo; it asks for sudo itself\n"
            "when you choose a system-wide install.",
            file=sys.stderr,
        )
        return False

    system_scope = _ask_scope() if kind == "systemd" else False

    try:
        print()
        if system_scope and os.geteuid() != 0:
            # Installing into /etc/systemd/system needs root, so re-run just the
            # install under sudo rather than asking for the whole wizard to be.
            if _sudo_install(path) != 0:
                print("\nThe config is still valid; `python run.py run` works.",
                      file=sys.stderr)
                return False
        else:
            # The same checks `service install` runs (the sudo branch above
            # gets them there): a missing library, or files the service
            # cannot find, would leave a unit that exits at once.
            from .cli import check_install  # cli imports this module

            check_install(path)
            print(install_service(system_scope=system_scope, config_path=path))
    except ServiceError as e:
        print(f"\nCould not install the service: {e}", file=sys.stderr)
        print("The config is still valid; `python run.py run` works.", file=sys.stderr)
        return False
    except ConfigError as e:
        print(f"\nNot installing the service: {e}", file=sys.stderr)
        return False

    print()
    print(service_status(system_scope=system_scope))
    return True


# ---------------------------------------------------------------- entry point


def cmd_setup(args) -> int:
    path = Path(args.config) if args.config else DEFAULT_CONFIG_PATH
    if not sys.stdin.isatty():
        raise SetupAborted(_no_tty_message(path))
    try:
        return _run_wizard(args, path)
    except NoInput:
        # The prompts do not know which file -c named; point at that one (F-065).
        raise SetupAborted(_no_tty_message(path)) from None


def _run_wizard(args, path: Path) -> int:
    print("ClaudeLimitReader setup")
    print(f"App directory: {ensure_app_dir()}")
    print("Ctrl-C stops at any point; the config is written only at the end.")

    handoff = _step_env(args)
    if handoff is not None:
        return handoff

    _step_login()
    answers = _step_platforms()
    if not _step_write(path, answers):
        return 1

    # Read it back through the real loader so a bad answer surfaces now rather
    # than at `python run.py run`.
    cfg = load_config(path)

    print("\nConfigured:")
    if cfg.discord.enabled:
        scope = f"guild {cfg.discord.guild_id}" if cfg.discord.guild_id else "global"
        print(f"  Discord  - {len(cfg.discord.allowed_user_ids)} allowed user(s), {scope}")
    if cfg.telegram.enabled:
        print(f"  Telegram - {len(cfg.telegram.allowed_user_ids)} allowed user(s)")

    if _ask_yes_no("\nFetch your usage now as a check?", True):
        from .render import render_plain
        from .usage import UsageError, fetch_usage

        try:
            print()
            print(render_plain(fetch_usage(), cfg.pace))
        except (UsageError, AuthError) as e:
            print(f"\nCould not fetch usage: {e}", file=sys.stderr)
            print("Fix that, then re-run `python run.py usage`.", file=sys.stderr)
            return 1

    running = _step_service(path)

    if running:
        print("\nDone - the bot is running in the background.")
        print("Check on it any time with: python run.py service status")
    else:
        print("\nDone. Start the bots with: python run.py run")
    return 0
