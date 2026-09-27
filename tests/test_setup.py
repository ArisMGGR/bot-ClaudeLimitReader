"""Tests for the setup wizard's pure parts: ID parsing and config rendering."""

import contextlib
import io
import os
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from clr import setup  # noqa: E402
from clr.config import ConfigError, load_config  # noqa: E402
from clr.setup import parse_ids, render_config_toml, write_config  # noqa: E402

BOTH = dict(
    discord_enabled=True,
    discord_token="discord-token",
    discord_ids=[111, 222],
    discord_guild_id=999,
    telegram_enabled=True,
    telegram_token="tg-token",
    telegram_ids=[333],
    tolerance_points=5.0,
    critical_ratio=1.5,
)


class ParseIdsTest(unittest.TestCase):
    def test_accepts_commas_and_spaces(self):
        self.assertEqual(parse_ids("1, 2  3,4", "x"), [1, 2, 3, 4])

    def test_empty_is_empty(self):
        self.assertEqual(parse_ids("   ", "x"), [])

    def test_rejects_non_numeric(self):
        with self.assertRaises(ConfigError):
            parse_ids("12, nope", "discord.allowed_user_ids")


class RenderConfigTest(unittest.TestCase):
    def _roundtrip(self, **overrides):
        answers = {**BOTH, **overrides}
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "config.toml"
            write_config(path, render_config_toml(**answers))
            # The loader lets CLR_*_TOKEN win; clear them so a developer's shell
            # cannot change what these assertions see.
            with mock.patch.dict(os.environ, {}, clear=True):
                return load_config(path)

    def test_roundtrips_through_the_real_loader(self):
        cfg = self._roundtrip()
        self.assertTrue(cfg.discord.enabled)
        self.assertEqual(cfg.discord.token, "discord-token")
        self.assertEqual(cfg.discord.allowed_user_ids, [111, 222])
        self.assertEqual(cfg.discord.guild_id, 999)
        self.assertEqual(cfg.telegram.allowed_user_ids, [333])
        self.assertEqual(cfg.pace.tolerance_points, 5.0)

    def test_omitted_guild_id_is_commented_out(self):
        cfg = self._roundtrip(discord_guild_id=None)
        self.assertIsNone(cfg.discord.guild_id)

    def test_disabled_platform_needs_no_token(self):
        cfg = self._roundtrip(
            telegram_enabled=False, telegram_token="", telegram_ids=[]
        )
        self.assertFalse(cfg.telegram.enabled)
        self.assertTrue(cfg.discord.enabled)

    def test_quotes_in_a_token_survive(self):
        # Real tokens are URL-safe, but a mistyped paste must not corrupt the file.
        cfg = self._roundtrip(discord_token='we"ird\\token')
        self.assertEqual(cfg.discord.token, 'we"ird\\token')

    def test_control_characters_in_a_token_survive(self):
        # A legacy-console Ctrl+V can paste \x16 into getpass (F-015).
        cfg = self._roundtrip(discord_token="ab\x16c\td\x7f")
        self.assertEqual(cfg.discord.token, "ab\x16c\td\x7f")

    def test_written_config_is_owner_only(self):
        if sys.platform == "win32":
            self.skipTest("POSIX permission bits only")
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "config.toml"
            write_config(path, render_config_toml(**BOTH))
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)


class StepWriteTest(unittest.TestCase):
    def test_backup_is_owner_only_and_identical(self):
        """The .bak holds the old tokens (F-014)."""
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "config.toml"
            path.write_text('[discord]\ntoken = "old-token"\n', encoding="utf-8")
            if sys.platform != "win32":
                os.chmod(path, 0o644)
            with mock.patch.object(setup, "_ask_yes_no", return_value=True), \
                 contextlib.redirect_stdout(io.StringIO()):
                setup._step_write(path, BOTH)
            backup = Path(d) / "config.toml.bak"
            self.assertIn("old-token", backup.read_text(encoding="utf-8"))
            if sys.platform != "win32":
                self.assertEqual(backup.stat().st_mode & 0o777, 0o600)
            self.assertEqual(sorted(p.name for p in Path(d).iterdir()),
                             ["config.toml", "config.toml.bak"])


