"""Explicit local run-data deletion contracts."""

from __future__ import annotations

import asyncio
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from aiohttp.test_utils import TestClient, TestServer

from orchestrator.daemon import APIError, DaemonState, create_app
from orchestrator.models import RunRecord, utc_now
from orchestrator.results import EvidenceStore
from orchestrator.worktree import WorktreeError, create_worktree


class RunDeletionTests(unittest.IsolatedAsyncioTestCase):
    def _init_repo(self, root: Path) -> None:
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
        subprocess.run(
            ["git", "config", "user.email", "test@example.com"],
            cwd=root,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test"], cwd=root, check=True
        )
        (root / "README.md").write_text("hello\n", encoding="utf-8")
        subprocess.run(["git", "add", "README.md"], cwd=root, check=True)
        subprocess.run(["git", "commit", "-qm", "initial"], cwd=root, check=True)

    def _state(self, root: Path) -> DaemonState:
        return DaemonState(
            data_dir=root / "app-data",
            project_root=root,
            persistent=True,
            catalog_fetcher=lambda query: [],
            key_getter=lambda: None,
        )

    def _record(
        self,
        state: DaemonState,
        run_id: str,
        *,
        status: str = "succeeded",
        registered: bool = True,
    ) -> tuple[RunRecord, Path, Path]:
        worktree = state.runtime_root / "worktrees" / run_id
        if registered:
            worktree = create_worktree(state.project_root, run_id).path
        else:
            worktree.mkdir(parents=True)
        record = RunRecord(
            run_id=run_id,
            profile_id="profile",
            model="provider/model",
            status=status,  # type: ignore[arg-type]
            created_at=utc_now(),
            updated_at=utc_now(),
            project_root=str(state.project_root),
            worktree=str(worktree),
        )
        evidence = EvidenceStore(state.runs_root / run_id)
        evidence.write_run(record)
        state.records[run_id] = record
        state._evidence[run_id] = evidence
        return record, evidence.run_dir, worktree

    async def test_terminal_run_deletes_registered_worktree_and_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._init_repo(root)
            state = self._state(root)
            record, run_dir, worktree = self._record(state, "done")

            policy = state.overview()["data_policy"]
            self.assertEqual(policy["retention"], "until_explicit_deletion")
            self.assertEqual(policy["runtime_root"], str(state.runtime_root))
            self.assertEqual(policy["uninstall"], "preserves_project_data")
            self.assertFalse(policy["raw_worktrees_sanitized"])

            self.assertEqual(await state.delete_run(record.run_id), {
                "deleted": [record.run_id],
                "failed": [],
            })
            self.assertNotIn(record.run_id, state.records)
            self.assertFalse(run_dir.exists())
            self.assertFalse(worktree.exists())
            listing = subprocess.run(
                ["git", "worktree", "list", "--porcelain"],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            self.assertNotIn(str(worktree), listing)

    async def test_active_status_task_and_process_each_refuse_deletion(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._init_repo(root)
            state = self._state(root)
            record, run_dir, worktree = self._record(
                state, "active", status="running"
            )

            with self.assertRaises(APIError) as context:
                await state.delete_run(record.run_id)
            self.assertEqual(context.exception.code, "run_not_deletable")

            record.status = "succeeded"
            state._evidence[record.run_id].write_run(record)
            task = asyncio.create_task(asyncio.Event().wait())
            state._tasks[record.run_id] = task
            try:
                with self.assertRaises(APIError) as context:
                    await state.delete_run(record.run_id)
                self.assertEqual(context.exception.code, "run_not_deletable")
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                state._tasks.pop(record.run_id)

            process = MagicMock()
            process.is_running.return_value = True
            state._processes[record.run_id] = process
            with self.assertRaises(APIError) as context:
                await state.delete_run(record.run_id)
            self.assertEqual(context.exception.code, "run_not_deletable")
            self.assertTrue(run_dir.exists())
            self.assertTrue(worktree.exists())

    async def test_path_mismatch_symlink_and_unregistered_directory_are_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._init_repo(root)
            state = self._state(root)
            record, run_dir, worktree = self._record(state, "mismatch")
            record.worktree = str(root / "other")
            state._evidence[record.run_id].write_run(record)
            with self.assertRaises(APIError) as context:
                await state.delete_run(record.run_id)
            self.assertEqual(context.exception.code, "run_delete_refused")
            self.assertTrue(run_dir.exists())
            self.assertTrue(worktree.exists())

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._init_repo(root)
            state = self._state(root)
            record, run_dir, worktree = self._record(
                state, "unregistered", registered=False
            )
            with self.assertRaises(APIError) as context:
                await state.delete_run(record.run_id)
            self.assertEqual(context.exception.code, "run_delete_refused")
            self.assertTrue(run_dir.exists())
            self.assertTrue(worktree.exists())

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._init_repo(root)
            state = self._state(root)
            target = root / "outside"
            target.mkdir()
            worktree = state.runtime_root / "worktrees" / "linked"
            worktree.parent.mkdir(parents=True, exist_ok=True)
            worktree.symlink_to(target, target_is_directory=True)
            record = RunRecord(
                run_id="linked",
                profile_id="profile",
                model="provider/model",
                status="succeeded",
                created_at=utc_now(),
                updated_at=utc_now(),
                project_root=str(state.project_root),
                worktree=str(worktree),
            )
            evidence = EvidenceStore(state.runs_root / record.run_id)
            evidence.write_run(record)
            state.records[record.run_id] = record
            state._evidence[record.run_id] = evidence
            with self.assertRaises(APIError) as context:
                await state.delete_run(record.run_id)
            self.assertEqual(context.exception.code, "run_delete_refused")
            self.assertTrue(target.exists())

    async def test_git_failure_keeps_evidence_and_partial_delete_can_retry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._init_repo(root)
            state = self._state(root)
            record, run_dir, worktree = self._record(state, "retry")

            with (
                patch(
                    "orchestrator.daemon.remove_worktree",
                    side_effect=WorktreeError("blocked"),
                ),
                self.assertRaises(APIError) as context,
            ):
                await state.delete_run(record.run_id)
            self.assertEqual(context.exception.code, "run_delete_refused")
            self.assertTrue(run_dir.exists())
            self.assertTrue(worktree.exists())

            with (
                patch(
                    "orchestrator.daemon.shutil.rmtree",
                    side_effect=OSError("disk busy"),
                ),
                self.assertRaises(APIError) as context,
            ):
                await state.delete_run(record.run_id)
            self.assertEqual(context.exception.code, "run_delete_failed")
            self.assertIn(record.run_id, state.records)
            self.assertTrue(run_dir.exists())
            self.assertFalse(worktree.exists())

            await state.delete_run(record.run_id)
            self.assertNotIn(record.run_id, state.records)
            self.assertFalse(run_dir.exists())

    async def test_corrupt_run_metadata_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._init_repo(root)
            state = self._state(root)
            record, run_dir, worktree = self._record(state, "corrupt")
            (run_dir / "run.json").write_text("{not-json", encoding="utf-8")

            with self.assertRaises(APIError) as context:
                await state.delete_run(record.run_id)

            self.assertEqual(context.exception.code, "run_delete_refused")
            self.assertTrue(run_dir.exists())
            self.assertTrue(worktree.exists())

    async def test_batch_delete_reports_active_run_without_stopping_other_deletes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._init_repo(root)
            state = self._state(root)
            done, done_dir, _ = self._record(state, "done")
            active, active_dir, active_worktree = self._record(
                state, "active", status="running"
            )

            result = await state.delete_runs()

            self.assertEqual(result["deleted"], [done.run_id])
            self.assertEqual(result["failed"][0]["run_id"], active.run_id)
            self.assertEqual(result["failed"][0]["code"], "run_not_deletable")
            self.assertFalse(done_dir.exists())
            self.assertTrue(active_dir.exists())
            self.assertTrue(active_worktree.exists())

    async def test_delete_routes_require_json_and_same_origin(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._init_repo(root)
            state = self._state(root)
            server = TestServer(create_app(state))
            client = TestClient(server)
            await client.start_server()
            try:
                await client.get("/")
                origin = f"http://127.0.0.1:{server.port}"
                response = await client.delete(
                    "/api/runs",
                    headers={
                        "Origin": origin,
                        "Sec-Fetch-Site": "same-origin",
                        "Sec-Fetch-Mode": "cors",
                        "Sec-Fetch-Dest": "empty",
                    },
                )
                self.assertEqual(response.status, 415)

                response = await client.delete(
                    "/api/runs",
                    json={},
                    headers={
                        "Origin": "http://evil.example",
                        "Sec-Fetch-Site": "same-origin",
                        "Sec-Fetch-Mode": "cors",
                        "Sec-Fetch-Dest": "empty",
                    },
                )
                self.assertEqual(response.status, 403)

                response = await client.delete(
                    "/api/runs",
                    json={},
                    headers={
                        "Origin": origin,
                        "Sec-Fetch-Site": "same-origin",
                        "Sec-Fetch-Mode": "cors",
                        "Sec-Fetch-Dest": "empty",
                    },
                )
                self.assertEqual(response.status, 200)
                self.assertEqual(await response.json(), {"deleted": [], "failed": []})

                response = await client.delete(
                    "/api/runs/missing",
                    json={},
                    headers={
                        "Origin": origin,
                        "Sec-Fetch-Site": "same-origin",
                        "Sec-Fetch-Mode": "cors",
                        "Sec-Fetch-Dest": "empty",
                    },
                )
                self.assertEqual(response.status, 404)
            finally:
                await client.close()


class RunDeletionUITests(unittest.TestCase):
    def test_dashboard_wires_explicit_single_and_batch_delete(self) -> None:
        app = (
            Path(__file__).parents[1]
            / "plugins/aiworker-relay/src/orchestrator/web/app.js"
        ).read_text(encoding="utf-8")

        self.assertIn('method: "DELETE"', app)
        self.assertIn("data-confirm-delete-run", app)
        self.assertIn("data-confirm-delete-runs", app)
        self.assertIn("手动删除前一直保留", app)
        self.assertIn("Plugin 更新或卸载不会删除", app)
