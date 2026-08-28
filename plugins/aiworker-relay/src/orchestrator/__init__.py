"""Codex-led coding-agent orchestration package."""

import json
import sys
from pathlib import Path


__version__ = Path(__file__).with_name("VERSION").read_text(encoding="utf-8").strip()


def _valid_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and value.startswith("sha256:")
        and len(value) == 71
        and all(character in "0123456789abcdef" for character in value[7:])
    )


def runtime_release_identity() -> dict[str, object] | None:
    """Return the validated non-secret runtime identity written by setup."""

    try:
        value = json.loads(
            (Path(sys.prefix) / ".aiworker-release.json").read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError):
        return None
    if (
        not isinstance(value, dict)
        or value.get("schema_version") not in {1, 2}
        or value.get("version") != __version__
        or not _valid_sha256(value.get("source_fingerprint"))
    ):
        return None
    if value["schema_version"] == 2:
        packages = value.get("packages")
        if (
            not isinstance(value.get("dependency_lock"), str)
            or not value["dependency_lock"]
            or not _valid_sha256(value.get("dependency_lock_sha256"))
            or not isinstance(value.get("python_version"), str)
            or not value["python_version"]
            or not isinstance(packages, list)
            or any(
                not isinstance(package, dict)
                or not isinstance(package.get("name"), str)
                or not package["name"]
                or not isinstance(package.get("version"), str)
                or not package["version"]
                for package in packages
            )
        ):
            return None
    return value


def runtime_source_fingerprint() -> str | None:
    """Return the source identity recorded by the Plugin launcher."""

    identity = runtime_release_identity()
    return str(identity["source_fingerprint"]) if identity is not None else None


def runtime_dependency_identity() -> dict[str, object] | None:
    """Return the accepted lock and resolved package set for this runtime."""

    identity = runtime_release_identity()
    if identity is None or identity.get("schema_version") != 2:
        return None
    return {
        "lock": identity["dependency_lock"],
        "lock_sha256": identity["dependency_lock_sha256"],
        "python_version": identity["python_version"],
        "packages": identity["packages"],
    }
