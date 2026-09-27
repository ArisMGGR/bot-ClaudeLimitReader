"""Install the bot as a background service so `run` need not hold a terminal.

Linux gets a systemd unit named claude-limit-reader-bot.service, user-scoped by
default so no root is involved. Windows has no systemd, so it gets the closest
equivalent under the same name: a logon-triggered scheduled task, or a Startup
launcher when the install is not elevated.

A service does not inherit the installing shell's environment, so whatever the
config loader reads from it (CLR_HOME, CLR_CONFIG, CLR_*_TOKEN) is written into
the unit or launcher at install time.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from .config import APP_DIR, DEFAULT_CONFIG_PATH, write_private
from .logs import LOG_DIR

SERVICE_NAME = "claude-limit-reader-bot.service"
TASK_NAME = "claude-limit-reader-bot"  # Windows task names take no suffix.

SYSTEM_UNIT_DIR = Path("/etc/systemd/system")
USER_UNIT_DIR = Path.home() / ".config" / "systemd" / "user"

# Bot tokens the loader accepts from the environment. They are secrets, so on
# systemd they go into an owner-only EnvironmentFile rather than the unit,
# which is world-readable.
TOKEN_ENV_VARS = ("CLR_DISCORD_TOKEN", "CLR_TELEGRAM_TOKEN")
SERVICE_ENV = APP_DIR / "service.env"


class ServiceError(Exception):
    """Raised when a service cannot be installed, removed, or inspected."""


# ------------------------------------------------------------------- platform


def backend() -> str:
    """Which service manager this host can use: 'systemd', 'windows', or ''."""
    if sys.platform == "win32":
        return "windows" if shutil.which("schtasks") else ""
    if shutil.which("systemctl") and Path("/run/systemd/system").exists():
        return "systemd"
    return ""


def entry_point() -> tuple[str, Path]:
    """The interpreter and run.py the service should launch.

    sys.executable keeps a virtualenv's Python, which is where the deps are
    installed; run.py sits one level above this package.
    """
    python = sys.executable or "python3"
    run_py = Path(__file__).resolve().parent.parent / "run.py"
    if not run_py.exists():
        raise ServiceError(f"cannot find run.py next to the package (looked at {run_py})")
    return python, run_py


def _home_of(user: str) -> Path:
    """The home directory of a local account (POSIX only)."""
    import pwd

    return Path(pwd.getpwnam(user).pw_dir)


def service_app_dir() -> Path:
    """The app directory as the service will see it.

    Under sudo, HOME can be root's (sudoers `always_set_home`, the default on
    RHEL and Fedora), which points APP_DIR at /root. Unless CLR_HOME names the
    directory, the service belongs to SUDO_USER, so use that user's home.
    """
    if os.environ.get("CLR_HOME"):
        return APP_DIR.absolute()
    geteuid = getattr(os, "geteuid", None)
    user = os.environ.get("SUDO_USER")
    if geteuid is not None and geteuid() == 0 and user:
        try:
            return _home_of(user) / APP_DIR.name
        except (ImportError, KeyError):
            pass
    return APP_DIR.absolute()


def service_env_path() -> Path:
    """Where the service's owner-only environment file lives."""
    app_dir = service_app_dir()
    return SERVICE_ENV if app_dir == APP_DIR.absolute() else app_dir / SERVICE_ENV.name


def service_environment(config_path: Path | None = None) -> dict[str, str]:
    """The non-secret variables the service needs to find its files.

    CLR_HOME is always written, so the service never depends on the HOME it
    happens to get. CLR_CONFIG is written only for a -c path or a CLR_CONFIG
    in this shell, and only when it differs from CLR_HOME/config.toml.
    """
    app_dir = service_app_dir()
    env = {"CLR_HOME": str(app_dir)}
    if config_path:
        config = Path(config_path)
    elif os.environ.get("CLR_CONFIG"):
        config = DEFAULT_CONFIG_PATH
    else:
        return env
    config = config.expanduser().absolute()
    if config != app_dir / "config.toml":
        env["CLR_CONFIG"] = str(config)
    return env


