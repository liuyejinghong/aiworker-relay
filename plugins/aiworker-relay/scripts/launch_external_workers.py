#!/usr/bin/env python3
"""Bootstrap the app-local AIworker Relay runtime from its Plugin source."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import venv
from pathlib import Path
from typing import Sequence


APP_NAME = "Codex External Workers"
SOURCE_ROOT = Path(__file__).resolve().parents[1]


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


def runtime_status() -> dict[str, object]:
    root = app_data_root()
    runtime = root / "venv"
    return {
        "python_supported": sys.version_info >= (3, 12),
        "python_version": ".".join(map(str, sys.version_info[:3])),
        "runtime_root": str(runtime),
        "runtime_ready": venv_python(runtime).is_file() and venv_orch(runtime).is_file(),
    }


def install_runtime() -> Path:
    """Create the dedicated venv and install this Plugin source into it."""

    if sys.version_info < (3, 12):
        raise RuntimeError("Python 3.12 or newer is required")
    runtime = app_data_root() / "venv"
    python = venv_python(runtime)
    if not python.is_file():
        runtime.parent.mkdir(parents=True, exist_ok=True)
        venv.EnvBuilder(with_pip=True).create(str(runtime))
    subprocess.run(
        [str(python), "-m", "pip", "install", "--upgrade", str(SOURCE_ROOT)],
        check=True,
    )
    orch = venv_orch(runtime)
    if not orch.is_file():
        raise RuntimeError("AIworker Relay runtime did not install an orch launcher")
    return orch


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
    args = build_parser().parse_args(argv)
    status = runtime_status()
    if args.command == "status":
        print(json.dumps(status, ensure_ascii=False))
        return 0
    if not status["python_supported"]:
        print("aiworker-relay: Python 3.12 or newer is required", file=sys.stderr)
        return 2
    runtime = Path(str(status["runtime_root"]))
    orch = venv_orch(runtime)
    if not bool(status["runtime_ready"]):
        if args.command != "setup":
            print(
                "aiworker-relay: local runtime is not installed; run setup first",
                file=sys.stderr,
            )
            return 2
        try:
            orch = install_runtime()
        except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
            print(f"aiworker-relay: runtime installation failed: {exc}", file=sys.stderr)
            return 1
    if args.command == "setup":
        command = ["setup"]
        if args.no_open:
            command.append("--no-open")
        return run_orch(orch, command)
    return run_orch(orch, ["dispatch", *args.orch_arguments])


if __name__ == "__main__":
    raise SystemExit(main())