class GuildPromptTest(unittest.TestCase):
    """A bad server ID is re-asked, not fatal (F-016)."""

    def ask(self, *answers):
        replies = iter(answers)
        with mock.patch.object(setup, "_ask", lambda *a, **k: next(replies)), \
             contextlib.redirect_stdout(io.StringIO()):
            return setup._ask_guild_id()

    def test_blank_means_global(self):
        self.assertIsNone(self.ask(""))

    def test_non_numeric_is_asked_again(self):
        self.assertEqual(self.ask("abc", "123"), 123)

    def test_more_than_one_id_is_asked_again(self):
        self.assertEqual(self.ask("1 2", "3"), 3)


class EnvironmentTokenPromptTest(unittest.TestCase):
    """A declined CLR_*_TOKEN must not override the token typed instead (F-061)."""

    def ask(self, use_env):
        from clr import service

        env = {k: v for k, v in os.environ.items() if not k.startswith("CLR_")}
        env["CLR_DISCORD_TOKEN"] = "old-env-token"
        with mock.patch.dict(os.environ, env, clear=True), \
             mock.patch.object(setup, "_ask_yes_no", return_value=use_env), \
             mock.patch.object(setup.getpass, "getpass", return_value="new-typed-token"), \
             contextlib.redirect_stdout(io.StringIO()):
            token = setup._ask_secret("  Bot token", "CLR_DISCORD_TOKEN")
            with tempfile.TemporaryDirectory() as d:
                path = Path(d) / "config.toml"
                write_config(path, render_config_toml(**{**BOTH, "discord_token": token}))
                loaded = load_config(path).discord.token
            return token, loaded, service.render_service_env()

    def test_declining_uses_the_typed_token_everywhere(self):
        token, loaded, service_env = self.ask(use_env=False)
        self.assertEqual(token, "new-typed-token")
        self.assertEqual(loaded, "new-typed-token")
        self.assertNotIn("old-env-token", service_env)

    def test_accepting_keeps_the_environment_token(self):
        token, loaded, service_env = self.ask(use_env=True)
        self.assertEqual(token, "")
        self.assertEqual(loaded, "old-env-token")
        self.assertIn("old-env-token", service_env)


class LoginWithoutInputTest(unittest.TestCase):
    """`login` with no stdin explains the login, not setup's config (F-055)."""

    def test_closed_stdin_names_the_login(self):
        with mock.patch("builtins.input", side_effect=EOFError), \
             contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(ConfigError) as ctx:
                setup.interactive_login()
        message = str(ctx.exception)
        self.assertIn("paste the code", message)
        self.assertNotIn("config.toml", message)

    def test_setup_prompts_keep_their_own_message(self):
        with mock.patch("builtins.input", side_effect=EOFError):
            with self.assertRaises(ConfigError) as ctx:
                setup._ask("Enable Discord?")
        self.assertIn("setup needs an interactive terminal", str(ctx.exception))