def token_environment() -> dict[str, str]:
    """Bot tokens set in this shell, which the service must be given too."""
    return {name: os.environ[name] for name in TOKEN_ENV_VARS if os.environ.get(name)}


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, capture_output=True, text=True)
    except OSError as e:
        raise ServiceError(f"could not execute {cmd[0]}: {e}") from e


def _fail(action: str, proc: subprocess.CompletedProcess) -> None:
    detail = (proc.stderr or proc.stdout or "").strip()
    raise ServiceError(f"{action} failed ({proc.returncode}): {detail}")


# -------------------------------------------------------------------- systemd


def _unit_quote(value: str) -> str:
    """Quote one word of a whitespace-separated directive (ExecStart, ReadWritePaths).

    '%' is a systemd specifier and must be doubled. A word with whitespace,
    quotes or backslashes is double-quoted with C-style escapes, which systemd
    removes again when it splits the line.
    """
    value = value.replace("%", "%%")
    if any(c in value for c in ' \t"\'\\'):
        value = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{value}"'
    return value


def _exec_word(value: str) -> str:
    """Quote one word of ExecStart=, which alone also substitutes $VAR.

    '$$' is a literal '$' there. ReadWritePaths= and Environment= do no such
    substitution, so doubling it in those would change the path.
    """
    return _unit_quote(value.replace("$", "$$"))


def _unit_path(value: Path) -> str:
    """A path for a directive that takes the rest of the line (WorkingDirectory=)."""
    return str(value).replace("%", "%%")


@dataclass
class UnitOptions:
    system_scope: bool = False
    config_path: Path | None = None
    user: str | None = None


def render_unit(options: UnitOptions | None = None) -> str:
    """Render claude-limit-reader-bot.service for the current install."""
    opts = options or UnitOptions()
    python, run_py = entry_point()

    lines = [
        "[Unit]",
        "Description=ClaudeLimitReader Discord/Telegram bot",
        "After=network-online.target",
        "Wants=network-online.target",
        "",
        "[Service]",
        "Type=simple",
        f"ExecStart={_exec_word(python)} {_exec_word(str(run_py))} run",
        f"WorkingDirectory={_unit_path(run_py.parent)}",
        "Restart=on-failure",
        "RestartSec=10",
        "Environment=PYTHONUNBUFFERED=1",
    ]

    # Carry over non-default locations; the service gets its own environment.
    for name, value in service_environment(opts.config_path).items():
        lines.append(f"Environment={_unit_quote(f'{name}={value}')}")
    if token_environment():
        lines += [
            "# Bot tokens from the installing shell (CLR_*_TOKEN); `service install`",
            "# writes this file owner-only.",
            f"EnvironmentFile=-{_unit_path(service_env_path())}",
        ]

    if opts.system_scope and opts.user:
        lines.append(f"User={opts.user}")

    lines += [
        "",
        "# The bot reads one HTTPS endpoint, its own state directory and its logs.",
        "NoNewPrivileges=true",
        "PrivateTmp=true",
        "ProtectSystem=strict",
        "ProtectKernelTunables=true",
        "ProtectControlGroups=true",
        "RestrictSUIDSGID=true",
        f"ReadWritePaths={_unit_quote(str(service_app_dir()))}",
        f"ReadWritePaths={_unit_quote(str(LOG_DIR))}",
        "",
        "[Install]",
        f"WantedBy={'multi-user.target' if opts.system_scope else 'default.target'}",
        "",
    ]
    return "\n".join(lines)


def render_service_env(reveal_tokens: bool = True) -> str:
    """The EnvironmentFile body: one single-quoted, literal assignment per token."""
    lines = []
    for name, value in token_environment().items():
        if not reveal_tokens:
            value = f"<your {name}>"
        elif "'" in value or "\n" in value:
            raise ServiceError(f"{name} contains a quote or newline and cannot be stored")
        lines.append(f"{name}='{value}'")
    return "".join(f"{line}\n" for line in lines)


def unit_path(system_scope: bool = False) -> Path:
    return (SYSTEM_UNIT_DIR if system_scope else USER_UNIT_DIR) / SERVICE_NAME


def _systemctl(system_scope: bool, *args: str) -> subprocess.CompletedProcess:
    cmd = ["systemctl"] + ([] if system_scope else ["--user"]) + list(args)
    return _run(cmd)


