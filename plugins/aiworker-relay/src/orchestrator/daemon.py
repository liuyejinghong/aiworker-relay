"""The loopback control plane for profiles and external runs.

The module intentionally keeps the HTTP surface close to the frozen v0.1
contract.  It is a local process, not a provider SDK, queue, or workflow
engine.
"""

from __future__ import annotations

import argparse
import asyncio
import errno
import json
import math
import os
import secrets
import signal
import shutil
import ssl
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable

from aiohttp import web
import psutil
import truststore

from orchestrator import __version__
from orchestrator.config import (
    AppPaths,
    KeyringUnavailable,
    ProfileStore,
    atomic_write_json,
    get_openrouter_key,
    project_runtime_root,
    read_json,
    save_openrouter_key,
)
from orchestrator.models import Profile, RunRecord, TaskPacket, utc_now
from orchestrator.results import EvidenceStore
from orchestrator.runner import ManagedProcess, ProcessControlError, start_codex_run
from orchestrator.tasks import PacketValidationError, load_packet
from orchestrator.worktree import (
    WorktreeError,
    changed_files,
    create_worktree,
    diff_text,
    remove_worktree,
)


RSS_SAMPLE_LIMIT = 120
DELETABLE_RUN_STATUSES = frozenset(
    {"incomplete", "succeeded", "failed", "stopped", "stopped_forced", "unavailable"}
)


def _bound_rss(record: RunRecord) -> None:
    """Keep current and legacy RSS records within the accepted window."""

    samples = record.rss_samples
    values = [
        sample.get("rss_bytes")
        for sample in samples
        if isinstance(sample, dict)
        and isinstance(sample.get("rss_bytes"), int)
        and not isinstance(sample.get("rss_bytes"), bool)
    ]
    record.rss_sample_count = max(record.rss_sample_count, len(samples))
    if values:
        record.rss_last_bytes = values[-1]
        peak = max(values)
        record.rss_peak_bytes = (
            max(record.rss_peak_bytes, peak)
            if isinstance(record.rss_peak_bytes, int)
            else peak
        )
    if len(samples) > RSS_SAMPLE_LIMIT:
        del samples[:-RSS_SAMPLE_LIMIT]


OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
OPENROUTER_CURRENT_KEY_URL = "https://openrouter.ai/api/v1/key"
OPENROUTER_CREDITS_URL = "https://openrouter.ai/api/v1/credits"
OPENROUTER_BENCHMARKS_URL = "https://openrouter.ai/api/v1/benchmarks"
NATIVE_WORKER_DECLARATIONS = (
    {
        "id": "luna_medium_worker",
        "display_name": "Luna Medium",
        "badge": "LM",
        "kind": "native",
        "control": "codex",
    },
    {
        "id": "luna_worker",
        "display_name": "Luna Max",
        "badge": "LX",
        "kind": "native",
        "control": "codex",
    },
)
GATEWAY_REASONING_EFFORTS = frozenset(
    {"max", "xhigh", "high", "medium", "low", "minimal", "none"}
)
STATE_KEY = web.AppKey("state")
STATIC_DIR_KEY = web.AppKey("static_dir")
BROWSER_CAPABILITY_COOKIE = "aiworker_capability"
CLI_CAPABILITY_HEADER = "X-AIworker-Capability"
RECOVERY_GRACE_SECONDS = 10.0
RECOVERY_POLL_SECONDS = 0.05
SURVIVOR_OUTCOMES = frozenset(
    {
        "recovery_survivor_alive",
        "lifecycle_survivor_alive",
        "stop_survivor_alive",
        "shutdown_survivor_alive",
    }
)


