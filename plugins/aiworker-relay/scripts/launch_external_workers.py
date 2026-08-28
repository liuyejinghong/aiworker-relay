#!/usr/bin/env python3
"""Bootstrap the app-local AIworker Relay runtime from its Plugin source."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import plistlib
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import venv
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence


APP_NAME = "Codex External Workers"
SOURCE_ROOT = Path(__file__).resolve().parents[1]
PERSISTENT_CONTROL_PORT = 49178
LAUNCH_AGENT_LABEL = "com.aiworker.relay"
CAPABILITY_HEADER = "X-AIworker-Capability"
RUNTIME_IDENTITY_FILE = ".aiworker-release.json"
GENERATED_SOURCE_DIRECTORIES = {
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "build",
    "dist",
}


class RuntimeUpdateError(RuntimeError):
    """The app-local runtime cannot be safely reconciled."""


class PersistentEntryChangedError(RuntimeUpdateError):
    """The current setup changed the owned persistent entry before failing."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never carry a local capability to a redirect target."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


@dataclass(frozen=True, slots=True)
class DaemonSnapshot:
    """The minimum facts needed before setup can replace a runtime."""

    state: str
    version: str | None = None
    source_fingerprint: str | None = None
    pid: int | None = None
    endpoint: str | None = None
    active_runs: tuple[str, ...] = ()
    reason: str | None = None
    persistent: bool = False
    project_root: str | None = None
    capability: str | None = field(default=None, repr=False)


def app_data_root() -> Path:
    """Return the same app-data location used by the installed runtime."""

    home = Path.home()
    if sys.platform == "darwin":
        return home / "Library" / "Application Support" / APP_NAME
    if sys.platform.startswith("win"):
        return Path(os.environ.get("LOCALAPPDATA", home / "AppData" / "Local")) / APP_NAME
    return Path(os.environ.get("XDG_DATA_HOME", home / ".local" / "share")) / "codex-external-workers"


def venv_python(venv_root: Path) -> Path:
    return venv_root / ("Scripts/python.exe" if sys.platform.startswith("win") else "bin/python")


def venv_orch(venv_root: Path) -> Path:
    return venv_root / ("Scripts/orch.exe" if sys.platform.startswith("win") else "bin/orch")


def venv_daemon(venv_root: Path) -> Path:
    return venv_root / (
        "Scripts/external-workersd.exe"
        if sys.platform.startswith("win")
        else "bin/external-workersd"
    )


def current_codex_cli() -> Path | None:
    """Return the current shell-visible Codex CLI without altering PATH."""

    value = shutil.which("codex")
    if not value:
        return None
    path = Path(value)
    return path if path.is_file() else None


def current_node_cli() -> Path | None:
    """Return the current shell-visible Node runtime for a Codex CLI wrapper."""

    value = shutil.which("node")
    if not value:
        return None
    path = Path(value)
    return path if path.is_file() else None


def bundle_version() -> str:
    """Read the canonical release version carried by this Plugin bundle."""

    try:
        value = (SOURCE_ROOT / "src" / "orchestrator" / "VERSION").read_text(
            encoding="utf-8"
        ).strip()
    except OSError as exc:
        raise RuntimeUpdateError(f"could not read Plugin version: {exc}") from exc
    if not value:
        raise RuntimeUpdateError("Plugin version is empty")
    return value


def bundle_source_fingerprint() -> str:
    """Identify the exact installable Plugin bytes used by setup.

    The digest covers every distributed regular file in the Plugin source,
    including the manifest, Skill, launcher, runtime package, and static UI.
    Build metadata and bytecode caches are excluded because setup can create
    them after Codex has installed the bundle.
    """

    if SOURCE_ROOT.is_symlink() or not SOURCE_ROOT.is_dir():
        raise RuntimeUpdateError("Plugin source root is not a normal directory")
    digest = hashlib.sha256(b"AIworker Relay Plugin source v1\0")
    files: list[Path] = []
    for directory, dirnames, filenames in os.walk(SOURCE_ROOT, followlinks=False):
        current = Path(directory)
        retained_directories: list[str] = []
        for name in sorted(dirnames):
            path = current / name
            if name in GENERATED_SOURCE_DIRECTORIES or name.endswith(".egg-info"):
                continue
            if path.is_symlink():
                raise RuntimeUpdateError(
                    f"Plugin source contains a symbolic-link directory: {path}"
                )
            retained_directories.append(name)
        dirnames[:] = retained_directories
        for name in sorted(filenames):
            path = current / name
            if path.suffix in {".pyc", ".pyo"} or name == ".DS_Store":
                continue
            if path.is_symlink() or not path.is_file():
                raise RuntimeUpdateError(
                    f"Plugin source contains a non-regular file: {path}"
                )
            files.append(path)
    for path in sorted(
        files, key=lambda value: value.relative_to(SOURCE_ROOT).as_posix()
    ):
        relative = path.relative_to(SOURCE_ROOT).as_posix()
        stat = path.stat()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0x\0" if stat.st_mode & 0o111 else b"\0-\0")
        digest.update(str(stat.st_size).encode("ascii"))
        digest.update(b"\0")
        with path.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                digest.update(chunk)
        digest.update(b"\0")
    return f"sha256:{digest.hexdigest()}"