def _install_systemd(system_scope: bool, config_path: Path | None) -> str:
    if system_scope and os.geteuid() != 0:
        raise ServiceError(
            "a system-wide unit needs root; re-run with sudo, or drop --system "
            "to install it for your user only"
        )

    # Under sudo, run the service as the user who owns the tokens.
    user = os.environ.get("SUDO_USER")

    # ReadWritePaths= refuses to start the unit if a path is missing, and the
    # service cannot create one itself once ProtectSystem=strict is in force.
    app_dir = service_app_dir()
    for directory in (app_dir, LOG_DIR):
        if not directory.exists():
            directory.mkdir(parents=True)
            if directory == app_dir:
                try:
                    os.chmod(directory, 0o700)
                except (OSError, NotImplementedError):
                    pass  # Windows ACLs; best effort only.
            if user:
                shutil.chown(directory, user=user)

    env_path = service_env_path()
    env_text = render_service_env()
    if env_text:
        write_private(env_path, env_text.encode())
        if user:
            shutil.chown(env_path, user=user)
    else:
        env_path.unlink(missing_ok=True)  # no stale tokens from an earlier install

    path = unit_path(system_scope)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        render_unit(
            UnitOptions(system_scope=system_scope, config_path=config_path, user=user)
        ),
        encoding="utf-8",
    )

    proc = _systemctl(system_scope, "daemon-reload")
    if proc.returncode != 0:
        _fail("systemctl daemon-reload", proc)

    proc = _systemctl(system_scope, "enable", SERVICE_NAME)
    if proc.returncode != 0:
        _fail(f"systemctl enable {SERVICE_NAME}", proc)

    # restart, not start: starting a unit that is already running does nothing,
    # so a re-install would leave the old environment and config in force.
    proc = _systemctl(system_scope, "restart", SERVICE_NAME)
    if proc.returncode != 0:
        _fail(f"systemctl restart {SERVICE_NAME}", proc)

    scope = "system" if system_scope else "user"
    note = f"Installed and (re)started {SERVICE_NAME} ({scope} scope)\n  Unit: {path}"
    if env_text:
        note += f"\n  Tokens: {env_path} (owner-only)"
    if not system_scope:
        # Without lingering, a user unit stops when the last session ends.
        note += (
            "\n  To keep it running while you are logged out:\n"
            f"    sudo loginctl enable-linger {os.environ.get('USER', 'YOUR_USER')}"
        )
    return note


def _other_scope_hint(system_scope: bool) -> str:
    """Point at the other scope when the unit is installed there instead."""
    if not unit_path(not system_scope).exists():
        return ""
    if system_scope:
        return "\nThere is a user unit; drop --system to act on it."
    return (
        "\nThere is a system unit at "
        f"{unit_path(True)}; add --system (as root) to act on it."
    )


def _uninstall_systemd(system_scope: bool) -> str:
    path = unit_path(system_scope)
    if not path.exists():
        raise ServiceError(f"no unit at {path}{_other_scope_hint(system_scope)}")

    if system_scope and os.geteuid() != 0:
        raise ServiceError(f"removing {path} needs root; re-run the same command with sudo")

    # Ignore a failure here: a unit that never started still needs its file gone.
    _systemctl(system_scope, "disable", "--now", SERVICE_NAME)
    try:
        path.unlink()
        service_env_path().unlink(missing_ok=True)
    except OSError as e:
        raise ServiceError(f"could not remove {path}: {e}") from e
    _systemctl(system_scope, "daemon-reload")
    return f"Removed {SERVICE_NAME} ({path})"


def _status_systemd(system_scope: bool) -> str:
    path = unit_path(system_scope)
    if not path.exists():
        return f"Not installed (no {path}){_other_scope_hint(system_scope)}"

    active = _systemctl(system_scope, "is-active", SERVICE_NAME).stdout.strip()
    enabled = _systemctl(system_scope, "is-enabled", SERVICE_NAME).stdout.strip()
    logs = "journalctl" if system_scope else "journalctl --user"
    return (
        f"Unit    : {path}\n"
        f"Active  : {active or 'unknown'}\n"
        f"Enabled : {enabled or 'unknown'}\n"
        f"Logs    : {logs} -u {SERVICE_NAME} -f"
    )


