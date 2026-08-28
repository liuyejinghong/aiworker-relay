"""Regression tests for the external Codex execution boundary."""

from __future__ import annotations

import json
import os
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from orchestrator.runner import (
    _tool_path_and_read_roots,
    _tool_read_candidates,
    start_codex_run,
)


class RunnerBoundaryTests(unittest.IsolatedAsyncioTestCase):
    def test_homebrew_path_does_not_grant_config_or_runtime_data(self) -> None:
        roots = _tool_read_candidates(Path("/opt/homebrew/bin"))

        self.assertIn(Path("/opt/homebrew/bin"), roots)
        self.assertIn(Path("/opt/homebrew/Cellar"), roots)
        self.assertIn(Path("/opt/homebrew/lib/node_modules"), roots)
        self.assertNotIn(Path("/opt/homebrew"), roots)
        self.assertNotIn(Path("/opt/homebrew/etc"), roots)
        self.assertNotIn(Path("/opt/homebrew/var"), roots)

    def test_package_and_framework_roots_are_not_granted_directly(self) -> None:
        homebrew_roots = _tool_read_candidates(Path("/opt/homebrew"))

        self.assertNotIn(Path("/opt/homebrew"), homebrew_roots)
        self.assertIn(Path("/opt/homebrew/Cellar"), homebrew_roots)
        self.assertIn(Path("/opt/homebrew/lib/node_modules"), homebrew_roots)
        self.assertEqual(_tool_read_candidates(Path("/Library/Frameworks")), ())

    def test_tool_path_rewrites_source_entries_to_the_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project_root = root / "source"
            worktree = root / "worktree"
            source_bin = project_root / "tools" / "bin"
            worktree_bin = worktree / "tools" / "bin"
            source_bin.mkdir(parents=True)
            worktree_bin.mkdir(parents=True)

            with (
                patch.dict(os.environ, {"PATH": str(source_bin)}),
                patch("orchestrator.runner.sys.platform", "linux"),
            ):
                tool_path, read_roots = _tool_path_and_read_roots(
                    project_root.resolve(), worktree.resolve()
                )

            self.assertEqual(tool_path, str(worktree_bin.resolve()))
            self.assertIn(worktree_bin.resolve(), read_roots)
            self.assertNotIn(source_bin.resolve(), read_roots)

    async def test_external_run_sets_permission_profile_and_filters_secrets(self) -> None:
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
                    project_root=root,
                    worktree=worktree,
                    git_common_dir=root,
                    source_checkout_index=root / "source.index",
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
        overrides = [
            command[index + 1]
            for index, value in enumerate(command)
            if value == "-c"
        ]
        self.assertNotIn("--sandbox", command)
        self.assertIn("--strict-config", command)
        self.assertIn("--approve-for-me", command)
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", command)
        self.assertIn("allow_login_shell=false", overrides)
        self.assertIn('default_permissions="aiworker"', overrides)
        self.assertFalse(any(value.startswith("projects.") for value in overrides))
        self.assertIn('shell_environment_policy.inherit="none"', overrides)
        self.assertIn(
            "shell_environment_policy.ignore_default_excludes=false", overrides
        )
        self.assertIn(
            "shell_environment_policy.experimental_use_profile=false", overrides
        )
        self.assertIn(
            'shell_environment_policy.filters.OPENROUTER_API_KEY="exclude"',
            overrides,
        )
        self.assertIn('model_reasoning_effort="high"', overrides)
        self.assertEqual(command[command.index("--model") + 1], "provider/model")

        environment = start.await_args.kwargs["env"]
        self.assertEqual(environment["OPENROUTER_API_KEY"], "openrouter-secret")
        self.assertEqual(environment["UNRELATED_SERVICE_TOKEN"], "ambient-secret")
        isolated_home = run_dir.resolve() / "HOME"
        self.assertEqual(environment["HOME"], str(isolated_home))
        self.assertEqual(environment["USERPROFILE"], str(isolated_home))

        config = tomllib.loads(config_text)
        self.assertFalse(config["allow_login_shell"])
        self.assertEqual(config["default_permissions"], "aiworker")
        self.assertEqual(config["model"], "provider/model")
        self.assertEqual(config["model_reasoning_effort"], "high")
        self.assertEqual(
            config["projects"][str(worktree.resolve())]["trust_level"],
            "untrusted",
        )
        permissions = config["permissions"]["aiworker"]
        self.assertNotIn("extends", permissions)
        self.assertTrue(permissions["workspace_roots"][str(isolated_home)])
        self.assertFalse(permissions["network"]["enabled"])
        filesystem = permissions["filesystem"]
        self.assertEqual(filesystem[":root"], "deny")
        self.assertEqual(filesystem[":minimal"], "read")
        self.assertEqual(filesystem[":tmpdir"], "deny")
        self.assertEqual(filesystem[":slash_tmp"], "deny")
        self.assertEqual(
            filesystem[str(root.resolve() / "source.index")],
            "deny",
        )
        self.assertEqual(filesystem[str(root.resolve())], "read")
        workspace_rules = filesystem[":workspace_roots"]
        self.assertEqual(workspace_rules["."], "write")
        self.assertEqual(workspace_rules[".codex"], "read")
        self.assertEqual(workspace_rules[".env*"], "deny")
        policy = config["shell_environment_policy"]
        self.assertEqual(policy["inherit"], "none")
        self.assertFalse(policy["ignore_default_excludes"])
        self.assertFalse(policy["experimental_use_profile"])
        self.assertEqual(policy["set"]["HOME"], str(isolated_home))
        self.assertNotIn("UNRELATED_SERVICE_TOKEN", policy["set"])
        self.assertEqual(policy["filters"]["OPENROUTER_API_KEY"], "exclude")
        self.assertEqual(
            config["model_providers"]["openrouter"]["env_key"],
            "OPENROUTER_API_KEY",
        )


if __name__ == "__main__":
    unittest.main()
