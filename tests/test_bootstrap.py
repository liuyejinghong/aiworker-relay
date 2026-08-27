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
from unittest.mock import MagicMock, patch

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
    def test_launcher_loopback_opener_disables_redirects(self) -> None:
        opener = MagicMock()
        response = MagicMock()
        response.status = 200
        response.read.return_value = b"{}"
        opener.open.return_value.__enter__.return_value = response
        with patch.object(
            launcher.urllib.request, "build_opener", return_value=opener
        ) as build_opener:
            launcher._local_request("http://127.0.0.1:49178", "/api/health")

        self.assertTrue(
            any(
                isinstance(handler, launcher._NoRedirect)
                for handler in build_opener.call_args.args
            )
        )

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
        codex_path = Path("/opt/test/codex")
        node_path = Path("/opt/test/node")
        status = {
            "python_supported": True,
            "runtime_root": "/tmp/external-workers-test/venv",
            "runtime_ready": False,
        }

        with (
            patch.object(launcher, "runtime_status", return_value=status),
            patch.object(
                launcher,
                "reconcile_runtime",
                return_value=(runtime, None, False),
            ) as reconcile,
            patch.object(launcher, "current_codex_cli", return_value=codex_path),
            patch.object(launcher, "current_node_cli", return_value=node_path),
            patch.object(launcher, "ensure_macos_persistent_entry") as persistent_entry,
            patch.object(launcher, "run_orch", return_value=0) as run_orch,
            patch.object(launcher, "_validate_setup_result") as validate,
        ):
            exit_code = launcher.main(["setup", "--no-open"])

        self.assertEqual(exit_code, 0)
        reconcile.assert_called_once_with()
        persistent_entry.assert_called_once_with(
            runtime=runtime.parent.parent,
            project_root=Path.cwd(),
            codex_path=codex_path,
            node_path=node_path,
            allow_loaded_replacement=False,
        )
        run_orch.assert_called_once_with(
            runtime,
            [
                "setup",
                "--port",
                "49178",
                "--persistent",
                "--codex-path",
                "/opt/test/codex",
                "--no-open",
            ],
        )
        validate.assert_called_once_with(__version__, project_root=Path.cwd())

    def test_orch_setup_forwards_the_persistent_endpoint(self) -> None:
        output = io.StringIO()

        with (
            patch("orchestrator.cli.ensure_daemon", return_value="http://127.0.0.1:49178") as ensure,
            redirect_stdout(output),
        ):
            exit_code = main(
                ["setup", "--no-open", "--port", "49178", "--persistent"]
            )

        self.assertEqual(exit_code, 0)
        ensure.assert_called_once_with(
            data_dir=None,
            project_root=None,
            port=49178,
            persistent=True,
            codex_path=None,
        )
        self.assertEqual(output.getvalue(), "http://127.0.0.1:49178\n")

    def test_macos_launch_agent_uses_only_the_local_daemon_arguments(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime = root / "venv"
            project = root / "project"
            codex_path = Path("/opt/homebrew/bin/codex")
            node_path = Path("/opt/homebrew/bin/node")
            with patch.object(launcher, "app_data_root", return_value=root / "app-data"):
                payload = launcher.macos_launch_agent_payload(
                    runtime=runtime,
                    project_root=project,
                    codex_path=codex_path,
                    node_path=node_path,
                )

        arguments = payload["ProgramArguments"]
        self.assertEqual(payload["Label"], "com.aiworker.relay")
        self.assertEqual(payload["RunAtLoad"], True)
        self.assertEqual(payload["KeepAlive"], {"SuccessfulExit": False})
        self.assertEqual(
            payload["EnvironmentVariables"],
            {"PATH": "/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"},
        )
        self.assertEqual(
            arguments,
            [
                str(runtime / "bin/external-workersd"),
                "--serve",
                "--data-dir",
                str(root / "app-data"),
                "--project-root",
                str(project.resolve()),
                "--port",
                "49178",
                "--persistent",
                "--codex-path",
                str(codex_path),
            ],
        )
        self.assertNotIn("key", " ".join(arguments).lower())

    def test_loaded_launch_agent_without_verified_daemon_is_not_booted_out(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with (
                patch.object(launcher.sys, "platform", "darwin"),
                patch.object(
                    launcher,
                    "daemon_snapshot",
                    return_value=launcher.DaemonSnapshot("missing"),
                ),
                patch.object(
                    launcher.subprocess,
                    "run",
                    return_value=MagicMock(returncode=0),
                ) as run,
                patch.object(launcher, "_write_launch_agent") as write_agent,
            ):
                with self.assertRaisesRegex(
                    launcher.RuntimeUpdateError, "no verified daemon identity"
                ):
                    launcher.ensure_macos_persistent_entry(
                        runtime=root / "venv",
                        project_root=root / "project",
                        codex_path=None,
                        node_path=None,
                    )

        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.args[0][1], "print")
        write_agent.assert_not_called()

    def test_failed_new_launch_agent_reports_that_the_entry_changed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with (
                patch.object(launcher.sys, "platform", "darwin"),
                patch.object(
                    launcher,
                    "daemon_snapshot",
                    return_value=launcher.DaemonSnapshot("missing"),
                ),
                patch.object(
                    launcher.subprocess,
                    "run",
                    side_effect=[
                        MagicMock(returncode=1),
                        MagicMock(returncode=0),
                    ],
                ),
                patch.object(launcher, "_assert_fixed_port_is_available"),
                patch.object(launcher, "_write_launch_agent"),
                patch.object(
                    launcher,
                    "_wait_for_persistent_daemon",
                    side_effect=launcher.RuntimeUpdateError("health failed"),
                ),
                self.assertRaisesRegex(
                    launcher.PersistentEntryChangedError, "health failed"
                ),
            ):
                launcher.ensure_macos_persistent_entry(
                    runtime=root / "venv",
                    project_root=root / "project",
                    codex_path=None,
                    node_path=None,
                )

    def test_wait_for_fixed_port_release_retries_after_owned_entry_teardown(self) -> None:
        with (
            patch.object(
                launcher,
                "_assert_fixed_port_is_available",
                side_effect=[launcher.RuntimeUpdateError("port busy"), None],
            ) as probe,
            patch.object(launcher.time, "sleep") as sleep,
        ):
            launcher._wait_for_fixed_port_release()

        self.assertEqual(probe.call_count, 2)
        sleep.assert_called_once_with(0.05)

    def test_fixed_port_probe_allows_normal_loopback_reuse(self) -> None:
        socket_factory = MagicMock()
        probe = socket_factory.return_value.__enter__.return_value
        with patch.object(launcher.socket, "socket", socket_factory):
            launcher._assert_fixed_port_is_available()

        probe.setsockopt.assert_called_once_with(
            launcher.socket.SOL_SOCKET,
            launcher.socket.SO_REUSEADDR,
            1,
        )
        probe.bind.assert_called_once_with(("127.0.0.1", 49178))

    def test_idle_shutdown_waits_for_fixed_port_release_after_exit(self) -> None:
        snapshot = launcher.DaemonSnapshot(
            "idle",
            pid=123,
            endpoint="http://127.0.0.1:49178",
            capability="test-capability",
        )
        with (
            patch.object(launcher, "_local_request", return_value=(200, {})),
            patch.object(launcher, "_wait_for_exit", return_value=True),
            patch.object(launcher, "_wait_for_fixed_port_release") as wait_for_port,
        ):
            launcher.shutdown_idle_daemon(snapshot)

        wait_for_port.assert_called_once_with()

    def test_launcher_marks_live_legacy_daemon_without_capability_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "daemon.json").write_text(
                json.dumps({"pid": 123, "port": 49178}), encoding="utf-8"
            )
            with patch.object(launcher, "_process_state", return_value=True):
                snapshot = launcher.daemon_snapshot(root)

        self.assertEqual(snapshot.state, "unknown")
        self.assertIn("capability", snapshot.reason or "")

    def test_launcher_validates_identity_and_sends_capability(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = root / "project"
            project.mkdir()
            record = {
                "pid": 123,
                "port": 61234,
                "project_root": str(project.resolve()),
                "runtime_root": str(project.resolve() / ".orch"),
                "version": "0.1.0",
                "persistent": True,
                "capability": "test-capability",
            }
            (root / "daemon.json").write_text(json.dumps(record), encoding="utf-8")
            with (
                patch.object(launcher, "_process_state", return_value=True),
                patch.object(
                    launcher,
                    "_local_request",
                    side_effect=[
                        (200, {"ok": True, **{key: record[key] for key in record if key != "capability"}}),
                        (200, {"runs": [], "active_run_ids": []}),
                    ],
                ) as request,
            ):
                snapshot = launcher.daemon_snapshot(root)

        self.assertEqual(snapshot.state, "idle")
        self.assertEqual(snapshot.capability, "test-capability")
        self.assertEqual(request.call_args_list[0].kwargs["capability"], "test-capability")
        self.assertEqual(request.call_args_list[1].kwargs["capability"], "test-capability")

    def test_launcher_uses_authoritative_active_run_ids(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = root / "project"
            project.mkdir()
            record = {
                "pid": 123,
                "port": 61234,
                "project_root": str(project.resolve()),
                "runtime_root": str(project.resolve() / ".orch"),
                "version": "0.1.0",
                "persistent": True,
                "capability": "test-capability",
            }
            (root / "daemon.json").write_text(json.dumps(record), encoding="utf-8")
            with (
                patch.object(launcher, "_process_state", return_value=True),
                patch.object(
                    launcher,
                    "_local_request",
                    side_effect=[
                        (200, {"ok": True, **{key: record[key] for key in record if key != "capability"}}),
                        (
                            200,
                            {
                                "runs": [{"run_id": "run-1", "status": "succeeded"}],
                                "active_run_ids": ["run-1"],
                            },
                        ),
                    ],
                ),
            ):
                snapshot = launcher.daemon_snapshot(root)

        self.assertEqual(snapshot.state, "active")
        self.assertEqual(snapshot.active_runs, ("run-1",))

    def test_launcher_never_signals_a_daemon_for_missing_shutdown_endpoint(self) -> None:
        snapshot = launcher.DaemonSnapshot(
            "idle",
            pid=123,
            endpoint="http://127.0.0.1:49178",
            capability="test-capability",
        )
        with (
            patch.object(launcher, "_local_request", return_value=(404, {})),
            patch.object(launcher, "_wait_for_exit") as wait_for_exit,
        ):
            with self.assertRaises(launcher.RuntimeUpdateError):
                launcher.shutdown_idle_daemon(snapshot)
        wait_for_exit.assert_not_called()

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
                orch, deferred, stopped = launcher.reconcile_runtime()

        self.assertIsNone(orch)
        self.assertIn("run-1", deferred or "")
        self.assertFalse(stopped)

    def test_pending_previous_marker_prevents_runtime_status_from_being_up_to_date(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime = root / "venv/bin"
            runtime.mkdir(parents=True)
            (runtime / "python").touch()
            (runtime / "orch").touch()
            (root / "venv.previous").mkdir()
            snapshot = launcher.DaemonSnapshot(
                "idle",
                version=__version__,
                endpoint="http://127.0.0.1:49178",
                persistent=True,
                project_root=str(Path.cwd().resolve()),
            )
            with (
                patch.object(launcher, "app_data_root", return_value=root),
                patch.object(launcher, "installed_runtime_version", return_value=__version__),
                patch.object(launcher, "daemon_snapshot", return_value=snapshot),
            ):
                status = launcher.runtime_status()

            self.assertEqual(status["update_status"], "update_required")

    def test_runtime_status_rejects_a_control_plane_for_another_project(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime = root / "venv/bin"
            runtime.mkdir(parents=True)
            (runtime / "python").touch()
            (runtime / "orch").touch()
            snapshot = launcher.DaemonSnapshot(
                "idle",
                version=__version__,
                endpoint="http://127.0.0.1:49178",
                persistent=True,
                project_root=str(root / "another-project"),
            )
            with (
                patch.object(launcher, "app_data_root", return_value=root),
                patch.object(launcher, "installed_runtime_version", return_value=__version__),
                patch.object(launcher, "daemon_snapshot", return_value=snapshot),
            ):
                status = launcher.runtime_status()

            self.assertEqual(status["update_status"], "update_required")

    def test_idle_transient_daemon_is_restarted_for_the_stable_entry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime = root / "venv"
            (runtime / "bin").mkdir(parents=True)
            (runtime / "bin/python").touch()
            orch = runtime / "bin/orch"
            orch.touch()
            snapshot = launcher.DaemonSnapshot(
                "idle",
                version=__version__,
                endpoint="http://127.0.0.1:61234",
            )
            with (
                patch.object(launcher, "app_data_root", return_value=root),
                patch.object(launcher, "installed_runtime_version", return_value=__version__),
                patch.object(launcher, "daemon_snapshot", return_value=snapshot),
                patch.object(launcher, "shutdown_idle_daemon") as shutdown,
            ):
                actual_orch, deferred, stopped = launcher.reconcile_runtime()

        self.assertEqual(actual_orch, orch)
        self.assertIsNone(deferred)
        self.assertTrue(stopped)
        shutdown.assert_called_once_with(snapshot)

    def test_install_failure_after_idle_shutdown_restores_the_old_control_plane(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._prepare_candidate_transaction(root)
            old_snapshot = launcher.DaemonSnapshot(
                "idle",
                version="0.1.0",
                endpoint="http://127.0.0.1:49178",
                persistent=True,
                project_root=str(Path.cwd().resolve()),
            )
            stale_snapshot = launcher.DaemonSnapshot("stale")
            ensure = MagicMock()
            run_orch = MagicMock(return_value=0)
            validate = MagicMock()
            with (
                patch.object(launcher, "app_data_root", return_value=root),
                patch.object(launcher, "bundle_version", return_value=__version__),
                patch.object(launcher, "installed_runtime_version", return_value="0.1.0"),
                patch.object(
                    launcher,
                    "daemon_snapshot",
                    side_effect=[old_snapshot, stale_snapshot, stale_snapshot],
                ),
                patch.object(launcher, "shutdown_idle_daemon") as shutdown,
                patch.object(
                    launcher,
                    "replace_runtime",
                    side_effect=launcher.RuntimeUpdateError("candidate version failed"),
                ),
                patch.object(launcher, "current_codex_cli", return_value=None),
                patch.object(launcher, "current_node_cli", return_value=None),
                patch.object(launcher, "ensure_macos_persistent_entry", ensure),
                patch.object(launcher, "run_orch", run_orch),
                patch.object(launcher, "_validate_setup_result", validate),
            ):
                with self.assertRaisesRegex(
                    launcher.RuntimeUpdateError, "persistent control plane was restored"
                ):
                    launcher.reconcile_runtime()

            shutdown.assert_called_once_with(old_snapshot)
            ensure.assert_called_once()
            run_orch.assert_called_once()
            validate.assert_called_once_with("0.1.0", project_root=Path.cwd())

    def test_missing_runtime_install_failure_does_not_stop_an_idle_daemon(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            snapshot = launcher.DaemonSnapshot(
                "idle",
                version="0.1.0",
                endpoint="http://127.0.0.1:49178",
                persistent=True,
                project_root=str(Path.cwd().resolve()),
            )
            with (
                patch.object(launcher, "app_data_root", return_value=root),
                patch.object(launcher, "daemon_snapshot", return_value=snapshot),
                patch.object(
                    launcher,
                    "install_runtime",
                    side_effect=launcher.RuntimeUpdateError("install failed"),
                ),
                patch.object(launcher, "shutdown_idle_daemon") as shutdown,
            ):
                with self.assertRaisesRegex(launcher.RuntimeUpdateError, "install failed"):
                    launcher.reconcile_runtime()

            shutdown.assert_not_called()

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

    def test_successful_runtime_replacement_retains_previous_until_commit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime = root / "venv"
            runtime.mkdir()
            (runtime / "old-runtime.txt").write_text("old", encoding="utf-8")

            def install_candidate(candidate: Path, *, expected_version: str) -> Path:
                del expected_version
                orch = candidate / "bin" / "orch"
                orch.parent.mkdir(parents=True)
                orch.touch()
                return orch

            with (
                patch.object(launcher, "app_data_root", return_value=root),
                patch.object(launcher, "_install_runtime_at", side_effect=install_candidate),
            ):
                candidate_orch = launcher.replace_runtime()

            self.assertEqual(candidate_orch, root / "venv/bin/orch")
            self.assertTrue((root / "venv.previous/old-runtime.txt").is_file())
            self.assertTrue((root / "venv/bin/orch").is_file())
            launcher.commit_runtime_update(root)
            self.assertFalse((root / "venv.previous").exists())

    def test_setup_commits_previous_only_after_authoritative_final_validation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime = root / "venv"
            (runtime / "bin").mkdir(parents=True)
            orch = runtime / "bin/orch"
            orch.touch()
            previous = root / "venv.previous"
            previous.mkdir()
            (previous / "old-runtime.txt").write_text("old", encoding="utf-8")
            events: list[str] = []

            def validate(version: str, *, project_root: Path) -> None:
                self.assertEqual(version, __version__)
                self.assertEqual(project_root, Path.cwd())
                self.assertTrue(previous.exists())
                events.append("validate")

            def commit(update_root: Path) -> None:
                self.assertEqual(update_root, root)
                self.assertTrue(previous.exists())
                events.append("commit")
                launcher._remove_runtime_tree(previous)

            with (
                patch.object(launcher, "app_data_root", return_value=root),
                patch.object(
                    launcher,
                    "runtime_status",
                    return_value={"python_supported": True},
                ),
                patch.object(
                    launcher,
                    "reconcile_runtime",
                    return_value=(orch, None, False),
                ),
                patch.object(launcher, "current_codex_cli", return_value=None),
                patch.object(launcher, "current_node_cli", return_value=None),
                patch.object(launcher, "ensure_macos_persistent_entry"),
                patch.object(launcher, "run_orch", return_value=0),
                patch.object(launcher, "_validate_setup_result", side_effect=validate),
                patch.object(launcher, "commit_runtime_update", side_effect=commit),
            ):
                exit_code = launcher.main(["setup", "--no-open"])

            self.assertEqual(exit_code, 0)
            self.assertEqual(events, ["validate", "commit"])

    def _prepare_candidate_transaction(self, root: Path) -> tuple[Path, Path]:
        runtime = root / "venv"
        (runtime / "bin").mkdir(parents=True)
        orch = runtime / "bin/orch"
        orch.touch()
        previous = root / "venv.previous"
        (previous / "bin").mkdir(parents=True)
        (previous / "old-runtime.txt").write_text("old", encoding="utf-8")
        (previous / "bin/orch").touch()
        return orch, previous

    def test_post_install_failures_roll_back_runtime_and_control_plane(self) -> None:
        for failure_step in (
            "persistent_entry_changed",
            "run_orch_return",
            "run_orch_exception",
            "final_validation",
        ):
            with self.subTest(failure_step=failure_step), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                orch, previous = self._prepare_candidate_transaction(root)
                profiles = root / "profiles.json"
                profiles.write_text('{"version": 1, "profiles": []}\n', encoding="utf-8")
                orch_marker = root / ".orch/run-marker"
                orch_marker.parent.mkdir()
                orch_marker.write_text("keep", encoding="utf-8")
                source_marker = root / "source-workspace/marker"
                source_marker.parent.mkdir()
                source_marker.write_text("keep", encoding="utf-8")
                snapshot = (
                    launcher.DaemonSnapshot("stale")
                    if failure_step == "persistent_entry_changed"
                    else launcher.DaemonSnapshot(
                        "idle",
                        version=__version__,
                        endpoint="http://127.0.0.1:49178",
                        persistent=True,
                        project_root=str(Path.cwd().resolve()),
                    )
                )
                ensure = MagicMock()
                run_orch = MagicMock(
                    side_effect=(
                        [RuntimeError("daemon start failed"), 0]
                        if failure_step == "run_orch_exception"
                        else [1, 0]
                        if failure_step == "run_orch_return"
                        else [0, 0]
                    )
                )
                validate = MagicMock()
                shutdown = MagicMock()
                if failure_step == "persistent_entry_changed":
                    ensure.side_effect = [
                        launcher.PersistentEntryChangedError("persistent entry failed"),
                        None,
                    ]
                elif failure_step == "final_validation":
                    validate.side_effect = [
                        launcher.RuntimeUpdateError("final validation failed"),
                        None,
                    ]

                with (
                    patch.object(launcher, "app_data_root", return_value=root),
                    patch.object(launcher, "runtime_status", return_value={"python_supported": True}),
                    patch.object(launcher, "reconcile_runtime", return_value=(orch, None, False)),
                    patch.object(launcher, "current_codex_cli", return_value=None),
                    patch.object(launcher, "current_node_cli", return_value=None),
                    patch.object(launcher, "ensure_macos_persistent_entry", ensure),
                    patch.object(launcher, "run_orch", run_orch),
                    patch.object(launcher, "_validate_setup_result", validate),
                    patch.object(launcher, "daemon_snapshot", return_value=snapshot),
                    patch.object(launcher, "shutdown_idle_daemon", shutdown),
                    patch.object(launcher, "installed_runtime_version", return_value="0.1.0"),
                ):
                    exit_code = launcher.main(["setup", "--no-open"])

                self.assertEqual(exit_code, 1)
                self.assertFalse(previous.exists())
                self.assertTrue((root / "venv/old-runtime.txt").is_file())
                self.assertEqual(ensure.call_count, 2)
                self.assertEqual(
                    run_orch.call_count,
                    1 if failure_step == "persistent_entry_changed" else 2,
                )
                self.assertEqual(validate.call_count, 2 if failure_step == "final_validation" else 1)
                if failure_step == "persistent_entry_changed":
                    shutdown.assert_not_called()
                    self.assertTrue(
                        ensure.call_args_list[1].kwargs["allow_loaded_replacement"]
                    )
                else:
                    shutdown.assert_called_once()
                self.assertEqual(
                    profiles.read_text(encoding="utf-8"),
                    '{"version": 1, "profiles": []}\n',
                )
                self.assertEqual(orch_marker.read_text(encoding="utf-8"), "keep")
                self.assertEqual(source_marker.read_text(encoding="utf-8"), "keep")

    def test_interrupted_runtime_with_only_previous_restores_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            previous = root / "venv.previous"
            previous.mkdir()
            (previous / "old-runtime.txt").write_text("old", encoding="utf-8")

            launcher.restore_interrupted_runtime(root)

            self.assertTrue((root / "venv/old-runtime.txt").is_file())
            self.assertFalse(previous.exists())

    def test_interrupted_accepted_candidate_commits_idle_and_defers_active(self) -> None:
        for state in ("idle", "active"):
            with self.subTest(state=state), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                orch, previous = self._prepare_candidate_transaction(root)
                snapshot = launcher.DaemonSnapshot(
                    state,
                    version=__version__,
                    endpoint="http://127.0.0.1:49178",
                    active_runs=("run-1",) if state == "active" else (),
                    persistent=True,
                    project_root=str(Path.cwd().resolve()),
                )
                with (
                    patch.object(launcher, "installed_runtime_version", return_value=__version__),
                    patch.object(launcher, "daemon_snapshot", return_value=snapshot),
                    patch.object(launcher, "shutdown_idle_daemon") as shutdown,
                ):
                    deferred, stopped = launcher.restore_interrupted_runtime(
                        root, project_root=Path.cwd(), expected_version=__version__
                    )
                self.assertTrue(orch.exists())
                if state == "idle":
                    self.assertIsNone(deferred)
                    self.assertFalse(previous.exists())
                else:
                    self.assertIn("deferred", deferred or "")
                    self.assertTrue(previous.exists())
                    self.assertFalse(stopped)
                shutdown.assert_not_called()

    def test_interrupted_nonaccepted_candidate_active_or_unknown_is_untouched(self) -> None:
        for state in ("active", "unknown"):
            with self.subTest(state=state), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                orch, previous = self._prepare_candidate_transaction(root)
                snapshot = launcher.DaemonSnapshot(
                    state,
                    active_runs=("run-1",) if state == "active" else (),
                    reason="identity unavailable" if state == "unknown" else None,
                )
                with (
                    patch.object(launcher, "installed_runtime_version", return_value="0.1.0"),
                    patch.object(launcher, "daemon_snapshot", return_value=snapshot),
                    patch.object(launcher, "shutdown_idle_daemon") as shutdown,
                ):
                    if state == "active":
                        deferred, stopped = launcher.restore_interrupted_runtime(
                            root, expected_version=__version__
                        )
                        self.assertIn("deferred", deferred or "")
                        self.assertFalse(stopped)
                    else:
                        with self.assertRaises(launcher.RuntimeUpdateError):
                            launcher.restore_interrupted_runtime(
                                root, expected_version=__version__
                            )
                self.assertTrue(orch.exists())
                self.assertTrue(previous.exists())
                shutdown.assert_not_called()

    def test_interrupted_nonaccepted_idle_missing_or_stale_recovers(self) -> None:
        for state in ("idle", "missing", "stale"):
            with self.subTest(state=state), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                orch, previous = self._prepare_candidate_transaction(root)
                snapshot = launcher.DaemonSnapshot(state)
                with (
                    patch.object(launcher, "installed_runtime_version", return_value="0.1.0"),
                    patch.object(launcher, "daemon_snapshot", return_value=snapshot),
                    patch.object(launcher, "shutdown_idle_daemon") as shutdown,
                ):
                    _, stopped = launcher.restore_interrupted_runtime(
                        root, expected_version=__version__
                    )
                self.assertTrue((root / "venv/old-runtime.txt").is_file())
                self.assertTrue(orch.exists())
                self.assertFalse(previous.exists())
                if state == "idle":
                    self.assertTrue(stopped)
                    shutdown.assert_called_once_with(snapshot)
                else:
                    self.assertFalse(stopped)
                    shutdown.assert_not_called()

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