# -------------------------------------------------------------------- windows
#
# A logon-triggered scheduled task is the closest thing to the unit above, but
# creating one requires Administrator. An unelevated install falls back to a
# Startup-folder launcher, which needs no privileges and starts at the same
# point in the logon. Both run the same launcher script, since a scheduled
# task's command line cannot set environment variables.

STARTUP_DIR = (
    Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
)
STARTUP_SCRIPT = STARTUP_DIR / f"{TASK_NAME}.cmd"
TASK_LAUNCHER = APP_DIR / f"{TASK_NAME}.cmd"
PID_FILE = APP_DIR / "service.pid"

# Set by the launcher and by the install-time spawn, so a bot started as the
# service records itself in service.pid (see record_running_instance).
SERVICE_FLAG = "CLR_SERVICE"


def _is_admin() -> bool:
    try:
        import ctypes

        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except (AttributeError, OSError):
        return False


def _windows_python() -> str:
    """Prefer pythonw.exe so the bot runs without a console window."""
    exe = Path(sys.executable)
    windowless = exe.with_name("pythonw.exe")
    return str(windowless if windowless.exists() else exe)


def task_command() -> str:
    _, run_py = entry_point()
    return f'"{_windows_python()}" "{run_py}" run'


def _cmd_escape(value: str) -> str:
    """Batch files expand %VAR% even inside quotes; a literal % is written %%."""
    return value.replace("%", "%%")


def launcher_body(config_path: Path | None, comment: str, reveal_tokens: bool = True) -> list[str]:
    """The .cmd lines that set the service's environment and start the bot.

    cmd.exe reads a batch file in the console's OEM codepage, which garbles any
    non-ASCII path written as UTF-8. Switching to UTF-8 (65001) on the first
    line makes cmd read every following line as UTF-8.
    """
    body = ["@echo off", "chcp 65001 >nul", f"rem {TASK_NAME} - {comment}"]
    for name, value in service_environment(config_path).items():
        body.append(f'set "{name}={_cmd_escape(value)}"')
    for name, value in token_environment().items():
        shown = _cmd_escape(value) if reveal_tokens else f"<your {name}>"
        body.append(f'set "{name}={shown}"')
    body.append(f'set "{SERVICE_FLAG}=1"')
    body.append(f'start "" {_cmd_escape(task_command())}')
    return body


def _write_launcher(path: Path, config_path: Path | None, comment: str) -> None:
    body = launcher_body(config_path, comment)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\r\n".join(body) + "\r\n", encoding="utf-8")


def _task_action() -> str:
    # Without /s, cmd.exe keeps the quotes only when the path has no special
    # characters; with & or parentheses it strips them and the path breaks.
    # /s always strips just the outer pair, leaving the inner quotes intact.
    return f'cmd.exe /s /c ""{TASK_LAUNCHER}""'


def _create_task_args() -> list[str]:
    """The schtasks command `install` runs; `show` prints the same argv."""
    return [
        "schtasks", "/Create",
        "/TN", TASK_NAME,
        "/TR", _task_action(),
        "/SC", "ONLOGON",
        "/RL", "LIMITED",
        "/F",
    ]


# ---- the running service instance
#
# service.pid records the PID *and* the process creation time. A PID alone is
# reused once the process exits (after a reboot, certainly), and killing
# whatever now holds it could take down an unrelated program. The instance
# writes it itself at startup, so one started at logon is recorded too.


def _process_started(pid: int) -> int | None:
    """Creation time of a running process, or None if there is no such process."""
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.GetProcessTimes.argtypes = (wintypes.HANDLE,) + (ctypes.POINTER(wintypes.FILETIME),) * 4
    kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)

    process_query_limited_information = 0x1000
    still_active = 259
    handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
    if not handle:
        return None
    try:
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value != still_active:
            return None
        times = [wintypes.FILETIME() for _ in range(4)]
        if not kernel32.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
            return None
        created = times[0]
        return (created.dwHighDateTime << 32) | created.dwLowDateTime
    finally:
        kernel32.CloseHandle(handle)


def _write_pid_file(pid: int) -> None:
    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(f"{pid} {_process_started(pid) or 0}", encoding="utf-8")


