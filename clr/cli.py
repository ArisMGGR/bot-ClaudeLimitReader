"""Command line entry point: setup, login, status, usage, run, service."""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import logging
import sys
from pathlib import Path

from .auth import AuthError, load_tokens
from .config import DEFAULT_CONFIG_PATH, TOKEN_PATH, Config, ConfigError, load_config
from .logs import build_handler as build_log_handler, heartbeat
from .render import render_plain
from .service import ServiceError, record_running_instance
from .setup import cmd_setup, interactive_login
from .usage import UsageError, fetch_usage


def _force_utf8_output() -> None:
    """Keep the bars and status icons printable on legacy Windows codepages.

    A console using e.g. cp1253 raises UnicodeEncodeError on the block and emoji
    characters, so switch the streams to UTF-8 and degrade rather than crash.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass


log = logging.getLogger("clr")

# Records carrying this attribute go to the log file only. The console already
# shows the same failure as a plain "Error: ..." line (or a traceback).
FILE_ONLY = {"file_only": True}


def _not_file_only(record: logging.LogRecord) -> bool:
    return not getattr(record, "file_only", False)


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    for handler in logging.getLogger().handlers:
        handler.addFilter(_not_file_only)
    try:
        logging.getLogger().addHandler(build_log_handler())
    except OSError as e:  # unwritable log dir must not stop the command
        logging.getLogger("clr").warning("file logging disabled: %s", e)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("discord").setLevel(logging.WARNING)


# ---------------------------------------------------------------- subcommands


def cmd_login(args) -> int:
    interactive_login(import_local=args.import_local)
    return 0


def check_install(cfg_path: Path | None) -> None:
    """Refuse anything `run` would refuse (a mistyped -c path included).

    Shared by `service install` and setup's service step, so neither installs
    a unit that exits at once. Checks the files the service will read, which
    under sudo are SUDO_USER's, not root's.
    """
    from . import service

    env = service.service_environment(cfg_path)
    app_dir = Path(env["CLR_HOME"])
    config = Path(env.get("CLR_CONFIG") or app_dir / "config.toml")
    _preflight(load_config(config), config, app_dir / TOKEN_PATH.name)


def cmd_service(args) -> int:
    from . import service

    if args.action == "install":
        cfg_path = Path(args.config) if args.config else None
        check_install(cfg_path)
        print(service.install(system_scope=args.system, config_path=cfg_path))
    elif args.action == "uninstall":
        print(service.uninstall(system_scope=args.system))
    elif args.action == "show":
        cfg_path = Path(args.config) if args.config else None
        print(service.show(system_scope=args.system, config_path=cfg_path))
    else:
        print(service.status(system_scope=args.system))
    return 0


def cmd_status(args) -> int:
    tokens = load_tokens()
    if tokens is None:
        print(f"Not logged in (no {TOKEN_PATH}). Run: python run.py setup")
        return 1

    expires = dt.datetime.fromtimestamp(tokens.expires_at, dt.timezone.utc)
    remaining = expires - dt.datetime.now(dt.timezone.utc)
    state = "expired" if tokens.expired else f"valid for {remaining}"

    print(f"Token file : {TOKEN_PATH}")
    if tokens.account_email:
        print(f"Account    : {tokens.account_email}")
    print(f"Scope      : {tokens.scope or 'unknown'}")
    print(f"Access tok : {state} (expires {expires:%Y-%m-%d %H:%M} UTC)")
    config_path = Path(args.config) if args.config else DEFAULT_CONFIG_PATH
    print(f"Config     : {config_path}"
          f"{'' if config_path.exists() else '  [missing]'}")
    return 0


def cmd_usage(args) -> int:
    # Only the pace settings are used here; bot tokens need not be present.
    cfg = load_config(args.config, check_platforms=False)
    report = fetch_usage()
    print(render_plain(report, cfg.pace))
    return 0


async def _run_telegram(cfg: Config) -> None:
    from telegram.error import NetworkError

    from .bots import retry_startup
    from .bots.telegram_bot import build_application

    async def initialize():
        app = build_application(cfg)
        try:
            await app.initialize()
            # Only run_polling()/run_webhook() call post_init; driving the
            # lifecycle by hand, as here, means calling it ourselves (it
            # registers /usage).
            if app.post_init is not None:
                await app.post_init(app)
        except BaseException:
            await app.shutdown()
            raise
        return app

    app = await retry_startup("telegram", initialize, (NetworkError,))
    await app.start()
    # -1: keep retrying the polling bootstrap too, rather than exiting on it.
    await app.updater.start_polling(bootstrap_retries=-1)
    try:
        await asyncio.Event().wait()  # run until cancelled
    finally:
        await app.updater.stop()
        await app.stop()
        await app.shutdown()


async def _run_all(cfg: Config) -> None:
    tasks = []
    if cfg.discord.enabled:
        from .bots.discord_bot import run as run_discord

        tasks.append(asyncio.create_task(run_discord(cfg), name="discord"))
    if cfg.telegram.enabled:
        tasks.append(asyncio.create_task(_run_telegram(cfg), name="telegram"))

    logging.getLogger("clr").info(
        "starting: %s", ", ".join(t.get_name() for t in tasks)
    )
    beat = asyncio.create_task(heartbeat(), name="heartbeat")

    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        pass
    finally:
        beat.cancel()
        for t in tasks:
            t.cancel()


# Import name and distribution name of each platform's library.
PLATFORM_LIBRARIES = {"discord": ("discord", "discord.py"), "telegram": ("telegram", "python-telegram-bot")}


def _check_libraries(cfg: Config) -> None:
    """Fail with a readable error, not an ImportError, for a missing library."""
    import importlib.util

    for name, enabled in (("discord", cfg.discord.enabled), ("telegram", cfg.telegram.enabled)):
        module, dist = PLATFORM_LIBRARIES[name]
        if enabled and importlib.util.find_spec(module) is None:
            raise ConfigError(
                f"{name}.enabled is true but {dist} is not installed for {sys.executable}; "
                "run: pip install -r requirements.txt"
            )


def _preflight(cfg: Config, config_path, token_path: Path) -> None:
    """What `run` needs before it can start a bot. `service install` checks it too."""
    if not cfg.any_platform_enabled:
        raise ConfigError(
            "No platform enabled. Set discord.enabled or telegram.enabled in "
            f"{config_path} (or run `python run.py setup`)"
        )
    if not token_path.exists():
        raise ConfigError(
            "Not logged in. Run `python run.py setup` (or `python run.py login`) first."
        )
    _check_libraries(cfg)


def cmd_run(args) -> int:
    cfg = load_config(args.config)
    _preflight(cfg, args.config or DEFAULT_CONFIG_PATH, TOKEN_PATH)
    load_tokens()  # a corrupt token file fails now, not on the first /usage
    record_running_instance()  # Windows service only; lets install/uninstall stop it

    try:
        asyncio.run(_run_all(cfg))
    except KeyboardInterrupt:
        print("\nStopped.")
    return 0


# --------------------------------------------------------------------- parser


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python run.py",
        description="Report remaining Claude subscription quota to Discord and Telegram.",
    )
    p.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    p.add_argument("-c", "--config", default=None, help="path to config.toml")
    sub = p.add_subparsers(dest="command", required=True)

    st = sub.add_parser("setup", help="interactive first-run setup")
    st.set_defaults(func=cmd_setup)

    lo = sub.add_parser("login", help="authorize this host against your Claude account")
    lo.add_argument(
        "--import",
        dest="import_local",
        action="store_true",
        help="adopt the local Claude Code credentials instead of a browser login",
    )
    lo.set_defaults(func=cmd_login)

    sub.add_parser("status", help="show token state").set_defaults(func=cmd_status)
    sub.add_parser("usage", help="print the usage report to stdout").set_defaults(func=cmd_usage)
    sub.add_parser("run", help="run the enabled bots").set_defaults(func=cmd_run)

    sv = sub.add_parser(
        "service",
        help="install, remove or inspect the background service",
    )
    sv.add_argument(
        "action",
        nargs="?",
        default="status",
        choices=["install", "uninstall", "status", "show"],
        help="default: status ('show' prints the unit without installing it)",
    )
    sv.add_argument(
        "--system",
        action="store_true",
        help="systemd only: /etc/systemd/system instead of the per-user unit (needs root)",
    )
    sv.set_defaults(func=cmd_service)

    return p


def main(argv: list[str] | None = None) -> int:
    _force_utf8_output()
    args = build_parser().parse_args(argv)
    if args.config:
        # Once here, so every command reads the file the service would (F-058).
        args.config = str(Path(args.config).expanduser())
    _setup_logging(args.verbose)
    try:
        return args.func(args)
    except (AuthError, UsageError, ConfigError, ServiceError) as e:
        # Printed for a terminal, and logged because under the background
        # service (pythonw.exe on Windows) there is no terminal to print to.
        print(f"\nError: {e}", file=sys.stderr)
        log.error("%s failed: %s", args.command, e, extra=FILE_ONLY)
        return 1
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        return 130
    except Exception:
        log.exception("%s failed unexpectedly", args.command, extra=FILE_ONLY)
        raise


if __name__ == "__main__":
    raise SystemExit(main())