class APIError(Exception):
    """An expected local API error with a stable contract code."""

    def __init__(self, code: str, message: str, *, status: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


async def _call_callback(callback: Callable[..., Any], *args: Any) -> Any:
    """Invoke an injected callback without running synchronous work on-loop."""

    abandoned = False
    result_holder: list[Any] = []
    result_lock = threading.Lock()

    def invoke() -> Any:
        result = callback(*args)
        with result_lock:
            should_discard = abandoned
            if not should_discard:
                result_holder.append(result)
        if should_discard:
            _discard_callback_result(result)
            return None
        return result

    callback_task = asyncio.create_task(asyncio.to_thread(invoke))
    try:
        result = await callback_task
    except asyncio.CancelledError:
        with result_lock:
            abandoned = True
            pending = result_holder.pop() if result_holder else None
        if pending is not None:
            _discard_callback_result(pending)
        raise
    with result_lock:
        result_holder.clear()
    if asyncio.iscoroutine(result):
        return await result
    return result


def _discard_callback_result(result: Any) -> None:
    if asyncio.iscoroutine(result):
        result.close()


def _positive_pid(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _process_start_value(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        value = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return value if math.isfinite(value) and value > 0 else None


def _exact_survivor(record: RunRecord) -> tuple[psutil.Process | None, str | None]:
    """Return a still-live process only when its persisted identity matches.

    A restarted daemon never reattaches to a process.  This helper is only a
    last-resort ownership check before startup recovery signals an exact
    survivor; any missing or changed identity is treated as ownership lost.
    """

    if not _positive_pid(record.pid):
        return None, "invalid_pid"
    started_at = _process_start_value(record.process_started_at)
    if started_at is None:
        return None, "missing_process_started_at"
    if os.name != "nt" and not _positive_pid(record.process_group):
        return None, "missing_process_group"

    try:
        process = psutil.Process(record.pid)
        observed_start = process.create_time()
    except (psutil.NoSuchProcess, ProcessLookupError, ValueError, OverflowError):
        return None, "process_not_found"
    except (psutil.AccessDenied, psutil.ZombieProcess, OSError):
        return None, "process_identity_unavailable"

    try:
        same_start = math.isclose(
            float(observed_start), started_at, rel_tol=0.0, abs_tol=1e-6
        )
    except (TypeError, ValueError, OverflowError):
        return None, "process_identity_unavailable"
    if not same_start:
        return None, "pid_reused"
    try:
        running = process.is_running()
        if running and hasattr(psutil, "STATUS_ZOMBIE"):
            running = process.status() != psutil.STATUS_ZOMBIE
    except (
        psutil.NoSuchProcess,
        ProcessLookupError,
    ):
        return None, "process_not_found"
    except (
        psutil.AccessDenied,
        psutil.ZombieProcess,
        OSError,
        ValueError,
    ):
        return None, "process_identity_unavailable"
    if not running:
        return None, "process_not_running"

    if os.name != "nt":
        try:
            observed_group = os.getpgid(record.pid)
        except ProcessLookupError:
            return None, "process_not_found"
        except OSError:
            return None, "process_identity_unavailable"
        if observed_group != record.process_group:
            return None, "process_group_mismatch"
    return process, None


def _recovery_error(reason: str | None) -> str:
    details = {
        "invalid_pid": "persisted run has no valid process identity",
        "missing_process_started_at": "persisted run has no process start time",
        "missing_process_group": "persisted run has no process-group identity",
        "process_not_found": "the persisted process no longer exists",
        "process_not_running": "the persisted process has already exited",
        "pid_reused": "the persisted PID now belongs to another process",
        "process_group_mismatch": "the persisted process group no longer matches",
        "process_identity_unavailable": "the persisted process identity could not be read",
    }
    return details.get(reason or "", "the persisted process identity could not be verified")


def _process_group_state(record: RunRecord) -> tuple[bool, str | None]:
    """Check the original POSIX group after the root process disappears."""

    if os.name == "nt":
        return False, None
    if not _positive_pid(record.process_group):
        return False, "missing_process_group"
    try:
        os.killpg(record.process_group, 0)
    except ProcessLookupError:
        return False, "process_not_found"
    except PermissionError:
        # A just-killed POSIX group may remain as an unreaped zombie and report
        # EPERM for signal 0.  It still exists, so recovery must keep waiting.
        return True, None
    except OSError as exc:
        if exc.errno == errno.ESRCH:
            return False, "process_not_found"
        return True, "process_identity_unavailable"
    return True, None


@contextmanager
def _daemon_record_lock(daemon_file: Path) -> Iterator[None]:
    """Serialize daemon record claims and cleanup without a persistent owner."""

    lock_file = daemon_file.with_name(f".{daemon_file.name}.lock")
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(lock_file, os.O_RDWR | os.O_CREAT, 0o600)
    locked = False
    try:
        os.chmod(lock_file, 0o600)
        try:
            if os.name == "nt":
                import msvcrt

                if os.fstat(descriptor).st_size == 0:
                    os.write(descriptor, b"\0")
                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError("daemon.json is being updated by another process") from exc
        locked = True
        yield
    finally:
        if locked:
            if os.name == "nt":
                import msvcrt

                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _listener_port(request: web.Request, state: DaemonState) -> int | None:
    """Return the actual local listener port, not the untrusted Host value."""

    transport = request.transport
    if transport is not None:
        sockname = transport.get_extra_info("sockname")
        if isinstance(sockname, (tuple, list)) and len(sockname) >= 2:
            port = sockname[1]
            if isinstance(port, int) and 1 <= port <= 65535:
                return port
    return state.port if isinstance(state.port, int) and state.port > 0 else None


def _expected_origin(request: web.Request, state: DaemonState) -> str | None:
    port = _listener_port(request, state)
    return f"http://127.0.0.1:{port}" if port is not None else None


def _validate_host_and_fetch_metadata(
    request: web.Request, state: DaemonState
) -> str | None:
    """Reject aliases and cross-site browser requests before any API work."""

    port = _listener_port(request, state)
    expected_host = f"127.0.0.1:{port}" if port is not None else None
    if expected_host is None or request.headers.get("Host") != expected_host:
        raise APIError(
            "invalid_host", "the local control address is not valid", status=403
        )
    if state.port is not None and port != state.port:
        raise APIError(
            "invalid_host", "the local control address is not valid", status=403
        )

    site = request.headers.get("Sec-Fetch-Site", "").strip().lower()
    if site not in {"", "same-origin", "none"}:
        raise APIError(
            "invalid_fetch_metadata",
            "cross-site requests are not accepted",
            status=403,
        )
    origin = request.headers.get("Origin")
    expected_origin = _expected_origin(request, state)
    if origin is not None and origin != expected_origin:
        raise APIError(
            "invalid_origin", "the request origin is not accepted", status=403
        )
    return expected_origin


def _authenticate(request: web.Request, state: DaemonState) -> str:
    """Authenticate exactly one local client mode.

    Browsers receive the capability only as an HttpOnly cookie. CLI and
    launcher callers read the owner-only daemon record and send the same value
    in a header. The modes are deliberately mutually exclusive.
    """

    header_value = request.headers.get(CLI_CAPABILITY_HEADER)
    cookie_value = request.cookies.get(BROWSER_CAPABILITY_COOKIE)
    if header_value is not None and cookie_value is not None:
        raise APIError(
            "unauthorized", "local capability authentication is invalid", status=401
        )
    provided = header_value if header_value is not None else cookie_value
    if not provided or provided != state.capability:
        raise APIError(
            "unauthorized", "local capability authentication is required", status=401
        )
    return "cli" if header_value is not None else "browser"


def _guard_request(request: web.Request, state: DaemonState) -> str | None:
    """Apply the narrow loopback and browser metadata contract."""

    expected_origin = _validate_host_and_fetch_metadata(request, state)
    if not request.path.startswith("/api/"):
        return None

    mode = _authenticate(request, state)
    fetch_site = request.headers.get("Sec-Fetch-Site", "").strip().lower()
    fetch_mode = request.headers.get("Sec-Fetch-Mode", "").strip().lower()
    fetch_dest = request.headers.get("Sec-Fetch-Dest", "").strip().lower()
    if mode == "browser" and not fetch_site:
        raise APIError(
            "invalid_fetch_metadata",
            "browser API requests must include Fetch Metadata",
            status=403,
        )
    if mode == "browser" and not fetch_mode:
        raise APIError(
            "invalid_fetch_metadata",
            "browser API requests must include Fetch Metadata",
            status=403,
        )
    if fetch_mode and fetch_mode not in {"cors", "same-origin"}:
        raise APIError(
            "invalid_fetch_metadata",
            "API requests must use a same-origin fetch",
            status=403,
        )
    if mode == "browser" and not fetch_dest:
        raise APIError(
            "invalid_fetch_metadata",
            "browser API requests must identify an empty destination",
            status=403,
        )
    if fetch_dest and fetch_dest != "empty":
        raise APIError(
            "invalid_fetch_metadata",
            "API requests cannot be used as a subresource",
            status=403,
        )
    if request.method not in {"GET", "HEAD"} and mode == "browser":
        if request.headers.get("Origin") != expected_origin:
            raise APIError(
                "invalid_origin", "write requests require the local origin", status=403
            )
    return mode


def _set_browser_capability_cookie(response: web.StreamResponse, state: DaemonState) -> None:
    """Issue the browser capability without making it script-readable."""

    response.set_cookie(
        BROWSER_CAPABILITY_COOKIE,
        state.capability,
        path="/",
        httponly=True,
        samesite="Strict",
    )


def _is_top_level_document(request: web.Request) -> bool:
    """Identify a normal first-page navigation eligible for the cookie."""

    if request.method != "GET":
        return False
    mode = request.headers.get("Sec-Fetch-Mode", "").strip().lower()
    dest = request.headers.get("Sec-Fetch-Dest", "").strip().lower()
    return mode in {"", "navigate"} and dest in {"", "document"}


async def _json_body(request: web.Request) -> dict[str, Any]:
    media_type = request.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
    if media_type != "application/json":
        raise APIError(
            "invalid_content_type",
            "request body must use application/json",
            status=415,
        )
    try:
        value = await request.json()
    except Exception as exc:
        raise APIError("invalid_json", "request body must be JSON") from exc
    if not isinstance(value, dict):
        raise APIError("invalid_json", "request body must be a JSON object")
    return value


def _json_error(error: APIError) -> web.Response:
    return web.json_response(
        {"code": error.code, "message": error.message},
        status=error.status,
    )


def _http_json(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    timeout: float = 8.0,
) -> dict[str, Any]:
    request = urllib.request.Request(url, headers=headers or {}, method="GET")
    try:
        with urllib.request.urlopen(
            request,
            timeout=timeout,
            context=truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT),
        ) as response:
            value = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            raise APIError("invalid_key", "OpenRouter rejected the API key") from exc
        if exc.code == 403:
            raise APIError(
                "forbidden", "OpenRouter does not allow this operation", status=403
            ) from exc
        if exc.code == 429:
            raise APIError(
                "rate_limited",
                "OpenRouter rate limited this request; try again later",
                status=429,
            ) from exc
        raise APIError(
            "provider_unavailable", "OpenRouter could not be reached", status=502
        ) from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise APIError(
            "provider_unavailable", "OpenRouter could not be reached", status=502
        ) from exc
    if not isinstance(value, dict):
        raise APIError(
            "provider_invalid_response",
            "OpenRouter returned an invalid catalog response",
            status=502,
        )
    return value


def validate_openrouter_key(value: str) -> bool:
    """Validate a key with OpenRouter's authenticated current-key endpoint."""

    try:
        _http_json(
            OPENROUTER_CURRENT_KEY_URL,
            headers={"Authorization": f"Bearer {value}"},
        )
        return True
    except APIError as exc:
        if exc.status == 502:
            raise
        return False


def fetch_openrouter_models(query: str = "") -> list[dict[str, Any]]:
    """Fetch and locally filter the current public OpenRouter model catalog."""

    body = _http_json(OPENROUTER_MODELS_URL)
    models = body.get("data", [])
    if not isinstance(models, list):
        raise APIError(
            "provider_invalid_response",
            "OpenRouter catalog has no model list",
            status=502,
        )
    query = query.strip().lower()
    if not query:
        return [value for value in models if isinstance(value, dict)]
    return [
        value
        for value in models
        if isinstance(value, dict)
        and (
            query in str(value.get("id", "")).lower()
            or query in str(value.get("name", "")).lower()
        )
    ]


def _provider_number(value: Any, *, field: str) -> float:
    """Read one numeric provider field without accepting booleans."""

    if isinstance(value, bool):
        raise APIError(
            "provider_invalid_response",
            f"OpenRouter returned an invalid {field}",
            status=502,
        )
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise APIError(
            "provider_invalid_response",
            f"OpenRouter returned an invalid {field}",
            status=502,
        ) from exc


def fetch_openrouter_account_summary(key: str) -> dict[str, Any]:
    """Read account credits, or the authenticated key's own limit when needed.

    Account credits are management-key-only in OpenRouter. A regular key can
    still expose its own configured limit through the current-key endpoint, so
    the returned status makes those two facts deliberately distinct.
    """

    headers = {"Authorization": f"Bearer {key}"}
    try:
        body = _http_json(OPENROUTER_CREDITS_URL, headers=headers)
    except APIError as exc:
        if exc.code != "forbidden":
            raise
        key_body = _http_json(OPENROUTER_CURRENT_KEY_URL, headers=headers)
        key_data = key_body.get("data")
        if not isinstance(key_data, dict):
            raise APIError(
                "provider_invalid_response",
                "OpenRouter returned invalid API key details",
                status=502,
            )
        if (
            "limit" not in key_data
            or "limit_remaining" not in key_data
            or key_data["limit"] is None
            or key_data["limit_remaining"] is None
        ):
            return {"status": "management_key_required"}
        return {
            "status": "key_limit",
            "limit": _provider_number(key_data["limit"], field="key limit"),
            "limit_remaining": _provider_number(
                key_data["limit_remaining"], field="remaining key limit"
            ),
            "usage": _provider_number(key_data.get("usage", 0), field="key usage"),
            "limit_reset": key_data.get("limit_reset"),
            "refreshed_at": utc_now(),
        }

    data = body.get("data")
    if not isinstance(data, dict):
        raise APIError(
            "provider_invalid_response",
            "OpenRouter returned invalid credit details",
            status=502,
        )
    total_credits = _provider_number(data.get("total_credits"), field="total credits")
    total_usage = _provider_number(data.get("total_usage"), field="total usage")
    return {
        "status": "account_balance",
        "total_credits": total_credits,
        "total_usage": total_usage,
        "remaining_credits": total_credits - total_usage,
        "refreshed_at": utc_now(),
    }


def fetch_openrouter_benchmarks(key: str, model: str) -> dict[str, Any]:
    """Return only benchmark records for one exact OpenRouter model slug."""

    body = _http_json(
        OPENROUTER_BENCHMARKS_URL,
        headers={"Authorization": f"Bearer {key}"},
    )
    data = body.get("data")
    if not isinstance(data, list):
        raise APIError(
            "provider_invalid_response",
            "OpenRouter returned an invalid benchmark list",
            status=502,
        )
    meta = body.get("meta")
    return {
        "model": model,
        "entries": [
            entry
            for entry in data
            if isinstance(entry, dict) and entry.get("model_permaslug") == model
        ],
        "meta": meta if isinstance(meta, dict) else {},
        "refreshed_at": utc_now(),
    }


class DaemonState:
    """Own all local state and active process handles for one daemon."""

    def __init__(
        self,
        *,
        data_dir: Path | None = None,
        project_root: Path | None = None,
        persistent: bool = False,
        catalog_fetcher: Callable[[str], list[dict[str, Any]]] | None = None,
        key_getter: Callable[[], str | None] = get_openrouter_key,
        key_saver: Callable[[str], None] = save_openrouter_key,
        key_validator: Callable[[str], bool] = validate_openrouter_key,
        codex_path: str | None = None,
    ):
        self.app_paths = AppPaths.for_user(data_dir)
        self.app_paths.root.mkdir(parents=True, exist_ok=True)
        self.project_root = (project_root or Path.cwd()).resolve()
        self.runtime_root = project_runtime_root(self.project_root)
        self.runtime_root.mkdir(parents=True, exist_ok=True)
        self.runs_root = self.runtime_root / "runs"
        self.runs_root.mkdir(parents=True, exist_ok=True)
        self.profiles = ProfileStore(self.app_paths.profiles_file)
        self.catalog_fetcher = catalog_fetcher or fetch_openrouter_models
        self.key_getter = key_getter
        self.key_saver = key_saver
        self.key_validator = key_validator
        self.codex_path = codex_path or shutil.which("codex")
        self.records: dict[str, RunRecord] = {}
        self._evidence: dict[str, EvidenceStore] = {}
        self._processes: dict[str, ManagedProcess] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._delete_lock = asyncio.Lock()
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self._sse_clients = 0
        self._last_activity = time.monotonic()
        self._shutdown = asyncio.Event()
        self._lifecycle_changed = asyncio.Event()
        self.pid = os.getpid()
        self.port: int | None = None
        self.persistent = persistent
        self.capability = secrets.token_urlsafe(32)
        self._load_records()

    def _load_records(self) -> None:
        for run_file in self.runs_root.glob("*/run.json"):
            value = read_json(run_file)
            if not isinstance(value, dict) or "run_id" not in value:
                continue
            try:
                record = RunRecord.from_dict(value)
            except (KeyError, TypeError, ValueError):
                continue
            _bound_rss(record)
            self.records[record.run_id] = record
            self._evidence[record.run_id] = EvidenceStore(run_file.parent)

    @property
    def base_url(self) -> str:
        if self.port is None:
            raise RuntimeError("daemon is not listening")
        return f"http://127.0.0.1:{self.port}"

    def touch(self) -> None:
        self._last_activity = time.monotonic()

    def _daemon_record(self) -> dict[str, Any]:
        return {
            "pid": self.pid,
            "port": self.port,
            "project_root": str(self.project_root),
            "runtime_root": str(self.runtime_root),
            "started_at": utc_now(),
            "version": __version__,
            "persistent": self.persistent,
            "capability": self.capability,
        }

    def write_daemon_file(self) -> None:
        daemon_file = self.app_paths.daemon_file
        with _daemon_record_lock(daemon_file):
            existing = read_json(daemon_file)
            if existing is None and daemon_file.exists():
                raise RuntimeError("cannot replace an invalid daemon.json")
            if isinstance(existing, dict):
                existing_pid = existing.get("pid")
                if (
                    not isinstance(existing_pid, int)
                    or isinstance(existing_pid, bool)
                    or existing_pid <= 0
                ):
                    raise RuntimeError("cannot replace an invalid daemon.json")
                try:
                    os.kill(existing_pid, 0)
                except ProcessLookupError:
                    pass
                except PermissionError as exc:
                    raise RuntimeError(
                        "cannot replace daemon.json while its recorded PID is inaccessible"
                    ) from exc
                except OSError:
                    pass
                else:
                    raise RuntimeError(
                        "cannot replace daemon.json while its recorded daemon is active"
                    )
            elif existing is not None:
                raise RuntimeError("cannot replace an invalid daemon.json")
            atomic_write_json(daemon_file, self._daemon_record())
            os.chmod(daemon_file, 0o600)

    def clear_daemon_file(self) -> None:
        daemon_file = self.app_paths.daemon_file
        with _daemon_record_lock(daemon_file):
            value = read_json(daemon_file)
            if (
                isinstance(value, dict)
                and value.get("pid") == self.pid
                and value.get("capability") == self.capability
            ):
                daemon_file.unlink(missing_ok=True)

    def _persist(self, record: RunRecord) -> None:
        record.updated_at = utc_now()
        self._evidence.setdefault(
            record.run_id, EvidenceStore(self.runs_root / record.run_id)
        ).write_run(record)

    @staticmethod
    def _append_error(record: RunRecord, detail: str) -> None:
        detail = detail.strip() or "lifecycle operation failed"
        if not record.error:
            record.error = detail
        elif detail not in record.error:
            record.error = f"{record.error}; {detail}"

    def _mark_incomplete(self, record: RunRecord, reason: str) -> None:
        """Leave a non-running, user-visible record when evidence is partial."""

        record.status = "incomplete"
        record.cost_state = "unavailable"
        self._append_error(record, reason)

    def _try_persist(self, record: RunRecord) -> str | None:
        try:
            self._persist(record)
        except Exception as exc:
            return str(exc) or exc.__class__.__name__
        return None

    async def _try_event(self, evidence: EvidenceStore, name: str, **data: Any) -> str | None:
        try:
            evidence.event(name, **data)
        except Exception as exc:
            return str(exc) or exc.__class__.__name__
        return None

    async def _try_broadcast(self, event: str, **data: Any) -> str | None:
        try:
            await self.broadcast(event, **data)
        except Exception as exc:
            return str(exc) or exc.__class__.__name__
        return None

    async def _record_incomplete(
        self,
        record: RunRecord,
        *,
        outcome: str,
        reason: str,
        event_name: str = "run.incomplete",
    ) -> None:
        """Persist a terminal incomplete outcome and best-effort notification."""

        self._mark_incomplete(record, reason)
        record.stop_outcome = outcome
        evidence = self._evidence[record.run_id]
        persist_error = self._try_persist(record)
        if persist_error:
            self._append_error(record, f"could not persist incomplete record: {persist_error}")
        event_error = await self._try_event(
            evidence,
            event_name,
            status=record.status,
            outcome=outcome,
            reason=reason,
        )
        if event_error:
            self._append_error(record, f"could not write incomplete event: {event_error}")
        broadcast_error = await self._try_broadcast(
            "run.updated", run=self._record_payload(record)
        )
        if broadcast_error:
            self._append_error(
                record, f"could not broadcast incomplete state: {broadcast_error}"
            )

    async def _record_recovery(
        self,
        record: RunRecord,
        *,
        outcome: str,
        reason: str,
    ) -> None:
        """Persist one startup recovery result without reattaching a process."""

        await self._record_incomplete(
            record,
            outcome=outcome,
            reason=reason,
            event_name="run.recovered",
        )

    async def _request_stop_pending(
        self, record: RunRecord, *, reason: str
    ) -> None:
        """Persist a TERM request while the process handle is still starting."""

        if record.status not in {"created", "starting", "running", "stopping"}:
            return
        already_pending = (
            record.status == "stopping" and record.stop_outcome == "stop_pending"
        )
        if not already_pending:
            record.status = "stopping"
            record.stop_outcome = "stop_pending"
        evidence = self._evidence[record.run_id]
        persist_error = self._try_persist(record)
        if persist_error:
            self._mark_incomplete(
                record,
                f"could not persist pending stop request: {persist_error}",
            )
        if not already_pending:
            event_error = await self._try_event(
                evidence,
                "stop.requested",
                force=False,
                pending=True,
                reason=reason,
            )
            if event_error:
                self._mark_incomplete(
                    record, f"could not write pending stop event: {event_error}"
                )
            broadcast_error = await self._try_broadcast(
                "run.updated", run=self._record_payload(record)
            )
            # SSE is only a live hint; a failed notification must not erase
            # the pending stop fact or turn it into a second request.
            if broadcast_error:
                self._append_error(
                    record, f"could not broadcast pending stop: {broadcast_error}"
                )

    async def _publish_stop_request(
        self, record: RunRecord, evidence: EvidenceStore, *, force: bool
    ) -> bool:
        """Make a live stop request durable before signaling its process."""

        if record.status in {"created", "starting", "running", "stopping"}:
            record.status = "stopping"
        persist_error = self._try_persist(record)
        if persist_error:
            self._mark_incomplete(
                record,
                f"could not persist stop request before signaling: {persist_error}",
            )
        event_error = await self._try_event(
            evidence, "stop.requested", force=force, pending=False
        )
        if event_error:
            self._mark_incomplete(
                record, f"could not write stop request before signaling: {event_error}"
            )
        broadcast_error = await self._try_broadcast(
            "run.updated", run=self._record_payload(record)
        )
        if broadcast_error:
            self._append_error(
                record, f"could not broadcast stop request: {broadcast_error}"
            )
        return persist_error is None and event_error is None

    @staticmethod
    def _capture_windows_recovery_targets(
        root: psutil.Process,
    ) -> tuple[list[tuple[int, float]], str | None]:
        """Capture a verified root and its current exact descendants."""

        targets: list[tuple[int, float]] = []
        try:
            children = root.children(recursive=True)
        except psutil.NoSuchProcess:
            return [], "process_not_found"
        except (psutil.AccessDenied, psutil.ZombieProcess, OSError):
            return [], "process_identity_unavailable"
        for process in [root, *children]:
            pid = getattr(process, "pid", None)
            if not _positive_pid(pid):
                return [], "invalid_pid"
            try:
                started_at = _process_start_value(process.create_time())
            except psutil.NoSuchProcess:
                continue
            except (psutil.AccessDenied, psutil.ZombieProcess, OSError, ValueError):
                return [], "process_identity_unavailable"
            if started_at is None:
                return [], "process_identity_unavailable"
            targets.append((pid, started_at))
        return targets, None

    @staticmethod
    def _windows_recovery_tree_state(
        targets: list[tuple[int, float]],
    ) -> tuple[bool, str | None]:
        live = False
        for pid, expected_start in targets:
            try:
                process = psutil.Process(pid)
                observed_start = _process_start_value(process.create_time())
                if observed_start is None or not math.isclose(
                    observed_start, expected_start, rel_tol=0.0, abs_tol=1e-6
                ):
                    return True, "pid_reused"
                if process.is_running():
                    live = True
            except psutil.NoSuchProcess:
                continue
            except (psutil.AccessDenied, psutil.ZombieProcess, OSError, ValueError):
                return True, "process_identity_unavailable"
        return live, None

    @staticmethod
    def _send_recovery_signal(
        record: RunRecord,
        *,
        force: bool,
        windows_targets: list[tuple[int, float]] | None = None,
    ) -> str | None:
        """Signal an exact survivor, rechecking identity immediately first."""

        process, reason = _exact_survivor(record)
        if os.name != "nt":
            if process is None:
                # Once the verified root exits, the original process group is
                # still the owned target.  This catches a surviving child and
                # allows the forced group signal without touching a new PID.
                if reason not in {"process_not_found", "process_not_running"}:
                    return reason
                group_alive, group_reason = _process_group_state(record)
                if not group_alive:
                    return group_reason or reason
                if group_reason is not None:
                    return group_reason
            signum = getattr(signal, "SIGKILL" if force else "SIGTERM", 9 if force else 15)
            try:
                os.killpg(record.process_group, signum)  # type: ignore[arg-type]
            except ProcessLookupError:
                return "process_not_found"
            except OSError:
                return "process_identity_unavailable"
            return None

        action_name = "kill" if force else "terminate"
        if windows_targets is not None:
            targets: list[psutil.Process] = []
            for pid, expected_start in windows_targets:
                try:
                    target = psutil.Process(pid)
                    observed_start = _process_start_value(target.create_time())
                    if observed_start is None or not math.isclose(
                        observed_start, expected_start, rel_tol=0.0, abs_tol=1e-6
                    ):
                        return "pid_reused"
                    if target.is_running():
                        targets.append(target)
                except psutil.NoSuchProcess:
                    continue
                except (psutil.AccessDenied, psutil.ZombieProcess, OSError, ValueError):
                    return "process_identity_unavailable"
            for target in reversed(targets):
                try:
                    getattr(target, action_name)()
                except psutil.NoSuchProcess:
                    return "process_not_found"
                except (psutil.AccessDenied, psutil.ZombieProcess, OSError):
                    return "process_identity_unavailable"
            return None
        if process is None:
            return reason
        try:
            children = process.children(recursive=True)
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess, OSError):
            return "process_identity_unavailable"
        for child in reversed(children):
            try:
                getattr(child, action_name)()
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess, OSError):
                pass
        try:
            getattr(process, action_name)()
        except psutil.NoSuchProcess:
            return "process_not_found"
        except (psutil.AccessDenied, psutil.ZombieProcess, OSError):
            return "process_identity_unavailable"
        return None

    @staticmethod
    async def _wait_for_recovery_exit(
        record: RunRecord,
        timeout: float,
        *,
        windows_targets: list[tuple[int, float]] | None = None,
    ) -> str | None:
        deadline = time.monotonic() + max(timeout, 0.0)
        while True:
            if windows_targets is not None and os.name == "nt":
                running, tree_reason = DaemonState._windows_recovery_tree_state(
                    windows_targets
                )
                if tree_reason is not None:
                    return tree_reason
                reason = None if running else "process_not_running"
            else:
                _, reason = _exact_survivor(record)
            if reason is not None:
                if os.name != "nt" and reason in {
                    "process_not_found",
                    "process_not_running",
                }:
                    group_alive, group_reason = _process_group_state(record)
                    if group_alive:
                        reason = None
                    elif group_reason is not None:
                        return group_reason
                    else:
                        return reason
                else:
                    return reason
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return "still_alive"
            await asyncio.sleep(min(RECOVERY_POLL_SECONDS, remaining))

    async def reconcile_records(self, *, grace_seconds: float = RECOVERY_GRACE_SECONDS) -> None:
        """Reconcile nonterminal disk records without ever reattaching them."""

        for record in tuple(self.records.values()):
            if record.status == "created":
                await self._record_recovery(
                    record,
                    outcome="recovery_ownership_lost",
                    reason=(
                        "run was only created when the daemon restarted; "
                        "no external process or task identity was persisted"
                    ),
                )
                continue
            if (
                record.status == "incomplete"
                and record.stop_outcome in SURVIVOR_OUTCOMES
            ):
                survivor, reason = _exact_survivor(record)
                if survivor is not None:
                    raise RuntimeError(
                        "cannot start while previously unrecovered run survivor remains alive: "
                        f"{record.run_id}"
                    )
                if os.name != "nt" and reason in {
                    "process_not_found",
                    "process_not_running",
                }:
                    group_alive, group_reason = _process_group_state(record)
                    if group_alive and group_reason is None:
                        raise RuntimeError(
                            "cannot start while previously unrecovered run process group "
                            f"remains alive: {record.run_id}"
                        )
                    reason = group_reason or reason
                await self._record_recovery(
                    record,
                    outcome="recovery_ownership_lost",
                    reason=(
                        "previous survivor-alive outcome could not be confirmed on restart; "
                        f"{_recovery_error(reason)}, no signal was sent"
                    ),
                )
            if record.status not in {"starting", "running", "stopping"}:
                continue

            process, reason = _exact_survivor(record)
            if process is None:
                group_alive = False
                group_reason: str | None = None
                if os.name != "nt" and reason in {
                    "process_not_found",
                    "process_not_running",
                }:
                    group_alive, group_reason = _process_group_state(record)
                if not group_alive or group_reason is not None:
                    await self._record_recovery(
                        record,
                        outcome="recovery_ownership_lost",
                        reason=(
                            "run was nonterminal when the daemon restarted; "
                            f"{_recovery_error(group_reason or reason)}, no signal was sent"
                        ),
                    )
                    continue

            windows_targets: list[tuple[int, float]] | None = None
            if os.name == "nt":
                assert process is not None
                windows_targets, tree_error = self._capture_windows_recovery_targets(
                    process
                )
                if tree_error:
                    await self._record_recovery(
                        record,
                        outcome="recovery_ownership_lost",
                        reason=(
                            "run survivor tree could not be captured before TERM; "
                            f"{_recovery_error(tree_error)}, no signal was sent"
                        ),
                    )
                    continue

            term_error = self._send_recovery_signal(
                record, force=False, windows_targets=windows_targets
            )
            if term_error:
                await self._record_recovery(
                    record,
                    outcome="recovery_ownership_lost",
                    reason=(
                        "run survivor identity changed before TERM; "
                        f"{_recovery_error(term_error)}, no signal was sent"
                    ),
                )
                continue

            exit_reason = await self._wait_for_recovery_exit(
                record, grace_seconds, windows_targets=windows_targets
            )
            if exit_reason is None or exit_reason in {"process_not_found", "process_not_running"}:
                await self._record_recovery(
                    record,
                    outcome="recovery_term_exited",
                    reason="daemon restart sent TERM to the exact survivor and confirmed exit",
                )
                continue
            if exit_reason != "still_alive":
                await self._record_recovery(
                    record,
                    outcome="recovery_ownership_lost",
                    reason=(
                        "run survivor identity changed after TERM; "
                        f"{_recovery_error(exit_reason)}, no KILL was sent"
                    ),
                )
                continue

            kill_error = self._send_recovery_signal(
                record, force=True, windows_targets=windows_targets
            )
            if kill_error:
                await self._record_recovery(
                    record,
                    outcome="recovery_ownership_lost",
                    reason=(
                        "run survivor remained after TERM but its identity could not be "
                        f"re-verified for KILL; {_recovery_error(kill_error)}"
                    ),
                )
                continue

            kill_reason = await self._wait_for_recovery_exit(
                record,
                min(max(grace_seconds, 0.0), 5.0),
                windows_targets=windows_targets,
            )
            if kill_reason == "still_alive":
                await self._record_recovery(
                    record,
                    outcome="recovery_survivor_alive",
                    reason="exact run survivor remained alive after TERM and KILL; daemon startup is blocked",
                )
                raise RuntimeError(
                    f"cannot start while exact run survivor remains alive: {record.run_id}"
                )
            if kill_reason not in {None, "process_not_found", "process_not_running"}:
                await self._record_recovery(
                    record,
                    outcome="recovery_ownership_lost",
                    reason=(
                        "run survivor identity changed after KILL; "
                        f"{_recovery_error(kill_reason)}"
                    ),
                )
                continue
            await self._record_recovery(
                record,
                outcome="recovery_killed",
                reason="daemon restart sent TERM then KILL to the exact survivor and confirmed exit",
            )

    async def broadcast(self, event: str, **data: Any) -> None:
        payload = {"event": event, "at": utc_now(), **data}
        for queue in tuple(self._subscribers):
            try:
                queue.put_nowait(payload)
            except asyncio.QueueFull:
                pass

    def _profile_payload(self, profile: Profile) -> dict[str, Any]:
        return profile.to_dict()

    def _record_payload(self, record: RunRecord) -> dict[str, Any]:
        return record.to_dict()

    def active_run_ids(self) -> tuple[str, ...]:
        """Return only external runs the daemon can currently protect."""

        active: list[str] = []
        for record in self.records.values():
            if record.status in {"starting", "running", "stopping"}:
                active.append(record.run_id)
                continue
            task = self._tasks.get(record.run_id)
            if task is not None and not task.done():
                active.append(record.run_id)
                continue
            process = self._processes.get(record.run_id)
            if process is None:
                continue
            try:
                if process.is_running():
                    active.append(record.run_id)
            except Exception:
                # An uninspectable owned handle remains a shutdown/update
                # blocker until the daemon can establish that it exited.
                active.append(record.run_id)
        return tuple(active)

    def _deletion_paths(self, record: RunRecord) -> tuple[Path, Path]:
        """Validate the exact on-disk identity of one run before deletion."""

        run_id = record.run_id
        if (
            not isinstance(run_id, str)
            or not run_id
            or run_id in {".", ".."}
            or Path(run_id).name != run_id
        ):
            raise APIError(
                "run_delete_refused",
                "run metadata contains an unsafe run id",
                status=409,
            )
        if record.project_root != str(self.project_root):
            raise APIError(
                "run_delete_refused",
                "run metadata belongs to a different project",
                status=409,
            )

        runs_root = self.runs_root
        worktrees_root = self.runtime_root / "worktrees"
        run_dir = runs_root / run_id
        worktree = worktrees_root / run_id
        expected_worktree = str(worktree)
        if record.worktree != expected_worktree:
            raise APIError(
                "run_delete_refused",
                "run metadata has an unexpected worktree path",
                status=409,
            )

        evidence = self._evidence.get(run_id)
        if evidence is None or evidence.run_dir != run_dir:
            raise APIError(
                "run_delete_refused",
                "run evidence metadata is unavailable or inconsistent",
                status=409,
            )

        if any(
            path.is_symlink()
            for path in (self.runtime_root, runs_root, worktrees_root, run_dir, worktree)
        ):
            raise APIError(
                "run_delete_refused",
                "run data contains a symbolic link at a protected boundary",
                status=409,
            )
        if not run_dir.is_dir() or not (run_dir / "run.json").is_file():
            raise APIError(
                "run_delete_refused",
                "run evidence metadata is missing",
                status=409,
            )

        try:
            stored = read_json(run_dir / "run.json")
            stored_record = (
                RunRecord.from_dict(stored) if isinstance(stored, dict) else None
            )
        except (OSError, TypeError, ValueError, KeyError) as exc:
            raise APIError(
                "run_delete_refused",
                "run evidence metadata is corrupt",
                status=409,
            ) from exc
        if stored_record is None or any(
            (
                stored_record.run_id != record.run_id,
                stored_record.project_root != record.project_root,
                stored_record.worktree != record.worktree,
                stored_record.status != record.status,
            )
        ):
            raise APIError(
                "run_delete_refused",
                "run evidence metadata does not match the loaded record",
                status=409,
            )
        return run_dir, worktree

    def _assert_deletable(self, record: RunRecord) -> None:
        """Reject active work before any filesystem or Git operation."""

        if record.status not in DELETABLE_RUN_STATUSES:
            raise APIError(
                "run_not_deletable",
                "only terminal runs can be deleted",
                status=409,
            )
        task = self._tasks.get(record.run_id)
        if task is not None and not task.done():
            raise APIError(
                "run_not_deletable",
                "active run task must finish before deletion",
                status=409,
            )
        process = self._processes.get(record.run_id)
        if process is None:
            return
        try:
            running = process.is_running()
        except Exception as exc:
            raise APIError(
                "run_not_deletable",
                "run process state could not be verified",
                status=409,
            ) from exc
        if running:
            raise APIError(
                "run_not_deletable",
                "active run process must exit before deletion",
                status=409,
            )

    async def delete_run(self, run_id: str) -> dict[str, Any]:
        """Delete one terminal run after Git-aware worktree cleanup."""

        async with self._delete_lock:
            record = self.records.get(run_id)
            if record is None:
                raise APIError("run_not_found", f"run not found: {run_id}", status=404)
            self._assert_deletable(record)
            run_dir, worktree = self._deletion_paths(record)

            try:
                await asyncio.to_thread(remove_worktree, self.project_root, worktree)
            except WorktreeError as exc:
                raise APIError(
                    "run_delete_refused",
                    f"Git worktree removal was refused: {exc}",
                    status=409,
                ) from exc
            except OSError as exc:
                raise APIError(
                    "run_delete_failed",
                    f"Git worktree removal failed: {exc}",
                    status=500,
                ) from exc

            try:
                await asyncio.to_thread(shutil.rmtree, run_dir)
            except OSError as exc:
                raise APIError(
                    "run_delete_failed",
                    f"run evidence removal failed: {exc}",
                    status=500,
                ) from exc

            self.records.pop(run_id, None)
            self._evidence.pop(run_id, None)
            self._tasks.pop(run_id, None)
            self._processes.pop(run_id, None)
            self._lifecycle_changed.set()
            await self._try_broadcast("run.deleted", run_id=run_id)
            return {"deleted": [run_id], "failed": []}

    async def delete_runs(self) -> dict[str, Any]:
        """Attempt each loaded run independently for a batch delete."""

        result: dict[str, Any] = {"deleted": [], "failed": []}
        for run_id in tuple(self.records):
            try:
                await self.delete_run(run_id)
            except APIError as exc:
                result["failed"].append(
                    {"run_id": run_id, "code": exc.code, "message": exc.message}
                )
            else:
                result["deleted"].append(run_id)
        return result

    def overview(self) -> dict[str, Any]:
        records = sorted(
            self.records.values(), key=lambda value: value.updated_at, reverse=True
        )
        return {
            "version": __version__,
            "profiles": [
                self._profile_payload(profile) for profile in self.profiles.all()
            ],
            "runs": [self._record_payload(record) for record in records[:100]],
            "active_run_ids": list(self.active_run_ids()),
            "native_workers": [dict(worker) for worker in NATIVE_WORKER_DECLARATIONS],
            "cost_attribution": "pending_or_unavailable",
            "data_policy": {
                "retention": "until_explicit_deletion",
                "runtime_root": str(self.runtime_root),
                "runs_root": str(self.runs_root),
                "worktrees_root": str(self.runtime_root / "worktrees"),
                "uninstall": "preserves_project_data",
                "text_redaction": "exact_openrouter_key_only",
                "raw_worktrees_sanitized": False,
            },
        }

    def _key(self) -> str | None:
        try:
            return self.key_getter()
        except KeyringUnavailable as exc:
            raise APIError("keyring_unavailable", str(exc), status=503) from exc

    def account_summary(self) -> dict[str, Any]:
        """Read the current account or API-key limit only on a UI request."""

        key = self._key()
        if not key:
            return {"status": "missing_key"}
        return fetch_openrouter_account_summary(key)

    def profile_benchmarks(self, profile_id: str) -> dict[str, Any]:
        """Read public benchmark records for one explicit Profile request."""

        profile = self.profiles.get(profile_id)
        if profile is None:
            raise APIError("profile_not_found", "profile not found", status=404)
        key = self._key()
        if not key:
            raise APIError("missing_key", "OpenRouter API key is not configured")
        return fetch_openrouter_benchmarks(key, profile.model) | {"profile_id": profile.id}

    async def create_profile(self, payload: dict[str, Any]) -> Profile:
        model = str(payload.get("model", "")).strip()
        if not model:
            raise APIError("invalid_profile", "model is required")
        try:
            catalog = await _call_callback(self.catalog_fetcher, model)
        except APIError:
            raise
        except Exception as exc:
            raise APIError(
                "provider_unavailable",
                "OpenRouter catalog could not be checked",
                status=502,
            ) from exc
        matches = [item for item in catalog if str(item.get("id", "")) == model]
        if not matches:
            raise APIError(
                "model_not_found", f"model not found in OpenRouter catalog: {model}"
            )
        catalog_model = matches[0]
        default_reasoning = str(payload.get("default_reasoning", "auto"))
        reasoning = catalog_model.get("reasoning")
        if default_reasoning != "auto":
            if not isinstance(reasoning, dict):
                raise APIError(
                    "unsupported_reasoning",
                    "OpenRouter does not expose a configurable reasoning effort for this model; use auto",
                )
            supported_efforts = reasoning.get("supported_efforts")
            if "supported_efforts" not in reasoning:
                allowed_efforts = set()
            elif isinstance(supported_efforts, list):
                allowed_efforts = {str(value) for value in supported_efforts}
            elif supported_efforts is None:
                allowed_efforts = GATEWAY_REASONING_EFFORTS
            else:
                allowed_efforts = set()
            if default_reasoning not in allowed_efforts:
                raise APIError(
                    "unsupported_reasoning",
                    f"reasoning effort is not supported by this model: {default_reasoning}",
                )
            if bool(reasoning.get("mandatory")) and default_reasoning == "none":
                raise APIError(
                    "unsupported_reasoning",
                    "this model requires reasoning and cannot use none",
                )
        profile_id = str(payload.get("id") or model.replace("/", "-").replace(":", "-"))
        state = str(payload.get("state", "enabled"))
        verification = str(payload.get("verification", "unverified"))
        if state not in {"enabled", "frozen"}:
            raise APIError("invalid_profile", "state must be enabled or frozen")
        if verification not in {"unverified", "verified"}:
            raise APIError(
                "invalid_profile", "verification must be unverified or verified"
            )
        profile = Profile(
            id=profile_id,
            model=model,
            display_name=payload.get("display_name") or model,
            state=state,
            verification=verification,
            default_reasoning=default_reasoning,
            metadata=dict(payload.get("metadata", {}))
            | {"catalog": catalog_model, "catalog_fetched_at": utc_now()},
        )
        self.profiles.put(profile)
        await self.broadcast("profile.updated", profile=self._profile_payload(profile))
        return profile

    async def save_key(self, value: str) -> None:
        if not value.strip():
            raise APIError("invalid_key", "OpenRouter API key must not be empty")
        try:
            valid = await _call_callback(self.key_validator, value.strip())
        except APIError:
            raise
        except Exception as exc:
            raise APIError(
                "provider_unavailable",
                "OpenRouter key could not be checked",
                status=502,
            ) from exc
        if not valid:
            raise APIError("invalid_key", "OpenRouter rejected the API key", status=400)
        try:
            await _call_callback(self.key_saver, value.strip())
        except KeyringUnavailable as exc:
            raise APIError("keyring_unavailable", str(exc), status=503) from exc

    async def create_run(self, payload: dict[str, Any]) -> RunRecord:
        profile_id = str(payload.get("profile_id", "")).strip()
        profile = self.profiles.get(profile_id)
        if profile is None:
            raise APIError(
                "profile_not_found", f"profile not found: {profile_id}", status=404
            )
        selection_source = str(payload.get("selection_source", "user"))
        if not bool(payload.get("consent", False)):
            raise APIError(
                "consent_required", "external dispatch requires explicit consent"
            )
        blocker = profile.dispatch_error(
            selection_source=selection_source,
            experimental_confirmation=bool(
                payload.get("experimental_confirmation", False)
            ),
        )
        if blocker == "frozen_profile":
            raise APIError(blocker, "profile is frozen; dispatch was refused")
        if blocker:
            raise APIError(
                blocker,
                "unverified profile requires explicit experimental confirmation",
            )
        if "reasoning_effort" in payload:
            raise APIError(
                "reasoning_override_not_supported",
                "per-run reasoning override is not supported; use the profile default",
            )
        api_key = self._key()
        if not api_key:
            raise APIError("missing_key", "OpenRouter API key is not configured")
        packet_value = str(payload.get("packet_path", "")).strip()
        if not packet_value:
            raise APIError("invalid_packet", "packet_path is required")
        packet_path = Path(packet_value).expanduser()
        run_id = uuid.uuid4().hex
        reasoning_effort = profile.default_reasoning
        reasoning_source = (
            "profile_auto" if reasoning_effort == "auto" else "profile_default"
        )
        try:
            packet = load_packet(
                packet_path,
                run_id=run_id,
                profile_id=profile.id,
                profile_model=profile.model,
                reasoning_effort=reasoning_effort,
                reasoning_source=reasoning_source,
                selection_source=selection_source,
            )
        except PacketValidationError as exc:
            raise APIError("invalid_packet", str(exc)) from exc
        try:
            worktree = create_worktree(self.project_root, run_id)
        except WorktreeError as exc:
            raise APIError("worktree_unavailable", str(exc)) from exc
        packet.workspace = {
            "project_root": str(self.project_root),
            "worktree": str(worktree.path),
            "source_head": worktree.source_head,
            "dirty_workspace_excluded": worktree.dirty_workspace_excluded,
        }
        run_dir = self.runs_root / run_id
        evidence = EvidenceStore(run_dir, secret=api_key)
        record = RunRecord(
            run_id=run_id,
            profile_id=profile.id,
            model=profile.model,
            reasoning_effort=reasoning_effort,
            reasoning_source=reasoning_source,
            status="created",
            created_at=utc_now(),
            updated_at=utc_now(),
            project_root=str(self.project_root),
            worktree=str(worktree.path),
            dirty_workspace_excluded=worktree.dirty_workspace_excluded,
            artifacts={
                "run": str(run_dir / "run.json"),
                "events": str(run_dir / "events.jsonl"),
                "packet": str(run_dir / "task-packet.md"),
            },
        )
        self.records[run_id] = record
        self._evidence[run_id] = evidence
        evidence.write_run(record)
        evidence.event("run.created", profile_id=profile.id, model=profile.model)
        evidence.write_task_packet(packet.prompt())
        await self._try_broadcast("run.created", run=self._record_payload(record))
        task = asyncio.create_task(
            self._execute_run(record, packet, api_key, worktree.path)
        )
        self._tasks[run_id] = task
        self._lifecycle_changed.set()
        return record

    async def _stop_after_lifecycle_error(
        self, record: RunRecord, process: ManagedProcess
    ) -> None:
        """Best-effort cleanup when startup/lifecycle evidence itself failed."""

        try:
            if process.is_running():
                outcome = await process.stop(force=False, grace_seconds=1.0)
                record.stop_outcome = outcome.state
                if outcome.state == "awaiting_force" and process.is_running():
                    outcome = await process.stop(force=True, grace_seconds=1.0)
                    record.stop_outcome = outcome.state
            if not process.is_running():
                record.exit_code = process.returncode
        except Exception as exc:
            self._append_error(record, f"could not stop process after lifecycle error: {exc}")

    async def shutdown_runs(self, *, grace_seconds: float = 1.0) -> bool:
        """Stop owned runs once, await finalization, and report survivors.

        A process that remains alive after TERM and KILL cannot be finalized
        safely during daemon shutdown.  Its handle stays owned and its run is
        persisted as incomplete, while unrelated finalization tasks are still
        awaited before the listener closes.
        """

        survivors: set[str] = set()
        pending: set[str] = set()
        for run_id, task in tuple(self._tasks.items()):
            if task.done() or run_id in self._processes:
                continue
            record = self.records.get(run_id)
            if record is None:
                continue
            pending.add(run_id)
            await self._request_stop_pending(
                record, reason="daemon shutdown arrived before process registration"
            )

        for run_id, process in tuple(self._processes.items()):
            try:
                running = process.is_running()
            except Exception:
                running = True
            if not running:
                continue

            stop_error: str | None = None
            record = self.records.get(run_id)
            request_ready = True
            if record is not None and not (
                record.status == "stopping" and record.stop_outcome == "stop_pending"
            ):
                request_ready = await self._publish_stop_request(
                    record, self._evidence[run_id], force=False
                )
                if not request_ready:
                    stop_error = "stop request could not be durably recorded"
            if request_ready:
                try:
                    outcome = await process.stop(
                        force=False, grace_seconds=max(grace_seconds, 0.0)
                    )
                    if outcome.state == "awaiting_force" and process.is_running():
                        outcome = await process.stop(
                            force=True,
                            grace_seconds=min(max(grace_seconds, 0.0), 5.0),
                        )
                except Exception as exc:
                    stop_error = str(exc) or exc.__class__.__name__

            try:
                running = process.is_running()
            except Exception as exc:
                running = True
                stop_error = stop_error or str(exc) or exc.__class__.__name__
            if not running:
                continue

            survivors.add(run_id)
            record = self.records.get(run_id)
            if record is not None:
                details = (
                    f"; stop error: {stop_error}" if stop_error else ""
                )
                await self._record_incomplete(
                    record,
                    outcome="shutdown_survivor_alive",
                    reason=(
                        "daemon shutdown could not confirm external process exit "
                        f"after TERM and KILL{details}"
                    ),
                )
            # Do not let the serve loop wait forever on a task whose process
            # has not exited.  The live process handle remains the blocker.
        tasks = []
        for run_id, task in tuple(self._tasks.items()):
            if run_id in survivors or run_id in pending or task.done():
                continue
            tasks.append(task)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for run_id, process in tuple(self._processes.items()):
            try:
                running = process.is_running()
            except Exception:
                running = True
            task = self._tasks.get(run_id)
            if not running and (task is None or task.done()):
                self._processes.pop(run_id, None)
        return bool(survivors or pending)

    async def _execute_run(
        self, record: RunRecord, packet: TaskPacket, api_key: str, worktree: Path
    ) -> None:
        evidence = self._evidence[record.run_id]
        process: ManagedProcess | None = None
        sample_errors: list[str] = []
        try:
            stop_pending = record.status == "stopping" or record.stop_outcome == "stop_pending"
            if not stop_pending:
                record.status = "starting"
            initial_error = self._try_persist(record)
            if initial_error:
                raise RuntimeError(f"could not persist run start: {initial_error}")
            initial_error = await self._try_event(evidence, "run.starting")
            if initial_error:
                raise RuntimeError(f"could not write run start event: {initial_error}")
            await self._try_broadcast(
                "run.updated", run=self._record_payload(record)
            )

            async def sample(rss: int) -> None:
                sample_value = {"at": utc_now(), "rss_bytes": rss}
                record.rss_samples.append(sample_value)
                record.rss_sample_count += 1
                _bound_rss(record)
                await self._try_broadcast(
                    "run.rss", run_id=record.run_id, **sample_value
                )

            if not self.codex_path:
                record.status = "unavailable"
                record.error = "codex CLI is not installed; run was not attempted"
                errors = [self._try_persist(record)]
                errors.append(
                    await self._try_event(evidence, "run.unavailable", reason=record.error)
                )
                await self._try_broadcast(
                    "run.updated", run=self._record_payload(record)
                )
                for error in errors:
                    if error:
                        self._append_error(record, error)
                if any(errors):
                    self._mark_incomplete(record, "run unavailable evidence was incomplete")
                    self._try_persist(record)
                return

            try:
                process = await start_codex_run(
                    worktree=worktree,
                    run_dir=self.runs_root / record.run_id,
                    prompt=packet.prompt(),
                    model=record.model,
                    reasoning_effort=record.reasoning_effort,
                    api_key=api_key,
                    code_home=self.runs_root / record.run_id / "CODEX_HOME",
                    executable=self.codex_path,
                    rss_callback=sample,
                )
            except ProcessControlError as exc:
                record.status = "unavailable"
                record.error = str(exc)
                errors = [self._try_persist(record)]
                errors.append(
                    await self._try_event(evidence, "run.unavailable", reason=record.error)
                )
                await self._try_broadcast(
                    "run.updated", run=self._record_payload(record)
                )
                for error in errors:
                    if error:
                        self._append_error(record, error)
                if any(errors):
                    self._mark_incomplete(record, "run unavailable evidence was incomplete")
                    self._try_persist(record)
                return

            self._processes[record.run_id] = process
            self._lifecycle_changed.set()
            stop_pending = (
                stop_pending
                or record.status == "stopping"
                or record.stop_outcome == "stop_pending"
            )
            if not stop_pending:
                record.status = "running"
            record.pid = process.pid
            identity_errors: list[str] = []
            if not _positive_pid(record.pid):
                identity_errors.append("external process returned an invalid PID")
            if os.name != "nt":
                if _positive_pid(record.pid):
                    record.process_group = getattr(process, "process_group", None)
                    if not _positive_pid(record.process_group):
                        try:
                            record.process_group = os.getpgid(record.pid)
                        except (ProcessLookupError, OSError, ValueError):
                            record.process_group = None
                else:
                    record.process_group = None
                if not _positive_pid(record.process_group):
                    identity_errors.append("POSIX process-group identity is unavailable")
            else:
                record.process_group = None
            if _positive_pid(record.pid):
                try:
                    started_at = psutil.Process(record.pid).create_time()
                    record.process_started_at = _process_start_value(started_at)
                except (psutil.Error, TypeError, ValueError, OSError, OverflowError):
                    record.process_started_at = None
            else:
                record.process_started_at = None
            if record.process_started_at is None:
                identity_errors.append("process start-time identity is unavailable")
            process_identity_error = getattr(process, "identity_error", None)
            if process_identity_error:
                identity_errors.append(str(process_identity_error))
            started_error = self._try_persist(record)
            if started_error:
                raise RuntimeError(f"could not persist running process: {started_error}")
            if identity_errors:
                raise RuntimeError(
                    "cannot keep an external process without recoverable identity: "
                    + "; ".join(identity_errors)
                )
            started_error = await self._try_event(
                evidence,
                "run.started",
                pid=record.pid,
                process_group=record.process_group,
                process_started_at=record.process_started_at,
            )
            if started_error:
                raise RuntimeError(f"could not write run started event: {started_error}")
            await self._try_broadcast(
                "run.updated", run=self._record_payload(record)
            )
            if stop_pending:
                try:
                    outcome = await process.stop(force=False)
                except ProcessControlError as exc:
                    raise RuntimeError(f"could not issue pending TERM: {exc}") from exc
                record.stop_outcome = outcome.state
                stop_error = self._try_persist(record)
                if stop_error:
                    raise RuntimeError(f"could not persist pending stop outcome: {stop_error}")
                stop_error = await self._try_event(
                    evidence, "stop.observed", outcome=asdict(outcome)
                )
                if stop_error:
                    raise RuntimeError(f"could not write pending stop event: {stop_error}")
                await self._try_broadcast(
                    "run.updated", run=self._record_payload(record)
                )

            returncode = await process.wait()
            # Shutdown/lifecycle cleanup may have marked the record incomplete
            # while wait() was pending.  Read that fact only after wait returns
            # so finalization cannot promote it back to a clean terminal state.
            incomplete_after_wait = record.status == "incomplete"
            record.exit_code = returncode
            if process.force_requested:
                record.stop_outcome = "killed"
                desired_status = "stopped_forced"
            elif process.term_requested:
                record.stop_outcome = "term_exited"
                desired_status = "stopped"
            else:
                desired_status = "succeeded" if returncode == 0 else "failed"
            if returncode != 0:
                try:
                    summary = process.failure_summary
                except Exception as exc:
                    summary = None
                    sample_errors.append(f"could not read process failure summary: {exc}")
                if summary:
                    record.error = summary.replace(api_key, "[REDACTED]")
            # The process is no longer owned by a running handle, but the
            # record stays incomplete until the mandatory artifacts settle.
            record.status = "incomplete"
            record.cost_state = "unavailable"
            checkpoint_error = self._try_persist(record)
            if checkpoint_error:
                sample_errors.append(
                    f"could not persist post-wait checkpoint: {checkpoint_error}"
                )

            artifact_errors: list[str] = []
            output_path = self.runs_root / record.run_id / "last-message.md"
            if output_path.exists():
                try:
                    text = output_path.read_text(encoding="utf-8")
                    written = evidence.write_last_message(text)
                    record.artifacts["last_message"] = str(written)
                except Exception as exc:
                    artifact_errors.append(f"could not read or persist last message: {exc}")
            elif desired_status == "succeeded":
                artifact_errors.append("successful run has no last-message.md evidence")

            try:
                written_diff = evidence.write_diff(diff_text(worktree))
                record.artifacts["diff"] = str(written_diff)
            except Exception as exc:
                artifact_errors.append(f"could not persist diff.patch: {exc}")
            try:
                written_files = evidence.write_file_list(changed_files(worktree))
                record.artifacts["files"] = str(written_files)
            except Exception as exc:
                artifact_errors.append(f"could not persist files.json: {exc}")

            all_errors = sample_errors + artifact_errors
            if all_errors:
                self._mark_incomplete(record, "; ".join(all_errors))
            candidate_status = (
                "incomplete"
                if all_errors or incomplete_after_wait
                else desired_status
            )
            # The durable checkpoint remains incomplete while the append-only
            # terminal event is written.  The event records the candidate
            # result, not a result that has not yet been committed to run.json.
            record.status = "incomplete"
            record.cost_state = "unavailable"

            event_error = await self._try_event(
                evidence,
                "run.finished",
                exit_code=returncode,
                status=record.status,
                candidate_status=candidate_status,
                evidence_complete=candidate_status != "incomplete",
            )
            if event_error:
                self._mark_incomplete(
                    record, f"could not write terminal run event: {event_error}"
                )
            elif candidate_status != "incomplete":
                record.status = candidate_status
            terminal_error = self._try_persist(record)
            if terminal_error:
                # The durable post-wait checkpoint remains the source of
                # truth.  Do not issue a compensating persist after failure.
                self._mark_incomplete(
                    record, f"could not persist terminal run record: {terminal_error}"
                )
            await self._try_broadcast(
                "run.updated", run=self._record_payload(record)
            )
        except Exception as exc:
            survivor = False
            if process is not None:
                try:
                    if process.is_running():
                        await self._stop_after_lifecycle_error(record, process)
                    survivor = process.is_running()
                except Exception as cleanup_error:
                    self._append_error(
                        record, f"could not inspect process after lifecycle error: {cleanup_error}"
                    )
                    survivor = True
            lifecycle_reason = f"run lifecycle failed: {exc}"
            if survivor:
                lifecycle_reason += "; external process remained alive after cleanup"
            await self._record_incomplete(
                record,
                outcome=(
                    "lifecycle_survivor_alive"
                    if survivor
                    else record.stop_outcome or "lifecycle_error"
                ),
                reason=lifecycle_reason,
            )
        finally:
            still_running = False
            if process is not None:
                try:
                    still_running = process.is_running()
                except Exception:
                    still_running = True
            if not still_running:
                self._processes.pop(record.run_id, None)
            self._tasks.pop(record.run_id, None)
            self._lifecycle_changed.set()

    async def stop_run(self, run_id: str, *, force: bool) -> RunRecord:
        record = self.records.get(run_id)
        if record is None:
            raise APIError("run_not_found", f"run not found: {run_id}", status=404)
        process = self._processes.get(run_id)
        task = self._tasks.get(run_id)
        if process is None:
            if task is not None and not task.done():
                if force:
                    raise APIError(
                        "run_not_stoppable",
                        "force stop requires a live external process after TERM",
                        status=409,
                    )
                await self._request_stop_pending(
                    record, reason="stop requested before process registration"
                )
                self._lifecycle_changed.set()
                return record
            if task is not None:
                self._tasks.pop(run_id, None)
            if force:
                raise APIError(
                    "run_not_stoppable",
                    "force stop requires a live external process after TERM",
                    status=409,
                )
            raise APIError("run_not_stoppable", "run has no live external process")

        try:
            process_running = process.is_running()
        except Exception as exc:
            self._append_error(record, f"could not inspect process before stop: {exc}")
            process_running = True
        if not process_running:
            if force:
                raise APIError(
                    "run_not_stoppable",
                    "force stop requires a live external process after TERM",
                    status=409,
                )
            if task is not None and not task.done():
                await task
            if record.status in {"starting", "running", "stopping"} and (
                task is None or task.done()
            ):
                await self._record_incomplete(
                    record,
                    outcome="already_exited",
                    reason="process exited before the stop request was observed",
                )
            self._processes.pop(run_id, None)
            if task is None or task.done():
                self._tasks.pop(run_id, None)
            self._lifecycle_changed.set()
            return self.records[run_id]

        evidence = self._evidence[run_id]
        if force and not bool(getattr(process, "term_requested", False)):
            raise APIError(
                "run_not_stoppable",
                "force stop requires a prior TERM request",
                status=409,
            )
        if not await self._publish_stop_request(record, evidence, force=force):
            raise APIError(
                "run_not_stoppable",
                "stop request could not be durably recorded",
                status=409,
            )
        try:
            outcome = await process.stop(force=force)
        except ProcessControlError as exc:
            raise APIError("run_not_stoppable", str(exc), status=409) from exc

        # The process can exit between the initial check and stop().  Do not
        # publish a transient ``stopping`` state for that already-exited run.
        if outcome.state == "already_exited":
            if task is not None and not task.done():
                await task
            if record.status in {"starting", "running", "stopping"} and (
                task is None or task.done()
            ):
                await self._record_incomplete(
                    record,
                    outcome="already_exited",
                    reason="process exited before the stop request was observed",
                )
            self._processes.pop(run_id, None)
            if task is None or task.done():
                self._tasks.pop(run_id, None)
            self._lifecycle_changed.set()
            return self.records[run_id]

        record.stop_outcome = outcome.state
        persist_error = self._try_persist(record)
        if persist_error:
            self._mark_incomplete(
                record, f"could not persist stop outcome: {persist_error}"
            )
        event_error = await self._try_event(
            evidence, "stop.observed", outcome=asdict(outcome)
        )
        if event_error:
            self._mark_incomplete(
                record, f"could not write stop outcome event: {event_error}"
            )
        broadcast_error = await self._try_broadcast(
            "run.updated", run=self._record_payload(record)
        )
        if broadcast_error:
            self._append_error(
                record, f"could not broadcast stop outcome: {broadcast_error}"
            )

        try:
            process_running = process.is_running()
        except Exception as exc:
            self._append_error(record, f"could not inspect process after stop: {exc}")
            process_running = True
        if process_running and outcome.state == "kill_timeout":
            await self._record_incomplete(
                record,
                outcome="stop_survivor_alive",
                reason="external process remained alive after the forced stop timeout",
            )
        elif not process_running and task is not None and not task.done():
            await task
        elif not process_running and (task is None or task.done()):
            if record.status in {"starting", "running", "stopping"}:
                await self._record_incomplete(
                    record,
                    outcome=outcome.state,
                    reason="stop completed without a run finalization task",
                )
            self._processes.pop(run_id, None)
        self._lifecycle_changed.set()
        return self.records[run_id]

    def request_idle_shutdown(self) -> dict[str, Any]:
        """Let the launcher replace an idle runtime without touching a run."""

        active_runs = self.active_run_ids()
        if active_runs:
            raise APIError(
                "active_runs",
                "daemon shutdown was refused because external runs are active",
                status=409,
            )
        self._shutdown.set()
        return {"status": "shutting_down", "version": __version__}

    async def idle_loop(self) -> None:
        if self.persistent:
            return
        while not self._shutdown.is_set():
            await asyncio.sleep(1)
            if self._sse_clients == 0 and not self.active_run_ids():
                if time.monotonic() - self._last_activity >= 60:
                    self._shutdown.set()


