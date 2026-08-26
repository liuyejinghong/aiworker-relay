"""Async external process ownership and two-stage stop semantics."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

import psutil


class ProcessControlError(RuntimeError):
    """A process lifecycle operation is invalid or could not be observed."""


@dataclass(frozen=True, slots=True)
class StopOutcome:
    state: str
    returncode: int | None
    forced: bool = False
    detail: str | None = None


class ManagedProcess:
    """Own one subprocess and its process group until the OS reports exit."""

    def __init__(
        self,
        process: asyncio.subprocess.Process,
        *,
        rss_callback=None,
        rss_interval: float = 2.0,
    ):
        self.process = process
        self.rss_callback = rss_callback
        self.rss_interval = rss_interval
        self.term_requested = False
        self.force_requested = False
        self._rss_task: asyncio.Task[None] | None = None

    @property
    def pid(self) -> int:
        return self.process.pid

    @property
    def returncode(self) -> int | None:
        return self.process.returncode

    def is_running(self) -> bool:
        return self.process.returncode is None

    async def _sample_rss(self) -> None:
        while self.is_running():
            try:
                rss = psutil.Process(self.pid).memory_info().rss
                if self.rss_callback is not None:
                    result = self.rss_callback(rss)
                    if asyncio.iscoroutine(result):
                        await result
            except (psutil.Error, ProcessLookupError):
                pass
            try:
                await asyncio.sleep(self.rss_interval)
            except asyncio.CancelledError:
                return

    def start_sampling(self) -> None:
        if self.rss_callback is not None and self._rss_task is None:
            self._rss_task = asyncio.create_task(self._sample_rss())

    async def _finish_sampling(self) -> None:
        if self._rss_task is not None:
            self._rss_task.cancel()
            await asyncio.gather(self._rss_task, return_exceptions=True)
            self._rss_task = None

    async def wait(self) -> int:
        try:
            return await self.process.wait()
        finally:
            await self._finish_sampling()

    async def _wait_gracefully(self, timeout: float) -> bool:
        if not self.is_running():
            return True
        try:
            await asyncio.wait_for(self.process.wait(), timeout=max(timeout, 0.0))
            return True
        except asyncio.TimeoutError:
            return False

    def _send_term(self) -> None:
        if not self.is_running():
            return
        if os.name == "nt":
            self._signal_windows(psutil.Process.terminate)
            return
        try:
            os.killpg(os.getpgid(self.pid), signal.SIGTERM)
        except ProcessLookupError:
            pass

    def _send_kill(self) -> None:
        if not self.is_running():
            return
        if os.name == "nt":
            self._signal_windows(psutil.Process.kill)
            return
        try:
            os.killpg(os.getpgid(self.pid), signal.SIGKILL)
        except ProcessLookupError:
            pass

    def _signal_windows(self, action) -> None:
        try:
            root = psutil.Process(self.pid)
            children = root.children(recursive=True)
            for child in children:
                try:
                    action(child)
                except psutil.Error:
                    pass
            action(root)
        except psutil.Error:
            pass

    async def stop(
        self, *, force: bool = False, grace_seconds: float = 10.0
    ) -> StopOutcome:
        """TERM first; KILL is only accepted after a still-live TERM stage."""

        if not self.is_running():
            await self._finish_sampling()
            return StopOutcome(
                "already_exited", self.returncode, forced=self.force_requested
            )

        if force:
            if not self.term_requested:
                raise ProcessControlError("force stop requires a prior TERM request")
            self.force_requested = True
            self._send_kill()
            exited = await self._wait_gracefully(min(grace_seconds, 5.0))
            await self._finish_sampling()
            if exited:
                return StopOutcome("killed", self.returncode, forced=True)
            return StopOutcome("kill_timeout", self.returncode, forced=True)

        self.term_requested = True
        self._send_term()
        exited = await self._wait_gracefully(grace_seconds)
        if exited:
            await self._finish_sampling()
            return StopOutcome("term_exited", self.returncode)
        return StopOutcome(
            "awaiting_force", self.returncode, detail="process still alive after TERM"
        )


async def start_process(
    command: Sequence[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
    rss_callback=None,
    rss_interval: float = 2.0,
) -> ManagedProcess:
    """Start a process in an isolated group suitable for external run control."""

    kwargs: dict[str, object] = {
        "cwd": str(cwd),
        "env": env,
        "stdin": asyncio.subprocess.DEVNULL,
        # The harness writes its authoritative final message to the explicit
        # output file.  Discarding raw streams keeps a long-running child from
        # blocking on an undrained pipe and avoids persisting a transcript.
        "stdout": asyncio.subprocess.DEVNULL,
        "stderr": asyncio.subprocess.DEVNULL,
    }
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    try:
        process = await asyncio.create_subprocess_exec(*command, **kwargs)
    except OSError as exc:
        raise ProcessControlError(str(exc)) from exc
    managed = ManagedProcess(
        process,
        rss_callback=rss_callback,
        rss_interval=rss_interval,
    )
    managed.start_sampling()
    return managed


async def start_codex_run(
    *,
    worktree: Path,
    run_dir: Path,
    prompt: str,
    model: str,
    reasoning_effort: str | None,
    api_key: str,
    code_home: Path,
    executable: str = "codex",
    rss_callback=None,
) -> ManagedProcess:
    """Start the only accepted v0.1 harness: isolated ``codex exec``."""

    code_home.mkdir(parents=True, exist_ok=True)
    run_dir.mkdir(parents=True, exist_ok=True)
    # Keep provider and model selection inside this run's CODEX_HOME.  The
    # parent Codex config and its hooks are never copied into this directory.
    config_lines = [
        'model_provider = "openrouter"',
        f"model = {json.dumps(model)}",
    ]
    if reasoning_effort and reasoning_effort != "auto":
        config_lines.append(f"model_reasoning_effort = {json.dumps(reasoning_effort)}")
    config_lines.extend(
        [
            "",
            "[model_providers.openrouter]",
            'name = "OpenRouter"',
            'base_url = "https://openrouter.ai/api/v1"',
            'env_key = "OPENROUTER_API_KEY"',
            'wire_api = "responses"',
            "",
        ]
    )
    (code_home / "config.toml").write_text("\n".join(config_lines), encoding="utf-8")
    output_path = run_dir / "last-message.md"
    command = [
        executable,
        "exec",
        "--json",
        "--ephemeral",
        "--model",
        model,
        "--output-last-message",
        str(output_path),
        "--cd",
        str(worktree),
        prompt,
    ]
    environment = os.environ.copy()
    environment["CODEX_HOME"] = str(code_home)
    # The key is only in the child environment; it is never part of a record.
    environment["OPENROUTER_API_KEY"] = api_key
    return await start_process(
        command,
        cwd=worktree,
        env=environment,
        rss_callback=rss_callback,
    )