def _valid_source_fingerprint(value: object) -> bool:
    if not isinstance(value, str) or not value.startswith("sha256:"):
        return False
    digest = value.removeprefix("sha256:")
    return len(digest) == 64 and all(
        character in "0123456789abcdef" for character in digest
    )


def installed_runtime_fingerprint(runtime: Path, *, version: str | None) -> str | None:
    """Read the bundle identity accepted when this runtime was installed."""

    if version is None:
        return None
    try:
        value = json.loads(
            (runtime / RUNTIME_IDENTITY_FILE).read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError):
        return None
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != 1
        or value.get("version") != version
        or not _valid_source_fingerprint(value.get("source_fingerprint"))
    ):
        return None
    return str(value["source_fingerprint"])


def _write_runtime_identity(
    runtime: Path, *, version: str, source_fingerprint: str
) -> None:
    payload = {
        "schema_version": 1,
        "version": version,
        "source_fingerprint": source_fingerprint,
    }
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=runtime,
        prefix=f".{RUNTIME_IDENTITY_FILE}.",
        delete=False,
    ) as temporary:
        json.dump(payload, temporary, ensure_ascii=False, sort_keys=True)
        temporary.write("\n")
        temporary_path = Path(temporary.name)
    temporary_path.replace(runtime / RUNTIME_IDENTITY_FILE)


