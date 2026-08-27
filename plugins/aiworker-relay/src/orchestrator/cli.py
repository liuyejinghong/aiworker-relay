"""The Skill-facing launcher for the local control plane."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from orchestrator import __version__
from orchestrator.config import AppPaths


CAPABILITY_HEADER = "X-AIworker-Capability"


class CLIError(RuntimeError):
    """A user-actionable launcher or local API error."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never carry a local capability to a redirect target."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def _loopback_open(request: urllib.request.Request | str, *, timeout: float):
    """Open a local control-plane URL without inheriting desktop proxies."""

    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}), _NoRedirect()
    ).open(
        request, timeout=timeout
    )


def _endpoint_from_record(record: dict[str, Any]) -> str | None:
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
        return None
    try:
        os.kill(pid, 0)
    except OSError:
        return None
    return f"http://127.0.0.1:{port}"


def _process_state(pid: int) -> bool | None:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return None
    except OSError:
        return False
    return True


def _health(
    endpoint: str,
    *,
    capability: str | None = None,
    expected: dict[str, Any] | None = None,
    timeout: float = 0.8,
) -> bool:
    headers = {CAPABILITY_HEADER: capability} if capability else {}
    request = urllib.request.Request(f"{endpoint}/api/health", headers=headers)
    try:
        with _loopback_open(request, timeout=timeout) as response:
            value = json.loads(response.read().decode("utf-8"))
        if not isinstance(value, dict) or value.get("ok") is not True:
            return False
        if expected is None:
            return True
        required = (
            "pid",
            "port",
            "project_root",
            "runtime_root",
            "version",
            "persistent",
        )
        if any(key not in value for key in required):
            return False
        for key in required:
            if value.get(key) != expected.get(key):
                return False
        return value.get("version") == __version__
    except (OSError, urllib.error.URLError, json.JSONDecodeError):
        return False


def _record_expected(
    record: dict[str, Any], requested_project_root: Path, *, expected_persistent: bool
) -> dict[str, Any] | None:
    """Return the non-secret daemon identity required for health validation."""

    pid = record.get("pid")
    port = record.get("port")
    recorded_project_root = record.get("project_root")
    runtime_root = record.get("runtime_root")
    version = record.get("version")
    record_persistent = record.get("persistent")
    capability = record.get("capability")
    if (
        not isinstance(pid, int)
        or isinstance(pid, bool)
        or pid <= 0
        or not isinstance(port, int)
        or isinstance(port, bool)
        or not 1 <= port <= 65535
        or not isinstance(recorded_project_root, str)
        or not recorded_project_root
        or not isinstance(runtime_root, str)
        or not isinstance(version, str)
        or not isinstance(record_persistent, bool)
        or not isinstance(capability, str)
        or not capability
    ):
        return None
    if Path(recorded_project_root).resolve() != requested_project_root:
        return None
    if Path(runtime_root).resolve() != requested_project_root / ".orch":
        return None
    if version != __version__:
        return None
    if record_persistent is not expected_persistent:
        return None
    return {
        "pid": pid,
        "port": port,
        "project_root": str(requested_project_root),
        "runtime_root": str(requested_project_root / ".orch"),
        "version": version,
        "persistent": record_persistent,
    }