def _route(handler):
    async def wrapped(request: web.Request) -> web.StreamResponse:
        state: DaemonState = request.app[STATE_KEY]
        try:
            _guard_request(request, state)
            state.touch()
            return await handler(request)
        except APIError as exc:
            return _json_error(exc)

    return wrapped


async def _health(request: web.Request) -> web.Response:
    state: DaemonState = request.app[STATE_KEY]
    return web.json_response(
        {
            "ok": True,
            "version": __version__,
            "pid": state.pid,
            "port": state.port,
            "project_root": str(state.project_root),
            "runtime_root": str(state.runtime_root),
            "persistent": state.persistent,
        }
    )


async def _overview(request: web.Request) -> web.Response:
    return web.json_response(request.app[STATE_KEY].overview())


async def _events(request: web.Request) -> web.StreamResponse:
    state: DaemonState = request.app[STATE_KEY]
    response = web.StreamResponse(
        status=200,
        headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )
    await response.prepare(request)
    queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=100)
    state._subscribers.add(queue)
    state._sse_clients += 1
    try:
        initial = {"event": "overview", "at": utc_now(), "data": state.overview()}
        await response.write(
            f"event: overview\ndata: {json.dumps(initial['data'], ensure_ascii=False)}\n\n".encode()
        )
        while not state._shutdown.is_set():
            try:
                event = await asyncio.wait_for(queue.get(), timeout=15)
            except asyncio.TimeoutError:
                await response.write(b": keepalive\n\n")
                continue
            name = str(event.get("event", "update"))
            data = {key: value for key, value in event.items() if key != "event"}
            await response.write(
                f"event: {name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode()
            )
    except (asyncio.CancelledError, ConnectionResetError, RuntimeError):
        pass
    finally:
        state._subscribers.discard(queue)
        state._sse_clients = max(0, state._sse_clients - 1)
    return response


