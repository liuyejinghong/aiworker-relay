"""Codex-led coding-agent orchestration package."""

import json
import sys
from pathlib import Path


__version__ = Path(__file__).with_name("VERSION").read_text(encoding="utf-8").strip()


def runtime_source_fingerprint() -> str | None:
    """Return the source identity recorded by the Plugin launcher."""

    try:
        value = json.loads(
            (Path(sys.prefix) / ".aiworker-release.json").read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError):
        return None
    fingerprint = value.get("source_fingerprint") if isinstance(value, dict) else None
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != 1
        or value.get("version") != __version__
        or not isinstance(fingerprint, str)
        or not fingerprint.startswith("sha256:")
        or len(fingerprint) != 71
        or any(
            character not in "0123456789abcdef" for character in fingerprint[7:]
        )
    ):
        return None
    return fingerprint
