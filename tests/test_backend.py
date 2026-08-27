"""Focused v0.1 backend contract tests."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from aiohttp.test_utils import TestClient, TestServer
import truststore
import psutil

from orchestrator import __version__
from orchestrator import cli as orchestrator_cli
from orchestrator.config import ProfileStore, atomic_write_json, user_data_root
from orchestrator.cli import CLIError, ensure_daemon
from orchestrator.daemon import (
    BROWSER_CAPABILITY_COOKIE,
    CLI_CAPABILITY_HEADER,
    OPENROUTER_BENCHMARKS_URL,
    OPENROUTER_CREDITS_URL,
    OPENROUTER_CURRENT_KEY_URL,
    APIError,
    DaemonState,
    _call_callback,
    _daemon_record_lock,
    _http_json,
    create_app,
    fetch_openrouter_account_summary,
    fetch_openrouter_benchmarks,
    validate_openrouter_key,
)
from orchestrator.models import Profile, RunRecord, TaskPacket, utc_now
from orchestrator.runner import ManagedProcess, ProcessControlError, start_codex_run, start_process
from orchestrator.tasks import PacketValidationError, parse_packet
from orchestrator.worktree import create_worktree


class ProfileAndPacketTests(unittest.TestCase):
    def test_cli_rejects_boolean_daemon_pid_and_port(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project_root = root / "project"
            project_root.mkdir()
            record = {
                "pid": os.getpid(),
                "port": 49178,
                "project_root": str(project_root),
                "runtime_root": str(project_root / ".orch"),
                "version": __version__,
                "persistent": True,
                "capability": "test-capability",
            }
            for field in ("pid", "port"):
                malformed = {**record, field: True}
                atomic_write_json(root / "app-data" / "daemon.json", malformed)
                with (
                    patch("orchestrator.cli._process_state") as process_state,
                    patch("orchestrator.cli.subprocess.Popen") as popen,
                    self.assertRaisesRegex(CLIError, "unknown daemon record"),
                ):
                    ensure_daemon(
                        data_dir=root / "app-data",
                        project_root=project_root,
                        persistent=True,
                    )
                process_state.assert_not_called()
                popen.assert_not_called()

    def test_cli_loopback_opener_disables_redirects(self) -> None:
        opener = MagicMock()
        with patch(
            "orchestrator.cli.urllib.request.build_opener", return_value=opener
        ) as build_opener:
            orchestrator_cli._loopback_open("http://127.0.0.1:49178", timeout=0.1)

        self.assertTrue(
            any(
                isinstance(handler, orchestrator_cli._NoRedirect)
                for handler in build_opener.call_args.args
            )
        )

    def test_provider_requests_use_the_system_trust_store(self) -> None:
        response = MagicMock()
        response.__enter__.return_value.read.return_value = b'{"data": []}'
        with patch("orchestrator.daemon.urllib.request.urlopen", return_value=response) as call:
            self.assertEqual(_http_json("https://openrouter.ai/api/v1/models"), {"data": []})
        context = call.call_args.kwargs["context"]
        self.assertIsInstance(context, truststore.SSLContext)

    def test_daemon_refuses_to_reuse_another_projects_control_plane(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            app_data = root / "app-data"
            first_project = root / "first-project"
            second_project = root / "second-project"
            first_project.mkdir()
            second_project.mkdir()
            atomic_write_json(
                app_data / "daemon.json",
                {
                    "pid": os.getpid(),
                    "port": 43210,
                    "project_root": str(first_project),
                    "runtime_root": str(first_project / ".orch"),
                    "version": __version__,
                    "persistent": False,
                    "capability": "test-capability",
                },
            )
            with patch("orchestrator.cli._health", return_value=True):
                with self.assertRaisesRegex(CLIError, "already active for"):
                    ensure_daemon(data_dir=app_data, project_root=second_project)
                self.assertEqual(
                    ensure_daemon(data_dir=app_data, project_root=first_project),
                    "http://127.0.0.1:43210",
                )

    def test_cli_does_not_reuse_or_replace_a_live_legacy_daemon(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = root / "project"
            project.mkdir()
            atomic_write_json(
                root / "app-data" / "daemon.json",
                {
                    "pid": os.getpid(),
                    "port": 43210,
                    "project_root": str(project.resolve()),
                },
            )
            with (
                patch("orchestrator.cli._process_state", return_value=True),
                patch("orchestrator.cli.subprocess.Popen") as popen,
            ):
                with self.assertRaisesRegex(CLIError, "no capability"):
                    ensure_daemon(data_dir=root / "app-data", project_root=project)
            popen.assert_not_called()

    def test_key_validation_uses_the_authenticated_key_endpoint(self) -> None:
        with patch("orchestrator.daemon._http_json", return_value={"data": {}}) as call:
            self.assertTrue(validate_openrouter_key("test-key"))

        call.assert_called_once_with(
            OPENROUTER_CURRENT_KEY_URL,
            headers={"Authorization": "Bearer test-key"},
        )

        with patch(
            "orchestrator.daemon._http_json",
            side_effect=APIError("invalid_key", "rejected"),
        ):
            self.assertFalse(validate_openrouter_key("bad-key"))

    def test_account_summary_distinguishes_account_credits_from_key_limit(self) -> None:
        with patch(
            "orchestrator.daemon._http_json",
            return_value={"data": {"total_credits": 100.5, "total_usage": 25.75}},
        ) as call:
            self.assertEqual(
                fetch_openrouter_account_summary("test-key")["remaining_credits"],
                74.75,
            )
        call.assert_called_once_with(
            OPENROUTER_CREDITS_URL,
            headers={"Authorization": "Bearer test-key"},
        )

        with patch(
            "orchestrator.daemon._http_json",
            side_effect=[
                APIError("forbidden", "management key required", status=403),
                {
                    "data": {
                        "limit": 50,
                        "limit_remaining": 12.5,
                        "usage": 37.5,
                        "limit_reset": "monthly",
                    }
                },
            ],
        ):
            value = fetch_openrouter_account_summary("test-key")
        self.assertEqual(value["status"], "key_limit")
        self.assertEqual(value["limit_remaining"], 12.5)
        self.assertEqual(value["limit_reset"], "monthly")

        with patch(
            "orchestrator.daemon._http_json",
            side_effect=[
                APIError("forbidden", "management key required", status=403),
                {"data": {"limit": None, "limit_remaining": None}},
            ],
        ):
            self.assertEqual(
                fetch_openrouter_account_summary("test-key")["status"],
                "management_key_required",
            )

    def test_benchmarks_keep_only_the_exact_profile_model(self) -> None:
        with patch(
            "orchestrator.daemon._http_json",
            return_value={
                "data": [
                    {
                        "model_permaslug": "google/gemini-3.7-flash",
                        "source": "artificial-analysis",
                        "coding_index": 76.1,
                    },
                    {
                        "model_permaslug": "google/gemini-3.7-flash-next",
                        "source": "artificial-analysis",
                        "coding_index": 90,
                    },
                ],
                "meta": {"as_of": "2026-08-25T00:00:00Z"},
            },
        ) as call:
            value = fetch_openrouter_benchmarks(
                "test-key", "google/gemini-3.7-flash"
            )
        self.assertEqual(value["entries"], [
            {
                "model_permaslug": "google/gemini-3.7-flash",
                "source": "artificial-analysis",
                "coding_index": 76.1,
            }
        ])
        self.assertEqual(value["meta"]["as_of"], "2026-08-25T00:00:00Z")
        call.assert_called_once_with(
            OPENROUTER_BENCHMARKS_URL,
            headers={"Authorization": "Bearer test-key"},
        )

    def test_frozen_and_unverified_profiles_block_without_confirmation(self) -> None:
        frozen = Profile(
            "frozen", "stealth/ox-alpha", state="frozen", verification="verified"
        )
        self.assertEqual(frozen.dispatch_error(), "frozen_profile")

        unverified = Profile("experimental", "stealth/ox-alpha")
        self.assertEqual(
            unverified.dispatch_error(selection_source="codex"),
            "unverified_profile_requires_confirmation",
        )
        self.assertIsNone(
            unverified.dispatch_error(
                selection_source="user", experimental_confirmation=True
            )
        )

    def test_packet_requires_fixed_headings(self) -> None:
        packet = """# Task Packet\n\n## Task\nDo the task.\n\n## Scope\nsrc/\n\n## Do Not Touch\nsecrets\n\n## Existing Behavior\nIt exists.\n\n## Expected Behavior\nIt works.\n\n## Constraints\nNo network.\n\n## Acceptance Criteria\nTests pass.\n\n## Verification\nRun tests.\n\n## Deliverables\nDiff.\n"""
        parsed = parse_packet(packet, run_id="run-1")
        self.assertEqual(parsed.fields["Task"], "Do the task.")
        with self.assertRaises(PacketValidationError) as context:
            parse_packet("## Task\nOnly one field", run_id="run-2")
        self.assertIn("Scope", context.exception.missing)

    def test_atomic_json_and_profile_store(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "state.json"
            atomic_write_json(path, {"value": 1})
            self.assertEqual(json.loads(path.read_text())["value"], 1)
            self.assertEqual(list(Path(temporary).glob("*.tmp")), [])
            store = ProfileStore(Path(temporary) / "profiles.json")
            profile = Profile("p", "provider/model")
            store.put(profile)
            reloaded = ProfileStore(store.path)
            self.assertEqual(reloaded.get("p").model, "provider/model")

    def test_cross_platform_app_data_shape(self) -> None:
        home = Path("/users/example")
        self.assertEqual(
            user_data_root(platform="darwin", home=home),
            home / "Library/Application Support/Codex External Workers",
        )
        self.assertEqual(
            user_data_root(
                platform="win32",
                home=home,
                environ={"LOCALAPPDATA": "C:/Users/example/AppData/Local"},
            ),
            Path("C:/Users/example/AppData/Local/Codex External Workers"),
        )


class WorktreeTests(unittest.TestCase):
    def test_worktree_is_created_from_head_and_dirty_state_is_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            subprocess.run(
                ["git", "config", "user.email", "test@example.com"],
                cwd=root,
                check=True,
            )
            subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
            (root / "README.md").write_text("hello\n", encoding="utf-8")
            subprocess.run(["git", "add", "README.md"], cwd=root, check=True)
            subprocess.run(["git", "commit", "-qm", "initial"], cwd=root, check=True)
            (root / "uncommitted.txt").write_text("not copied\n", encoding="utf-8")
            info = create_worktree(root, "run-1")
            self.assertTrue(info.path.exists())
            self.assertEqual(info.git_common_dir, (root / ".git").resolve())
            self.assertEqual(
                info.source_checkout_index, (root / ".git" / "index").resolve()
            )
            self.assertTrue(info.dirty_workspace_excluded)
            self.assertEqual((info.path / "README.md").read_text(), "hello\n")
            self.assertFalse((info.path / "uncommitted.txt").exists())


class ProcessTests(unittest.IsolatedAsyncioTestCase):
    async def test_codex_run_uses_noninteractive_workspace_approval(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch(
                "orchestrator.runner.start_process",
                new_callable=AsyncMock,
            ) as start:
                await start_codex_run(
                    project_root=root,
                    worktree=root,
                    git_common_dir=root,
                    source_checkout_index=root / "source.index",
                    run_dir=root / "run",
                    prompt="Create one file.",
                    model="nvidia/nemotron-3-ultra-550b-a55b:free",
                    reasoning_effort="auto",
                    api_key="test-key",
                    code_home=root / "CODEX_HOME",
                )

        command = start.await_args.args[0]
        self.assertIn("--approve-for-me", command)
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", command)

    async def test_failed_process_keeps_one_bounded_error_summary(self) -> None:
        process = await start_process(
            [
                sys.executable,
                "-c",
                "import json, sys; print(json.dumps({'type': 'turn.failed', 'error': {'message': json.dumps({'error': {'message': 'provider rate limited'}, 'user_id': 'do-not-store'})}})); sys.stderr.write('fallback\\n'); raise SystemExit(1)",
            ],
            cwd=Path.cwd(),
        )

        self.assertEqual(await process.wait(), 1)
        self.assertEqual(process.failure_summary, "provider rate limited")

    async def test_term_then_force_kill(self) -> None:
        process = await start_process(
            [
                sys.executable,
                "-c",
                "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)",
            ],
            cwd=Path.cwd(),
            rss_interval=0.01,
        )
        try:
            # Let the child install its SIGTERM handler before exercising the
            # deliberately uncooperative stop path.
            await asyncio.sleep(0.1)
            first = await process.stop(grace_seconds=0.05)
            self.assertEqual(first.state, "awaiting_force")
            second = await process.stop(force=True, grace_seconds=0.5)
            self.assertEqual(second.state, "killed")
            self.assertIsNotNone(second.returncode)
        finally:
            if process.is_running():
                await process.stop(force=True, grace_seconds=0.5)

    async def test_kill_timeout_does_not_wait_on_live_child_pipes(self) -> None:
        class RootProcess:
            pid = 4242
            returncode: int | None = None
            stdout = None
            stderr = None

            async def wait(self) -> int:
                await asyncio.Event().wait()
                return 0

        with patch("orchestrator.runner.os.getpgid", return_value=7000):
            managed = ManagedProcess(RootProcess())
        pipe_reader = asyncio.create_task(asyncio.Event().wait())
        managed._stdout_task = pipe_reader
        managed.term_requested = True
        try:
            with (
                patch.object(managed, "is_running", return_value=True),
                patch.object(managed, "_send_kill"),
                patch.object(managed, "_wait_gracefully", new=AsyncMock(return_value=False)),
            ):
                outcome = await asyncio.wait_for(
                    managed.stop(force=True, grace_seconds=0), timeout=0.2
                )
            self.assertEqual(outcome.state, "kill_timeout")
            self.assertIs(managed._stdout_task, pipe_reader)
            self.assertFalse(pipe_reader.done())
        finally:
            pipe_reader.cancel()
            await asyncio.gather(pipe_reader, return_exceptions=True)

    async def test_posix_stop_waits_for_original_group_after_root_exit(self) -> None:
        class RootProcess:
            pid = 4242
            returncode: int | None = None
            stdout = None
            stderr = None

            async def wait(self) -> int:
                self.returncode = 0
                return 0

        root = RootProcess()
        group_alive = True
        signals: list[tuple[int, int]] = []

        def killpg(group: int, signum: int) -> None:
            nonlocal group_alive
            signals.append((group, signum))
            if signum == signal.SIGKILL:
                group_alive = False
            elif signum == 0 and not group_alive:
                raise ProcessLookupError

        with (
            patch("orchestrator.runner.os.getpgid", return_value=7000),
            patch("orchestrator.runner.os.killpg", side_effect=killpg),
        ):
            managed = ManagedProcess(root)
            first = await managed.stop(grace_seconds=0)
            self.assertEqual(first.state, "awaiting_force")
            self.assertTrue(managed.is_running())
            second = await managed.stop(force=True, grace_seconds=0.2)

        self.assertEqual(second.state, "killed")
        self.assertEqual(
            [item for item in signals if item[1] != 0],
            [(7000, signal.SIGTERM), (7000, signal.SIGKILL)],
        )

    async def test_posix_wait_treats_transient_group_eperm_as_still_existing(self) -> None:
        class RootProcess:
            pid = 4242
            returncode = 0
            stdout = None
            stderr = None

            async def wait(self) -> int:
                return 0

        with (
            patch("orchestrator.runner.os.getpgid", return_value=7000),
            patch(
                "orchestrator.runner.os.killpg",
                side_effect=[PermissionError(1, "not permitted"), ProcessLookupError()],
            ),
        ):
            managed = ManagedProcess(RootProcess())
            self.assertEqual(await managed.wait(), 0)

    async def test_windows_wait_tracks_known_child_and_surfaces_identity_error(self) -> None:
        class RootProcess:
            pid = 4242
            returncode: int | None = None
            stdout = None
            stderr = None

            async def wait(self) -> int:
                self.returncode = 0
                return 0

        root = MagicMock()
        child = MagicMock()
        root.pid = 4242
        child.pid = 4243
        root.children.return_value = [child]
        root.create_time.return_value = 1.0
        child.create_time.return_value = 2.0
        root.is_running.return_value = False
        child.is_running.return_value = True
        process = RootProcess()

        with (
            patch("orchestrator.runner.os.name", "nt"),
            patch(
                "orchestrator.runner.psutil.Process",
                side_effect=lambda pid: root if pid == 4242 else child,
            ),
        ):
            managed = ManagedProcess(process)
            wait_task = asyncio.create_task(managed.wait())
            await asyncio.sleep(0)
            self.assertFalse(wait_task.done())
            child.is_running.return_value = False
            self.assertEqual(await wait_task, 0)

            # A known child that becomes unreadable cannot be reported as a
            # clean exit after the root has already returned.
            process.returncode = None
            child.is_running.return_value = True
            child.create_time.side_effect = psutil.AccessDenied(pid=4243)
            managed = ManagedProcess(process)
            with self.assertRaises(ProcessControlError):
                await managed.wait()


class LocalAPIQualifyingTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancelled_sync_callback_consumes_its_late_exception(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        finished = threading.Event()
        contexts: list[dict[str, object]] = []

        def callback() -> None:
            entered.set()
            release.wait()
            try:
                raise RuntimeError("provider failed after cancellation")
            finally:
                finished.set()

        loop = asyncio.get_running_loop()
        previous_handler = loop.get_exception_handler()
        loop.set_exception_handler(lambda _loop, context: contexts.append(context))
        try:
            callback_task = asyncio.create_task(_call_callback(callback))
            self.assertTrue(await asyncio.to_thread(entered.wait, 1))
            callback_task.cancel()
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await callback_task
            self.assertTrue(await asyncio.to_thread(finished.wait, 1))
            await asyncio.sleep(0)
            self.assertEqual(contexts, [])
        finally:
            loop.set_exception_handler(previous_handler)

    async def test_cancelled_sync_callback_closes_its_returned_coroutine(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        returned: list[object] = []

        async def result() -> None:
            await asyncio.sleep(0)

        def callback() -> object:
            entered.set()
            release.wait()
            coroutine = result()
            returned.append(coroutine)
            return coroutine

        callback_task = asyncio.create_task(_call_callback(callback))
        self.assertTrue(await asyncio.to_thread(entered.wait, 1))
        callback_task.cancel()
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await callback_task

        for _ in range(100):
            if returned and returned[0].cr_frame is None:
                break
            await asyncio.sleep(0.001)
        self.assertEqual(len(returned), 1)
        self.assertIsNone(returned[0].cr_frame)

    async def test_blocking_model_catalog_does_not_block_health(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            entered = threading.Event()
            release = threading.Event()

            def blocked_catalog(query: str) -> list[dict[str, object]]:
                entered.set()
                release.wait()
                return []

            state = DaemonState(
                data_dir=Path(temporary) / "app",
                project_root=Path(temporary),
                persistent=True,
                catalog_fetcher=blocked_catalog,
                key_getter=lambda: None,
            )
            server = TestServer(create_app(state))
            client = TestClient(server)
            await client.start_server()
            try:
                models_task = asyncio.create_task(
                    client.get(
                        "/api/models?query=blocked",
                        headers={CLI_CAPABILITY_HEADER: state.capability},
                    )
                )
                self.assertTrue(await asyncio.to_thread(entered.wait, 1))

                health = await asyncio.wait_for(
                    client.get(
                        "/api/health",
                        headers={CLI_CAPABILITY_HEADER: state.capability},
                    ),
                    timeout=1,
                )
                self.assertEqual(health.status, 200)
            finally:
                release.set()
                await models_task
                await client.close()

    async def test_profile_and_key_callbacks_run_off_loop_and_allow_async_injection(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            loop_thread = threading.get_ident()
            callback_threads: list[int] = []
            saved: list[str] = []

            def catalog(query: str) -> list[dict[str, object]]:
                callback_threads.append(threading.get_ident())
                return [{"id": query}]

            async def async_validator(value: str) -> bool:
                return value == "test-key"

            def validator(value: str) -> bool:
                callback_threads.append(threading.get_ident())
                return value == "test-key"

            def saver(value: str) -> None:
                callback_threads.append(threading.get_ident())
                saved.append(value)

            state = DaemonState(
                data_dir=Path(temporary) / "app",
                project_root=Path(temporary),
                catalog_fetcher=catalog,
                key_validator=async_validator,
                key_saver=saver,
                key_getter=lambda: None,
            )
            profile = await state.create_profile({"model": "provider/model"})
            self.assertEqual(profile.model, "provider/model")
            await state.save_key("test-key")
            state.key_validator = validator
            await state.save_key("test-key")
            self.assertEqual(saved, ["test-key", "test-key"])
            self.assertEqual(len(callback_threads), 4)
            self.assertTrue(all(thread_id != loop_thread for thread_id in callback_threads))

            async def catalog_result() -> list[dict[str, object]]:
                return [{"id": "provider/async-model"}]

            async_state = DaemonState(
                data_dir=Path(temporary) / "async-app",
                project_root=Path(temporary),
                catalog_fetcher=lambda query: catalog_result(),
                key_getter=lambda: None,
            )
            async_profile = await async_state.create_profile(
                {"model": "provider/async-model"}
            )
            self.assertEqual(async_profile.model, "provider/async-model")

    async def test_run_rejects_reasoning_override_before_packet_or_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = DaemonState(
                data_dir=root / "app",
                project_root=root,
                catalog_fetcher=lambda query: [],
                key_getter=lambda: "test-key",
            )
            state.profiles.put(Profile("p", "provider/model"))
            for override in ("high", None, ""):
                with (
                    patch("orchestrator.daemon.load_packet") as load,
                    patch("orchestrator.daemon.create_worktree") as worktree,
                    self.assertRaises(APIError) as context,
                ):
                    await state.create_run(
                        {
                            "profile_id": "p",
                            "packet_path": str(root / "packet.md"),
                            "consent": True,
                            "selection_source": "user",
                            "experimental_confirmation": True,
                            "reasoning_effort": override,
                        }
                    )
                self.assertEqual(
                    context.exception.code, "reasoning_override_not_supported"
                )
                load.assert_not_called()
                worktree.assert_not_called()
            self.assertEqual(state.records, {})
            self.assertEqual(state._evidence, {})
            self.assertEqual(list(state.runs_root.iterdir()), [])
            self.assertFalse((state.runtime_root / "worktrees").exists())

    async def test_run_persists_profile_reasoning_value_and_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            worktree = MagicMock(
                path=root / "worktree", dirty_workspace_excluded=False
            )
            worktree.source_head = "HEAD"
            packets: dict[str, TaskPacket] = {}

            def make_packet(_path: Path, **kwargs: str | None) -> TaskPacket:
                packet = TaskPacket(fields={}, raw="# Task Packet\n", **kwargs)
                packets[packet.run_id] = packet
                return packet

            for default, expected_source in (
                ("high", "profile_default"),
                ("auto", "profile_auto"),
            ):
                catalog_fetcher = MagicMock(return_value=[])
                state = DaemonState(
                    data_dir=root / f"app-{default}",
                    project_root=root,
                    catalog_fetcher=catalog_fetcher,
                    key_getter=lambda: "test-key",
                )
                state.profiles.put(
                    Profile(
                        "p",
                        "provider/model",
                        default_reasoning=default,
                    )
                )
                execute_run = MagicMock()
                with (
                    patch(
                        "orchestrator.daemon.load_packet", side_effect=make_packet
                    ) as load,
                    patch("orchestrator.daemon.create_worktree", return_value=worktree),
                    patch("orchestrator.daemon.asyncio.create_task"),
                    patch.object(state, "_execute_run", execute_run),
                ):
                    record = await state.create_run(
                        {
                            "profile_id": "p",
                            "packet_path": "unused",
                            "consent": True,
                            "selection_source": "user",
                            "experimental_confirmation": True,
                        }
                    )
                self.assertEqual(record.reasoning_effort, default)
                self.assertEqual(record.reasoning_source, expected_source)
                self.assertEqual(load.call_args.kwargs["reasoning_effort"], default)
                self.assertEqual(
                    load.call_args.kwargs["reasoning_source"], expected_source
                )
                packet = packets[record.run_id]
                self.assertEqual(packet.reasoning_effort, default)
                self.assertEqual(packet.reasoning_source, expected_source)
                catalog_fetcher.assert_not_called()

    async def test_profile_fixed_reasoning_is_limited_to_catalog_options(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = DaemonState(
                data_dir=Path(temporary) / "app",
                project_root=Path(temporary),
                catalog_fetcher=lambda query: [
                    {
                        "id": "google/gemini-example",
                        "reasoning": {
                            "supported_efforts": ["high", "low"],
                            "mandatory": True,
                        },
                    }
                ],
                key_getter=lambda: None,
            )
            profile = await state.create_profile(
                {"model": "google/gemini-example", "default_reasoning": "high"}
            )
            self.assertEqual(profile.default_reasoning, "high")
            self.assertIn("catalog_fetched_at", profile.metadata)

            with self.assertRaises(APIError) as context:
                await state.create_profile(
                    {"model": "google/gemini-example", "default_reasoning": "none"}
                )
            self.assertEqual(context.exception.code, "unsupported_reasoning")

            no_effort_state = DaemonState(
                data_dir=Path(temporary) / "no-effort-app",
                project_root=Path(temporary),
                catalog_fetcher=lambda query: [
                    {"id": "example/no-effort", "reasoning": {}}
                ],
                key_getter=lambda: None,
            )
            with self.assertRaises(APIError) as context:
                await no_effort_state.create_profile(
                    {"model": "example/no-effort", "default_reasoning": "high"}
                )
            self.assertEqual(context.exception.code, "unsupported_reasoning")

    async def test_health_is_loopback_and_key_is_never_returned(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = DaemonState(
                data_dir=Path(temporary) / "app",
                project_root=Path(temporary),
                persistent=True,
                catalog_fetcher=lambda query: [],
                key_getter=lambda: None,
            )
            self.assertEqual(
                state._daemon_record()["project_root"], str(Path(temporary).resolve())
            )
            self.assertTrue(state._daemon_record()["persistent"])
            static = Path(temporary) / "web"
            static.mkdir()
            (static / "styles.css").write_text("body {}", encoding="utf-8")
            server = TestServer(create_app(state, static_dir=static))
            client = TestClient(server)
            await client.start_server()
            try:
                response = await client.get("/")
                self.assertEqual(response.status, 200)
                set_cookie = response.headers["Set-Cookie"]
                self.assertIn("HttpOnly", set_cookie)
                self.assertIn("SameSite=Strict", set_cookie)
                self.assertNotIn("Domain=", set_cookie)
                self.assertEqual(server.host, "127.0.0.1")
                response = await client.get(
                    "/api/health",
                    headers={
                        "Sec-Fetch-Site": "same-origin",
                        "Sec-Fetch-Mode": "cors",
                        "Sec-Fetch-Dest": "empty",
                    },
                )
                self.assertEqual(response.status, 200)
                health = await response.json()
                self.assertTrue(health["ok"])
                self.assertTrue(health["persistent"])
                self.assertEqual(health["project_root"], str(Path(temporary).resolve()))
                self.assertEqual(
                    health["runtime_root"],
                    str(Path(temporary).resolve() / ".orch"),
                )
                self.assertNotIn("capability", health)
                response = await client.get(
                    "/api/openrouter-key",
                    headers={
                        "Sec-Fetch-Site": "same-origin",
                        "Sec-Fetch-Mode": "cors",
                        "Sec-Fetch-Dest": "empty",
                    },
                )
                self.assertEqual(await response.json(), {"configured": False})
                response = await client.get("/styles.css")
                self.assertEqual(response.status, 200)
                self.assertEqual(await response.text(), "body {}")
            finally:
                await client.close()

    async def test_local_capability_auth_rejects_alias_and_cross_site_requests(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = DaemonState(
                data_dir=root / "app",
                project_root=root,
                persistent=True,
                catalog_fetcher=lambda query: [],
                key_getter=lambda: None,
            )
            server = TestServer(create_app(state))
            client = TestClient(server)
            await client.start_server()
            try:
                response = await client.get("/api/health")
                self.assertEqual(response.status, 401)

                response = await client.get(
                    "/api/health",
                    headers={CLI_CAPABILITY_HEADER: state.capability},
                )
                self.assertEqual(response.status, 200)

                origin = f"http://127.0.0.1:{server.port}"
                response = await client.get(
                    "/api/health",
                    headers={
                        CLI_CAPABILITY_HEADER: state.capability,
                        "Host": f"localhost:{server.port}",
                    },
                )
                self.assertEqual(response.status, 403)
                self.assertEqual((await response.json())["code"], "invalid_host")

                response = await client.get(
                    "/api/health",
                    headers={
                        CLI_CAPABILITY_HEADER: state.capability,
                        "Sec-Fetch-Site": "cross-site",
                    },
                )
                self.assertEqual(response.status, 403)
                self.assertEqual(
                    (await response.json())["code"], "invalid_fetch_metadata"
                )

                response = await client.get(
                    "/api/health",
                    headers={
                        CLI_CAPABILITY_HEADER: state.capability,
                        "Sec-Fetch-Mode": "no-cors",
                    },
                )
                self.assertEqual(response.status, 403)

                await client.get("/")
                response = await client.get(
                    "/api/health",
                    headers={CLI_CAPABILITY_HEADER: state.capability},
                )
                self.assertEqual(response.status, 401)
                self.assertEqual((await response.json())["code"], "unauthorized")

                response = await client.put(
                    "/api/openrouter-key",
                    data=json.dumps({"key": "not-used"}),
                    headers={
                        "Content-Type": "text/plain",
                        "Origin": origin,
                        "Sec-Fetch-Site": "same-origin",
                        "Sec-Fetch-Mode": "cors",
                        "Sec-Fetch-Dest": "empty",
                    },
                )
                self.assertEqual(response.status, 415)
                self.assertEqual(
                    (await response.json())["code"], "invalid_content_type"
                )
            finally:
                await client.close()

    async def test_hostile_browser_requests_have_no_control_plane_side_effects(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = DaemonState(
                data_dir=root / "app",
                project_root=root,
                persistent=True,
                catalog_fetcher=lambda query: [],
                key_getter=lambda: "test-key",
            )
            account_fetcher = MagicMock()
            server = TestServer(create_app(state))
            client = TestClient(server)
            await client.start_server()
            try:
                await client.get("/")
                hostile = {
                    "Origin": "http://attacker.example",
                    "Sec-Fetch-Site": "cross-site",
                    "Sec-Fetch-Mode": "no-cors",
                    "Sec-Fetch-Dest": "image",
                }
                with patch(
                    "orchestrator.daemon.fetch_openrouter_account_summary",
                    account_fetcher,
                ):
                    response = await client.get(
                        "/api/openrouter-account", headers=hostile
                    )
                self.assertEqual(response.status, 403)
                account_fetcher.assert_not_called()

                response = await client.post(
                    "/api/profiles",
                    data="model=provider/model",
                    headers={
                        **hostile,
                        "Content-Type": "application/x-www-form-urlencoded",
                    },
                )
                self.assertEqual(response.status, 403)
                self.assertEqual(state.profiles.all(), [])

                response = await client.post(
                    "/api/shutdown",
                    data="",
                    headers={**hostile, "Content-Type": "text/plain"},
                )
                self.assertEqual(response.status, 403)
                self.assertFalse(state._shutdown.is_set())
            finally:
                await client.close()

    async def test_browser_cookie_is_host_only_and_write_requires_local_origin(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = DaemonState(
                data_dir=root / "app",
                project_root=root,
                persistent=True,
                catalog_fetcher=lambda query: [],
                key_getter=lambda: None,
            )
            static = root / "web"
            static.mkdir()
            (static / "index.html").write_text("<p>index</p>", encoding="utf-8")
            (static / "styles.css").write_text("body {}", encoding="utf-8")
            server = TestServer(create_app(state, static_dir=static))
            client = TestClient(server)
            await client.start_server()
            try:
                response = await client.get(
                    "/",
                    headers={
                        "Sec-Fetch-Site": "none",
                        "Sec-Fetch-Mode": "navigate",
                        "Sec-Fetch-Dest": "document",
                    },
                )
                self.assertEqual(response.status, 200)
                set_cookie = response.headers["Set-Cookie"]
                self.assertIn(f"{BROWSER_CAPABILITY_COOKIE}=", set_cookie)
                self.assertIn("HttpOnly", set_cookie)
                self.assertIn("SameSite=Strict", set_cookie)
                self.assertNotIn("Domain=", set_cookie)

                response = await client.get("/styles.css")
                self.assertEqual(response.status, 200)
                response = await client.get("/api/health")
                self.assertEqual(response.status, 403)
                self.assertEqual(
                    (await response.json())["code"], "invalid_fetch_metadata"
                )
                response = await client.get(
                    "/api/health",
                    headers={
                        "Sec-Fetch-Site": "same-origin",
                        "Sec-Fetch-Mode": "cors",
                        "Sec-Fetch-Dest": "empty",
                    },
                )
                self.assertEqual(response.status, 200)

                origin = f"http://127.0.0.1:{server.port}"
                response = await client.post(
                    "/api/shutdown",
                    json={},
                    headers={
                        "Origin": "http://evil.example",
                        "Sec-Fetch-Site": "same-origin",
                        "Sec-Fetch-Mode": "cors",
                        "Sec-Fetch-Dest": "empty",
                    },
                )
                self.assertEqual(response.status, 403)
                self.assertEqual((await response.json())["code"], "invalid_origin")

                response = await client.post(
                    "/api/shutdown",
                    json={},
                    headers={
                        "Origin": origin,
                        "Sec-Fetch-Site": "same-origin",
                        "Sec-Fetch-Mode": "cors",
                        "Sec-Fetch-Dest": "empty",
                    },
                )
                self.assertEqual(response.status, 200)
            finally:
                await client.close()

    def test_daemon_capability_is_persisted_owner_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = DaemonState(
                data_dir=root / "app",
                project_root=root,
                persistent=True,
                catalog_fetcher=lambda query: [],
                key_getter=lambda: None,
            )
            state.port = 49178
            state.write_daemon_file()
            record = json.loads(state.app_paths.daemon_file.read_text(encoding="utf-8"))
            self.assertEqual(record["capability"], state.capability)
            self.assertEqual(
                state.app_paths.daemon_file.stat().st_mode & 0o777,
                0o600,
            )
            self.assertNotIn("capability", state.overview())

    def test_daemon_does_not_overwrite_a_live_legacy_record(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = DaemonState(
                data_dir=root / "app",
                project_root=root,
                persistent=True,
                catalog_fetcher=lambda query: [],
                key_getter=lambda: None,
            )
            legacy = {"pid": os.getpid(), "port": 49178}
            state.app_paths.daemon_file.write_text(json.dumps(legacy), encoding="utf-8")

            with self.assertRaisesRegex(RuntimeError, "active"):
                state.write_daemon_file()

            self.assertEqual(
                json.loads(state.app_paths.daemon_file.read_text(encoding="utf-8")),
                legacy,
            )

    def test_daemon_replaces_a_stale_record_after_pid_is_dead(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = DaemonState(
                data_dir=root / "app",
                project_root=root,
                persistent=True,
                catalog_fetcher=lambda query: [],
                key_getter=lambda: None,
            )
            state.app_paths.daemon_file.write_text(
                json.dumps({"pid": 123, "port": 49178}), encoding="utf-8"
            )
            state.port = 49178

            with patch("orchestrator.daemon.os.kill", side_effect=ProcessLookupError):
                state.write_daemon_file()

            record = json.loads(state.app_paths.daemon_file.read_text(encoding="utf-8"))
            self.assertEqual(record["pid"], state.pid)
            self.assertEqual(record["capability"], state.capability)

    def test_daemon_record_claim_is_serialized(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = DaemonState(
                data_dir=root / "app",
                project_root=root,
                persistent=True,
                catalog_fetcher=lambda query: [],
                key_getter=lambda: None,
            )
            state.port = 49178
            with _daemon_record_lock(state.app_paths.daemon_file):
                with self.assertRaisesRegex(RuntimeError, "being updated"):
                    state.write_daemon_file()

    async def test_persistent_daemon_has_no_idle_shutdown_timer(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = DaemonState(
                data_dir=Path(temporary) / "app",
                project_root=Path(temporary),
                persistent=True,
                catalog_fetcher=lambda query: [],
                key_getter=lambda: None,
            )

            await state.idle_loop()

        self.assertFalse(state._shutdown.is_set())

    async def test_launcher_shutdown_only_accepts_an_idle_daemon(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = DaemonState(
                data_dir=root / "app",
                project_root=root,
                catalog_fetcher=lambda query: [],
                key_getter=lambda: None,
            )
            server = TestServer(create_app(state))
            client = TestClient(server)
            await client.start_server()
            try:
                await client.get("/")
                origin = f"http://127.0.0.1:{server.port}"
                response = await client.post(
                    "/api/shutdown",
                    json={},
                    headers={
                        "Origin": origin,
                        "Sec-Fetch-Site": "same-origin",
                        "Sec-Fetch-Mode": "cors",
                        "Sec-Fetch-Dest": "empty",
                    },
                )
                self.assertEqual(response.status, 200)
                self.assertEqual((await response.json())["status"], "shutting_down")
                self.assertTrue(state._shutdown.is_set())
            finally:
                await client.close()

            state._shutdown.clear()
            state.records["active"] = RunRecord(
                run_id="active",
                profile_id="profile",
                model="provider/model",
                status="running",
                created_at=utc_now(),
                updated_at=utc_now(),
                project_root=str(root),
            )
            server = TestServer(create_app(state))
            client = TestClient(server)
            await client.start_server()
            try:
                await client.get("/")
                origin = f"http://127.0.0.1:{server.port}"
                response = await client.post(
                    "/api/shutdown",
                    json={},
                    headers={
                        "Origin": origin,
                        "Sec-Fetch-Site": "same-origin",
                        "Sec-Fetch-Mode": "cors",
                        "Sec-Fetch-Dest": "empty",
                    },
                )
                self.assertEqual(response.status, 409)
                self.assertEqual((await response.json())["code"], "active_runs")
                self.assertFalse(state._shutdown.is_set())
            finally:
                await client.close()

    async def test_account_and_benchmark_endpoints_are_on_demand(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = DaemonState(
                data_dir=Path(temporary) / "app",
                project_root=Path(temporary),
                catalog_fetcher=lambda query: [],
                key_getter=lambda: "stored-key",
            )
            state.profiles.put(Profile("gemini", "google/gemini-3.7-flash"))
            server = TestServer(create_app(state))
            client = TestClient(server)
            await client.start_server()
            try:
                await client.get("/")
                with patch(
                    "orchestrator.daemon.fetch_openrouter_account_summary",
                    return_value={
                        "status": "account_balance",
                        "remaining_credits": 9.5,
                    },
                ) as account_fetcher:
                    response = await client.get(
                        "/api/openrouter-account",
                        headers={
                            "Sec-Fetch-Site": "same-origin",
                            "Sec-Fetch-Mode": "cors",
                            "Sec-Fetch-Dest": "empty",
                        },
                    )
                    self.assertEqual(response.status, 200)
                    self.assertEqual(
                        await response.json(),
                        {"status": "account_balance", "remaining_credits": 9.5},
                    )
                account_fetcher.assert_called_once_with("stored-key")

                with patch(
                    "orchestrator.daemon.fetch_openrouter_benchmarks",
                    return_value={
                        "model": "google/gemini-3.7-flash",
                        "entries": [],
                        "meta": {},
                        "refreshed_at": "2026-08-25T00:00:00+00:00",
                    },
                ) as benchmark_fetcher:
                    response = await client.get(
                        "/api/profiles/gemini/benchmarks",
                        headers={
                            "Sec-Fetch-Site": "same-origin",
                            "Sec-Fetch-Mode": "cors",
                            "Sec-Fetch-Dest": "empty",
                        },
                    )
                    self.assertEqual(response.status, 200)
                    payload = await response.json()
                    self.assertEqual(payload["profile_id"], "gemini")
                    self.assertEqual(payload["entries"], [])
                benchmark_fetcher.assert_called_once_with(
                    "stored-key", "google/gemini-3.7-flash"
                )
            finally:
                await client.close()
