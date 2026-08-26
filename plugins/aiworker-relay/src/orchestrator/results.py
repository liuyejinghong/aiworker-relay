"""Local run evidence: append-only lifecycle plus atomic summaries."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from orchestrator.models import RunRecord, utc_now


def redact_secret(value: str, secret: str | None) -> str:
    """Remove the exact configured secret before writing user-visible evidence."""

    if not secret:
        return value
    return value.replace(secret, "[REDACTED]")


def _atomic_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary_path = Path(temporary)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def _atomic_json(path: Path, value: Any) -> None:
    _atomic_text(
        path, json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    )


class EvidenceStore:
    """Persist the evidence files for one run."""

    def __init__(self, run_dir: Path, *, secret: str | None = None):
        self.run_dir = run_dir
        self.secret = secret
        self.run_file = run_dir / "run.json"
        self.events_file = run_dir / "events.jsonl"
        self.run_dir.mkdir(parents=True, exist_ok=True)

    def write_run(self, value: RunRecord | dict[str, Any]) -> None:
        payload = value.to_dict() if isinstance(value, RunRecord) else dict(value)
        _atomic_json(self.run_file, payload)

    def read_run(self) -> dict[str, Any] | None:
        try:
            return json.loads(self.run_file.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None

    def event(self, name: str, **data: Any) -> None:
        payload = {"at": utc_now(), "event": name, **data}
        encoded = redact_secret(json.dumps(payload, ensure_ascii=False), self.secret)
        with self.events_file.open("a", encoding="utf-8") as handle:
            handle.write(encoded + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def write_last_message(self, text: str) -> Path:
        path = self.run_dir / "last-message.md"
        _atomic_text(path, redact_secret(text, self.secret))
        return path

    def write_task_packet(self, text: str) -> Path:
        path = self.run_dir / "task-packet.md"
        _atomic_text(path, redact_secret(text, self.secret))
        return path

    def write_diff(self, text: str) -> Path:
        path = self.run_dir / "diff.patch"
        _atomic_text(path, redact_secret(text, self.secret))
        return path

    def write_file_list(self, files: list[str]) -> Path:
        path = self.run_dir / "files.json"
        _atomic_json(path, files)
        return path