async def _get_key(request: web.Request) -> web.Response:
    state: DaemonState = request.app[STATE_KEY]
    try:
        configured = bool(state._key())
    except APIError as exc:
        return _json_error(exc)
    return web.json_response({"configured": configured})


async def _put_key(request: web.Request) -> web.Response:
    payload = await _json_body(request)
    value = payload.get("key")
    if not isinstance(value, str):
        raise APIError("invalid_key", "key must be a string")
    await request.app[STATE_KEY].save_key(value)
    return web.json_response({"configured": True})


async def _account_summary(request: web.Request) -> web.Response:
    state: DaemonState = request.app[STATE_KEY]
    return web.json_response(await asyncio.to_thread(state.account_summary))


async def _models(request: web.Request) -> web.Response:
    query = request.query.get("query", "")
    models = await _call_callback(request.app[STATE_KEY].catalog_fetcher, query)
    return web.json_response({"models": models})


async def _profiles(request: web.Request) -> web.Response:
    state: DaemonState = request.app[STATE_KEY]
    if request.method == "GET":
        return web.json_response(
            {"profiles": [state._profile_payload(p) for p in state.profiles.all()]}
        )
    profile = await state.create_profile(await _json_body(request))
    return web.json_response(state._profile_payload(profile), status=201)


async def _profile_state(request: web.Request) -> web.Response:
    state: DaemonState = request.app[STATE_KEY]
    payload = await _json_body(request)
    value = str(payload.get("state", ""))
    try:
        profile = state.profiles.update_state(request.match_info["profile_id"], value)
    except KeyError as exc:
        raise APIError("profile_not_found", "profile not found", status=404) from exc
    except ValueError as exc:
        raise APIError("invalid_profile_state", str(exc)) from exc
    await state.broadcast("profile.updated", profile=state._profile_payload(profile))
    return web.json_response(state._profile_payload(profile))


