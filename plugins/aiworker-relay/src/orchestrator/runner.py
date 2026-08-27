"""Async external process ownership and two-stage stop semantics."""

from __future__ import annotations

import asyncio
import errno
import json
import math
import os
import signal
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

import psutil


class ProcessControlError(RuntimeError):
    """A process lifecycle operation is invalid or could not be observed."""


def _error_message(value: object) -> str | None:
    """Keep the provider's human message, not its opaque event payload."""

    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return value.strip() or None
        return _error_message(decoded)
    if isinstance(value, dict):
        nested = value.get("error")
        if nested is not None:
            return _error_message(nested)
        for key in ("message", "detail"):
            detail = value.get(key)
            message = _error_message(detail)
            if message:
                return message
    return None


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
        self._stdout_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._stdout_tail = ""
        self._stderr_tail = ""
        self._stop_lock = asyncio.Lock()
        self.process_group: int | None = None
        self._identity_error: str | None = None
        self._windows_targets: dict[int, float] = {}
        if os.name == "nt":
            self._capture_windows_tree()
        else:
            try:
                self.process_group = os.getpgid(self.pid)
            except (ProcessLookupError, OSError, ValueError) as exc:
                self._identity_error = f"could not capture process group: {exc}"

    @property
    def pid(self) -> int:
        return self.process.pid

    @property
    def returncode(self) -> int | None:
        return self.process.returncode

    def is_running(self) -> bool:
        if os.name == "nt":
            running, _ = self._windows_tree_state()
        else:
            running = self._posix_group_state()
        return running or self._identity_error is not None

    @property
    def identity_error(self) -> str | None:
        return self._identity_error

    def _set_identity_error(self, detail: str) -> None:
        if not self._identity_error:
            self._identity_error = detail.strip() or "process identity could not be verified"

    def _posix_group_state(self) -> bool:
        """Return whether the captured process group still exists."""

        if self.process_group is None:
            return self.process.returncode is None
        try:
            os.killpg(self.process_group, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            # On macOS a process group containing only a freshly killed,
            # unreaped child can transiently return EPERM for signal 0.  The
            # group still exists; keep polling instead of permanently losing
            # the identity of an already-owned group.
            return True
        except OSError as exc:
            if exc.errno == errno.ESRCH:
                return False
            self._set_identity_error(f"could not inspect process group: {exc}")
            return True
        return True

    @staticmethod
    def _valid_start_time(value: object) -> float | None:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        try:
            value = float(value)
        except (TypeError, ValueError, OverflowError):
            return None
        return value if math.isfinite(value) and value > 0 else None

    def _capture_windows_tree(self) -> None:
        """Capture exact root/descendant identities while the root is live."""

        if os.name != "nt":
            return
        try:
            root = psutil.Process(self.pid)
        except psutil.NoSuchProcess:
            if not self._windows_targets:
                self._set_identity_error(
                    "external process disappeared before its identity was captured"
                )
            return
        except (psutil.AccessDenied, psutil.ZombieProcess, OSError) as exc:
            self._set_identity_error(f"could not inspect external process: {exc}")
            return
        try:
            children = root.children(recursive=True)
        except psutil.NoSuchProcess:
            children = []
        except (psutil.AccessDenied, psutil.ZombieProcess, OSError) as exc:
            self._set_identity_error(f"could not capture external process tree: {exc}")
            return
        for candidate in [root, *children]:
            try:
                started_at = self._valid_start_time(candidate.create_time())
            except psutil.NoSuchProcess:
                continue
            except (psutil.AccessDenied, psutil.ZombieProcess, OSError) as exc:
                self._set_identity_error(
                    f"could not capture process-tree identity: {exc}"
                )
                continue
            if started_at is None:
                self._set_identity_error("process-tree member has no valid start time")
                continue
            self._windows_targets[candidate.pid] = started_at

    def _windows_tree_state(self) -> tuple[bool, str | None]:
        """Return live/error state for the captured exact process tree."""

        if os.name != "nt":
            return False, None
        if self.process.returncode is None:
            self._capture_windows_tree()
        if self._identity_error:
            return True, self._identity_error
        live = False
        for pid, expected_start in tuple(self._windows_targets.items()):
            try:
                process = psutil.Process(pid)
                observed_start = self._valid_start_time(process.create_time())
                if observed_start is None or not math.isclose(
                    observed_start, expected_start, rel_tol=0.0, abs_tol=1e-6
                ):
                    self._set_identity_error(f"process-tree PID {pid} changed identity")
                    return True, self._identity_error
                if process.is_running():
                    live = True
            except psutil.NoSuchProcess:
                continue
            except (psutil.AccessDenied, psutil.ZombieProcess, OSError, ValueError) as exc:
                self._set_identity_error(
                    f"could not inspect process-tree member {pid}: {exc}"
                )
                return True, self._identity_error
        return live, None

    @property
    def stderr_summary(self) -> str | None:
        """Return one bounded failure line, never a persisted transcript."""

        lines = [line.strip() for line in self._stderr_tail.splitlines() if line.strip()]
        return lines[-1][:1000] if lines else None

    @property
    def failure_summary(self) -> str | None:
        """Extract a structured CLI failure without retaining its transcript."""

        for line in reversed(self._stdout_tail.splitlines()):
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            event_type = str(event.get("type", "")).lower()
            if "error" not in event_type and "fail" not in event_type:
                continue
            detail = event.get("error") or event.get("message") or event.get("detail")
            message = _error_message(detail)
            if message:
                return message[:1000]
        return self.stderr_summary

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
        if self.process.stdout is not None and self._stdout_task is None:
            self._stdout_task = asyncio.create_task(self._capture_stdout())
        if self.process.stderr is not None and self._stderr_task is None:
            self._stderr_task = asyncio.create_task(self._capture_stderr())

    async def _capture_stdout(self) -> None:
        assert self.process.stdout is not None
        while True:
            chunk = await self.process.stdout.read(4096)
            if not chunk:
                return
            self._stdout_tail = (self._stdout_tail + chunk.decode(errors="replace"))[-4096:]

    async def _capture_stderr(self) -> None:
        assert self.process.stderr is not None
        while True:
            chunk = await self.process.stderr.read(4096)
            if not chunk:
                return
            self._stderr_tail = (self._stderr_tail + chunk.decode(errors="replace"))[-4096:]

    async def _stop_rss_sampling(self) -> None:
        if self._rss_task is not None:
            self._rss_task.cancel()
            await asyncio.gather(self._rss_task, return_exceptions=True)
            self._rss_task = None

    async def _finish_sampling(self) -> None:
        await self._stop_rss_sampling()
        if self._stdout_task is not None:
            await asyncio.gather(self._stdout_task, return_exceptions=True)
            self._stdout_task = None
        if self._stderr_task is not None:
            await asyncio.gather(self._stderr_task, return_exceptions=True)
            self._stderr_task = None

    async def wait(self) -> int:
        try:
            returncode = await self._wait_for_owned_exit()
        except BaseException:
            # A still-live child may keep the inherited pipes open.  When
            # ownership cannot be confirmed, stop only RSS sampling and leave
            # the pipe readers attached to the retained process handle.
            await self._stop_rss_sampling()
            raise
        else:
            await self._finish_sampling()
            return returncode

    async def _wait_for_owned_exit(self) -> int:
        returncode = await self.process.wait()
        if not await self._wait_for_group_exit():
            raise ProcessControlError(
                self._identity_error or "owned process group could not be verified"
            )
        return returncode

    async def _wait_for_group_exit(self) -> bool:
        """Wait until the owned group/tree is gone, not only its root."""

        while True:
            if os.name == "nt":
                running, error = self._windows_tree_state()
            else:
                running = self._posix_group_state()
                error = self._identity_error
            if error:
                return False
            if not running:
                return True
            await asyncio.sleep(0.05)

    async def _wait_gracefully(self, timeout: float) -> bool:
        if not self.is_running():
            return True
        try:
            await asyncio.wait_for(self._wait_for_owned_exit(), timeout=max(timeout, 0.0))
            return True
        except asyncio.TimeoutError:
            return False

    def _send_term(self) -> None:
        if not self.is_running():
            return
        if os.name == "nt":
            self._signal_windows("terminate")
            return
        if self.process_group is None:
            self._set_identity_error(
                "cannot TERM an external process without its process group"
            )
            return
        try:
            os.killpg(self.process_group, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except OSError as exc:
            self._set_identity_error(f"could not TERM process group: {exc}")

    def _send_kill(self) -> None:
        if not self.is_running():
            return
        if os.name == "nt":
            self._signal_windows("kill")
            return
        if self.process_group is None:
            self._set_identity_error(
                "cannot KILL an external process without its process group"
            )
            return
        try:
            os.killpg(self.process_group, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError as exc:
            self._set_identity_error(f"could not KILL process group: {exc}")

    def _signal_windows(self, action_name: str) -> None:
        """Signal all currently captured exact tree members, child first."""

        if self.process.returncode is None:
            self._capture_windows_tree()
        targets: list[psutil.Process] = []
        for pid, expected_start in tuple(self._windows_targets.items()):
            try:
                process = psutil.Process(pid)
                observed_start = self._valid_start_time(process.create_time())
                if observed_start is None or not math.isclose(
                    observed_start, expected_start, rel_tol=0.0, abs_tol=1e-6
                ):
                    self._set_identity_error(f"process-tree PID {pid} changed identity")
                    return
                if process.is_running():
                    targets.append(process)
            except psutil.NoSuchProcess:
                continue
            except (psutil.AccessDenied, psutil.ZombieProcess, OSError, ValueError) as exc:
                self._set_identity_error(
                    f"could not inspect process-tree member {pid}: {exc}"
                )
                return
        for process in reversed(targets):
            try:
                getattr(process, action_name)()
            except psutil.NoSuchProcess:
                continue
            except (psutil.AccessDenied, psutil.ZombieProcess, OSError) as exc:
                self._set_identity_error(
                    f"could not {action_name} process-tree member {process.pid}: {exc}"
                )
                return

    async def stop(
        self, *, force: bool = False, grace_seconds: float = 10.0
    ) -> StopOutcome:
        """TERM first; KILL is only accepted after a still-live TERM stage."""

        async with self._stop_lock:
            return await self._stop_locked(
                force=force, grace_seconds=grace_seconds
            )

    async def _stop_locked(
        self, *, force: bool, grace_seconds: float
    ) -> StopOutcome:
        """Serialize stop requests for one owned process."""

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
            if exited:
                await self._finish_sampling()
                return StopOutcome("killed", self.returncode, forced=True)
            await self._stop_rss_sampling()
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
        # output file. JSONL stdout and stderr are drained only in memory; on
        # failure, one structured error is retained instead of a transcript.
        "stdout": asyncio.subprocess.PIPE,
        "stderr": asyncio.subprocess.PIPE,
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
        "allow_login_shell = false",
        'model_provider = "openrouter"',
        f"model = {json.dumps(model)}",
    ]
    if reasoning_effort and reasoning_effort != "auto":
        config_lines.append(f"model_reasoning_effort = {json.dumps(reasoning_effort)}")
    config_lines.extend(
        [
            "",
            "[shell_environment_policy]",
            'inherit = "core"',
            "ignore_default_excludes = false",
            "",
            "[shell_environment_policy.filters]",
            '"OPENROUTER_API_KEY" = "exclude"',
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
        "-c",
        "allow_login_shell=false",
        "--sandbox",
        "workspace-write",
        # External runs cannot answer a terminal approval prompt. Keep their
        # automatic approval within Codex's supported workspace-write mode.
        "--approve-for-me",
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
    # The provider process needs the Key, while shell_environment_policy keeps
    # it and other ambient secrets out of commands spawned by the worker.
    environment["OPENROUTER_API_KEY"] = api_key
    return await start_process(
        command,
        cwd=worktree,
        env=environment,
        rss_callback=rss_callback,
    )