def record_running_instance() -> None:
    """Called by `run`: a Windows service instance records itself in service.pid.

    Whether it was started at install time or by the logon launcher, a later
    install or uninstall can then stop it instead of leaving it running.
    """
    if sys.platform != "win32" or not os.environ.get(SERVICE_FLAG):
        return
    try:
        _write_pid_file(os.getpid())
    except OSError:
        pass  # best effort; uninstall then reports it could not stop it


def _recorded_instance() -> int | None:
    """The PID in service.pid if that exact process is still running, else None."""
    try:
        pid_text, started_text = PID_FILE.read_text(encoding="utf-8").split()
        pid, started = int(pid_text), int(started_text)
    except (OSError, ValueError):  # missing, or an old PID-only file
        return None
    if started and _process_started(pid) == started:
        return pid
    return None


def _spawn_detached(config_path: Path | None = None) -> int:
    """Start the bot now, outliving this console. Returns the new PID.

    It gets the same environment the launcher gives it at logon.
    """
    _, run_py = entry_point()
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
        subprocess, "CREATE_NEW_PROCESS_GROUP", 0
    )
    env = {
        **os.environ,
        **service_environment(config_path),
        **token_environment(),
        SERVICE_FLAG: "1",
    }
    proc = subprocess.Popen(
        [_windows_python(), str(run_py), "run"],
        cwd=str(run_py.parent),
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=flags,
    )
    _write_pid_file(proc.pid)
    return proc.pid


def _stop_detached() -> str:
    """Stop an instance this tool started. Returns 'stopped' or 'not running'."""
    pid = _recorded_instance()
    state = "not running"
    if pid is not None and _run(["taskkill", "/PID", str(pid), "/F"]).returncode == 0:
        state = "stopped"
    PID_FILE.unlink(missing_ok=True)
    return state


def _task_exists() -> bool:
    return _run(["schtasks", "/Query", "/TN", TASK_NAME]).returncode == 0


def _install_windows(config_path: Path | None) -> str:
    elevated = _is_admin()
    # The two mechanisms are alternatives: left side by side, both would start
    # a bot at logon. An unelevated shell cannot remove an elevated task.
    if not elevated and _task_exists():
        raise ServiceError(
            f"scheduled task {TASK_NAME} is already installed; re-run this from an "
            "elevated terminal, or remove it there first with `python run.py service uninstall`"
        )

    # A re-install replaces the running bot rather than adding a second one
    # beside it, which would still run with the old settings.
    replaced = _stop_detached() == "stopped"
    previous = "\n  Stopped the instance that was already running." if replaced else ""

    if elevated:
        _write_launcher(TASK_LAUNCHER, config_path, "run at logon by the scheduled task")
        proc = _run(_create_task_args())
        if proc.returncode != 0:
            _fail(f"schtasks /Create {TASK_NAME}", proc)

        note = (
            f"Installed scheduled task {TASK_NAME} (starts at logon)\n"
            f"  Launcher: {TASK_LAUNCHER}{previous}"
        )
        if STARTUP_SCRIPT.exists():
            STARTUP_SCRIPT.unlink()
            note += f"\n  Removed the Startup-folder launcher it replaces ({STARTUP_SCRIPT})."
        # The trigger only fires at the next logon, so start it now as well.
        if _run(["schtasks", "/Run", "/TN", TASK_NAME]).returncode == 0:
            note += "\n  Started."
        else:
            note += "\n  Could not start it now; it will start at your next logon."
        return note

    _write_launcher(
        STARTUP_SCRIPT, config_path, "delete this file to stop the bot starting at logon"
    )
    pid = _spawn_detached(config_path)
    return (
        f"Installed {STARTUP_SCRIPT.name} (starts at logon)\n"
        f"  Launcher: {STARTUP_SCRIPT}{previous}\n"
        f"  Started now, PID {pid}.\n"
        "  Creating a logon-triggered scheduled task requires Administrator;\n"
        "  re-run this from an elevated terminal if you want one instead."
    )