async def _profile_benchmarks(request: web.Request) -> web.Response:
    state: DaemonState = request.app[STATE_KEY]
    return web.json_response(
        await asyncio.to_thread(
            state.profile_benchmarks, request.match_info["profile_id"]
        )
    )


async def _runs(request: web.Request) -> web.Response:
    state: DaemonState = request.app[STATE_KEY]
    if request.method == "GET":
        return web.json_response({"runs": state.overview()["runs"]})
    if request.method == "DELETE":
        await _json_body(request)
        return web.json_response(await state.delete_runs())
    record = await state.create_run(await _json_body(request))
    return web.json_response(state._record_payload(record), status=201)


async def _delete_run(request: web.Request) -> web.Response:
    await _json_body(request)
    return web.json_response(
        await request.app[STATE_KEY].delete_run(request.match_info["run_id"])
    )


async def _stop(request: web.Request) -> web.Response:
    payload = await _json_body(request)
    record = await request.app[STATE_KEY].stop_run(
        request.match_info["run_id"],
        force=bool(payload.get("force", False)),
    )
    return web.json_response(request.app[STATE_KEY]._record_payload(record))


async def _shutdown(request: web.Request) -> web.Response:
    await _json_body(request)
    return web.json_response(request.app[STATE_KEY].request_idle_shutdown())