class NoTerminalMessageTest(unittest.TestCase):
    """Setup's no-terminal error names the -c file, not the default (F-065)."""

    def run_setup(self, *, tty):
        other = Path(tempfile.mkdtemp()) / "other.toml"
        with mock.patch.object(setup.sys, "stdin", mock.Mock(isatty=lambda: tty)), \
             mock.patch("builtins.input", side_effect=EOFError), \
             mock.patch.object(setup, "ensure_app_dir", return_value=other.parent), \
             mock.patch.object(setup, "_in_venv", return_value=True), \
             mock.patch.object(setup, "missing_deps", return_value=[]), \
             mock.patch.object(setup, "load_tokens", return_value=None), \
             contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(ConfigError) as ctx:
                setup.cmd_setup(types.SimpleNamespace(config=str(other)))
        return str(ctx.exception), other

    def test_no_tty_names_the_given_config(self):
        message, other = self.run_setup(tty=False)
        self.assertIn(str(other), message)
        self.assertNotIn(str(setup.DEFAULT_CONFIG_PATH), message)

    def test_input_closed_at_a_prompt_names_the_given_config(self):
        # stdin can look like a tty and still be closed (`< NUL` on Windows).
        message, other = self.run_setup(tty=True)
        self.assertIn("interactive terminal", message)
        self.assertIn(str(other), message)
        self.assertNotIn(str(setup.DEFAULT_CONFIG_PATH), message)


class ServiceStepTest(unittest.TestCase):
    """Setup's service step refuses whatever `service install` refuses (F-045)."""

    ENABLED = '[discord]\nenabled = true\ntoken = "t"\nallowed_user_ids = [1]\n'

    def step(self, *, logged_in=True, library=True):
        app = Path(tempfile.mkdtemp())
        config = app / "config.toml"
        config.write_text(self.ENABLED, encoding="utf-8")
        if logged_in:
            (app / "tokens.json").write_text("{}", encoding="utf-8")
        env = {k: v for k, v in os.environ.items() if not k.startswith("CLR_")}
        env["CLR_HOME"] = str(app)
        err = io.StringIO()
        with mock.patch.dict(os.environ, env, clear=True), \
             mock.patch("clr.service.service_app_dir", return_value=app), \
             mock.patch("importlib.util.find_spec", return_value=object() if library else None), \
             mock.patch.object(setup, "service_backend", return_value="windows"), \
             mock.patch.object(setup, "_ask_yes_no", return_value=True), \
             mock.patch.object(setup, "install_service", return_value="installed") as install, \
             mock.patch.object(setup, "service_status", return_value="status"), \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            running = setup._step_service(config)
        return running, install, err.getvalue()

    def test_everything_in_place_installs(self):
        running, install, _ = self.step()
        self.assertTrue(running)
        install.assert_called_once()

    def test_missing_library_is_not_installed(self):
        running, install, err = self.step(library=False)
        self.assertFalse(running)  # so setup does not claim the bot is running
        install.assert_not_called()
        self.assertIn("not installed", err)

    def test_missing_login_is_not_installed(self):
        running, install, err = self.step(logged_in=False)
        self.assertFalse(running)
        install.assert_not_called()
        self.assertIn("Not logged in", err)

    def systemd_step(self, env):
        """As root on systemd; the SUDO_USER already has a login of their own."""
        root_app, alice_home = Path(tempfile.mkdtemp()), Path(tempfile.mkdtemp())
        config = root_app / "config.toml"
        config.write_text(self.ENABLED, encoding="utf-8")
        (root_app / "tokens.json").write_text("{}", encoding="utf-8")
        (alice_home / ".claudelimitreader").mkdir()
        (alice_home / ".claudelimitreader" / "tokens.json").write_text("{}", encoding="utf-8")
        base = {k: v for k, v in os.environ.items() if not k.startswith(("CLR_", "SUDO_"))}
        err = io.StringIO()
        with mock.patch.dict(os.environ, {**base, **env}, clear=True), \
             mock.patch("clr.service.APP_DIR", root_app), \
             mock.patch("clr.service._home_of", lambda user: alice_home), \
             mock.patch("clr.service.os.geteuid", lambda: 0, create=True), \
             mock.patch.object(setup.os, "geteuid", lambda: 0, create=True), \
             mock.patch("importlib.util.find_spec", return_value=object()), \
             mock.patch.object(setup, "service_backend", return_value="systemd"), \
             mock.patch.object(setup, "_ask_yes_no", return_value=True), \
             mock.patch.object(setup, "install_service", return_value="installed") as install, \
             mock.patch.object(setup, "service_status", return_value="status"), \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            running = setup._step_service(config)
        return running, install, err.getvalue()

    def test_setup_under_sudo_does_not_install_for_the_sudo_user(self):
        """Root's config and login are unreadable to SUDO_USER's unit (F-045)."""
        running, install, err = self.systemd_step({"SUDO_USER": "alice"})
        self.assertFalse(running)
        install.assert_not_called()
        self.assertIn("without sudo", err)

    def test_plain_root_still_installs(self):
        running, install, _ = self.systemd_step({})
        self.assertTrue(running)
        install.assert_called_once()


