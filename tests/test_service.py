"""Tests for the rendered systemd unit and the Windows launcher command."""

import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from clr import service  # noqa: E402

SEP = chr(92)  # a literal backslash, kept out of the string literals
NO_CLR_ENV = {k: v for k, v in os.environ.items() if not k.startswith("CLR_")}


def directive(unit: str, key: str) -> list[list[str]]:
    """Every `key=` line of a unit, split into words the way systemd does.

    systemd resolves %% to % and unquotes double-quoted words with C escapes;
    shlex in POSIX mode unquotes the two escapes the renderer emits (\\\\, \\").
    """
    prefix = f"{key}="
    return [
        shlex.split(line[len(prefix):].replace("%%", "%"))
        for line in unit.splitlines()
        if line.startswith(prefix)
    ]


class RenderUnitTest(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, NO_CLR_ENV, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def render(self, **kwargs):
        return service.render_unit(service.UnitOptions(**kwargs))

    def test_user_scope_defaults(self):
        unit = self.render()
        self.assertIn("WantedBy=default.target", unit)
        self.assertNotIn("User=", unit)
        self.assertIn("Restart=on-failure", unit)
        self.assertIn("run.py", unit)

    def test_system_scope_runs_as_a_named_user(self):
        unit = self.render(system_scope=True, user="claudebot")
        self.assertIn("WantedBy=multi-user.target", unit)
        self.assertIn("User=claudebot", unit)

    def test_state_directory_stays_writable_under_protectsystem(self):
        unit = self.render()
        self.assertIn("ProtectSystem=strict", unit)
        paths = directive(unit, "ReadWritePaths")
        self.assertIn([str(service.APP_DIR)], paths)
        # '-' makes a missing logs/ skipped instead of failing the start;
        # the app dir stays required, since the bot cannot run without it.
        self.assertIn(["-" + str(service.LOG_DIR)], paths)

    def test_custom_config_path_is_passed_through(self):
        other = Path("/srv/clr/other.toml")
        unit = self.render(config_path=other)
        self.assertIn([f"CLR_CONFIG={other.absolute()}"], directive(unit, "Environment"))

    def test_default_config_path_adds_no_override(self):
        unit = self.render(config_path=service.APP_DIR / "config.toml")
        self.assertNotIn("CLR_CONFIG", unit)

    def test_unit_name(self):
        self.assertEqual(service.SERVICE_NAME, "claude-limit-reader-bot.service")
        self.assertTrue(str(service.unit_path()).endswith(service.SERVICE_NAME))
        self.assertEqual(service.unit_path(system_scope=True).parent, service.SYSTEM_UNIT_DIR)
        self.assertEqual(service.unit_path().parent, service.USER_UNIT_DIR)


class EnvironmentCarryOverTest(unittest.TestCase):
    """What the installing shell sets must reach the service."""

    def render(self, env, **kwargs):
        with mock.patch.dict(os.environ, {**NO_CLR_ENV, **env}, clear=True):
            return service.render_unit(service.UnitOptions(**kwargs))

    def test_clr_config_from_the_environment_is_carried(self):
        # DEFAULT_CONFIG_PATH already reflects this shell's CLR_CONFIG.
        elsewhere = Path("/srv/clr/cfg.toml")
        with mock.patch.object(service, "DEFAULT_CONFIG_PATH", elsewhere):
            unit = self.render({"CLR_CONFIG": str(elsewhere)})
            also = self.render({"CLR_CONFIG": str(elsewhere)}, config_path=elsewhere)
        for rendered in (unit, also):
            self.assertIn(
                [f"CLR_CONFIG={elsewhere.absolute()}"], directive(rendered, "Environment")
            )

    def test_tokens_go_to_an_environment_file_not_the_unit(self):
        unit = self.render({"CLR_DISCORD_TOKEN": "discord-secret"})
        self.assertNotIn("discord-secret", unit)
        # EnvironmentFile= takes the rest of the line; it is not split into words.
        self.assertIn(f"EnvironmentFile=-{service.SERVICE_ENV}\n", unit)

    def test_no_environment_file_without_tokens(self):
        self.assertNotIn("EnvironmentFile", self.render({}))

    def test_environment_file_holds_literal_tokens(self):
        with mock.patch.dict(os.environ, {"CLR_TELEGRAM_TOKEN": "123:abc$x"}):
            self.assertIn("CLR_TELEGRAM_TOKEN='123:abc$x'\n", service.render_service_env())

    def test_tokens_are_carried_without_surrounding_whitespace(self):
        """As the loader reads them: stripped, and a blank one counts as unset."""
        env = {**NO_CLR_ENV, "CLR_TELEGRAM_TOKEN": "123:abc\r", "CLR_DISCORD_TOKEN": "  "}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(service.token_environment(), {"CLR_TELEGRAM_TOKEN": "123:abc"})
            self.assertEqual(service.render_service_env(), "CLR_TELEGRAM_TOKEN='123:abc'\n")

    def test_a_token_that_cannot_be_stored_is_refused(self):
        with mock.patch.dict(os.environ, {"CLR_DISCORD_TOKEN": "it's"}):
            with self.assertRaises(service.ServiceError):
                service.render_service_env()


class InstallSystemdTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        for name, value in (
            ("APP_DIR", self.dir / "app"),
            ("LOG_DIR", self.dir / "logs"),
            ("SERVICE_ENV", self.dir / "app" / "service.env"),
            ("USER_UNIT_DIR", self.dir / "units"),
            ("SYSTEM_UNIT_DIR", self.dir / "system"),
        ):
            patcher = mock.patch.object(service, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        (self.dir / "app").mkdir()
        ok = subprocess.CompletedProcess([], 0, "", "")
        patcher = mock.patch.object(service, "_systemctl", return_value=ok)
        patcher.start()
        self.addCleanup(patcher.stop)

    def install(self, env):
        with mock.patch.dict(os.environ, {**NO_CLR_ENV, **env}, clear=True):
            service._install_systemd(system_scope=False, config_path=None)

    def test_tokens_are_written_owner_only(self):
        self.install({"CLR_DISCORD_TOKEN": "discord-secret"})
        env_file = self.dir / "app" / "service.env"
        self.assertIn("discord-secret", env_file.read_text(encoding="utf-8"))
        if sys.platform != "win32":
            self.assertEqual(env_file.stat().st_mode & 0o777, 0o600)

    def test_a_reinstall_without_tokens_removes_stale_ones(self):
        self.install({"CLR_DISCORD_TOKEN": "discord-secret"})
        self.install({})
        self.assertFalse((self.dir / "app" / "service.env").exists())

    def elsewhere(self):
        """As a shell whose CLR_HOME is not the one the unit was installed from."""
        other = self.dir / "other"
        return mock.patch.multiple(service, APP_DIR=other, SERVICE_ENV=other / "service.env")

    def test_uninstall_removes_the_tokens_the_unit_names(self):
        self.install({"CLR_DISCORD_TOKEN": "discord-secret"})
        installed = self.dir / "app" / "service.env"
        self.assertTrue(installed.exists())
        with self.elsewhere(), mock.patch.dict(os.environ, NO_CLR_ENV, clear=True):
            service._uninstall_systemd(system_scope=False)
        self.assertFalse(installed.exists())

    def test_a_reinstall_from_another_app_dir_removes_the_old_tokens(self):
        self.install({"CLR_DISCORD_TOKEN": "discord-secret"})
        with self.elsewhere():
            self.install({"CLR_DISCORD_TOKEN": "discord-secret"})
        self.assertFalse((self.dir / "app" / "service.env").exists())
        self.assertIn("discord-secret",
                      (self.dir / "other" / "service.env").read_text(encoding="utf-8"))

    def hand_installed(self, name, text):
        """A unit set up by hand from `service show`, naming an env file of the user's."""
        own = self.dir / "mine" / name
        own.parent.mkdir()
        own.write_text(text, encoding="utf-8")
        unit = service.unit_path(False)
        unit.parent.mkdir(parents=True, exist_ok=True)
        unit.write_text(f"[Service]\nEnvironmentFile=-{own}\n", encoding="utf-8")
        return own

    def test_a_users_own_environment_file_is_never_deleted(self):
        """Only a service.env holding just the CLR_*_TOKEN lines install writes."""
        for name, text in (
            ("my-secrets.env", "CLR_DISCORD_TOKEN='x'\n"),
            ("service.env", "CLR_DISCORD_TOKEN='x'\nOTHER_APP_KEY=keep-me\n"),
        ):
            for action in ("uninstall", "install"):
                with self.subTest(name=name, action=action):
                    own = self.hand_installed(name, text)
                    if action == "uninstall":
                        with mock.patch.dict(os.environ, NO_CLR_ENV, clear=True):
                            service._uninstall_systemd(system_scope=False)
                    else:
                        self.install({"CLR_DISCORD_TOKEN": "discord-secret"})
                    self.assertEqual(own.read_text(encoding="utf-8"), text)
                    service.unit_path(False).unlink(missing_ok=True)
                    shutil.rmtree(own.parent)

    def test_missing_log_dir_is_created(self):
        self.install({})
        self.assertTrue((self.dir / "logs").is_dir())

    def test_install_restarts_a_running_unit(self):
        """`enable --now` leaves a running unit on its old settings."""
        self.install({})
        calls = [c.args[1:] for c in service._systemctl.call_args_list]
        self.assertIn(("restart", service.SERVICE_NAME), calls)
        self.assertNotIn("--now", [word for call in calls for word in call])

    def other_scope_refuses(self, existing_scope, euid=1000):
        """Install into the scope other than `existing_scope`; return the refusal."""
        existing = service.unit_path(existing_scope)
        existing.parent.mkdir(parents=True)
        existing.write_text("[Unit]\n", encoding="utf-8")
        with mock.patch.object(service.os, "geteuid", lambda: euid, create=True), \
             mock.patch.dict(os.environ, NO_CLR_ENV, clear=True), \
             self.assertRaises(service.ServiceError) as ctx:
            service._install_systemd(system_scope=not existing_scope, config_path=None)
        self.assertFalse(service.unit_path(not existing_scope).exists())
        service._systemctl.assert_not_called()
        return str(ctx.exception)

    def test_a_user_unit_is_refused_beside_a_system_unit(self):
        """Side by side, both units would run a bot."""
        error = self.other_scope_refuses(existing_scope=True)
        self.assertIn(str(service.unit_path(True)), error)
        self.assertIn("--system", error)

    def test_a_system_unit_is_refused_beside_a_user_unit(self):
        error = self.other_scope_refuses(existing_scope=False, euid=0)
        self.assertIn(str(service.unit_path(False)), error)
        self.assertIn("service uninstall", error)

    def test_under_sudo_the_sudo_users_unit_is_found(self):
        """HOME can be root's under sudo; the user unit is SUDO_USER's."""
        alice = self.dir / "alice"
        unit = alice / service.USER_UNIT_SUBDIR / service.SERVICE_NAME
        unit.parent.mkdir(parents=True)
        unit.write_text("[Unit]\n", encoding="utf-8")
        with mock.patch.object(service.os, "geteuid", lambda: 0, create=True), \
             mock.patch.object(service, "_home_of", lambda user: alice), \
             mock.patch.dict(os.environ, {**NO_CLR_ENV, "SUDO_USER": "alice"}, clear=True), \
             self.assertRaises(service.ServiceError) as ctx:
            service._install_systemd(system_scope=True, config_path=None)
        self.assertIn(str(unit), str(ctx.exception))

    def test_status_names_a_unit_in_the_other_scope(self):
        for scope in (True, False):
            service.unit_path(scope).parent.mkdir(parents=True, exist_ok=True)
            service.unit_path(scope).write_text("[Unit]\n", encoding="utf-8")
        self.assertIn(str(service.unit_path(True)), service._status_systemd(False))
        self.assertIn("drop --system", service._status_systemd(True))


class SudoAppDirTest(unittest.TestCase):
    """Under sudo, the unit uses SUDO_USER's app directory, not root's."""

    def render(self, env):
        home = Path("/home/alice")
        with mock.patch.dict(os.environ, {**NO_CLR_ENV, "SUDO_USER": "alice", **env},
                             clear=True), \
             mock.patch.object(service.os, "geteuid", lambda: 0, create=True), \
             mock.patch.object(service, "_home_of", lambda user: home):
            return (service.service_app_dir(), service.service_env_path(),
                    service.render_unit(service.UnitOptions(system_scope=True, user="alice")))

    def test_root_home_is_not_used(self):
        app_dir, env_path, unit = self.render({})
        expected = Path("/home/alice") / service.APP_DIR.name
        self.assertEqual(app_dir, expected)
        self.assertEqual(env_path, expected / "service.env")
        self.assertIn([str(expected)], directive(unit, "ReadWritePaths"))
        self.assertIn([f"CLR_HOME={expected}"], directive(unit, "Environment"))
        self.assertNotIn("CLR_CONFIG", unit)

    def test_an_explicit_clr_home_wins(self):
        app_dir, _, _ = self.render({"CLR_HOME": str(service.APP_DIR)})
        self.assertEqual(app_dir, service.APP_DIR.absolute())


class UnitQuotingTest(unittest.TestCase):
    """Paths reach systemd as one word each, whatever they contain."""

    def setUp(self):
        patcher = mock.patch.dict(os.environ, NO_CLR_ENV, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_paths_with_spaces_are_quoted(self):
        python, run_py = "/opt/py env/bin/python", Path("/opt/a b/run.py")
        with mock.patch.object(service, "entry_point", return_value=(python, run_py)):
            unit = service.render_unit()
        self.assertEqual(directive(unit, "ExecStart"), [[python, str(run_py), "run"]])

    def test_percent_is_escaped_from_systemd_specifiers(self):
        run_py = Path("/opt/100%/run.py")
        with mock.patch.object(
            service, "entry_point", return_value=("/usr/bin/python3", run_py)
        ):
            unit = service.render_unit()
        self.assertIn("%%", unit.split("ExecStart=")[1].splitlines()[0])
        self.assertEqual(directive(unit, "ExecStart"), [["/usr/bin/python3", str(run_py), "run"]])

    def test_dollar_is_escaped_only_where_systemd_expands_it(self):
        """ExecStart= substitutes $VAR; ReadWritePaths= and Environment= do not."""
        run_py, app = Path("/opt/a$USER/run.py"), Path("/home/u/a$HOME")
        with mock.patch.object(
            service, "entry_point", return_value=("/usr/bin/python3", run_py)
        ), mock.patch.object(service, "APP_DIR", app), \
             mock.patch.dict(os.environ, {"CLR_HOME": str(app)}):
            unit = service.render_unit()
        exec_line = next(ln for ln in unit.splitlines() if ln.startswith("ExecStart="))
        self.assertIn("a$$USER", exec_line)
        words = shlex.split(exec_line[len("ExecStart="):].replace("$$", "$"))
        self.assertEqual(words, ["/usr/bin/python3", str(run_py), "run"])
        self.assertIn([str(app.absolute())], directive(unit, "ReadWritePaths"))
        self.assertIn([f"CLR_HOME={app.absolute()}"], directive(unit, "Environment"))

    def test_spaced_state_and_log_dirs_stay_single_paths(self):
        app, logs = Path("/home/u/my stuff/.clr"), Path("/home/u/My Projects/clr/logs")
        with mock.patch.object(service, "APP_DIR", app), \
             mock.patch.object(service, "LOG_DIR", logs), \
             mock.patch.dict(os.environ, {"CLR_HOME": str(app)}):
            unit = service.render_unit()
        self.assertEqual(directive(unit, "ReadWritePaths"),
                         [[str(app.absolute())], ["-" + str(logs)]])
        self.assertIn([f"CLR_HOME={app.absolute()}"], directive(unit, "Environment"))


class WindowsCommandTest(unittest.TestCase):
    def test_command_quotes_both_paths(self):
        py = "C:" + SEP + "Program Files" + SEP + "pythonw.exe"
        run_py = "C:" + SEP + "my bot" + SEP + "run.py"
        with mock.patch.object(service, "_windows_python", return_value=py):
            with mock.patch.object(
                service, "entry_point", return_value=("py", Path(run_py))
            ):
                cmd = service.task_command()
        self.assertEqual(cmd, f'"{py}" "{run_py}" run')

    def test_task_name_has_no_service_suffix(self):
        self.assertEqual(service.TASK_NAME, "claude-limit-reader-bot")


class WindowsLauncherTest(unittest.TestCase):
    def body(self, env, **kwargs):
        with mock.patch.dict(os.environ, {**NO_CLR_ENV, **env}, clear=True):
            return service.launcher_body(None, "test", **kwargs)

    def test_switches_to_utf8_before_any_path(self):
        body = self.body({})
        self.assertEqual(body[:2], ["@echo off", "chcp 65001 >nul"])

    def test_environment_and_tokens_are_set(self):
        body = self.body({"CLR_HOME": str(service.APP_DIR), "CLR_DISCORD_TOKEN": "tok"})
        self.assertIn(f'set "CLR_HOME={service.APP_DIR.absolute()}"', body)
        self.assertIn('set "CLR_DISCORD_TOKEN=tok"', body)

    def test_show_hides_tokens(self):
        """`service show` itself, in both install modes, never prints a token."""
        env = {**NO_CLR_ENV, "CLR_DISCORD_TOKEN": "sekrit-7f3a"}
        for elevated in (True, False):
            with self.subTest(elevated=elevated), \
                 mock.patch.dict(os.environ, env, clear=True), \
                 mock.patch.object(service.sys, "platform", "win32"), \
                 mock.patch.object(service, "_is_admin", return_value=elevated):
                shown = service.show()
            self.assertNotIn("sekrit-7f3a", shown)
            self.assertIn('set "CLR_DISCORD_TOKEN=<your CLR_DISCORD_TOKEN>"', shown)

    def test_percent_is_doubled(self):
        with mock.patch.object(service, "task_command", return_value='"C:\\100%\\py" run'):
            body = self.body({})
        self.assertEqual(body[-1], 'start "" "C:\\100%%\\py" run')

    @unittest.skipUnless(sys.platform == "win32", "runs cmd.exe")
    def test_launcher_survives_a_non_ascii_path(self):
        root = Path(tempfile.mkdtemp()) / "Γιώργος 100%"
        root.mkdir()
        marker = root / "ran.txt"
        probe = root / "probe.py"
        probe.write_text(
            f"import os; open({str(marker)!r}, 'w', encoding='utf-8')"
            ".write(os.environ.get('CLR_HOME', ''))",
            encoding="utf-8",
        )
        launcher = root / "launch.cmd"
        # `start` would return before the probe ran; run it in the foreground.
        command = f'"{sys.executable}" "{probe}"'
        with mock.patch.object(service, "task_command", return_value=command), \
             mock.patch.object(service, "APP_DIR", root), \
             mock.patch.dict(os.environ, {"CLR_HOME": str(root)}):
            service._write_launcher(launcher, None, "test")
        text = launcher.read_text(encoding="utf-8").replace('start "" ', "")
        launcher.write_text(text, encoding="utf-8")
        # A console of its own, as at logon: in this test's console, chcp would
        # outlive the run and let a later launcher without it pass.
        subprocess.run(["cmd", "/c", str(launcher)], capture_output=True, timeout=60,
                       creationflags=subprocess.CREATE_NO_WINDOW)
        self.assertTrue(marker.exists(), "the launcher did not run the script")
        self.assertEqual(marker.read_text(encoding="utf-8"), str(root.absolute()))


class TaskActionTest(unittest.TestCase):
    """The scheduled task's command line reaches the launcher from any path."""

    @unittest.skipUnless(sys.platform == "win32", "runs cmd.exe")
    def test_launcher_runs_from_paths_with_cmd_metacharacters(self):
        root = Path(tempfile.mkdtemp())
        for name in ("plain", "with space", "A&B x", "no_space(x)"):
            with self.subTest(name):
                folder = root / name
                folder.mkdir()
                marker = folder / "ran.txt"
                launcher = folder / f"{service.TASK_NAME}.cmd"
                launcher.write_text(f'@echo off\r\necho ok> "{marker}"\r\n', encoding="utf-8")
                with mock.patch.object(service, "TASK_LAUNCHER", launcher):
                    action = service._task_action()
                # Task Scheduler hands /TR to CreateProcess as one command line.
                subprocess.run(action, capture_output=True, timeout=60)
                self.assertTrue(marker.exists(), action)


class WindowsReinstallTest(unittest.TestCase):
    """A re-install replaces the running bot instead of adding a second."""

    def test_running_instance_is_stopped_before_the_new_one_starts(self):
        order = []
        with mock.patch.object(service, "_is_admin", return_value=False), \
             mock.patch.object(service, "_task_exists", return_value=False), \
             mock.patch.object(service, "_write_launcher"), \
             mock.patch.object(service, "_stop_detached",
                               side_effect=lambda: order.append("stop") or "stopped"), \
             mock.patch.object(service, "_spawn_detached",
                               side_effect=lambda cfg: order.append("spawn") or 42):
            note = service._install_windows(None)
        self.assertEqual(order, ["stop", "spawn"])
        self.assertIn("already running", note)

    def test_elevated_reinstall_stops_the_old_bot_before_the_task_runs(self):
        """The elevated path starts the bot with `schtasks /Run`, not a spawn."""
        order = []

        def run(cmd):
            order.append(cmd[1])  # /Create, /Run
            return subprocess.CompletedProcess(cmd, 0, "", "")

        # A private Startup path, so the real one is never touched.
        startup = Path(tempfile.mkdtemp()) / "Startup" / "claude-limit-reader-bot.cmd"
        with mock.patch.object(service, "_is_admin", return_value=True), \
             mock.patch.object(service, "STARTUP_SCRIPT", startup), \
             mock.patch.object(service, "_write_launcher"), \
             mock.patch.object(service, "_run", side_effect=run), \
             mock.patch.object(service, "_stop_detached",
                               side_effect=lambda: order.append("stop") or "stopped"):
            note = service._install_windows(None)
        self.assertEqual(order, ["stop", "/Create", "/Run"])
        self.assertIn("already running", note)

    def test_install_time_bot_gets_the_launchers_clr_config(self):
        """Not this shell's CLR_CONFIG, which the logon launcher would not set."""
        env = {**NO_CLR_ENV, "CLR_HOME": str(service.APP_DIR), "CLR_CONFIG": "~nosuchuser/c.toml"}
        for config_path, expected in (
            (service.APP_DIR / "config.toml", None),
            (Path("/srv/clr/other.toml"), str(Path("/srv/clr/other.toml").absolute())),
        ):
            with self.subTest(config=str(config_path)), \
                 mock.patch.dict(os.environ, env, clear=True), \
                 mock.patch.object(service.subprocess, "Popen") as popen, \
                 mock.patch.object(service, "_write_pid_file"):
                service._spawn_detached(config_path)
            self.assertEqual(popen.call_args.kwargs["env"].get("CLR_CONFIG"), expected)

    def test_launcher_marks_the_bot_as_the_service(self):
        with mock.patch.dict(os.environ, NO_CLR_ENV, clear=True):
            body = service.launcher_body(None, "test")
        self.assertIn(f'set "{service.SERVICE_FLAG}=1"', body)
        self.assertTrue(body[-1].startswith("start "))

    def test_a_service_instance_records_itself(self):
        pid_file = Path(tempfile.mkdtemp()) / "service.pid"
        with mock.patch.object(service, "PID_FILE", pid_file), \
             mock.patch.object(service.sys, "platform", "win32"), \
             mock.patch.object(service, "_process_started", return_value=777):
            with mock.patch.dict(os.environ, {service.SERVICE_FLAG: ""}):
                service.record_running_instance()
                self.assertFalse(pid_file.exists())  # a plain `run` is not the service
            with mock.patch.dict(os.environ, {service.SERVICE_FLAG: "1"}):
                service.record_running_instance()
        self.assertEqual(pid_file.read_text(encoding="utf-8"), f"{os.getpid()} 777")


class WindowsModeSwitchTest(unittest.TestCase):
    """The Startup launcher and the scheduled task never coexist."""

    def setUp(self):
        root = Path(tempfile.mkdtemp())
        for name, value in (
            ("STARTUP_SCRIPT", root / "Startup" / "claude-limit-reader-bot.cmd"),
            ("TASK_LAUNCHER", root / "app" / "claude-limit-reader-bot.cmd"),
            ("PID_FILE", root / "app" / "service.pid"),
        ):
            patcher = mock.patch.object(service, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.dict(os.environ, NO_CLR_ENV, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_elevated_install_removes_the_startup_launcher(self):
        with mock.patch.object(service, "_is_admin", return_value=False), \
             mock.patch.object(service, "_task_exists", return_value=False), \
             mock.patch.object(service, "_spawn_detached", return_value=1):
            service._install_windows(None)
        self.assertTrue(service.STARTUP_SCRIPT.exists())

        ok = subprocess.CompletedProcess([], 0, "", "")
        with mock.patch.object(service, "_is_admin", return_value=True), \
             mock.patch.object(service, "_run", return_value=ok):
            note = service._install_windows(None)
        self.assertTrue(service.TASK_LAUNCHER.exists())
        self.assertFalse(service.STARTUP_SCRIPT.exists())
        self.assertIn("Removed the Startup-folder launcher", note)

    def test_unelevated_install_refuses_beside_a_task(self):
        with mock.patch.object(service, "_is_admin", return_value=False), \
             mock.patch.object(service, "_task_exists", return_value=True), \
             mock.patch.object(service, "_stop_detached") as stop, \
             mock.patch.object(service, "_spawn_detached") as spawn:
            with self.assertRaises(service.ServiceError) as ctx:
                service._install_windows(None)
        self.assertIn("elevated", str(ctx.exception))
        stop.assert_not_called()  # the running bot is left alone
        spawn.assert_not_called()
        self.assertFalse(service.STARTUP_SCRIPT.exists())


class WindowsStatePathTest(unittest.TestCase):
    """uninstall and status work from a shell without the install's CLR_HOME."""

    def test_launcher_and_pid_file_do_not_follow_clr_home(self):
        project = Path(__file__).resolve().parent.parent
        code = "from clr import service; print(service.TASK_LAUNCHER); print(service.PID_FILE)"
        seen = []
        for name in ("one", "two"):
            env = {**NO_CLR_ENV, "CLR_HOME": str(Path(tempfile.mkdtemp()) / name)}
            out = subprocess.run([sys.executable, "-B", "-c", code], cwd=project, env=env,
                                 capture_output=True, text=True, check=True)
            seen.append(out.stdout)
        self.assertEqual(seen[0], seen[1])

    def test_uninstall_removes_this_and_the_earlier_launcher_location(self):
        """Both hold the tokens; earlier versions kept the launcher in the app dir."""
        root = Path(tempfile.mkdtemp())
        launcher, legacy = root / "state" / "bot.cmd", root / "app" / "bot.cmd"
        for path in (launcher, legacy):
            path.parent.mkdir()
            path.write_text('set "CLR_DISCORD_TOKEN=x"', encoding="utf-8")
        ok = subprocess.CompletedProcess([], 0, "", "")
        with mock.patch.multiple(service, TASK_LAUNCHER=launcher, LEGACY_TASK_LAUNCHER=legacy,
                                 STARTUP_SCRIPT=root / "Startup" / "bot.cmd",
                                 PID_FILE=root / "state" / "service.pid"), \
             mock.patch.object(service, "_run", return_value=ok):
            note = service._uninstall_windows()
        self.assertFalse(launcher.exists())
        self.assertFalse(legacy.exists())
        self.assertIn(str(legacy), note)


class WindowsStatusTest(unittest.TestCase):
    """Both install modes say whether the bot runs and where it logs."""

    TASK = ("TaskName: \\claude-limit-reader-bot\nNext Run Time: N/A\nStatus: Ready\n"
            "Last Run Time: N/A\nLast Result: 0\n")

    def status(self, *, task, started):
        root = Path(tempfile.mkdtemp())
        pid_file, startup = root / "service.pid", root / "Startup" / "bot.cmd"
        pid_file.write_text("4242 1000", encoding="utf-8")
        startup.parent.mkdir()
        startup.write_text("", encoding="utf-8")
        query = subprocess.CompletedProcess([], 0 if task else 1, self.TASK if task else "", "")
        with mock.patch.object(service, "PID_FILE", pid_file), \
             mock.patch.object(service, "STARTUP_SCRIPT", startup), \
             mock.patch.object(service, "_run", return_value=query), \
             mock.patch.object(service, "_process_started", return_value=started):
            return service._status_windows()

    def test_scheduled_task_reports_the_running_bot_and_its_logs(self):
        text = self.status(task=True, started=1000)
        self.assertIn("Status: Ready", text)
        self.assertIn("running, PID 4242", text)
        self.assertIn(str(service.LOG_DIR), text)

    def test_scheduled_task_reports_an_exited_bot(self):
        self.assertIn("has exited", self.status(task=True, started=None))

    def test_startup_launcher_reports_the_same(self):
        text = self.status(task=False, started=1000)
        self.assertIn("running, PID 4242", text)
        self.assertIn(str(service.LOG_DIR), text)


class ShowOutputTest(unittest.TestCase):
    def test_printed_schtasks_command_quotes_the_action(self):
        """Pasted as printed, /TR must stay one argument."""
        with mock.patch.dict(os.environ, NO_CLR_ENV, clear=True), \
             mock.patch.object(service.sys, "platform", "win32"), \
             mock.patch.object(service, "_is_admin", return_value=True):
            line = service.show().splitlines()[-1]
        self.assertEqual(line, subprocess.list2cmdline(service._create_task_args()))
        self.assertIn('/TR "cmd.exe /s /c \\"\\"', line)

    def test_systemd_show_includes_the_environment_file(self):
        """The unit reads service.env, so show prints it, token hidden."""
        env = {**NO_CLR_ENV, "CLR_DISCORD_TOKEN": "sekrit-7f3a"}
        with mock.patch.dict(os.environ, env, clear=True), \
             mock.patch.object(service.sys, "platform", "linux"):
            shown = service.show()
        self.assertIn(f"{service.service_env_path()} (owner-only", shown)
        self.assertIn("CLR_DISCORD_TOKEN='<your CLR_DISCORD_TOKEN>'", shown)
        self.assertNotIn("sekrit-7f3a", shown)


class ShowMatchesInstallTest(unittest.TestCase):
    """`service show` prints what `install` writes."""

    def test_system_unit_names_the_sudo_user(self):
        env = {**NO_CLR_ENV, "SUDO_USER": "alice"}
        with mock.patch.dict(os.environ, env, clear=True), \
             mock.patch.object(service.sys, "platform", "linux"):
            unit = service.show(system_scope=True)
        self.assertIn("User=alice", unit)

    def test_windows_launcher_matches_the_installed_one(self):
        env = {**NO_CLR_ENV, "CLR_HOME": str(service.APP_DIR)}
        with mock.patch.dict(os.environ, env, clear=True), \
             mock.patch.object(service.sys, "platform", "win32"), \
             mock.patch.object(service, "_is_admin", return_value=False):
            shown = service.show()
            installed = service.launcher_body(
                None, "delete this file to stop the bot starting at logon"
            )
        self.assertEqual(shown.split("\n\n", 1)[1], "\n".join(installed))


class PidFileTest(unittest.TestCase):
    """A recycled PID must never be killed."""

    def setUp(self):
        self.pid_file = Path(tempfile.mkdtemp()) / "service.pid"
        patcher = mock.patch.object(service, "PID_FILE", self.pid_file)
        patcher.start()
        self.addCleanup(patcher.stop)

    def stop(self, started_now):
        with mock.patch.object(service, "_process_started", return_value=started_now), \
             mock.patch.object(service, "_run") as run:
            run.return_value = subprocess.CompletedProcess([], 0)
            state = service._stop_detached()
        return state, run

    def test_same_process_is_stopped(self):
        self.pid_file.write_text("4242 1000", encoding="utf-8")
        state, run = self.stop(started_now=1000)
        self.assertEqual(state, "stopped")
        run.assert_called_once()

    def test_recycled_pid_is_left_alone(self):
        self.pid_file.write_text("4242 1000", encoding="utf-8")
        state, run = self.stop(started_now=2000)
        self.assertEqual(state, "not running")
        run.assert_not_called()

    def test_legacy_pid_only_file_is_never_trusted(self):
        self.pid_file.write_text("4242", encoding="utf-8")
        state, run = self.stop(started_now=1000)
        self.assertEqual(state, "not running")
        run.assert_not_called()
        self.assertFalse(self.pid_file.exists())

    @unittest.skipUnless(sys.platform == "win32", "Windows process API")
    def test_creation_time_of_real_processes(self):
        self.assertIsNotNone(service._process_started(os.getpid()))
        child = subprocess.Popen([sys.executable, "-c", "pass"])
        child.wait()
        self.assertIsNone(service._process_started(child.pid))


class ScopeMixupTest(unittest.TestCase):
    """The two scopes are separate registries; say so instead of 'no unit'."""

    def only(self, existing_scope):
        """Pretend a unit exists in exactly one scope."""
        target = service.unit_path(existing_scope)
        return mock.patch.object(Path, "exists", lambda self: self == target)

    def test_uninstall_points_at_the_system_unit(self):
        with self.only(True):
            with self.assertRaises(service.ServiceError) as ctx:
                service._uninstall_systemd(system_scope=False)
        self.assertIn("--system", str(ctx.exception))

    def test_uninstall_points_at_the_user_unit(self):
        with self.only(False):
            with self.assertRaises(service.ServiceError) as ctx:
                service._uninstall_systemd(system_scope=True)
        self.assertIn("drop --system", str(ctx.exception))

    def test_no_hint_when_neither_scope_has_one(self):
        with mock.patch.object(Path, "exists", lambda self: False):
            with self.assertRaises(service.ServiceError) as ctx:
                service._uninstall_systemd(system_scope=False)
        self.assertNotIn("--system", str(ctx.exception))

    def test_status_mentions_the_other_scope(self):
        with self.only(True):
            text = service._status_systemd(system_scope=False)
        self.assertIn("Not installed", text)
        self.assertIn("--system", text)

    def test_removing_a_system_unit_without_root_is_refused_cleanly(self):
        with self.only(True), mock.patch.object(service.os, "geteuid", lambda: 1000,
                                                create=True):
            with mock.patch.object(service, "_systemctl") as systemctl:
                with self.assertRaises(service.ServiceError) as ctx:
                    service._uninstall_systemd(system_scope=True)
        self.assertIn("needs root", str(ctx.exception))
        # Refused before touching the unit, so nothing is half-removed.
        systemctl.assert_not_called()


class BackendTest(unittest.TestCase):
    def test_no_manager_reports_empty(self):
        with mock.patch.object(service.sys, "platform", "linux"):
            with mock.patch.object(service.shutil, "which", return_value=None):
                self.assertEqual(service.backend(), "")

    def test_install_without_a_manager_explains_itself(self):
        with mock.patch.object(service, "backend", return_value=""):
            with self.assertRaises(service.ServiceError) as ctx:
                service.install()
        self.assertIn("run.py run", str(ctx.exception))

    def test_uninstall_without_a_manager_says_the_same(self):
        """uninstall names what is missing, as install does."""
        with mock.patch.object(service, "backend", return_value=""):
            with self.assertRaises(service.ServiceError) as uninstall:
                service.uninstall()
            with self.assertRaises(service.ServiceError) as install:
                service.install()
        self.assertEqual(str(uninstall.exception), str(install.exception))


if __name__ == "__main__":
    unittest.main()
