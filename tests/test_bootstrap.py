"""Tests for setup's virtualenv bootstrap: detection, creation, and handoff."""

import contextlib
import importlib.util
import io
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from clr import setup  # noqa: E402


def args(config=None):
    return types.SimpleNamespace(config=config)


class VenvPathTest(unittest.TestCase):
    def test_layout_matches_the_platform(self):
        got = setup.venv_python(Path("/proj/venv"))
        expected = "Scripts" if sys.platform == "win32" else "bin"
        self.assertEqual(got.parent.name, expected)
        self.assertTrue(got.name.startswith("python"))

    # Debian and Ubuntu ship ensurepip separately (python3-venv); without it
    # no venv can be built here, which setup reports on its own (F-059).
    @unittest.skipIf(importlib.util.find_spec("ensurepip") is None,
                     "ensurepip is not installed (python3-venv on Debian/Ubuntu)")
    def test_creates_a_usable_interpreter(self):
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "venv"
            with mock.patch.object(setup, "VENV_DIR", target):
                with contextlib.redirect_stdout(io.StringIO()):
                    python = setup._create_venv()
            self.assertTrue(python.exists())
            out = subprocess.run(
                [str(python), "-c", "import sys; print(sys.prefix)"],
                capture_output=True, text=True,
            )
            self.assertEqual(out.stdout.strip(), str(target))

    def test_reports_a_venv_that_cannot_be_built(self):
        failed = subprocess.CompletedProcess([], 1, "", "No module named venv")
        with mock.patch.object(subprocess, "run", return_value=failed):
            with contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(setup.SetupAborted) as ctx:
                    setup._create_venv()
        self.assertIn("python3-venv", str(ctx.exception))


class HandoffTest(unittest.TestCase):
    def _capture(self, cfg=None):
        done = subprocess.CompletedProcess([], 0)
        with mock.patch.object(subprocess, "run", return_value=done) as run:
            with contextlib.redirect_stdout(io.StringIO()):
                code = setup._handoff(Path("/proj/venv/bin/python"), args(cfg))
        return code, run.call_args

    def test_reruns_setup_under_the_venv(self):
        code, call = self._capture()
        cmd = call.args[0]
        self.assertEqual(code, 0)
        self.assertEqual(cmd[0], str(Path("/proj/venv/bin/python")))
        self.assertTrue(cmd[1].endswith("run.py"))
        self.assertEqual(cmd[-1], "setup")

    def test_carries_a_custom_config_path(self):
        _, call = self._capture("/srv/other.toml")
        cmd = call.args[0]
        self.assertIn("-c", cmd)
        self.assertEqual(cmd[cmd.index("-c") + 1], "/srv/other.toml")

    def test_child_is_flagged_so_it_cannot_hand_off_again(self):
        _, call = self._capture()
        self.assertEqual(call.kwargs["env"][setup.HANDOFF_FLAG], "1")

    def test_child_exit_code_is_returned(self):
        failed = subprocess.CompletedProcess([], 3)
        with mock.patch.object(subprocess, "run", return_value=failed):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(setup._handoff(Path("py"), args()), 3)


