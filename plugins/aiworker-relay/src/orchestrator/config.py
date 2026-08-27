"""User-level state, application paths, and OS-backed secret storage."""

from __future__ import annotations

import json
import os
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import keyring

from orchestrator.models import Profile


APP_NAME = "Codex External Workers"
KEYRING_SERVICE = "codex-external-workers"
KEYRING_USERNAME = "openrouter"


class ConfigurationError(RuntimeError):
    """A local configuration operation could not be completed."""


class KeyringUnavailable(ConfigurationError):
    """The operating system secret service is not available."""


def user_data_root(
    *,
    platform: str | None = None,
    environ: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> Path:
    """Return the platform-specific application data directory.

    ``platform``, ``environ`` and ``home`` are injectable for tests; normal
    callers use the current operating system and environment.
    """

    platform = platform or sys.platform
    environ = environ or os.environ
    home = home or Path.home()

    if platform == "darwin":
        return home / "Library" / "Application Support" / APP_NAME
    if platform.startswith("win"):
        local_app_data = environ.get("LOCALAPPDATA")
        return (
            Path(local_app_data) if local_app_data else home / "AppData" / "Local"
        ) / APP_NAME
    data_home = environ.get("XDG_DATA_HOME")
    return (
        Path(data_home) if data_home else home / ".local" / "share"
    ) / "codex-external-workers"


@dataclass(frozen=True, slots=True)
class AppPaths:
    """Paths owned by one local control-plane installation."""

    root: Path

    @property
    def daemon_file(self) -> Path:
        return self.root / "daemon.json"

    @property
    def profiles_file(self) -> Path:
        return self.root / "profiles.json"

    @property
    def venv(self) -> Path:
        return self.root / "venv"

    @classmethod
    def for_user(cls, root: Path | None = None) -> "AppPaths":
        return cls((root or user_data_root()).expanduser())


def project_runtime_root(project_root: Path) -> Path:
    """Return the ignored project runtime directory."""

    return project_root.resolve() / ".orch"


def atomic_write_json(path: Path, value: Any) -> None:
    """Replace a small JSON file atomically in its containing directory."""

    path = path.expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary_path = Path(temporary)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def read_json(path: Path, default: Any = None) -> Any:
    """Read JSON, returning ``default`` only when the file does not exist."""

    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        return default


class ProfileStore:
    """Atomic persistence for the small user-level profile collection."""

    def __init__(self, path: Path):
        self.path = path
        self._profiles: dict[str, Profile] = {}
        self.reload()

    def reload(self) -> None:
        raw = read_json(self.path, {"profiles": []}) or {"profiles": []}
        values = raw.get("profiles", []) if isinstance(raw, dict) else []
        self._profiles = {
            profile.id: profile for profile in map(Profile.from_dict, values)
        }

    def _save(self) -> None:
        atomic_write_json(
            self.path,
            {
                "version": 1,
                "profiles": [profile.to_dict() for profile in self._profiles.values()],
            },
        )

    def all(self) -> list[Profile]:
        return list(self._profiles.values())

    def get(self, profile_id: str) -> Profile | None:
        return self._profiles.get(profile_id)

    def put(self, profile: Profile) -> Profile:
        # No evidence-backed promotion operation has been accepted. Fail closed
        # for every current write while preserving existing records on reload.
        profile.verification = "unverified"
        self._profiles[profile.id] = profile
        self._save()
        return profile

    def update_state(self, profile_id: str, state: str) -> Profile:
        profile = self._profiles.get(profile_id)
        if profile is None:
            raise KeyError(profile_id)
        if state not in {"enabled", "frozen"}:
            raise ValueError("state must be enabled or frozen")
        profile.state = state  # type: ignore[assignment]
        from orchestrator.models import utc_now

        profile.updated_at = utc_now()
        self._save()
        return profile


def get_openrouter_key() -> str | None:
    """Read the key from the OS keyring without exposing it to callers."""

    try:
        return keyring.get_password(KEYRING_SERVICE, KEYRING_USERNAME)
    except Exception as exc:  # keyring backends expose different exceptions
        raise KeyringUnavailable("OS keyring is unavailable") from exc


def save_openrouter_key(value: str) -> None:
    """Store a non-empty key in the OS keyring; never write a plaintext fallback."""

    value = value.strip()
    if not value:
        raise ConfigurationError("OpenRouter API key must not be empty")
    try:
        keyring.set_password(KEYRING_SERVICE, KEYRING_USERNAME, value)
    except Exception as exc:
        raise KeyringUnavailable("OS keyring is unavailable") from exc