async def _index(request: web.Request) -> web.StreamResponse:
    static_dir = request.app[STATIC_DIR_KEY]
    index = static_dir / "index.html"
    if index.exists():
        response = web.FileResponse(index)
    else:
        response = web.Response(
            text="<!doctype html><title>AIworker Relay</title><p>AIworker Relay is running.</p>",
            content_type="text/html",
        )
    if _is_top_level_document(request):
        _set_browser_capability_cookie(response, request.app[STATE_KEY])
    return response


async def _static(request: web.Request) -> web.StreamResponse:
    """Serve the replaceable static bundle with a safe path boundary."""

    static_dir = request.app[STATIC_DIR_KEY].resolve()
    relative = request.match_info.get("path", "")
    candidate = (static_dir / relative).resolve()
    try:
        candidate.relative_to(static_dir)
    except ValueError as exc:
        raise web.HTTPNotFound() from exc
    if not candidate.is_file():
        raise web.HTTPNotFound()
    response = web.FileResponse(candidate)
    if candidate.suffix.lower() == ".html" and _is_top_level_document(request):
        _set_browser_capability_cookie(response, request.app[STATE_KEY])
    return response


def create_app(
    state: DaemonState, *, static_dir: Path | None = None
) -> web.Application:
    app = web.Application()
    app[STATE_KEY] = state
    app[STATIC_DIR_KEY] = static_dir or Path(__file__).parent / "web"
    app.router.add_get("/", _route(_index))
    app.router.add_get("/api/health", _route(_health))
    app.router.add_get("/api/overview", _route(_overview))
    app.router.add_get("/api/events", _route(_events))
    app.router.add_get("/api/openrouter-key", _route(_get_key))
    app.router.add_put("/api/openrouter-key", _route(_put_key))
    app.router.add_get("/api/openrouter-account", _route(_account_summary))
    app.router.add_get("/api/models", _route(_models))
    app.router.add_get("/api/profiles", _route(_profiles))
    app.router.add_post("/api/profiles", _route(_profiles))
    app.router.add_post("/api/profiles/{profile_id}/state", _route(_profile_state))
    app.router.add_get(
        "/api/profiles/{profile_id}/benchmarks", _route(_profile_benchmarks)
    )
    app.router.add_get("/api/runs", _route(_runs))
    app.router.add_post("/api/runs", _route(_runs))
    app.router.add_delete("/api/runs", _route(_runs))
    app.router.add_delete("/api/runs/{run_id}", _route(_delete_run))
    app.router.add_post("/api/runs/{run_id}/stop", _route(_stop))
    app.router.add_post("/api/shutdown", _route(_shutdown))
    resolved_static_dir = app[STATIC_DIR_KEY]
    if resolved_static_dir.is_dir():
        # Keep the frontend a replaceable static bundle. API routes are
        # registered first; this catch-all only serves existing asset files.
        app.router.add_get("/{path:.*}", _route(_static))
    return app


