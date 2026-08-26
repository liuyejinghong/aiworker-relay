"""Smoke tests for the bootstrap package and CLI."""

from __future__ import annotations

import io
import unittest
from contextlib import redirect_stdout
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from unittest.mock import patch

from orchestrator import __version__
from orchestrator.cli import main


LAUNCHER_PATH = Path(__file__).resolve().parents[1] / "plugins/aiworker-relay/scripts/launch_external_workers.py"
LAUNCHER_SPEC = spec_from_file_location("external_workers_launcher", LAUNCHER_PATH)
assert LAUNCHER_SPEC and LAUNCHER_SPEC.loader
launcher = module_from_spec(LAUNCHER_SPEC)
LAUNCHER_SPEC.loader.exec_module(launcher)


class BootstrapSmokeTests(unittest.TestCase):
    def test_package_exposes_a_version(self) -> None:
        self.assertTrue(__version__)

    def test_version_command(self) -> None:
        output = io.StringIO()

        with redirect_stdout(output):
            exit_code = main(["version"])

        self.assertEqual(exit_code, 0)
        self.assertEqual(output.getvalue(), f"{__version__}\n")

    def test_help_command(self) -> None:
        output = io.StringIO()

        with redirect_stdout(output):
            with self.assertRaises(SystemExit) as result:
                main(["--help"])

        self.assertEqual(result.exception.code, 0)
        self.assertIn("usage: orch", output.getvalue())

    def test_explicit_setup_bootstraps_without_a_second_install_flag(self) -> None:
        runtime = Path("/tmp/external-workers-test/orch")
        status = {
            "python_supported": True,
            "runtime_root": "/tmp/external-workers-test/venv",
            "runtime_ready": False,
        }

        with (
            patch.object(launcher, "runtime_status", return_value=status),
            patch.object(launcher, "install_runtime", return_value=runtime) as install_runtime,
            patch.object(launcher, "run_orch", return_value=0) as run_orch,
        ):
            exit_code = launcher.main(["setup", "--no-open"])

        self.assertEqual(exit_code, 0)
        install_runtime.assert_called_once_with()
        run_orch.assert_called_once_with(runtime, ["setup", "--no-open"])
