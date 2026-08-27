"""The loopback control plane for profiles and external runs.

The module intentionally keeps the HTTP surface close to the frozen v0.1
contract.  It is a local process, not a provider SDK, queue, or workflow
engine.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import signal
import shutil
import ssl
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable

from aiohttp import web
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
)


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


class APIError(Exception):
    """An expected local API error with a stable contract code."""

    def __init__(self, code: str, message: str, *, status: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


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
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self._sse_clients = 0
        self._last_activity = time.monotonic()
        self._shutdown = asyncio.Event()
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
        existing = read_json(daemon_file)
        if existing is None and daemon_file.exists():
            raise RuntimeError("cannot replace an invalid daemon.json")
        if isinstance(existing, dict):
            existing_pid = existing.get("pid")
            if not isinstance(existing_pid, int) or existing_pid <= 0:
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
        value = read_json(self.app_paths.daemon_file)
        if (
            isinstance(value, dict)
            and value.get("pid") == self.pid
            and value.get("capability") == self.capability
        ):
            self.app_paths.daemon_file.unlink(missing_ok=True)

    def _persist(self, record: RunRecord) -> None:
        record.updated_at = utc_now()
        self._evidence.setdefault(
            record.run_id, EvidenceStore(self.runs_root / record.run_id)
        ).write_run(record)

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

        return tuple(
            record.run_id
            for record in self.records.values()
            if record.status in {"starting", "running", "stopping"}
        )

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
            "native_workers": [dict(worker) for worker in NATIVE_WORKER_DECLARATIONS],
            "cost_attribution": "pending_or_unavailable",
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
            catalog = self.catalog_fetcher(model)
            if asyncio.iscoroutine(catalog):
                catalog = await catalog
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
            valid = self.key_validator(value.strip())
            if asyncio.iscoroutine(valid):
                valid = await valid
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
            self.key_saver(value.strip())
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
        await self.broadcast("run.created", run=self._record_payload(record))
        task = asyncio.create_task(
            self._execute_run(record, packet, api_key, worktree.path)
        )
        self._tasks[run_id] = task
        return record

    async def _execute_run(
        self, record: RunRecord, packet: TaskPacket, api_key: str, worktree: Path
    ) -> None:
        evidence = self._evidence[record.run_id]
        record.status = "starting"
        self._persist(record)
        evidence.event("run.starting")
        await self.broadcast("run.updated", run=self._record_payload(record))

        async def sample(rss: int) -> None:
            sample_value = {"at": utc_now(), "rss_bytes": rss}
            record.rss_samples.append(sample_value)
            self._persist(record)
            evidence.event("rss.sample", **sample_value)
            await self.broadcast("run.rss", run_id=record.run_id, **sample_value)

        if not self.codex_path:
            record.status = "unavailable"
            record.error = "codex CLI is not installed; run was not attempted"
            self._persist(record)
            evidence.event("run.unavailable", reason=record.error)
            await self.broadcast("run.updated", run=self._record_payload(record))
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
            self._persist(record)
            evidence.event("run.unavailable", reason=record.error)
            await self.broadcast("run.updated", run=self._record_payload(record))
            return

        self._processes[record.run_id] = process
        record.status = "running"
        record.pid = process.pid
        try:
            record.process_group = os.getpgid(process.pid) if os.name != "nt" else None
        except ProcessLookupError:
            record.process_group = None
        self._persist(record)
        evidence.event(
            "run.started", pid=record.pid, process_group=record.process_group
        )
        await self.broadcast("run.updated", run=self._record_payload(record))
        returncode = await process.wait()
        record.exit_code = returncode
        if process.force_requested:
            record.status = "stopped_forced"
            record.stop_outcome = "killed"
        elif process.term_requested:
            record.status = "stopped"
            record.stop_outcome = "term_exited"
        else:
            record.status = "succeeded" if returncode == 0 else "failed"
        if returncode != 0 and process.failure_summary:
            record.error = process.failure_summary.replace(api_key, "[REDACTED]")
        output_path = self.runs_root / record.run_id / "last-message.md"
        if output_path.exists():
            evidence.write_last_message(output_path.read_text(encoding="utf-8"))
            record.artifacts["last_message"] = str(output_path)
        try:
            evidence.write_diff(diff_text(worktree))
            evidence.write_file_list(changed_files(worktree))
            record.artifacts["diff"] = str(
                self.runs_root / record.run_id / "diff.patch"
            )
            record.artifacts["files"] = str(
                self.runs_root / record.run_id / "files.json"
            )
        except WorktreeError as exc:
            record.error = record.error or str(exc)
        record.cost_state = "unavailable"
        self._persist(record)
        evidence.event("run.finished", exit_code=returncode, status=record.status)
        await self.broadcast("run.updated", run=self._record_payload(record))
        self._processes.pop(record.run_id, None)

    async def stop_run(self, run_id: str, *, force: bool) -> RunRecord:
        record = self.records.get(run_id)
        if record is None:
            raise APIError("run_not_found", f"run not found: {run_id}", status=404)
        process = self._processes.get(run_id)
        if process is None:
            raise APIError("run_not_stoppable", "run has no live external process")
        evidence = self._evidence[run_id]
        record.status = "stopping"
        self._persist(record)
        evidence.event("stop.requested", force=force)
        try:
            outcome = await process.stop(force=force)
        except ProcessControlError as exc:
            raise APIError("run_not_stoppable", str(exc), status=409) from exc
        record.stop_outcome = outcome.state
        if outcome.state == "term_exited":
            record.status = "stopped"
        elif outcome.state == "killed":
            record.status = "stopped_forced"
        self._persist(record)
        evidence.event("stop.observed", outcome=asdict(outcome))
        await self.broadcast("run.updated", run=self._record_payload(record))
        return record

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
    models = request.app[STATE_KEY].catalog_fetcher(query)
    if asyncio.iscoroutine(models):
        models = await models
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
    record = await state.create_run(await _json_body(request))
    return web.json_response(state._record_payload(record), status=201)


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
    app.router.add_post("/api/runs/{run_id}/stop", _route(_stop))
    app.router.add_post("/api/shutdown", _route(_shutdown))
    resolved_static_dir = app[STATIC_DIR_KEY]
    if resolved_static_dir.is_dir():
        # Keep the frontend a replaceable static bundle. API routes are
        # registered first; this catch-all only serves existing asset files.
        app.router.add_get("/{path:.*}", _route(_static))
    return app


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
    state.write_daemon_file()
    loop = asyncio.get_running_loop()
    if hasattr(loop, "add_signal_handler") and os.name != "nt":
        for signum in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(signum, state._shutdown.set)
            except (NotImplementedError, RuntimeError):
                pass
    idle_task = None if persistent else asyncio.create_task(state.idle_loop())
    try:
        await state._shutdown.wait()
    finally:
        if idle_task is not None:
            idle_task.cancel()
            await asyncio.gather(idle_task, return_exceptions=True)
        for process in tuple(state._processes.values()):
            if process.is_running():
                try:
                    outcome = await process.stop(force=False, grace_seconds=1.0)
                    if outcome.state == "awaiting_force" and process.is_running():
                        await process.stop(force=True, grace_seconds=1.0)
                except ProcessControlError:
                    pass
        for task in state._tasks.values():
            if not task.done():
                task.cancel()
        state.clear_daemon_file()
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
