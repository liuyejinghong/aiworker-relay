"""Regression tests for the external Codex execution boundary."""

from __future__ import annotations

import os
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from orchestrator.runner import start_codex_run


class RunnerBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_external_run_sets_explicit_sandbox_and_filters_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            worktree = root / "worktree"
            run_dir = root / "run"
            code_home = run_dir / "CODEX_HOME"
            worktree.mkdir()
            with (
                patch.dict(
                    os.environ,
                    {"UNRELATED_SERVICE_TOKEN": "ambient-secret"},
                    clear=False,
                ),
                patch(
                    "orchestrator.runner.start_process",
                    new_callable=AsyncMock,
                ) as start,
            ):
                await start_codex_run(
                    worktree=worktree,
                    run_dir=run_dir,
                    prompt="Create one bounded file.",
                    model="provider/model",
                    reasoning_effort="high",
                    api_key="openrouter-secret",
                    code_home=code_home,
                )
                config_text = (code_home / "config.toml").read_text(
                    encoding="utf-8"
                )

        command = list(start.await_args.args[0])
        sandbox_index = command.index("--sandbox")
        self.assertEqual(command[sandbox_index + 1], "workspace-write")
        self.assertIn("--approve-for-me", command)
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", command)
        self.assertEqual(command[command.index("--model") + 1], "provider/model")

        environment = start.await_args.kwargs["env"]
        self.assertEqual(environment["OPENROUTER_API_KEY"], "openrouter-secret")
        self.assertEqual(environment["UNRELATED_SERVICE_TOKEN"], "ambient-secret")

        config = tomllib.loads(config_text)
        self.assertEqual(config["model"], "provider/model")
        self.assertEqual(config["model_reasoning_effort"], "high")
        policy = config["shell_environment_policy"]
        self.assertEqual(policy["inherit"], "core")
        self.assertFalse(policy["ignore_default_excludes"])
        self.assertEqual(policy["filters"]["OPENROUTER_API_KEY"], "exclude")
        self.assertEqual(
            config["model_providers"]["openrouter"]["env_key"],
            "OPENROUTER_API_KEY",
        )


if __name__ == "__main__":
    unittest.main()