def _daemon_capability(data_dir: Path | None) -> str:
    paths = AppPaths.for_user(data_dir)
    try:
        record = json.loads(paths.daemon_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CLIError("local daemon capability is unavailable") from exc
    capability = record.get("capability") if isinstance(record, dict) else None
    if not isinstance(capability, str) or not capability:
        raise CLIError("local daemon capability is unavailable")
    return capability


def ensure_daemon(
    *,
    data_dir: Path | None = None,
    project_root: Path | None = None,
    timeout: float = 8.0,
    port: int = 0,
    persistent: bool = False,
    codex_path: str | None = None,
) -> str:
    """Reuse a daemon only after a real health response, otherwise launch one."""

    paths = AppPaths.for_user(data_dir)
    paths.root.mkdir(parents=True, exist_ok=True)
    requested_project_root = (project_root or Path.cwd()).resolve()
    record_present = True
    try:
        record = json.loads(paths.daemon_file.read_text(encoding="utf-8"))
    except FileNotFoundError:
        record = None
        record_present = False
    except (OSError, json.JSONDecodeError) as exc:
        raise CLIError(
            "external-workersd has an unreadable daemon record; inspect it before reuse"
        ) from exc
    if record_present and not isinstance(record, dict):
        raise CLIError(
            "external-workersd has an unknown daemon record; inspect it before reuse"
        )
    if isinstance(record, dict):
        pid = record.get("pid")
        port_value = record.get("port")
        if (
            not isinstance(pid, int)
            or isinstance(pid, bool)
            or pid <= 0
            or not isinstance(port_value, int)
            or isinstance(port_value, bool)
            or not 1 <= port_value <= 65535
        ):
            raise CLIError(
                "external-workersd has an unknown daemon record; inspect it before reuse"
            )
        process_state = _process_state(pid)
        if process_state is True:
            recorded_project_root = record.get("project_root")
            if (
                isinstance(recorded_project_root, str)
                and recorded_project_root
                and Path(recorded_project_root).resolve() != requested_project_root
            ):
                raise CLIError(
                    "external-workersd is already active for "
                    f"{Path(recorded_project_root).resolve()}; stop it before using "
                    f"AIworker Relay in {requested_project_root}"
                )
            expected = _record_expected(
                record, requested_project_root, expected_persistent=persistent
            )
            endpoint = _endpoint_from_record(record)
            capability = record.get("capability")
            if (
                expected is not None
                and endpoint
                and isinstance(capability, str)
                and _health(
                    endpoint,
                    capability=capability,
                    expected=expected,
                )
            ):
                return endpoint
            if not isinstance(capability, str) or not capability:
                raise CLIError(
                    "external-workersd is already active with no capability; "
                    "do not reuse or replace this unknown daemon"
                )
            raise CLIError(
                "external-workersd is already active but its identity cannot be verified; "
                "inspect it before reuse"
            )
        if process_state is None:
            raise CLIError(
                "external-workersd PID cannot be inspected; do not replace the daemon"
            )

    command = [
        sys.executable,
        "-m",
        "orchestrator.daemon",
        "--serve",
        "--data-dir",
        str(paths.root),
        "--project-root",
        str(requested_project_root),
    ]
    if port:
        command.extend(["--port", str(port)])
    if persistent:
        command.append("--persistent")
    if codex_path:
        command.extend(["--codex-path", codex_path])
    kwargs: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "cwd": str(requested_project_root),
    }
    if os.name == "nt":
        kwargs["creationflags"] = (
            subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
        )
    else:
        kwargs["start_new_session"] = True
    try:
        subprocess.Popen(command, **kwargs)
    except OSError as exc:
        raise CLIError(f"unable to start external-workersd: {exc}") from exc

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            record = json.loads(paths.daemon_file.read_text(encoding="utf-8"))
        except FileNotFoundError:
            record = None
        except (OSError, json.JSONDecodeError) as exc:
            raise CLIError(
                "external-workersd has an unreadable daemon record; inspect it before reuse"
            ) from exc
        if isinstance(record, dict):
            pid = record.get("pid")
            port_value = record.get("port")
            if (
                not isinstance(pid, int)
                or isinstance(pid, bool)
                or pid <= 0
                or not isinstance(port_value, int)
                or isinstance(port_value, bool)
                or not 1 <= port_value <= 65535
            ):
                continue
            endpoint = _endpoint_from_record(record)
            capability = record.get("capability")
            expected = _record_expected(
                record, requested_project_root, expected_persistent=persistent
            )
            if (
                endpoint
                and expected is not None
                and isinstance(capability, str)
                and _health(endpoint, capability=capability, expected=expected)
            ):
                return endpoint
        time.sleep(0.05)
    raise CLIError("external-workersd did not become healthy")