class LoadConfigValidationTest(unittest.TestCase):
    """Wrong types are ConfigErrors, never silently coerced (F-009)."""

    def load(self, text, env=None):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "config.toml"
            path.write_text(text, encoding="utf-8")
            with mock.patch.dict(os.environ, env or {}, clear=True):
                return load_config(path)

    def test_bad_values_are_config_errors(self):
        for label, text in (
            # With a token and IDs, only the boolean check can refuse it (F-057).
            ("quoted false", '[discord]\nenabled = "false"\ntoken = "t"\nallowed_user_ids = [1]\n'),
            ("text tolerance", '[pace]\ntolerance_points = "x"\n'),
            ("bool ratio", "[pace]\ncritical_ratio = true\n"),
            ("negative tolerance", "[pace]\ntolerance_points = -1\n"),
            ("zero ratio", "[pace]\ncritical_ratio = 0\n"),
            ("nan tolerance", "[pace]\ntolerance_points = nan\n"),
            # Too large for a float: OverflowError before F-063.
            ("huge tolerance", "[pace]\ntolerance_points = 1" + "0" * 400 + "\n"),
            ("huge ratio", "[pace]\ncritical_ratio = 1" + "0" * 400 + "\n"),
            ("text guild", '[discord]\nguild_id = "abc"\n'),
            ("fractional id", "[telegram]\nallowed_user_ids = [1.5]\n"),
            ("bool id", "[telegram]\nallowed_user_ids = [true]\n"),
            ("numeric token", "[discord]\ntoken = 123\n"),
            ("section not a table", 'discord = "yes"\n'),
        ):
            with self.subTest(label):
                with self.assertRaises(ConfigError):
                    self.load(text)

    def test_numeric_string_ids_are_accepted(self):
        cfg = self.load('[telegram]\nallowed_user_ids = ["42"]\n')
        self.assertEqual(cfg.telegram.allowed_user_ids, [42])

    def test_an_empty_environment_variable_does_not_hide_the_file_token(self):
        cfg = self.load(
            '[discord]\nenabled = true\ntoken = "file-token"\nallowed_user_ids = [1]\n',
            env={"CLR_DISCORD_TOKEN": ""},
        )
        self.assertEqual(cfg.discord.token, "file-token")

    def test_a_named_file_must_exist(self):
        """A mistyped -c path is an error, not an empty config (F-034)."""
        missing = Path(tempfile.mkdtemp()) / "typo.toml"
        with self.assertRaises(ConfigError) as ctx:
            load_config(missing)
        self.assertIn("does not exist", str(ctx.exception))

    def test_platform_checks_can_be_skipped(self):
        """`usage` needs no bot token (F-033)."""
        text = '[discord]\nenabled = true\ntoken = ""\nallowed_user_ids = []\n'
        with self.assertRaises(ConfigError):
            self.load(text)
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "config.toml"
            path.write_text(text, encoding="utf-8")
            with mock.patch.dict(os.environ, {}, clear=True):
                cfg = load_config(path, check_platforms=False)
        self.assertTrue(cfg.discord.enabled)

    def test_unreadable_files_are_config_errors(self):
        """Decode and I/O failures are ConfigErrors, not tracebacks (F-043)."""
        with tempfile.TemporaryDirectory() as d:
            ansi = Path(d) / "ansi.toml"
            ansi.write_bytes(b"# \xe1\xed\xf4\xdf\n[pace]\n")  # cp1253 comment
            with self.assertRaises(ConfigError) as ctx:
                load_config(ansi)
            self.assertIn("UTF-8", str(ctx.exception))
            with self.assertRaises(ConfigError):
                load_config(Path(d))  # a directory given as -c

    def test_a_utf8_bom_is_accepted(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "bom.toml"
            path.write_bytes(b"\xef\xbb\xbf[pace]\ntolerance_points = 7.0\n")
            with mock.patch.dict(os.environ, {}, clear=True):
                self.assertEqual(load_config(path).pace.tolerance_points, 7.0)

    def test_a_tilde_path_is_expanded(self):
        """-c "~/x.toml" names the same file `service install` would use (F-058)."""
        with tempfile.TemporaryDirectory() as home:
            (Path(home) / "clr.toml").write_text("[pace]\ntolerance_points = 7.0\n",
                                                 encoding="utf-8")
            # expanduser reads HOME on POSIX and USERPROFILE on Windows.
            with mock.patch.dict(os.environ, {"HOME": home, "USERPROFILE": home}, clear=True):
                cfg = load_config(Path("~") / "clr.toml")
        self.assertEqual(cfg.pace.tolerance_points, 7.0)
        self.assertEqual(cfg.path, Path(home) / "clr.toml")

    def test_remembers_where_it_was_read_from(self):
        """For /status and `status` with -c (F-017)."""
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "elsewhere.toml"
            path.write_text("", encoding="utf-8")
            self.assertEqual(load_config(path).path, path)


class EmptyPathVariablesTest(unittest.TestCase):
    """An empty CLR_HOME / CLR_CONFIG counts as unset (F-032).

    The paths are computed at import, so a fresh interpreter reads them.
    """

    def test_empty_values_fall_back_to_the_defaults(self):
        env = {k: v for k, v in os.environ.items() if not k.startswith("CLR_")}
        env.update(CLR_HOME="", CLR_CONFIG="")
        out = subprocess.run(
            [sys.executable, "-B", "-c",
             "from clr.config import APP_DIR, DEFAULT_CONFIG_PATH;"
             "print(APP_DIR); print(DEFAULT_CONFIG_PATH)"],
            cwd=Path(__file__).resolve().parent.parent,
            env=env, capture_output=True, text=True, check=True,
        )
        app_dir, config = (Path(line) for line in out.stdout.splitlines())
        self.assertEqual(app_dir, Path.home() / ".claudelimitreader")
        self.assertEqual(config, app_dir / "config.toml")

    def test_relative_values_become_absolute(self):
        """A service runs elsewhere, and systemd ignores relative paths (F-039)."""
        project = Path(__file__).resolve().parent.parent
        env = {k: v for k, v in os.environ.items() if not k.startswith("CLR_")}
        env.update(CLR_HOME="state", CLR_CONFIG="cfg.toml", CLR_DISCORD_TOKEN="x")
        out = subprocess.run(
            [sys.executable, "-B", "-c",
             "from clr.config import APP_DIR, DEFAULT_CONFIG_PATH;"
             "from clr.service import SERVICE_ENV, TASK_LAUNCHER, PID_FILE;"
             "print(APP_DIR, DEFAULT_CONFIG_PATH, SERVICE_ENV, TASK_LAUNCHER, PID_FILE,"
             " sep='\\n')"],
            cwd=project, env=env, capture_output=True, text=True, check=True,
        )
        paths = [Path(line) for line in out.stdout.splitlines()]
        self.assertEqual(paths[0], project / "state")
        self.assertEqual(paths[1], project / "cfg.toml")
        for path in paths:
            self.assertTrue(path.is_absolute(), path)


if __name__ == "__main__":
    unittest.main()
