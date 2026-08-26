"""Smoke tests for the bootstrap package and CLI."""

from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from unittest.mock import patch

from orchestrator import __version__
from orchestrator.cli import main


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = REPOSITORY_ROOT / "plugins/aiworker-relay"
LAUNCHER_PATH = PLUGIN_ROOT / "scripts/launch_external_workers.py"
LAUNCHER_SPEC = spec_from_file_location("external_workers_launcher", LAUNCHER_PATH)
assert LAUNCHER_SPEC and LAUNCHER_SPEC.loader
launcher = module_from_spec(LAUNCHER_SPEC)
sys.modules[LAUNCHER_SPEC.name] = launcher
LAUNCHER_SPEC.loader.exec_module(launcher)


class BootstrapSmokeTests(unittest.TestCase):
    def test_package_exposes_a_version(self) -> None:
        self.assertTrue(__version__)

    def test_release_version_is_shared_by_manifest_and_package(self) -> None:
        expected = (PLUGIN_ROOT / "src/orchestrator/VERSION").read_text(
            encoding="utf-8"
        ).strip()
        manifest = json.loads(
            (PLUGIN_ROOT / ".codex-plugin/plugin.json").read_text(encoding="utf-8")
        )
        pyproject = (PLUGIN_ROOT / "pyproject.toml").read_text(encoding="utf-8")

        self.assertEqual(__version__, expected)
        self.assertEqual(manifest["version"], expected)
        self.assertIn('version = {file = ["src/orchestrator/VERSION"]}', pyproject)

    def test_public_marketplace_uses_the_repository_plugin_source(self) -> None:
        marketplace = json.loads(
            (REPOSITORY_ROOT / ".agents/plugins/marketplace.json").read_text(
                encoding="utf-8"
            )
        )
        source = marketplace["plugins"][0]["source"]

        self.assertEqual(source["source"], "git-subdir")
        self.assertEqual(source["url"], "https://github.com/liuyejinghong/aiworker-relay.git")
        self.assertEqual(source["path"], "./plugins/aiworker-relay")
        self.assertEqual(source["ref"], "main")

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
            patch.object(launcher, "reconcile_runtime", return_value=(runtime, None)) as reconcile,
            patch.object(launcher, "run_orch", return_value=0) as run_orch,
            patch.object(launcher, "_validate_setup_result") as validate,
        ):
            exit_code = launcher.main(["setup", "--no-open"])

        self.assertEqual(exit_code, 0)
        reconcile.assert_called_once_with()
        run_orch.assert_called_once_with(runtime, ["setup", "--no-open"])
        validate.assert_called_once_with(__version__)

    def test_active_run_defers_runtime_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime = root / "venv"
            (runtime / "bin").mkdir(parents=True)
            (runtime / "bin/python").touch()
            (runtime / "bin/orch").touch()
            snapshot = launcher.DaemonSnapshot(
                "active", version="0.1.1", active_runs=("run-1",)
            )
            with (
                patch.object(launcher, "app_data_root", return_value=root),
                patch.object(launcher, "installed_runtime_version", return_value="0.1.1"),
                patch.object(launcher, "daemon_snapshot", return_value=snapshot),
            ):
                orch, deferred = launcher.reconcile_runtime()

        self.assertIsNone(orch)
        self.assertIn("run-1", deferred or "")

    def test_failed_runtime_replacement_restores_the_previous_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime = root / "venv"
            runtime.mkdir()
            marker = runtime / "old-runtime.txt"
            marker.write_text("keep", encoding="utf-8")
            profiles = root / "profiles.json"
            profiles.write_text('{"version": 1, "profiles": []}\n', encoding="utf-8")
            with (
                patch.object(launcher, "app_data_root", return_value=root),
                patch.object(
                    launcher,
                    "_install_runtime_at",
                    side_effect=RuntimeError("simulated install failure"),
                ),
            ):
                with self.assertRaisesRegex(RuntimeError, "simulated install failure"):
                    launcher.replace_runtime()

            self.assertEqual(marker.read_text(encoding="utf-8"), "keep")
            self.assertFalse((root / "venv.previous").exists())
            self.assertEqual(
                profiles.read_text(encoding="utf-8"),
                '{"version": 1, "profiles": []}\n',
            )

    def test_dispatch_refuses_a_runtime_that_requires_setup(self) -> None:
        output = io.StringIO()
        status = {
            "python_supported": True,
            "runtime_root": "/tmp/external-workers-test/venv",
            "runtime_ready": True,
            "update_status": "update_required",
        }
        with (
            patch.object(launcher, "runtime_status", return_value=status),
            patch.object(launcher, "run_orch") as run_orch,
            redirect_stderr(output),
        ):
            exit_code = launcher.main(["dispatch", "--profile", "p"])

        self.assertEqual(exit_code, 2)
        self.assertIn("run setup first", output.getvalue())
        run_orch.assert_not_called()