async def _serve_until_clean_shutdown(
    state: DaemonState, *, grace_seconds: float = 1.0
) -> None:
    """Keep the listener alive until a shutdown attempt has no survivors."""

    retry_on_change = False
    while True:
        if not retry_on_change:
            await state._shutdown.wait()
        retry_on_change = False
        state._lifecycle_changed.clear()
        if await state.shutdown_runs(grace_seconds=grace_seconds):
            # A live external process remains an active blocker.  Clear only
            # this attempt's trigger.  A process-registration/finalization
            # change wakes the loop for an automatic bounded retry; a later
            # signal remains sufficient for an unchanged survivor.
            state._shutdown.clear()
            if state._lifecycle_changed.is_set():
                continue
            shutdown_wait = asyncio.create_task(state._shutdown.wait())
            lifecycle_wait = asyncio.create_task(state._lifecycle_changed.wait())
            done, pending = await asyncio.wait(
                (shutdown_wait, lifecycle_wait),
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            if shutdown_wait in done and state._shutdown.is_set():
                state._shutdown.clear()
            retry_on_change = lifecycle_wait in done or shutdown_wait in done
            continue
        return


async def serve(
    *,
    data_dir: Path | None = None,
    project_root: Path | None = None,
    port: int = 0,
    persistent: bool = False,
    codex_path: str | None = None,
) -> int:
    state = DaemonState(
        data_dir=data_dir,
        project_root=project_root,
        persistent=persistent,
        codex_path=codex_path,
    )
    app = create_app(state)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", port)
    await site.start()
    sockets = site._server.sockets if site._server else []
    state.port = int(sockets[0].getsockname()[1]) if sockets else port
    loop = asyncio.get_running_loop()
    idle_task = None if persistent else asyncio.create_task(state.idle_loop())
    clean_shutdown = False
    shutdown_survivors = True
    try:
        await state.reconcile_records()
        state.write_daemon_file()
        if hasattr(loop, "add_signal_handler") and os.name != "nt":
            for signum in (signal.SIGTERM, signal.SIGINT):
                try:
                    loop.add_signal_handler(signum, state._shutdown.set)
                except (NotImplementedError, RuntimeError):
                    pass
        await _serve_until_clean_shutdown(state, grace_seconds=1.0)
        clean_shutdown = True
    finally:
        if idle_task is not None:
            idle_task.cancel()
            await asyncio.gather(idle_task, return_exceptions=True)
        # If setup or the shutdown loop itself failed, make one bounded stop
        # attempt before cleanup.  A survivor keeps daemon identity on disk so
        # the next startup cannot mistake this for a clean exit.
        if not clean_shutdown:
            try:
                shutdown_survivors = await state.shutdown_runs(grace_seconds=1.0)
            except BaseException:
                shutdown_survivors = True
                raise
        try:
            if clean_shutdown or not shutdown_survivors:
                state.clear_daemon_file()
        finally:
            await runner.cleanup()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="external-workersd")
    parser.add_argument("--serve", action="store_true", help="run the loopback daemon")
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--project-root", type=Path)
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--persistent", action="store_true")
    parser.add_argument("--codex-path")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.serve:
        build_parser().print_help()
        return 0
    return asyncio.run(
        serve(
            data_dir=args.data_dir,
            project_root=args.project_root,
            port=args.port,
            persistent=args.persistent,
            codex_path=args.codex_path,
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