class StepEnvTest(unittest.TestCase):
    """The step decides between using a venv, creating one, and carrying on."""

    def run_step(self, answers, *, in_venv, venv_exists, missing, flag=""):
        replies = iter(answers)
        with mock.patch.object(setup, "_in_venv", return_value=in_venv), \
             mock.patch.object(setup, "missing_deps", return_value=missing), \
             mock.patch.object(setup, "_ask_yes_no", lambda *a, **k: next(replies)), \
             mock.patch.dict(setup.os.environ, {setup.HANDOFF_FLAG: flag}), \
             mock.patch.object(Path, "exists", lambda self: venv_exists), \
             mock.patch.object(setup, "_create_venv") as create, \
             mock.patch.object(setup, "_pip_install") as pip, \
             mock.patch.object(setup, "_handoff", return_value=0) as handoff, \
             contextlib.redirect_stdout(io.StringIO()):
            result = setup._step_env(args())
        return result, create, pip, handoff

    def test_inside_a_venv_with_deps_does_nothing(self):
        result, create, pip, handoff = self.run_step(
            [], in_venv=True, venv_exists=True, missing=[]
        )
        self.assertIsNone(result)
        create.assert_not_called()
        pip.assert_not_called()
        handoff.assert_not_called()

    def test_inside_a_venv_offers_the_missing_library(self):
        result, _, pip, handoff = self.run_step(
            [True], in_venv=True, venv_exists=True, missing=["discord.py"]
        )
        self.assertIsNone(result)
        pip.assert_called_once()
        handoff.assert_not_called()

    def test_declining_the_install_still_continues(self):
        result, _, pip, _ = self.run_step(
            [False], in_venv=True, venv_exists=True, missing=["discord.py"]
        )
        self.assertIsNone(result)
        pip.assert_not_called()

    def test_outside_a_venv_creates_one_and_hands_off(self):
        result, create, pip, handoff = self.run_step(
            [True], in_venv=False, venv_exists=False, missing=["discord.py"]
        )
        self.assertEqual(result, 0)  # the child's exit code ends the wizard
        create.assert_called_once()
        pip.assert_called_once()
        handoff.assert_called_once()

    def test_outside_a_venv_reuses_an_existing_one(self):
        result, create, pip, handoff = self.run_step(
            [True], in_venv=False, venv_exists=True, missing=[]
        )
        self.assertEqual(result, 0)
        create.assert_not_called()  # reused, not rebuilt
        pip.assert_called_once()
        handoff.assert_called_once()

    def test_declining_the_venv_carries_on_with_this_interpreter(self):
        result, create, _, handoff = self.run_step(
            [False, False], in_venv=False, venv_exists=False, missing=["discord.py"]
        )
        self.assertIsNone(result)
        create.assert_not_called()
        handoff.assert_not_called()

    def test_the_child_never_hands_off_again(self):
        result, create, _, handoff = self.run_step(
            [], in_venv=False, venv_exists=True, missing=[], flag="1"
        )
        self.assertIsNone(result)
        create.assert_not_called()
        handoff.assert_not_called()


class ServiceScopeTest(unittest.TestCase):
    """systemd installs can be user-scoped or, via sudo, system-wide."""

    def ask_scope(self, answer, *, euid=1000, has_sudo=True):
        sudo = "/usr/bin/sudo" if has_sudo else None
        with mock.patch.object(setup.os, "geteuid", lambda: euid, create=True), \
             mock.patch.object(setup.shutil, "which", lambda name: sudo), \
             mock.patch.object(setup, "_ask_yes_no", lambda *a, **k: answer), \
             contextlib.redirect_stdout(io.StringIO()):
            return setup._ask_scope()

    def test_root_is_asked_directly(self):
        self.assertTrue(self.ask_scope(True, euid=0))
        self.assertFalse(self.ask_scope(False, euid=0))

    def test_an_ordinary_user_can_still_choose_system_scope(self):
        self.assertTrue(self.ask_scope(True))

    def test_declining_keeps_the_user_unit(self):
        self.assertFalse(self.ask_scope(False))

    def test_no_sudo_falls_back_to_a_user_unit(self):
        self.assertFalse(self.ask_scope(True, has_sudo=False))

    def test_sudo_install_preserves_the_virtualenv(self):
        done = subprocess.CompletedProcess([], 0)
        with mock.patch.object(subprocess, "run", return_value=done) as run:
            with contextlib.redirect_stdout(io.StringIO()):
                code = setup._sudo_install(setup.DEFAULT_CONFIG_PATH)
        cmd = run.call_args.args[0]
        self.assertEqual(code, 0)
        self.assertEqual(cmd[:2], ["sudo", "-E"])
        # The venv's interpreter, not whatever python sudo would resolve to.
        self.assertEqual(cmd[2], sys.executable)
        self.assertEqual(cmd[-3:], ["service", "install", "--system"])

    def test_sudo_install_carries_a_custom_config(self):
        done = subprocess.CompletedProcess([], 0)
        with mock.patch.object(subprocess, "run", return_value=done) as run:
            with contextlib.redirect_stdout(io.StringIO()):
                setup._sudo_install(Path("/srv/other.toml"))
        cmd = run.call_args.args[0]
        self.assertIn("-c", cmd)
        self.assertEqual(cmd[cmd.index("-c") + 1], str(Path("/srv/other.toml")))

    def test_a_refused_sudo_is_reported_not_swallowed(self):
        failed = subprocess.CompletedProcess([], 1)
        with mock.patch.object(subprocess, "run", return_value=failed):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(setup._sudo_install(setup.DEFAULT_CONFIG_PATH), 1)


if __name__ == "__main__":
    unittest.main()