def _api_post(
    endpoint: str,
    path: str,
    payload: dict[str, Any],
    *,
    capability: str | None = None,
) -> dict[str, Any]:
    headers = {"Content-Type": "application/json"}
    if capability:
        headers[CAPABILITY_HEADER] = capability
    request = urllib.request.Request(
        f"{endpoint}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with _loopback_open(request, timeout=10) as response:
            value = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            value = json.loads(exc.read().decode("utf-8"))
        except (OSError, json.JSONDecodeError):
            value = {"code": "http_error", "message": str(exc)}
        raise CLIError(
            f"{value.get('code', 'http_error')}: {value.get('message', value)}"
        ) from exc
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        raise CLIError(f"local daemon request failed: {exc}") from exc
    if not isinstance(value, dict):
        raise CLIError("local daemon returned an invalid response")
    return value


def build_parser() -> argparse.ArgumentParser:
    """Build the parser for the currently implemented commands."""
    parser = argparse.ArgumentParser(
        prog="orch",
        description="AIworker Relay local control-plane CLI.",
    )
    commands = parser.add_subparsers(dest="command", metavar="COMMAND")
    commands.add_parser("version", help="Print the package version.")
    setup = commands.add_parser(
        "setup", help="Start or reuse the local Web control plane."
    )
    setup.add_argument("--data-dir", type=Path, help=argparse.SUPPRESS)
    setup.add_argument("--project-root", type=Path, help=argparse.SUPPRESS)
    setup.add_argument("--no-open", action="store_true", help=argparse.SUPPRESS)
    setup.add_argument("--port", type=int, default=0, help=argparse.SUPPRESS)
    setup.add_argument("--persistent", action="store_true", help=argparse.SUPPRESS)
    setup.add_argument("--codex-path", help=argparse.SUPPRESS)
    dispatch = commands.add_parser(
        "dispatch", help="Dispatch a fixed Task Packet through the local daemon."
    )
    dispatch.add_argument("--profile", required=True, help="Profile id.")
    dispatch.add_argument(
        "--packet", required=True, type=Path, help="Task Packet Markdown path."
    )
    dispatch.add_argument(
        "--selection-source", choices=("user", "codex"), default="user"
    )
    dispatch.add_argument("--confirm-experimental", action="store_true")
    dispatch.add_argument("--data-dir", type=Path, help=argparse.SUPPRESS)
    dispatch.add_argument("--project-root", type=Path, help=argparse.SUPPRESS)
    dispatch.add_argument("--port", type=int, default=0, help=argparse.SUPPRESS)
    dispatch.add_argument("--persistent", action="store_true", help=argparse.SUPPRESS)
    dispatch.add_argument("--codex-path", help=argparse.SUPPRESS)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run an implemented bootstrap command."""
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        if args.command == "version":
            print(__version__)
            return 0
        if args.command == "setup":
            endpoint = ensure_daemon(
                data_dir=args.data_dir,
                project_root=args.project_root,
                port=args.port,
                persistent=args.persistent,
                codex_path=args.codex_path,
            )
            print(endpoint)
            if not args.no_open:
                webbrowser.open(endpoint)
            return 0
        if args.command == "dispatch":
            endpoint = ensure_daemon(
                data_dir=args.data_dir,
                project_root=args.project_root,
                port=args.port,
                persistent=args.persistent,
                codex_path=args.codex_path,
            )
            capability = _daemon_capability(args.data_dir)
            value = _api_post(
                endpoint,
                "/api/runs",
                {
                    "profile_id": args.profile,
                    "packet_path": str(args.packet.resolve()),
                    "selection_source": args.selection_source,
                    "experimental_confirmation": args.confirm_experimental,
                    "consent": True,
                },
                capability=capability,
            )
            print(json.dumps(value, ensure_ascii=False))
            return 0
    except CLIError as exc:
        print(f"orch: {exc}", file=sys.stderr)
        return 1

    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
