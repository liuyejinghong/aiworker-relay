"""Focused v0.1 backend contract tests."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from aiohttp.test_utils import TestClient, TestServer
import truststore

from orchestrator.config import ProfileStore, atomic_write_json, user_data_root
from orchestrator.cli import CLIError, ensure_daemon
from orchestrator.daemon import (
    OPENROUTER_BENCHMARKS_URL,
    OPENROUTER_CREDITS_URL,
    OPENROUTER_CURRENT_KEY_URL,
    APIError,
    DaemonState,
    _http_json,
    create_app,
    fetch_openrouter_account_summary,
    fetch_openrouter_benchmarks,
    validate_openrouter_key,
)
from orchestrator.models import Profile, RunRecord, utc_now
from orchestrator.runner import start_codex_run, start_process
from orchestrator.tasks import PacketValidationError, parse_packet
from orchestrator.worktree import create_worktree


class ProfileAndPacketTests(unittest.TestCase):
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
                },
            )
            with patch("orchestrator.cli._health", return_value=True):
                with self.assertRaisesRegex(CLIError, "already active for"):
                    ensure_daemon(data_dir=app_data, project_root=second_project)
                self.assertEqual(
                    ensure_daemon(data_dir=app_data, project_root=first_project),
                    "http://127.0.0.1:43210",
                )

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
                    worktree=root,
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


class LocalAPIQualifyingTests(unittest.IsolatedAsyncioTestCase):
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
                self.assertEqual(server.host, "127.0.0.1")
                response = await client.get("/api/health")
                self.assertEqual(response.status, 200)
                health = await response.json()
                self.assertTrue(health["ok"])
                self.assertTrue(health["persistent"])
                response = await client.get("/api/openrouter-key")
                self.assertEqual(await response.json(), {"configured": False})
                response = await client.get("/styles.css")
                self.assertEqual(response.status, 200)
                self.assertEqual(await response.text(), "body {}")
            finally:
                await client.close()

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
                response = await client.post("/api/shutdown", json={})
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
                response = await client.post("/api/shutdown", json={})
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
                with patch(
                    "orchestrator.daemon.fetch_openrouter_account_summary",
                    return_value={
                        "status": "account_balance",
                        "remaining_credits": 9.5,
                    },
                ) as account_fetcher:
                    response = await client.get("/api/openrouter-account")
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
                    response = await client.get("/api/profiles/gemini/benchmarks")
                    self.assertEqual(response.status, 200)
                    payload = await response.json()
                    self.assertEqual(payload["profile_id"], "gemini")
                    self.assertEqual(payload["entries"], [])
                benchmark_fetcher.assert_called_once_with(
                    "stored-key", "google/gemini-3.7-flash"
                )
            finally:
                await client.close()
