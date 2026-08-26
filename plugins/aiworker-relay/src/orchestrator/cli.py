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


class CLIError(RuntimeError):
    """A user-actionable launcher or local API error."""


def _loopback_open(request: urllib.request.Request | str, *, timeout: float):
    """Open a local control-plane URL without inheriting desktop proxies."""

    return urllib.request.build_opener(urllib.request.ProxyHandler({})).open(
        request, timeout=timeout
    )


def _endpoint_from_record(record: dict[str, Any]) -> str | None:
    pid = record.get("pid")
    port = record.get("port")
    if not isinstance(pid, int) or not isinstance(port, int) or port <= 0:
        return None
    try:
        os.kill(pid, 0)
    except OSError:
        return None
    return f"http://127.0.0.1:{port}"


def _health(endpoint: str, *, timeout: float = 0.8) -> bool:
    try:
        with _loopback_open(f"{endpoint}/api/health", timeout=timeout) as response:
            value = json.loads(response.read().decode("utf-8"))
        return bool(value.get("ok"))
    except (OSError, urllib.error.URLError, json.JSONDecodeError):
        return False


def ensure_daemon(
    *,
    data_dir: Path | None = None,
    project_root: Path | None = None,
    timeout: float = 8.0,
) -> str:
    """Reuse a daemon only after a real health response, otherwise launch one."""

    paths = AppPaths.for_user(data_dir)
    paths.root.mkdir(parents=True, exist_ok=True)
    requested_project_root = (project_root or Path.cwd()).resolve()
    try:
        record = json.loads(paths.daemon_file.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        record = None
    if isinstance(record, dict):
        endpoint = _endpoint_from_record(record)
        if endpoint and _health(endpoint):
            recorded_project_root = record.get("project_root")
            if not isinstance(recorded_project_root, str) or not recorded_project_root:
                raise CLIError(
                    "external-workersd is already active with an unknown project binding; "
                    "stop it before using AIworker Relay in this project"
                )
            if Path(recorded_project_root).resolve() != requested_project_root:
                raise CLIError(
                    "external-workersd is already active for "
                    f"{Path(recorded_project_root).resolve()}; stop it before using "
                    f"AIworker Relay in {requested_project_root}"
                )
            return endpoint

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
        except (FileNotFoundError, json.JSONDecodeError):
            record = None
        if isinstance(record, dict):
            endpoint = _endpoint_from_record(record)
            if endpoint and _health(endpoint):
                return endpoint
        time.sleep(0.05)
    raise CLIError("external-workersd did not become healthy")


def _api_post(endpoint: str, path: str, payload: dict[str, Any]) -> dict[str, Any]:
    request = urllib.request.Request(
        f"{endpoint}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
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
                data_dir=args.data_dir, project_root=args.project_root
            )
            print(endpoint)
            if not args.no_open:
                webbrowser.open(endpoint)
            return 0
        if args.command == "dispatch":
            endpoint = ensure_daemon(
                data_dir=args.data_dir, project_root=args.project_root
            )
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