def _uninstall_windows() -> str:
    removed = []

    if _task_exists():
        proc = _run(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"])
        if proc.returncode != 0:
            _fail(f"schtasks /Delete {TASK_NAME}", proc)
        removed.append(f"scheduled task {TASK_NAME}")

    for launcher in (TASK_LAUNCHER, STARTUP_SCRIPT):
        if launcher.exists():
            launcher.unlink()
            removed.append(str(launcher))

    if not removed:
        raise ServiceError(
            f"nothing installed (no scheduled task {TASK_NAME}, no {STARTUP_SCRIPT})"
        )

    note = "Removed " + ", ".join(removed)
    if _stop_detached() == "stopped":
        note += "\n  Stopped the running instance."
    else:
        # Only a recorded instance can be told apart from other Python
        # processes safely; one from an older launcher never recorded itself.
        note += (
            "\n  No running instance was recorded. If the bot is still running, "
            "end pythonw.exe in Task Manager."
        )
    return note


def _instance_lines() -> list[str]:
    """Whether the recorded bot is running, and where it logs.

    The task's own Status says nothing about the bot: its action is a launcher
    that starts pythonw.exe and exits, so the task reads "Ready" either way.
    """
    pid = _recorded_instance()
    if pid is not None:
        process = f"running, PID {pid}"
    elif PID_FILE.exists():
        process = "the last recorded instance has exited"
    else:
        process = "no running instance recorded"
    return [f"Process  : {process}", f"Logs     : {LOG_DIR}"]


def _status_windows() -> str:
    proc = _run(["schtasks", "/Query", "/TN", TASK_NAME, "/FO", "LIST"])
    if proc.returncode == 0:
        wanted = ("TaskName:", "Status:", "Next Run Time:", "Last Run Time:", "Last Result:")
        keep = [ln.strip() for ln in proc.stdout.splitlines() if ln.startswith(wanted)]
        return "\n".join((keep or [proc.stdout.strip()]) + _instance_lines())

    if STARTUP_SCRIPT.exists():
        lines = [f"Launcher : {STARTUP_SCRIPT}", "Trigger  : at logon"]
        return "\n".join(lines + _instance_lines())

    return f"Not installed (no scheduled task {TASK_NAME}, no {STARTUP_SCRIPT})"


# ---------------------------------------------------------------- public API


def install(system_scope: bool = False, config_path: Path | None = None) -> str:
    kind = backend()
    if kind == "systemd":
        return _install_systemd(system_scope, config_path)
    if kind == "windows":
        return _install_windows(config_path)
    raise ServiceError(
        "no supported service manager here (need systemd or Windows Task "
        "Scheduler); keep using `python run.py run`"
    )


def uninstall(system_scope: bool = False) -> str:
    kind = backend()
    if kind == "systemd":
        return _uninstall_systemd(system_scope)
    if kind == "windows":
        return _uninstall_windows()
    raise ServiceError("no supported service manager here")


def status(system_scope: bool = False) -> str:
    kind = backend()
    if kind == "systemd":
        return _status_systemd(system_scope)
    if kind == "windows":
        return _status_windows()
    return "No supported service manager here (need systemd or Task Scheduler)."


def show(system_scope: bool = False, config_path: Path | None = None) -> str:
    """Print what `install` would write, for a host you would rather set up by hand.

    Bot tokens are shown as placeholders, never printed.
    """
    if sys.platform == "win32":
        if _is_admin():
            body = launcher_body(config_path, "run at logon by the scheduled task", False)
            return (
                f"{TASK_LAUNCHER}:\n\n" + "\n".join(body) + "\n\n"
                + subprocess.list2cmdline(_create_task_args())
            )
        body = launcher_body(
            config_path, "delete this file to stop the bot starting at logon", False
        )
        return f"{STARTUP_SCRIPT}:\n\n" + "\n".join(body)
    unit = render_unit(
        UnitOptions(
            system_scope=system_scope,
            config_path=config_path,
            user=os.environ.get("SUDO_USER"),
        )
    )
    env_text = render_service_env(reveal_tokens=False)
    if not env_text:
        return unit
    # The unit reads the tokens from service.env, which `install` writes too.
    return (
        f"{unit_path(system_scope)}:\n\n{unit}\n"
        f"{service_env_path()} (owner-only, chmod 600):\n\n{env_text}"
    )
