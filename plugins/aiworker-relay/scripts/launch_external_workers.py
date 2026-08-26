#!/usr/bin/env python3
"""Bootstrap the app-local AIworker Relay runtime from its Plugin source."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
import venv
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence


APP_NAME = "Codex External Workers"
SOURCE_ROOT = Path(__file__).resolve().parents[1]


class RuntimeUpdateError(RuntimeError):
    """The app-local runtime cannot be safely reconciled."""


@dataclass(frozen=True, slots=True)
class DaemonSnapshot:
    """The minimum facts needed before setup can replace a runtime."""

    state: str
    version: str | None = None
    pid: int | None = None
    endpoint: str | None = None
    active_runs: tuple[str, ...] = ()
    reason: str | None = None


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
    timeout: float = 0.8,
) -> tuple[int, dict[str, Any] | None] | None:
    """Call the loopback daemon without inheriting a desktop proxy setting."""

    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        f"{endpoint}{path}",
        data=data,
        headers={"Content-Type": "application/json"} if data is not None else {},
        method=method,
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
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
    if not isinstance(pid, int) or not isinstance(port, int) or not 1 <= port <= 65535:
        return DaemonSnapshot("unknown", reason="daemon record has no valid PID or port")
    process_state = _process_state(pid)
    if process_state is False:
        return DaemonSnapshot("stale", pid=pid)
    if process_state is None:
        return DaemonSnapshot("unknown", pid=pid, reason="daemon PID cannot be inspected")
    endpoint = f"http://127.0.0.1:{port}"
    health_response = _local_request(endpoint, "/api/health")
    if health_response is None:
        return DaemonSnapshot("unknown", pid=pid, endpoint=endpoint, reason="daemon health is unavailable")
    health_status, health = health_response
    if (
        health_status != 200
        or not isinstance(health, dict)
        or health.get("ok") is not True
        or health.get("pid") != pid
        or health.get("port") != port
        or not isinstance(health.get("version"), str)
    ):
        return DaemonSnapshot("unknown", pid=pid, endpoint=endpoint, reason="daemon health does not match its record")
    overview_response = _local_request(endpoint, "/api/overview")
    if overview_response is None or overview_response[0] != 200:
        return DaemonSnapshot("unknown", pid=pid, endpoint=endpoint, reason="daemon overview is unavailable")
    overview = overview_response[1]
    if not isinstance(overview, dict) or not isinstance(overview.get("runs"), list):
        return DaemonSnapshot("unknown", pid=pid, endpoint=endpoint, reason="daemon overview is invalid")
    active_runs = tuple(
        str(run.get("run_id"))
        for run in overview["runs"]
        if isinstance(run, dict)
        and run.get("status") in {"starting", "running", "stopping"}
        and run.get("run_id")
    )
    return DaemonSnapshot(
        "active" if active_runs else "idle",
        version=str(health["version"]),
        pid=pid,
        endpoint=endpoint,
        active_runs=active_runs,
    )


def _wait_for_exit(pid: int, *, timeout: float = 5.0) -> bool:
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
    response = _local_request(snapshot.endpoint, "/api/shutdown", method="POST", payload={})
    if response is None:
        latest = daemon_snapshot(app_data_root())
        if latest.state in {"missing", "stale"}:
            return
        raise RuntimeUpdateError("idle daemon stopped responding before it could be updated")
    status, _ = response
    if status in {404, 405}:
        # The first release bridge targets a verified old daemon which does
        # not yet expose the narrow shutdown endpoint.  It has no active run.
        try:
            os.kill(snapshot.pid, signal.SIGTERM)
        except OSError as exc:
            raise RuntimeUpdateError(f"could not stop the idle daemon: {exc}") from exc
    elif status == 409:
        raise RuntimeUpdateError("runtime update deferred because an external run is now active")
    elif status != 200:
        raise RuntimeUpdateError("idle daemon refused its controlled shutdown")
    if not _wait_for_exit(snapshot.pid):
        raise RuntimeUpdateError("idle daemon did not exit; runtime was not replaced")


def _remove_runtime_tree(path: Path) -> None:
    if not path.exists():
        return
    if path.is_symlink() or not path.is_dir():
        raise RuntimeUpdateError(f"runtime path is not a normal directory: {path}")
    shutil.rmtree(path)


def restore_interrupted_runtime(root: Path) -> None:
    """Recover the sole short-lived backup left by an interrupted replacement."""

    runtime = root / "venv"
    previous = root / "venv.previous"
    if not previous.exists():
        return
    if runtime.exists():
        raise RuntimeUpdateError("runtime recovery is blocked: both venv and venv.previous exist")
    if previous.is_symlink() or not previous.is_dir():
        raise RuntimeUpdateError("runtime recovery is blocked: venv.previous is invalid")
    previous.replace(runtime)


def _install_runtime_at(runtime: Path, *, expected_version: str) -> Path:
    runtime.parent.mkdir(parents=True, exist_ok=True)
    venv.EnvBuilder(with_pip=True).create(str(runtime))
    python = venv_python(runtime)
    subprocess.run(
        [str(python), "-m", "pip", "install", "--upgrade", str(SOURCE_ROOT)],
        check=True,
    )
    orch = venv_orch(runtime)
    actual_version = installed_runtime_version(orch)
    if actual_version != expected_version:
        raise RuntimeUpdateError(
            "installed runtime version does not match this Plugin bundle "
            f"({actual_version or 'unavailable'} != {expected_version})"
        )
    return orch


def runtime_status() -> dict[str, object]:
    root = app_data_root()
    runtime = root / "venv"
    orch = venv_orch(runtime)
    ready = venv_python(runtime).is_file() and orch.is_file()
    expected_version = bundle_version()
    actual_version = installed_runtime_version(orch) if ready else None
    daemon = daemon_snapshot(root)
    if not ready:
        update_status = "runtime_missing"
    elif actual_version != expected_version:
        update_status = (
            "update_deferred_active_run"
            if daemon.state == "active"
            else "update_required"
        )
    elif daemon.state == "unknown":
        update_status = "update_blocked_unknown_daemon"
    elif daemon.state in {"active", "idle"} and daemon.version != expected_version:
        update_status = (
            "update_deferred_active_run"
            if daemon.state == "active"
            else "update_required"
        )
    else:
        update_status = "up_to_date"
    return {
        "python_supported": sys.version_info >= (3, 12),
        "python_version": ".".join(map(str, sys.version_info[:3])),
        "runtime_root": str(runtime),
        "runtime_ready": ready,
        "bundle_version": expected_version,
        "runtime_version": actual_version,
        "daemon_version": daemon.version,
        "daemon_state": daemon.state,
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
        return _install_runtime_at(runtime, expected_version=bundle_version())
    except Exception:
        _remove_runtime_tree(runtime)
        raise


def replace_runtime() -> Path:
    """Replace one inactive app-local runtime and restore it on failure."""

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
        orch = _install_runtime_at(runtime, expected_version=bundle_version())
    except Exception as exc:
        _remove_runtime_tree(runtime)
        previous.replace(runtime)
        raise RuntimeUpdateError(
            f"runtime update failed; the previous runtime was restored: {exc}"
        ) from exc
    _remove_runtime_tree(previous)
    return orch


def reconcile_runtime() -> tuple[Path | None, str | None]:
    """Prepare setup without updating during a live external run."""

    root = app_data_root()
    restore_interrupted_runtime(root)
    runtime = root / "venv"
    orch = venv_orch(runtime)
    expected_version = bundle_version()
    actual_version = installed_runtime_version(orch)
    snapshot = daemon_snapshot(root)
    runtime_needs_replace = actual_version != expected_version
    daemon_needs_restart = (
        snapshot.state in {"active", "idle"} and snapshot.version != expected_version
    )
    needs_change = runtime_needs_replace or daemon_needs_restart
    if snapshot.state == "active" and needs_change:
        run_hint = ", ".join(snapshot.active_runs) or "an external run"
        return None, (
            "runtime update deferred while "
            f"{run_hint} remains active; rerun setup after it finishes"
        )
    if snapshot.state == "unknown":
        raise RuntimeUpdateError(
            f"setup is blocked because daemon state is unknown: {snapshot.reason}"
        )
    if snapshot.state == "idle" and daemon_needs_restart:
        shutdown_idle_daemon(snapshot)
    if runtime_needs_replace:
        return (replace_runtime() if runtime.exists() else install_runtime()), None
    if not orch.is_file():
        raise RuntimeUpdateError("local runtime is incomplete; rerun setup after resolving it")
    return orch, None


def _validate_setup_result(expected_version: str) -> None:
    snapshot = daemon_snapshot(app_data_root())
    if snapshot.state not in {"active", "idle"} or snapshot.version != expected_version:
        raise RuntimeUpdateError(
            "runtime setup did not produce a healthy daemon matching this Plugin version"
        )


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
            orch, deferred = reconcile_runtime()
            if deferred:
                print(f"aiworker-relay: {deferred}")
                return 0
            assert orch is not None
            command = ["setup"]
            if args.no_open:
                command.append("--no-open")
            exit_code = run_orch(orch, command)
            if exit_code:
                return exit_code
            _validate_setup_result(bundle_version())
            return 0
        if status["update_status"] != "up_to_date":
            print(
                "aiworker-relay: local runtime does not match this Plugin bundle; run setup first",
                file=sys.stderr,
            )
            return 2
        runtime = Path(str(status["runtime_root"]))
        return run_orch(venv_orch(runtime), ["dispatch", *args.orch_arguments])
    except (OSError, RuntimeUpdateError, subprocess.CalledProcessError) as exc:
        print(f"aiworker-relay: runtime setup failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
