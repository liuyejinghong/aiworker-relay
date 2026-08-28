"""Focused fault-injection tests for external run lifecycle recovery."""

from __future__ import annotations

import asyncio
import json
import signal
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from orchestrator.daemon import (
    APIError,
    DaemonState,
    RSS_SAMPLE_LIMIT,
    _process_group_state,
    _process_start_value,
    _serve_until_clean_shutdown,
)
from orchestrator.models import RunRecord, TaskPacket, utc_now
from orchestrator.results import EvidenceStore
from orchestrator.runner import StopOutcome


class FakeProcess:
    def __init__(self, *, running: bool = False, returncode: int | None = 0):
        self.pid = 4242
        self._running = running
        self.returncode = returncode
        self.term_requested = False
        self.force_requested = False
        self.stop_calls: list[bool] = []

    def is_running(self) -> bool:
        return self._running

    @property
    def failure_summary(self) -> str | None:
        return None

    async def wait(self) -> int | None:
        self._running = False
        return self.returncode

    async def stop(self, *, force: bool = False, grace_seconds: float = 10.0) -> StopOutcome:
        self.stop_calls.append(force)
        if force:
            self.force_requested = True
            self._running = False
            self.returncode = -signal.SIGKILL
            return StopOutcome("killed", self.returncode, forced=True)
        self.term_requested = True
        self._running = False
        return StopOutcome("term_exited", self.returncode)


class ShutdownProcess(FakeProcess):
    def __init__(self, *, kill_timeout: bool = False):
        super().__init__(running=True, returncode=None)
        self.kill_timeout = kill_timeout

    async def stop(self, *, force: bool = False, grace_seconds: float = 10.0) -> StopOutcome:
        self.stop_calls.append(force)
        if force:
            self.force_requested = True
            if self.kill_timeout:
                return StopOutcome("kill_timeout", None, forced=True)
            self._running = False
            self.returncode = -signal.SIGKILL
            return StopOutcome("killed", self.returncode, forced=True)
        self.term_requested = True
        return StopOutcome("awaiting_force", None)


class DeferredProcess(FakeProcess):
    def __init__(self) -> None:
        super().__init__(running=True, returncode=0)
        self.released = asyncio.Event()

    async def wait(self) -> int | None:
        await self.released.wait()
        self._running = False
        return self.returncode


class RunLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def _state_and_record(self, root: Path, *, status: str = "created") -> tuple[DaemonState, RunRecord]:
        state = DaemonState(
            data_dir=root / "app",
            project_root=root,
            codex_path="/usr/bin/codex",
            key_getter=lambda: "test-key",
        )
        run_id = "run-1"
        run_dir = state.runs_root / run_id
        run_dir.mkdir(parents=True)
        record = RunRecord(
            run_id=run_id,
            profile_id="profile",
            model="provider/model",
            status=status,
            created_at=utc_now(),
            updated_at=utc_now(),
            project_root=str(root),
            worktree=str(root / "worktree"),
        )
        state.records[run_id] = record
        state._evidence[run_id] = EvidenceStore(run_dir, secret="test-key")
        return state, record

    async def _execute_success(self, state: DaemonState, record: RunRecord) -> None:
        run_dir = state.runs_root / record.run_id
        (run_dir / "last-message.md").write_text("finished\n", encoding="utf-8")
        process = FakeProcess()
        packet = TaskPacket(record.run_id, {}, "# Task Packet\n")
        with (
            patch("orchestrator.daemon.start_codex_run", new=AsyncMock(return_value=process)),
            patch("orchestrator.daemon.psutil.Process") as process_probe,
            patch("orchestrator.daemon.os.getpgid", return_value=process.pid),
            patch("orchestrator.daemon.diff_text", return_value=""),
            patch("orchestrator.daemon.changed_files", return_value=[]),
        ):
            process_probe.return_value.create_time.return_value = 123.0
            await self._execute_run(state, record, packet, Path(record.worktree))

    async def _execute_run(
        self,
        state: DaemonState,
        record: RunRecord,
        packet: TaskPacket,
        worktree: Path,
    ) -> None:
        project_root = Path(record.project_root)
        await state._execute_run(
            record,
            packet,
            "test-key",
            worktree,
            project_root / ".git",
            project_root / ".git" / "index",
        )

    async def test_success_requires_all_artifacts_and_cleans_handles(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state, record = self._state_and_record(Path(temporary))
            await self._execute_success(state, record)

            self.assertEqual(record.status, "succeeded")
            self.assertEqual(record.process_started_at, 123.0)
            self.assertIn("last_message", record.artifacts)
            self.assertIn("diff", record.artifacts)
            self.assertIn("files", record.artifacts)
            self.assertNotIn(record.run_id, state._processes)
            self.assertNotIn(record.run_id, state._tasks)

    async def test_rss_sampling_keeps_window_summary_and_sse_without_sample_persistence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state, record = self._state_and_record(Path(temporary))
            run_dir = state.runs_root / record.run_id
            (run_dir / "last-message.md").write_text("finished\n", encoding="utf-8")
            process = FakeProcess()
            packet = TaskPacket(record.run_id, {}, "# Task Packet\n")
            persist = Mock(wraps=state._persist)
            event = Mock(wraps=state._evidence[record.run_id].event)
            broadcast = AsyncMock(wraps=state.broadcast)

            async def start(**kwargs: object) -> FakeProcess:
                callback = kwargs["rss_callback"]
                for rss in range(1, RSS_SAMPLE_LIMIT + 6):
                    await callback(rss)  # type: ignore[operator]
                self.assertEqual(persist.call_count, 1)
                return process

            with (
                patch("orchestrator.daemon.start_codex_run", new=start),
                patch("orchestrator.daemon.psutil.Process") as process_probe,
                patch("orchestrator.daemon.os.getpgid", return_value=process.pid),
                patch("orchestrator.daemon.diff_text", return_value=""),
                patch("orchestrator.daemon.changed_files", return_value=[]),
                patch.object(state, "_persist", persist),
                patch.object(state._evidence[record.run_id], "event", event),
                patch.object(state, "broadcast", broadcast),
            ):
                process_probe.return_value.create_time.return_value = 123.0
                await self._execute_run(state, record, packet, Path(record.worktree))

            self.assertEqual(len(record.rss_samples), RSS_SAMPLE_LIMIT)
            self.assertEqual(
                [sample["rss_bytes"] for sample in record.rss_samples],
                list(range(6, RSS_SAMPLE_LIMIT + 6)),
            )
            self.assertEqual(record.rss_sample_count, RSS_SAMPLE_LIMIT + 5)
            self.assertEqual(record.rss_last_bytes, RSS_SAMPLE_LIMIT + 5)
            self.assertEqual(record.rss_peak_bytes, RSS_SAMPLE_LIMIT + 5)
            self.assertEqual(
                sum(call.args[0] == "rss.sample" for call in event.call_args_list), 0
            )
            self.assertEqual(
                sum(call.args[0] == "run.rss" for call in broadcast.call_args_list),
                RSS_SAMPLE_LIMIT + 5,
            )
            durable = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
            self.assertEqual(len(durable["rss_samples"]), RSS_SAMPLE_LIMIT)
            self.assertEqual(durable["rss_sample_count"], RSS_SAMPLE_LIMIT + 5)

    async def test_legacy_rss_history_is_bounded_when_loaded(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state, record = self._state_and_record(root)
            legacy = record.to_dict()
            legacy.pop("rss_sample_count")
            legacy.pop("rss_last_bytes")
            legacy.pop("rss_peak_bytes")
            legacy["rss_samples"] = [
                {"at": "first", "rss_bytes": 1000},
                *[
                    {"at": str(value), "rss_bytes": value}
                    for value in range(2, RSS_SAMPLE_LIMIT + 5)
                ],
            ]
            (state.runs_root / record.run_id / "run.json").write_text(
                json.dumps(legacy), encoding="utf-8"
            )

            loaded = DaemonState(
                data_dir=root / "reloaded-app",
                project_root=root,
                codex_path="/usr/bin/true",
            ).records[record.run_id]

            self.assertEqual(len(loaded.rss_samples), RSS_SAMPLE_LIMIT)
            self.assertEqual(loaded.rss_sample_count, RSS_SAMPLE_LIMIT + 4)
            self.assertEqual(loaded.rss_last_bytes, RSS_SAMPLE_LIMIT + 4)
            self.assertEqual(loaded.rss_peak_bytes, 1000)

    async def test_invalid_legacy_rss_types_do_not_block_daemon_load(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state, record = self._state_and_record(root)
            invalid = record.to_dict()
            invalid["rss_samples"] = None
            invalid["rss_sample_count"] = None
            (state.runs_root / record.run_id / "run.json").write_text(
                json.dumps(invalid), encoding="utf-8"
            )

            reloaded = DaemonState(
                data_dir=root / "reloaded-app",
                project_root=root,
                codex_path="/usr/bin/true",
            )

            self.assertNotIn(record.run_id, reloaded.records)

    async def test_broadcast_delivers_one_payload_per_subscriber(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state, _ = self._state_and_record(Path(temporary))
            queue: asyncio.Queue[dict[str, object]] = asyncio.Queue()
            state._subscribers.add(queue)

            await state.broadcast("run.updated", run={"run_id": "run-1"})

            self.assertEqual(queue.qsize(), 1)
            self.assertEqual((await queue.get())["event"], "run.updated")
            self.assertTrue(queue.empty())

    async def test_artifact_failure_leaves_incomplete_terminal_record(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state, record = self._state_and_record(root)
            (state.runs_root / record.run_id / "last-message.md").write_text(
                "finished\n", encoding="utf-8"
            )
            process = FakeProcess()
            packet = TaskPacket(record.run_id, {}, "# Task Packet\n")
            with (
                patch("orchestrator.daemon.start_codex_run", new=AsyncMock(return_value=process)),
                patch("orchestrator.daemon.psutil.Process") as process_probe,
                patch("orchestrator.daemon.os.getpgid", return_value=process.pid),
                patch("orchestrator.daemon.diff_text", side_effect=OSError("diff unavailable")),
                patch("orchestrator.daemon.changed_files", return_value=[]),
            ):
                process_probe.return_value.create_time.return_value = 123.0
                await self._execute_run(state, record, packet, root / "worktree")

            self.assertEqual(record.status, "incomplete")
            self.assertIn("diff.patch", record.error or "")
            self.assertNotIn(record.run_id, state._processes)
            self.assertNotIn(record.run_id, state._tasks)

    async def test_last_message_encoding_failure_leaves_incomplete(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state, record = self._state_and_record(root)
            (state.runs_root / record.run_id / "last-message.md").write_bytes(b"\xff")
            process = FakeProcess()
            packet = TaskPacket(record.run_id, {}, "# Task Packet\n")
            with (
                patch("orchestrator.daemon.start_codex_run", new=AsyncMock(return_value=process)),
                patch("orchestrator.daemon.psutil.Process") as process_probe,
                patch("orchestrator.daemon.os.getpgid", return_value=process.pid),
                patch("orchestrator.daemon.diff_text", return_value=""),
                patch("orchestrator.daemon.changed_files", return_value=[]),
            ):
                process_probe.return_value.create_time.return_value = 123.0
                await self._execute_run(state, record, packet, root / "worktree")

            self.assertEqual(record.status, "incomplete")
            self.assertIn("last message", record.error or "")

    async def test_files_artifact_failure_leaves_incomplete(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state, record = self._state_and_record(root)
            (state.runs_root / record.run_id / "last-message.md").write_text(
                "finished\n", encoding="utf-8"
            )
            process = FakeProcess()
            packet = TaskPacket(record.run_id, {}, "# Task Packet\n")
            with (
                patch("orchestrator.daemon.start_codex_run", new=AsyncMock(return_value=process)),
                patch("orchestrator.daemon.psutil.Process") as process_probe,
                patch("orchestrator.daemon.os.getpgid", return_value=process.pid),
                patch("orchestrator.daemon.diff_text", return_value=""),
                patch("orchestrator.daemon.changed_files", return_value=[]),
                patch.object(
                    state._evidence[record.run_id],
                    "write_file_list",
                    side_effect=OSError("files write unavailable"),
                ),
            ):
                process_probe.return_value.create_time.return_value = 123.0
                await self._execute_run(state, record, packet, root / "worktree")

            self.assertEqual(record.status, "incomplete")
            self.assertIn("files.json", record.error or "")
            self.assertIn("diff", record.artifacts)

    async def test_missing_process_identity_stops_provider_process(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state, record = self._state_and_record(root)
            process = ShutdownProcess()
            packet = TaskPacket(record.run_id, {}, "# Task Packet\n")
            with (
                patch("orchestrator.daemon.start_codex_run", new=AsyncMock(return_value=process)),
                patch("orchestrator.daemon.psutil.Process") as process_probe,
                patch("orchestrator.daemon.os.getpgid", side_effect=OSError("pgid unavailable")),
            ):
                process_probe.return_value.create_time.return_value = 123.0
                await self._execute_run(state, record, packet, root / "worktree")

            self.assertEqual(record.status, "incomplete")
            self.assertIn("process-group identity", record.error or "")
            self.assertEqual(process.stop_calls, [False, True])
            self.assertNotIn(record.run_id, state._processes)
            self.assertNotIn(record.run_id, state._tasks)

    async def test_terminal_persist_event_and_broadcast_failures_do_not_leak(self) -> None:
        for failure_kind in ("persist", "event", "broadcast"):
            with self.subTest(failure_kind=failure_kind), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                state, record = self._state_and_record(root)
                if failure_kind == "persist":
                    original_persist = state._persist

                    def fail_terminal_persist(value: RunRecord) -> None:
                        if value.exit_code == 0:
                            raise OSError("run record unavailable")
                        original_persist(value)

                    persist_patch = patch.object(state, "_persist", side_effect=fail_terminal_persist)
                else:
                    persist_patch = patch.object(state, "_persist", wraps=state._persist)

                original_event = state._evidence[record.run_id].event

                def event(value: str, **data: object) -> None:
                    if failure_kind == "event" and value == "run.finished":
                        raise OSError("event unavailable")
                    original_event(value, **data)

                original_broadcast = state.broadcast

                async def broadcast(value: str, **data: object) -> None:
                    run = data.get("run")
                    if (
                        failure_kind == "broadcast"
                        and value == "run.updated"
                        and isinstance(run, dict)
                        and run.get("status") == "succeeded"
                    ):
                        raise OSError("broadcast unavailable")
                    await original_broadcast(value, **data)

                with (
                    persist_patch,
                    patch.object(state._evidence[record.run_id], "event", side_effect=event),
                    patch.object(state, "broadcast", side_effect=broadcast),
                ):
                    await self._execute_success(state, record)

                expected_status = "succeeded" if failure_kind == "broadcast" else "incomplete"
                self.assertEqual(record.status, expected_status)
                if failure_kind == "broadcast":
                    durable = json.loads(
                        (state.runs_root / record.run_id / "run.json").read_text(
                            encoding="utf-8"
                        )
                    )
                    self.assertEqual(durable["status"], "succeeded")
                self.assertNotIn(record.run_id, state._processes)
                self.assertNotIn(record.run_id, state._tasks)

    async def test_terminal_event_and_persist_failure_keeps_disk_checkpoint_incomplete(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state, record = self._state_and_record(root)
            checkpoint_writes = 0
            original_persist = state._persist

            def persist(value: RunRecord) -> None:
                nonlocal checkpoint_writes
                if value.exit_code == 0:
                    checkpoint_writes += 1
                    if checkpoint_writes > 1:
                        raise OSError("terminal run record unavailable")
                original_persist(value)

            original_event = state._evidence[record.run_id].event

            def event(value: str, **data: object) -> None:
                if value == "run.finished":
                    raise OSError("terminal event unavailable")
                original_event(value, **data)

            with (
                patch.object(state, "_persist", side_effect=persist),
                patch.object(state._evidence[record.run_id], "event", side_effect=event),
            ):
                await self._execute_success(state, record)

            durable = json.loads(
                (state.runs_root / record.run_id / "run.json").read_text(encoding="utf-8")
            )
            self.assertEqual(record.status, "incomplete")
            self.assertEqual(durable["status"], "incomplete")
            self.assertIn("terminal event", record.error or "")
            self.assertIn("terminal run record", record.error or "")
            self.assertNotIn(record.run_id, state._processes)
            self.assertNotIn(record.run_id, state._tasks)

    async def test_terminal_event_never_claims_uncommitted_success(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state, record = self._state_and_record(root)
            await self._execute_success(state, record)

            events = [
                json.loads(line)
                for line in (state.runs_root / record.run_id / "events.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            finished = [event for event in events if event["event"] == "run.finished"][-1]
            self.assertEqual(finished["status"], "incomplete")
            self.assertEqual(finished["candidate_status"], "succeeded")
            self.assertEqual(record.status, "succeeded")

    async def test_already_exited_stop_does_not_rewind_to_stopping(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state, record = self._state_and_record(Path(temporary), status="running")
            process = FakeProcess(running=False, returncode=0)
            state._processes[record.run_id] = process  # type: ignore[assignment]
            state._tasks[record.run_id] = asyncio.create_task(asyncio.sleep(0))
            result = await state.stop_run(record.run_id, force=False)

            self.assertIs(result, record)
            self.assertEqual(record.status, "incomplete")
            self.assertEqual(process.stop_calls, [])

    async def test_starting_stop_records_pending_without_awaiting_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state, record = self._state_and_record(Path(temporary), status="created")
            release = asyncio.Event()

            async def pending_run() -> None:
                await release.wait()

            task = asyncio.create_task(pending_run())
            state._tasks[record.run_id] = task

            result = await asyncio.wait_for(
                state.stop_run(record.run_id, force=False), timeout=0.2
            )

            self.assertIs(result, record)
            self.assertEqual(record.status, "stopping")
            self.assertEqual(record.stop_outcome, "stop_pending")
            self.assertFalse(task.done())
            with self.assertRaisesRegex(APIError, "force stop"):
                await state.stop_run(record.run_id, force=True)

            release.set()
            await task
            state._tasks.pop(record.run_id, None)

    async def test_shutdown_retries_after_pending_process_registration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state, record = self._state_and_record(Path(temporary), status="created")
            process = ShutdownProcess()
            state._shutdown.set()

            async def pending_run() -> None:
                await asyncio.sleep(0)
                state._processes[record.run_id] = process  # type: ignore[assignment]
                state._lifecycle_changed.set()

            task = asyncio.create_task(pending_run())
            state._tasks[record.run_id] = task

            await asyncio.wait_for(
                _serve_until_clean_shutdown(state, grace_seconds=0), timeout=0.2
            )

            self.assertEqual(record.status, "stopping")
            self.assertEqual(process.stop_calls, [False, True])
            self.assertNotIn(record.run_id, state._processes)
            self.assertTrue(task.done())

    async def test_shutdown_terms_then_kills_and_awaits_finalization(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state, record = self._state_and_record(Path(temporary), status="running")
            process = ShutdownProcess()
            state._processes[record.run_id] = process  # type: ignore[assignment]
            finalized = False

            async def finalize() -> None:
                nonlocal finalized
                await asyncio.sleep(0)
                finalized = True

            task = asyncio.create_task(finalize())
            state._tasks[record.run_id] = task
            survivors = await asyncio.wait_for(
                state.shutdown_runs(grace_seconds=0), timeout=0.2
            )

            self.assertFalse(survivors)
            self.assertEqual(process.stop_calls, [False, True])
            self.assertTrue(finalized)
            self.assertFalse(task.cancelled())
            self.assertNotIn(record.run_id, state._processes)

    async def test_shutdown_kill_timeout_is_bounded_and_keeps_blocker(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state, record = self._state_and_record(Path(temporary), status="running")
            process = ShutdownProcess(kill_timeout=True)
            state._processes[record.run_id] = process  # type: ignore[assignment]
            released = asyncio.Event()
            finalized = False

            async def finalize() -> None:
                nonlocal finalized
                await released.wait()
                finalized = True

            task = asyncio.create_task(finalize())
            state._tasks[record.run_id] = task

            survivors = await asyncio.wait_for(
                state.shutdown_runs(grace_seconds=0), timeout=0.2
            )

            self.assertTrue(survivors)
            self.assertEqual(record.status, "incomplete")
            self.assertEqual(record.stop_outcome, "shutdown_survivor_alive")
            self.assertIn(record.run_id, state._processes)
            self.assertIs(state._tasks.get(record.run_id), task)
            self.assertFalse(task.cancelled())
            self.assertFalse(finalized)
            self.assertIn(record.run_id, state.active_run_ids())
            self.assertIn(record.run_id, state.overview()["active_run_ids"])
            with self.assertRaisesRegex(APIError, "active"):
                state.request_idle_shutdown()

            released.set()
            process.kill_timeout = False
            survivors = await asyncio.wait_for(
                state.shutdown_runs(grace_seconds=0), timeout=0.2
            )
            self.assertFalse(survivors)
            self.assertTrue(finalized)
            self.assertFalse(task.cancelled())
            self.assertNotIn(record.run_id, state._processes)

    async def test_serve_shutdown_loop_retries_after_survivor(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state, _ = self._state_and_record(Path(temporary))
            state._shutdown.set()
            calls = 0

            async def shutdown_runs(*, grace_seconds: float) -> bool:
                nonlocal calls
                calls += 1
                if calls == 1:
                    state._shutdown.clear()
                    asyncio.get_running_loop().call_soon(state._shutdown.set)
                    return True
                return False

            state.shutdown_runs = shutdown_runs  # type: ignore[method-assign]
            await asyncio.wait_for(
                _serve_until_clean_shutdown(state, grace_seconds=0), timeout=0.2
            )

            self.assertEqual(calls, 2)

    async def test_durable_lifecycle_error_survivor_keeps_process_handle(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state, record = self._state_and_record(root)
            process = ShutdownProcess(kill_timeout=True)
            packet = TaskPacket(record.run_id, {}, "# Task Packet\n")

            original_event = state._evidence[record.run_id].event

            def event(name: str, **data: object) -> None:
                if name == "run.started":
                    raise OSError("event unavailable")
                original_event(name, **data)

            with (
                patch("orchestrator.daemon.start_codex_run", new=AsyncMock(return_value=process)),
                patch("orchestrator.daemon.psutil.Process") as process_probe,
                patch("orchestrator.daemon.os.getpgid", return_value=process.pid),
                patch.object(state._evidence[record.run_id], "event", side_effect=event),
            ):
                process_probe.return_value.create_time.return_value = 123.0
                await self._execute_run(state, record, packet, root / "worktree")

            self.assertEqual(record.status, "incomplete")
            self.assertEqual(record.stop_outcome, "lifecycle_survivor_alive")
            self.assertIn(record.run_id, state._processes)
            self.assertNotIn(record.run_id, state._tasks)
            self.assertIn(record.run_id, state.active_run_ids())

            stopped = await state.stop_run(record.run_id, force=False)
            self.assertIs(stopped, record)
            self.assertEqual(record.stop_outcome, "awaiting_force")
            process.kill_timeout = False
            stopped = await state.stop_run(record.run_id, force=True)
            self.assertIs(stopped, record)
            self.assertEqual(record.status, "incomplete")
            self.assertNotIn(record.run_id, state._processes)

    async def test_all_run_broadcast_failures_are_non_authoritative(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state, record = self._state_and_record(Path(temporary))

            with patch.object(
                state, "broadcast", new=AsyncMock(side_effect=OSError("broadcast unavailable"))
            ):
                await self._execute_success(state, record)

            self.assertEqual(record.status, "succeeded")
            durable = json.loads(
                (state.runs_root / record.run_id / "run.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(durable["status"], "succeeded")

    async def test_incomplete_during_wait_is_not_promoted_after_later_exit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state, record = self._state_and_record(root)
            (state.runs_root / record.run_id / "last-message.md").write_text(
                "finished\n", encoding="utf-8"
            )
            process = DeferredProcess()
            packet = TaskPacket(record.run_id, {}, "# Task Packet\n")
            with (
                patch("orchestrator.daemon.start_codex_run", new=AsyncMock(return_value=process)),
                patch("orchestrator.daemon.psutil.Process") as process_probe,
                patch("orchestrator.daemon.os.getpgid", return_value=process.pid),
                patch("orchestrator.daemon.diff_text", return_value=""),
                patch("orchestrator.daemon.changed_files", return_value=[]),
            ):
                process_probe.return_value.create_time.return_value = 123.0
                task = asyncio.create_task(
                    self._execute_run(state, record, packet, root / "worktree")
                )
                await asyncio.sleep(0)
                await state._record_incomplete(
                    record,
                    outcome="shutdown_survivor_alive",
                    reason="shutdown observed a live process while wait was pending",
                )
                process.released.set()
                await task

            self.assertEqual(record.status, "incomplete")
            self.assertEqual(record.stop_outcome, "shutdown_survivor_alive")
            self.assertNotIn(record.run_id, state._processes)
            self.assertNotIn(record.run_id, state._tasks)

    def test_windows_recovery_force_action_does_not_require_sigkill(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state, record = self._state_and_record(Path(temporary), status="running")
            record.pid = 4242
            root = SimpleNamespace()
            child = SimpleNamespace(terminate=Mock(), kill=Mock())
            root.children = lambda recursive=True: [child]
            root.terminate = Mock()
            root.kill = Mock()
            with (
                patch("orchestrator.daemon.os.name", "nt"),
                patch("orchestrator.daemon.signal", SimpleNamespace()),
                patch("orchestrator.daemon._exact_survivor", return_value=(root, None)),
            ):
                error = state._send_recovery_signal(record, force=True)

            self.assertIsNone(error)
            root.kill.assert_called_once()
            child.kill.assert_called_once()

    async def test_restart_reconciles_exact_survivor_with_term(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state, record = self._state_and_record(Path(temporary), status="running")
            record.pid = 4242
            record.process_group = 4242
            record.process_started_at = 123.0
            survivor = object()
            with (
                patch(
                    "orchestrator.daemon._exact_survivor",
                    side_effect=[
                        (survivor, None),
                        (survivor, None),
                        (None, "process_not_running"),
                    ],
                ),
                patch(
                    "orchestrator.daemon.os.killpg",
                    side_effect=[None, ProcessLookupError()],
                ) as killpg,
            ):
                await state.reconcile_records(grace_seconds=0)

            self.assertEqual(killpg.call_args_list[0].args, (4242, signal.SIGTERM))
            self.assertEqual(killpg.call_args_list[1].args, (4242, 0))
            self.assertEqual(record.status, "incomplete")
            self.assertEqual(record.stop_outcome, "recovery_term_exited")
            self.assertEqual(state.active_run_ids(), ())

    async def test_restart_reconciles_root_gone_posix_group_with_term(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state, record = self._state_and_record(Path(temporary), status="running")
            record.pid = 4242
            record.process_group = 4242
            record.process_started_at = 123.0

            def killpg(group: int, signum: int) -> None:
                self.assertEqual(group, 4242)
                if signum == 0 and killpg.call_count == 4:
                    raise ProcessLookupError
                killpg.call_count += 1

            killpg.call_count = 1
            with (
                patch(
                    "orchestrator.daemon._exact_survivor",
                    return_value=(None, "process_not_found"),
                ),
                patch("orchestrator.daemon.os.killpg", side_effect=killpg) as signal_group,
            ):
                await state.reconcile_records(grace_seconds=0)

            self.assertIn(
                (4242, signal.SIGTERM),
                [call.args for call in signal_group.call_args_list],
            )
            self.assertEqual(record.status, "incomplete")
            self.assertEqual(record.stop_outcome, "recovery_term_exited")

    async def test_restart_blocks_on_root_gone_recorded_survivor_group(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state, record = self._state_and_record(Path(temporary), status="incomplete")
            record.pid = 4242
            record.process_group = 4242
            record.process_started_at = 123.0
            record.stop_outcome = "shutdown_survivor_alive"
            with (
                patch(
                    "orchestrator.daemon._exact_survivor",
                    return_value=(None, "process_not_found"),
                ),
                patch("orchestrator.daemon.os.killpg", return_value=None),
            ):
                with self.assertRaisesRegex(RuntimeError, "process group remains alive"):
                    await state.reconcile_records(grace_seconds=0)

    def test_recovery_treats_group_eperm_as_still_existing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _, record = self._state_and_record(Path(temporary), status="running")
            record.process_group = 4242
            with patch(
                "orchestrator.daemon.os.killpg",
                side_effect=PermissionError(1, "not permitted"),
            ):
                self.assertEqual(_process_group_state(record), (True, None))

    async def test_restart_pid_mismatch_never_signals_and_frees_active_record(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state, record = self._state_and_record(Path(temporary), status="stopping")
            record.pid = 4242
            record.process_group = 4242
            record.process_started_at = 123.0
            with patch("orchestrator.daemon._exact_survivor", return_value=(None, "pid_reused")), patch(
                "orchestrator.daemon.os.killpg"
            ) as killpg:
                await state.reconcile_records(grace_seconds=0)

            killpg.assert_not_called()
            self.assertEqual(record.status, "incomplete")
            self.assertIn("another process", record.error or "")
            self.assertEqual(state.active_run_ids(), ())

    async def test_restart_legacy_or_missing_process_identity_is_not_signaled(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state, record = self._state_and_record(Path(temporary), status="running")
            with patch("orchestrator.daemon.psutil.Process") as process:
                await state.reconcile_records(grace_seconds=0)

            process.assert_not_called()
            self.assertEqual(record.status, "incomplete")
            self.assertIn("no valid process identity", record.error or "")
            self.assertEqual(state.active_run_ids(), ())

    async def test_restart_created_record_becomes_ownership_lost(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state, record = self._state_and_record(Path(temporary), status="created")

            await state.reconcile_records(grace_seconds=0)

            self.assertEqual(record.status, "incomplete")
            self.assertEqual(record.stop_outcome, "recovery_ownership_lost")
            self.assertIn("only created", record.error or "")
            self.assertEqual(state.active_run_ids(), ())

    def test_invalid_process_start_value_is_ownership_lost(self) -> None:
        self.assertIsNone(_process_start_value(10**10000))

    async def test_restart_survivor_after_kill_blocks_startup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state, record = self._state_and_record(Path(temporary), status="running")
            record.pid = 4242
            record.process_group = 4242
            record.process_started_at = 123.0
            with (
                patch("orchestrator.daemon._exact_survivor", return_value=(object(), None)),
                patch.object(state, "_send_recovery_signal", return_value=None),
                patch.object(
                    state, "_wait_for_recovery_exit", new=AsyncMock(side_effect=["still_alive", "still_alive"])
                ),
            ):
                with self.assertRaisesRegex(RuntimeError, "survivor remains alive"):
                    await state.reconcile_records(grace_seconds=0)

            self.assertEqual(record.status, "incomplete")
            self.assertEqual(record.stop_outcome, "recovery_survivor_alive")
            self.assertEqual(state.active_run_ids(), ())

            with patch(
                "orchestrator.daemon._exact_survivor", return_value=(object(), None)
            ):
                with self.assertRaisesRegex(RuntimeError, "previously unrecovered"):
                    await state.reconcile_records(grace_seconds=0)

    async def test_stale_survivor_record_is_marked_ownership_lost(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state, record = self._state_and_record(Path(temporary), status="incomplete")
            record.pid = 4242
            record.process_group = 4242
            record.process_started_at = 123.0
            record.stop_outcome = "shutdown_survivor_alive"
            with (
                patch(
                    "orchestrator.daemon._exact_survivor",
                    return_value=(None, "pid_reused"),
                ),
                patch("orchestrator.daemon.os.killpg") as killpg,
            ):
                await state.reconcile_records(grace_seconds=0)

            killpg.assert_not_called()
            self.assertEqual(record.status, "incomplete")
            self.assertEqual(record.stop_outcome, "recovery_ownership_lost")
            self.assertIn("another process", record.error or "")
            self.assertEqual(state.active_run_ids(), ())