def installed_runtime_version(orch: Path) -> str | None:
    """Read the installed runtime's own version without importing it here."""

    if not orch.is_file():
        return None
    try:
        result = subprocess.run(
            [str(orch), "version"],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return None
    value = result.stdout.strip()
    if result.returncode or not value or "\n" in value:
        return None
    return value


def _process_state(pid: int) -> bool | None:
    """Return live, absent, or unknown for one recorded local PID."""

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return None
    except OSError:
        return False
    return True


def _local_request(
    endpoint: str,
    path: str,
    *,
    method: str = "GET",
    payload: dict[str, object] | None = None,
    capability: str | None = None,
    timeout: float = 0.8,
) -> tuple[int, dict[str, Any] | None] | None:
    """Call the loopback daemon without inheriting a desktop proxy setting."""

    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers: dict[str, str] = {}
    if data is not None:
        headers["Content-Type"] = "application/json"
    if capability:
        headers[CAPABILITY_HEADER] = capability
    request = urllib.request.Request(
        f"{endpoint}{path}",
        data=data,
        headers=headers,
        method=method,
    )
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}), _NoRedirect()
    )
    try:
        with opener.open(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
            value = json.loads(raw)
            return response.status, value if isinstance(value, dict) else None
    except urllib.error.HTTPError as exc:
        try:
            value = json.loads(exc.read().decode("utf-8"))
        except (OSError, json.JSONDecodeError):
            value = None
        return exc.code, value if isinstance(value, dict) else None
    except (OSError, urllib.error.URLError, json.JSONDecodeError):
        return None


def daemon_snapshot(root: Path) -> DaemonSnapshot:
    """Inspect one recorded daemon before a runtime-replacing setup action."""

    try:
        record = json.loads((root / "daemon.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        return DaemonSnapshot("missing")
    except (OSError, json.JSONDecodeError):
        return DaemonSnapshot("unknown", reason="daemon record cannot be read")
    if not isinstance(record, dict):
        return DaemonSnapshot("unknown", reason="daemon record is invalid")
    pid = record.get("pid")
    port = record.get("port")
    if (
        not isinstance(pid, int)
        or isinstance(pid, bool)
        or pid <= 0
        or not isinstance(port, int)
        or isinstance(port, bool)
        or not 1 <= port <= 65535
    ):
        return DaemonSnapshot("unknown", reason="daemon record has no valid PID or port")
    process_state = _process_state(pid)
    if process_state is False:
        return DaemonSnapshot("stale", pid=pid)
    if process_state is None:
        return DaemonSnapshot("unknown", pid=pid, reason="daemon PID cannot be inspected")
    capability = record.get("capability")
    if not isinstance(capability, str) or not capability:
        return DaemonSnapshot(
            "unknown",
            pid=pid,
            reason="daemon record has no capability",
        )
    project_root = record.get("project_root")
    runtime_root = record.get("runtime_root")
    version = record.get("version")
    source_fingerprint = record.get("source_fingerprint")
    persistent = record.get("persistent")
    if (
        not isinstance(project_root, str)
        or not project_root
        or not isinstance(runtime_root, str)
        or not isinstance(version, str)
        or (
            source_fingerprint is not None
            and not _valid_source_fingerprint(source_fingerprint)
        )
        or not isinstance(persistent, bool)
        or Path(runtime_root).resolve()
        != Path(project_root).resolve() / ".orch"
    ):
        return DaemonSnapshot(
            "unknown",
            pid=pid,
            reason="daemon record has an incomplete identity",
        )
    endpoint = f"http://127.0.0.1:{port}"
    health_response = _local_request(
        endpoint, "/api/health", capability=capability
    )
    if health_response is None:
        return DaemonSnapshot("unknown", pid=pid, endpoint=endpoint, reason="daemon health is unavailable")
    health_status, health = health_response
    if (
        health_status != 200
        or not isinstance(health, dict)
        or health.get("ok") is not True
        or health.get("pid") != pid
        or health.get("port") != port
        or health.get("project_root") != project_root
        or health.get("runtime_root") != runtime_root
        or health.get("version") != version
        or health.get("source_fingerprint") != source_fingerprint
        or health.get("persistent") != persistent
    ):
        return DaemonSnapshot("unknown", pid=pid, endpoint=endpoint, reason="daemon health does not match its record")
    overview_response = _local_request(
        endpoint, "/api/overview", capability=capability
    )
    if overview_response is None or overview_response[0] != 200:
        return DaemonSnapshot("unknown", pid=pid, endpoint=endpoint, reason="daemon overview is unavailable")
    overview = overview_response[1]
    if (
        not isinstance(overview, dict)
        or not isinstance(overview.get("runs"), list)
        or not isinstance(overview.get("active_run_ids"), list)
    ):
        return DaemonSnapshot("unknown", pid=pid, endpoint=endpoint, reason="daemon overview is invalid")
    active_runs: tuple[str, ...] = tuple(
        run_id
        for run_id in overview["active_run_ids"]
        if isinstance(run_id, str) and run_id
    )
    if len(active_runs) != len(overview["active_run_ids"]):
        return DaemonSnapshot("unknown", pid=pid, endpoint=endpoint, reason="daemon overview active_run_ids is invalid")
    return DaemonSnapshot(
        "active" if active_runs else "idle",
        version=str(health["version"]),
        source_fingerprint=(
            str(source_fingerprint) if source_fingerprint is not None else None
        ),
        pid=pid,
        endpoint=endpoint,
        active_runs=active_runs,
        persistent=(
            health["persistent"]
            if isinstance(health.get("persistent"), bool)
            else record.get("persistent") is True
        ),
        project_root=(
            project_root
        ),
        capability=capability,
    )


def _wait_for_exit(pid: int, *, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _process_state(pid) is False:
            return True
        time.sleep(0.05)
    return _process_state(pid) is False


def shutdown_idle_daemon(snapshot: DaemonSnapshot) -> None:
    """Stop a verified idle daemon, never an active or unknown process."""

    if snapshot.state != "idle" or snapshot.pid is None or snapshot.endpoint is None:
        raise RuntimeUpdateError("cannot stop a daemon whose idle state is not verified")
    response = _local_request(
        snapshot.endpoint,
        "/api/shutdown",
        method="POST",
        payload={},
        capability=snapshot.capability,
    )
    if response is None:
        latest = daemon_snapshot(app_data_root())
        if latest.state in {"missing", "stale"}:
            return
        raise RuntimeUpdateError("idle daemon stopped responding before it could be updated")
    status, _ = response
    if status == 409:
        raise RuntimeUpdateError("runtime update deferred because an external run is now active")
    elif status != 200:
        raise RuntimeUpdateError("idle daemon refused its controlled shutdown")
    if not _wait_for_exit(snapshot.pid):
        raise RuntimeUpdateError("idle daemon did not exit; runtime was not replaced")
    if snapshot.endpoint == f"http://127.0.0.1:{PERSISTENT_CONTROL_PORT}":
        _wait_for_fixed_port_release()


def _remove_runtime_tree(path: Path) -> None:
    if not path.exists():
        return
    if path.is_symlink() or not path.is_dir():
        raise RuntimeUpdateError(f"runtime path is not a normal directory: {path}")
    shutil.rmtree(path)


def _candidate_daemon_is_accepted(
    snapshot: DaemonSnapshot,
    *,
    expected_version: str,
    expected_source_fingerprint: str | None,
    project_root: Path,
) -> bool:
    """Return whether the current runtime has passed the setup identity checks."""

    return (
        snapshot.state in {"active", "idle"}
        and snapshot.version == expected_version
        and snapshot.source_fingerprint == expected_source_fingerprint
        and snapshot.endpoint == f"http://127.0.0.1:{PERSISTENT_CONTROL_PORT}"
        and snapshot.persistent
        and snapshot.project_root is not None
        and Path(snapshot.project_root).resolve() == project_root.resolve()
    )


def _active_run_hint(snapshot: DaemonSnapshot) -> str:
    return ", ".join(snapshot.active_runs) or "an external run"


def restore_interrupted_runtime(
    root: Path,
    *,
    project_root: Path | None = None,
    expected_version: str | None = None,
    expected_source_fingerprint: str | None = None,
) -> tuple[str | None, bool]:
    """Recover or defer the one transaction marked by ``venv.previous``.

    The marker is intentionally the only persisted transaction state.  A
    verified candidate daemon may finish its setup while active; cleanup is
    deferred until the next idle setup.  Any other live daemon state is left
    untouched unless it is verified idle and can be stopped through the
    existing capability-gated endpoint.
    """

    runtime = root / "venv"
    previous = root / "venv.previous"
    if not previous.exists():
        return None, False
    if previous.is_symlink() or not previous.is_dir():
        raise RuntimeUpdateError("runtime recovery is blocked: venv.previous is invalid")
    requested_project_root = (project_root or Path.cwd()).resolve()
    snapshot = daemon_snapshot(root)
    if snapshot.state in {"active", "idle"} and (
        snapshot.project_root is None
        or Path(snapshot.project_root).resolve() != requested_project_root
    ):
        raise RuntimeUpdateError(
            "runtime recovery is blocked because the persistent control plane "
            "belongs to another project"
        )
    if not runtime.exists():
        previous.replace(runtime)
        return None, False
    if runtime.is_symlink() or not runtime.is_dir():
        raise RuntimeUpdateError("runtime recovery is blocked: venv is invalid")

    expected = expected_version or bundle_version()
    expected_fingerprint = (
        expected_source_fingerprint or bundle_source_fingerprint()
    )
    candidate_version = installed_runtime_version(venv_orch(runtime))
    candidate_fingerprint = installed_runtime_fingerprint(
        runtime, version=candidate_version
    )
    candidate_accepted = (
        candidate_version == expected
        and candidate_fingerprint == expected_fingerprint
        and _candidate_daemon_is_accepted(
            snapshot,
            expected_version=expected,
            expected_source_fingerprint=expected_fingerprint,
            project_root=requested_project_root,
        )
    )
    if candidate_accepted:
        if snapshot.state == "active":
            return (
                "runtime update finalization deferred while "
                f"{_active_run_hint(snapshot)} remains active; rerun setup after it finishes"
            ), False
        _remove_runtime_tree(previous)
        return None, False

    if snapshot.state == "active":
        return (
            "runtime recovery deferred while "
            f"{_active_run_hint(snapshot)} remains active; rerun setup after it finishes"
        ), False
    if snapshot.state not in {"active", "idle", "missing", "stale"}:
        raise RuntimeUpdateError(
            "runtime recovery is blocked because daemon state is unknown: "
            f"{snapshot.reason}"
        )
    stopped_verified_daemon = False
    if snapshot.state == "idle":
        shutdown_idle_daemon(snapshot)
        stopped_verified_daemon = True

    _remove_runtime_tree(runtime)
    previous.replace(runtime)
    return None, stopped_verified_daemon


def _install_runtime_at(
    runtime: Path, *, expected_version: str, expected_source_fingerprint: str
) -> Path:
    runtime.parent.mkdir(parents=True, exist_ok=True)
    venv.EnvBuilder(with_pip=True).create(str(runtime))
    python = venv_python(runtime)
    install = subprocess.run(
        [str(python), "-m", "pip", "install", "--upgrade", str(SOURCE_ROOT)],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if install.returncode:
        raise RuntimeUpdateError(
            "could not install local runtime dependencies; check your network and retry setup"
        )
    orch = venv_orch(runtime)
    actual_version = installed_runtime_version(orch)
    if actual_version != expected_version:
        raise RuntimeUpdateError(
            "installed runtime version does not match this Plugin bundle "
            f"({actual_version or 'unavailable'} != {expected_version})"
        )
    _write_runtime_identity(
        runtime,
        version=expected_version,
        source_fingerprint=expected_source_fingerprint,
    )
    return orch


def runtime_status() -> dict[str, object]:
    root = app_data_root()
    runtime = root / "venv"
    orch = venv_orch(runtime)
    ready = venv_python(runtime).is_file() and orch.is_file()
    expected_version = bundle_version()
    expected_fingerprint = bundle_source_fingerprint()
    actual_version = installed_runtime_version(orch) if ready else None
    actual_fingerprint = (
        installed_runtime_fingerprint(runtime, version=actual_version)
        if ready
        else None
    )
    daemon = daemon_snapshot(root)
    if not ready:
        update_status = "runtime_missing"
    elif (
        actual_version != expected_version
        or actual_fingerprint != expected_fingerprint
    ):
        update_status = (
            "update_deferred_active_run"
            if daemon.state == "active"
            else "update_required"
        )
    elif daemon.state == "unknown":
        update_status = "update_blocked_unknown_daemon"
    elif daemon.state in {"active", "idle"} and (
        daemon.version != expected_version
        or daemon.source_fingerprint != expected_fingerprint
        or daemon.endpoint != f"http://127.0.0.1:{PERSISTENT_CONTROL_PORT}"
        or not daemon.persistent
        or daemon.project_root is None
        or Path(daemon.project_root).resolve() != Path.cwd().resolve()
    ):
        update_status = (
            "update_deferred_active_run"
            if daemon.state == "active"
            else "update_required"
        )
    else:
        update_status = "up_to_date"
    if (root / "venv.previous").exists():
        # A pending marker always requires setup to finish or recover the
        # transaction before dispatch may reuse the current runtime.
        if daemon.state == "active":
            update_status = "update_deferred_active_run"
        elif daemon.state == "unknown":
            update_status = "update_blocked_unknown_daemon"
        else:
            update_status = "update_required"
    return {
        "python_supported": sys.version_info >= (3, 12),
        "python_version": ".".join(map(str, sys.version_info[:3])),
        "runtime_root": str(runtime),
        "runtime_ready": ready,
        "bundle_version": expected_version,
        "bundle_source_fingerprint": expected_fingerprint,
        "runtime_version": actual_version,
        "runtime_source_fingerprint": actual_fingerprint,
        "daemon_version": daemon.version,
        "daemon_source_fingerprint": daemon.source_fingerprint,
        "daemon_state": daemon.state,
        "daemon_endpoint": daemon.endpoint,
        "daemon_persistent": daemon.persistent,
        "active_run_ids": list(daemon.active_runs),
        "update_status": update_status,
    }


def install_runtime() -> Path:
    """Create the dedicated venv and install this Plugin source into it."""

    if sys.version_info < (3, 12):
        raise RuntimeError("Python 3.12 or newer is required")
    runtime = app_data_root() / "venv"
    if runtime.exists():
        raise RuntimeUpdateError("runtime already exists; use the controlled replacement path")
    try:
        return _install_runtime_at(
            runtime,
            expected_version=bundle_version(),
            expected_source_fingerprint=bundle_source_fingerprint(),
        )
    except Exception:
        _remove_runtime_tree(runtime)
        raise


def replace_runtime() -> Path:
    """Install a candidate runtime while retaining the previous runtime marker."""

    root = app_data_root()
    runtime = root / "venv"
    previous = root / "venv.previous"
    if previous.exists():
        raise RuntimeUpdateError("runtime replacement is blocked: venv.previous already exists")
    if not runtime.exists():
        return install_runtime()
    if runtime.is_symlink() or not runtime.is_dir():
        raise RuntimeUpdateError("runtime replacement is blocked: venv is invalid")
    runtime.replace(previous)
    try:
        orch = _install_runtime_at(
            runtime,
            expected_version=bundle_version(),
            expected_source_fingerprint=bundle_source_fingerprint(),
        )
    except Exception as exc:
        _remove_runtime_tree(runtime)
        previous.replace(runtime)
        raise RuntimeUpdateError(
            f"runtime update failed; the previous runtime was restored: {exc}"
        ) from exc
    return orch


def commit_runtime_update(root: Path | None = None) -> None:
    """Commit an accepted candidate by removing its previous-runtime marker."""

    _remove_runtime_tree((root or app_data_root()) / "venv.previous")


def reconcile_runtime() -> tuple[Path | None, str | None, bool]:
    """Prepare setup without updating during a live external run."""

    root = app_data_root()
    expected_version = bundle_version()
    expected_fingerprint = bundle_source_fingerprint()
    recovery_deferred, recovery_stopped_verified_daemon = restore_interrupted_runtime(
        root,
        expected_version=expected_version,
        expected_source_fingerprint=expected_fingerprint,
    )
    if recovery_deferred:
        return None, recovery_deferred, False
    runtime = root / "venv"
    orch = venv_orch(runtime)
    actual_version = installed_runtime_version(orch)
    actual_fingerprint = installed_runtime_fingerprint(runtime, version=actual_version)
    snapshot = daemon_snapshot(root)
    if snapshot.state in {"active", "idle"} and (
        snapshot.project_root is None
        or Path(snapshot.project_root).resolve() != Path.cwd().resolve()
    ):
        raise RuntimeUpdateError(
            "setup is blocked because the persistent control plane belongs to another project"
        )
    runtime_needs_replace = (
        actual_version != expected_version
        or actual_fingerprint != expected_fingerprint
    )
    daemon_needs_restart = snapshot.state in {"active", "idle"} and (
        runtime_needs_replace
        or snapshot.version != expected_version
        or snapshot.source_fingerprint != expected_fingerprint
        or snapshot.endpoint != f"http://127.0.0.1:{PERSISTENT_CONTROL_PORT}"
        or not snapshot.persistent
    )
    needs_change = runtime_needs_replace or daemon_needs_restart
    if snapshot.state == "active" and needs_change:
        run_hint = ", ".join(snapshot.active_runs) or "an external run"
        return (
            None,
            "runtime update deferred while "
            f"{run_hint} remains active; rerun setup after it finishes",
            False,
        )
    if snapshot.state == "unknown":
        raise RuntimeUpdateError(
            f"setup is blocked because daemon state is unknown: {snapshot.reason}"
        )
    stopped_current_daemon = (
        snapshot.state == "idle"
        and daemon_needs_restart
        and runtime.is_dir()
        and not runtime.is_symlink()
    )
    stopped_verified_daemon = recovery_stopped_verified_daemon or stopped_current_daemon
    if stopped_current_daemon:
        shutdown_idle_daemon(snapshot)
    if runtime_needs_replace:
        try:
            candidate_orch = replace_runtime() if runtime.exists() else install_runtime()
            if snapshot.state == "idle" and daemon_needs_restart and not stopped_current_daemon:
                shutdown_idle_daemon(snapshot)
                stopped_verified_daemon = True
        except Exception as failure:
            if stopped_verified_daemon:
                rollback_runtime_update(
                    root=root,
                    project_root=Path.cwd(),
                    codex_path=current_codex_cli(),
                    node_path=current_node_cli(),
                    failure=failure,
                    restore_runtime=False,
                    allow_loaded_replacement=True,
                )
                raise RuntimeUpdateError(
                    f"{failure}; the persistent control plane was restored"
                ) from failure
            raise
        return candidate_orch, None, stopped_verified_daemon
    if not orch.is_file():
        raise RuntimeUpdateError("local runtime is incomplete; rerun setup after resolving it")
    return orch, None, stopped_verified_daemon


def _validate_setup_result(
    expected_version: str,
    *,
    expected_source_fingerprint: str | None = None,
    project_root: Path | None = None,
) -> None:
    """Validate the daemon identity that authoritatively accepts a setup."""

    snapshot = daemon_snapshot(app_data_root())
    if not _candidate_daemon_is_accepted(
        snapshot,
        expected_version=expected_version,
        expected_source_fingerprint=expected_source_fingerprint,
        project_root=(project_root or Path.cwd()).resolve(),
    ):
        raise RuntimeUpdateError(
            "runtime setup did not produce the persistent control plane expected by this Plugin"
        )


def _persistent_setup_command(
    *, codex_path: Path | None, no_open: bool = False
) -> list[str]:
    command = [
        "setup",
        "--port",
        str(PERSISTENT_CONTROL_PORT),
        "--persistent",
    ]
    if codex_path is not None:
        command.extend(["--codex-path", str(codex_path)])
    if no_open:
        command.append("--no-open")
    return command


def rollback_runtime_update(
    *,
    root: Path,
    project_root: Path,
    codex_path: Path | None,
    node_path: Path | None,
    failure: Exception,
    restore_runtime: bool = True,
    allow_loaded_replacement: bool = False,
) -> None:
    """Boundedly restore the old runtime after candidate setup fails."""

    runtime = root / "venv"
    previous = root / "venv.previous"
    if restore_runtime and not previous.exists():
        raise RuntimeUpdateError(
            f"{failure}; runtime rollback is unavailable because venv.previous is missing"
        )
    snapshot = daemon_snapshot(root)
    if snapshot.state == "active":
        raise RuntimeUpdateError(
            f"{failure}; runtime rollback deferred while "
            f"{_active_run_hint(snapshot)} remains active; "
            "venv and venv.previous were preserved"
        )
    if snapshot.state not in {"active", "idle", "missing", "stale"}:
        raise RuntimeUpdateError(
            f"{failure}; runtime rollback is blocked because daemon state is unknown: "
            f"{snapshot.reason}; venv and venv.previous were preserved"
        )

    stopped_verified_daemon = allow_loaded_replacement
    try:
        if snapshot.state == "idle":
            shutdown_idle_daemon(snapshot)
            stopped_verified_daemon = True
        if restore_runtime:
            _remove_runtime_tree(runtime)
            previous.replace(runtime)
        old_orch = venv_orch(runtime)
        old_version = installed_runtime_version(old_orch)
        if old_version is None:
            raise RuntimeUpdateError("restored runtime version is unavailable")
        old_fingerprint = installed_runtime_fingerprint(runtime, version=old_version)
        ensure_macos_persistent_entry(
            runtime=runtime,
            project_root=project_root,
            codex_path=codex_path,
            node_path=node_path,
            expected_version=old_version,
            expected_source_fingerprint=old_fingerprint,
            allow_loaded_replacement=stopped_verified_daemon,
        )
        exit_code = run_orch(
            old_orch,
            _persistent_setup_command(codex_path=codex_path, no_open=True),
        )
        if exit_code:
            raise RuntimeUpdateError(
                f"restored control-plane setup failed with exit code {exit_code}"
            )
        _validate_setup_result(
            old_version,
            expected_source_fingerprint=old_fingerprint,
            project_root=project_root,
        )
    except Exception as rollback_failure:
        raise RuntimeUpdateError(
            f"{failure}; runtime rollback failed: {rollback_failure}"
        ) from rollback_failure


def macos_launch_agent_path(home: Path | None = None) -> Path:
    """Return the user-owned LaunchAgent path for the fixed local endpoint."""

    return (
        (home or Path.home())
        / "Library"
        / "LaunchAgents"
        / f"{LAUNCH_AGENT_LABEL}.plist"
    )


def macos_launch_agent_payload(
    *,
    runtime: Path,
    project_root: Path,
    codex_path: Path | None,
    node_path: Path | None,
) -> dict[str, object]:
    """Describe the existing daemon as a login-started local control plane."""

    arguments = [
        str(venv_daemon(runtime)),
        "--serve",
        "--data-dir",
        str(app_data_root()),
        "--project-root",
        str(project_root.resolve()),
        "--port",
        str(PERSISTENT_CONTROL_PORT),
        "--persistent",
    ]
    if codex_path is not None:
        arguments.extend(["--codex-path", str(codex_path)])
    payload: dict[str, object] = {
        "Label": LAUNCH_AGENT_LABEL,
        "ProgramArguments": arguments,
        "WorkingDirectory": str(project_root.resolve()),
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "ProcessType": "Background",
    }
    executable_directories: list[str] = []
    for executable in (codex_path, node_path):
        if executable is not None and str(executable.parent) not in executable_directories:
            executable_directories.append(str(executable.parent))
    if executable_directories:
        payload["EnvironmentVariables"] = {
            "PATH": ":".join(
                [*executable_directories, "/usr/bin", "/bin", "/usr/sbin", "/sbin"]
            )
        }
    return payload


def _write_launch_agent(
    path: Path, payload: dict[str, object], *, allow_owned_update: bool = False
) -> bool:
    """Create one exact user LaunchAgent without overwriting a conflicting file."""

    if path.exists() or path.is_symlink():
        if path.is_symlink() or not path.is_file():
            raise RuntimeUpdateError(
                "AIworker Relay LaunchAgent path is not a normal file"
            )
    try:
        existing = plistlib.loads(path.read_bytes())
    except FileNotFoundError:
        existing = None
    except (OSError, plistlib.InvalidFileException) as exc:
        raise RuntimeUpdateError(f"LaunchAgent file cannot be read: {exc}") from exc
    if existing is not None:
        if not isinstance(existing, dict):
            raise RuntimeUpdateError("AIworker Relay LaunchAgent file is invalid")
        if existing != payload:
            existing_arguments = existing.get("ProgramArguments")
            owns_existing_agent = (
                existing.get("Label") == LAUNCH_AGENT_LABEL
                and existing.get("WorkingDirectory") == payload["WorkingDirectory"]
                and isinstance(existing_arguments, list)
                and existing_arguments[:1]
                == [str(venv_daemon(app_data_root() / "venv"))]
            )
            if not allow_owned_update or not owns_existing_agent:
                raise RuntimeUpdateError(
                    "AIworker Relay LaunchAgent already exists with a different configuration; "
                    "inspect it before changing the persistent control plane"
                )
        else:
            return False

    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as temporary:
        temporary.write(plistlib.dumps(payload, fmt=plistlib.FMT_XML, sort_keys=True))
        temporary_path = Path(temporary.name)
    try:
        temporary_path.replace(path)
    except OSError as exc:
        temporary_path.unlink(missing_ok=True)
        raise RuntimeUpdateError(
            f"could not install the AIworker Relay LaunchAgent: {exc}"
        ) from exc
    return True


def _assert_fixed_port_is_available() -> None:
    """Check whether the fixed loopback listener can safely be rebound."""

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind(("127.0.0.1", PERSISTENT_CONTROL_PORT))
        except OSError as exc:
            raise RuntimeUpdateError(
                "fixed local control address "
                f"127.0.0.1:{PERSISTENT_CONTROL_PORT} is already in use"
            ) from exc


def _wait_for_fixed_port_release(*, timeout: float = 5.0) -> None:
    """Wait briefly for a just-unloaded owned LaunchAgent to release its socket."""

    deadline = time.monotonic() + timeout
    while True:
        try:
            _assert_fixed_port_is_available()
            return
        except RuntimeUpdateError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.05)


def _wait_for_persistent_daemon(
    *,
    project_root: Path,
    expected_version: str,
    expected_source_fingerprint: str | None,
    timeout: float = 5.0,
) -> None:
    """Wait for the LaunchAgent to publish the expected loopback control plane."""

    deadline = time.monotonic() + timeout
    expected_project_root = project_root.resolve()
    while time.monotonic() < deadline:
        snapshot = daemon_snapshot(app_data_root())
        if (
            snapshot.state in {"active", "idle"}
            and snapshot.version == expected_version
            and snapshot.source_fingerprint == expected_source_fingerprint
            and snapshot.endpoint == f"http://127.0.0.1:{PERSISTENT_CONTROL_PORT}"
            and snapshot.persistent
            and snapshot.project_root is not None
            and Path(snapshot.project_root).resolve() == expected_project_root
        ):
            return
        time.sleep(0.05)
    raise RuntimeUpdateError(
        "AIworker Relay LaunchAgent did not start its local control plane"
    )


def ensure_macos_persistent_entry(
    *,
    runtime: Path,
    project_root: Path,
    codex_path: Path | None,
    node_path: Path | None,
    expected_version: str,
    expected_source_fingerprint: str | None = None,
    allow_loaded_replacement: bool = False,
) -> None:
    """Install and start the macOS user entry without changing a live run."""

    if sys.platform != "darwin":
        return
    snapshot = daemon_snapshot(app_data_root())
    if snapshot.state == "unknown":
        raise RuntimeUpdateError(
            f"setup is blocked because daemon state is unknown: {snapshot.reason}"
        )
    if snapshot.state in {"active", "idle"}:
        if (
            snapshot.project_root is None
            or Path(snapshot.project_root).resolve() != project_root.resolve()
        ):
            raise RuntimeUpdateError(
                "the persistent control plane is already bound to another project; "
                "do not switch it while it may own that project's runs"
            )
        return

    agent_path = macos_launch_agent_path()
    payload = macos_launch_agent_payload(
        runtime=runtime,
        project_root=project_root,
        codex_path=codex_path,
        node_path=node_path,
    )
    domain = f"gui/{os.getuid()}"
    target = f"{domain}/{LAUNCH_AGENT_LABEL}"
    loaded = subprocess.run(
        ["launchctl", "print", target],
        check=False,
        capture_output=True,
        text=True,
    ).returncode == 0
    if loaded:
        if not allow_loaded_replacement:
            raise RuntimeUpdateError(
                "setup is blocked because the loaded LaunchAgent has no verified daemon identity"
            )
        result = subprocess.run(
            ["launchctl", "bootout", target],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode:
            detail = result.stderr.strip() or result.stdout.strip() or "launchctl failed"
            raise RuntimeUpdateError(
                f"could not update the AIworker Relay LaunchAgent: {detail}"
            )
        _wait_for_fixed_port_release()
    else:
        _assert_fixed_port_is_available()
    _write_launch_agent(agent_path, payload, allow_owned_update=True)
    try:
        result = subprocess.run(
            ["launchctl", "bootstrap", domain, str(agent_path)],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        raise PersistentEntryChangedError(
            f"could not start the AIworker Relay LaunchAgent: {exc}"
        ) from exc
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip() or "launchctl failed"
        raise PersistentEntryChangedError(
            f"could not start the AIworker Relay LaunchAgent: {detail}"
        )
    try:
        _wait_for_persistent_daemon(
            project_root=project_root,
            expected_version=expected_version,
            expected_source_fingerprint=expected_source_fingerprint,
        )
    except RuntimeUpdateError as exc:
        raise PersistentEntryChangedError(str(exc)) from exc


def run_orch(orch: Path, arguments: Sequence[str]) -> int:
    return subprocess.run([str(orch), *arguments], check=False).returncode


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="launch_external_workers.py",
        description="Bootstrap and invoke the local AIworker Relay runtime.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status", help="Print bootstrap readiness as JSON.")
    setup = commands.add_parser("setup", help="Initialize the local runtime if needed and open the control plane.")
    setup.add_argument("--no-open", action="store_true", help=argparse.SUPPRESS)
    dispatch = commands.add_parser("dispatch", help="Pass an authorized Task Packet to orch.")
    dispatch.add_argument("orch_arguments", nargs=argparse.REMAINDER)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(argv) if argv is not None else sys.argv[1:]
    parser = build_parser()
    # Forward the inner CLI's option-shaped arguments unchanged.  argparse
    # otherwise treats `--profile` as an option of this bootstrap command.
    if arguments[:1] == ["dispatch"] and arguments[1:2] != ["--help"]:
        args = parser.parse_args(["dispatch"])
        args.orch_arguments = arguments[1:]
    else:
        args = parser.parse_args(arguments)
    try:
        status = runtime_status()
        if args.command == "status":
            print(json.dumps(status, ensure_ascii=False))
            return 0
        if not status["python_supported"]:
            print("aiworker-relay: Python 3.12 or newer is required", file=sys.stderr)
            return 2
        if args.command == "setup":
            if status.get("update_status") in {"runtime_missing", "update_required"}:
                print("aiworker-relay: preparing the local control plane...", flush=True)
            orch, deferred, stopped_verified_daemon = reconcile_runtime()
            if deferred:
                print(f"aiworker-relay: {deferred}")
                return 0
            assert orch is not None
            root = app_data_root()
            candidate_transaction = (root / "venv.previous").exists()
            codex_path = current_codex_cli()
            node_path = current_node_cli()
            project_root = Path.cwd()
            expected_version = bundle_version()
            expected_fingerprint = bundle_source_fingerprint()
            try:
                ensure_macos_persistent_entry(
                    runtime=orch.parent.parent,
                    project_root=project_root,
                    codex_path=codex_path,
                    node_path=node_path,
                    expected_version=expected_version,
                    expected_source_fingerprint=expected_fingerprint,
                    allow_loaded_replacement=stopped_verified_daemon,
                )
                exit_code = run_orch(
                    orch,
                    _persistent_setup_command(
                        codex_path=codex_path,
                        no_open=args.no_open,
                    ),
                )
                if exit_code:
                    raise RuntimeUpdateError(
                        f"persistent control-plane setup failed with exit code {exit_code}"
                    )
                _validate_setup_result(
                    expected_version,
                    expected_source_fingerprint=expected_fingerprint,
                    project_root=project_root,
                )
            except Exception as failure:
                if candidate_transaction or stopped_verified_daemon:
                    entry_was_changed = isinstance(
                        failure, PersistentEntryChangedError
                    )
                    rollback_runtime_update(
                        root=root,
                        project_root=project_root,
                        codex_path=codex_path,
                        node_path=node_path,
                        failure=failure,
                        restore_runtime=candidate_transaction,
                        allow_loaded_replacement=(
                            stopped_verified_daemon or entry_was_changed
                        ),
                    )
                    restored = (
                        "the previous runtime and control plane were restored"
                        if candidate_transaction
                        else "the persistent control plane was restored"
                    )
                    raise RuntimeUpdateError(f"{failure}; {restored}") from failure
                raise
            if candidate_transaction:
                commit_runtime_update(root)
            return 0
        if status["update_status"] != "up_to_date":
            print(
                "aiworker-relay: local runtime does not match this Plugin bundle; run setup first",
                file=sys.stderr,
            )
            return 2
        runtime = Path(str(status["runtime_root"]))
        codex_path = current_codex_cli()
        command = [
            "dispatch",
            "--port",
            str(PERSISTENT_CONTROL_PORT),
            "--persistent",
        ]
        if codex_path is not None:
            command.extend(["--codex-path", str(codex_path)])
        return run_orch(
            venv_orch(runtime),
            [*command, *args.orch_arguments],
        )
    except (OSError, RuntimeUpdateError, subprocess.CalledProcessError) as exc:
        print(f"aiworker-relay: runtime setup failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
