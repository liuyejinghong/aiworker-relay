"""Small domain records shared by the local control plane.

These records deliberately describe only the v0.1 profile, packet and run
contract.  They are plain dataclasses so the HTTP layer and the file layer do
not need a framework-specific model system.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal


def utc_now() -> str:
    """Return an ISO-8601 timestamp with an explicit UTC offset."""

    return datetime.now(UTC).isoformat()


ProfileState = Literal["enabled", "frozen"]
VerificationState = Literal["unverified", "verified"]


@dataclass(slots=True)
class Profile:
    """A user-controlled external model profile."""

    id: str
    model: str
    display_name: str | None = None
    state: ProfileState = "enabled"
    verification: VerificationState = "unverified"
    default_reasoning: str = "auto"
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)

    @property
    def enabled(self) -> bool:
        return self.state == "enabled"

    @property
    def frozen(self) -> bool:
        return self.state == "frozen"

    def dispatch_error(
        self,
        *,
        selection_source: str = "user",
        experimental_confirmation: bool = False,
    ) -> str | None:
        """Return the contract error that blocks dispatch, if any."""

        if self.frozen:
            return "frozen_profile"
        if self.verification == "unverified":
            if selection_source != "user" or not experimental_confirmation:
                return "unverified_profile_requires_confirmation"
        return None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Profile":
        return cls(
            id=str(value["id"]),
            model=str(value["model"]),
            display_name=value.get("display_name"),
            state=value.get("state", "enabled"),
            verification=value.get("verification", "unverified"),
            default_reasoning=value.get("default_reasoning", "auto"),
            metadata=dict(value.get("metadata", {})),
            created_at=value.get("created_at", utc_now()),
            updated_at=value.get("updated_at", utc_now()),
        )


@dataclass(slots=True)
class TaskPacket:
    """The fixed-heading Markdown packet sent to an external worker."""

    run_id: str
    fields: dict[str, str]
    raw: str
    profile_id: str | None = None
    profile_model: str | None = None
    reasoning_effort: str | None = None
    reasoning_source: str | None = None
    selection_source: str | None = None
    workspace: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "fields": dict(self.fields),
            "profile_id": self.profile_id,
            "profile_model": self.profile_model,
            "reasoning_effort": self.reasoning_effort,
            "reasoning_source": self.reasoning_source,
            "selection_source": self.selection_source,
            "workspace": dict(self.workspace),
        }

    def prompt(self) -> str:
        """Return the packet text with a small execution instruction."""

        metadata = [
            "\nExecution metadata (control-plane facts):",
            f"- run_id: {self.run_id}",
        ]
        if self.profile_id:
            metadata.append(f"- profile_id: {self.profile_id}")
        if self.profile_model:
            metadata.append(f"- model: {self.profile_model}")
        if self.reasoning_effort:
            metadata.append(f"- reasoning_effort: {self.reasoning_effort}")
        if self.reasoning_source:
            metadata.append(f"- reasoning_source: {self.reasoning_source}")
        if self.selection_source:
            metadata.append(f"- selection_source: {self.selection_source}")
        if self.workspace:
            metadata.append(
                "- workspace: "
                + json.dumps(self.workspace, ensure_ascii=False, sort_keys=True)
            )
        return (
            "Execute the following Task Packet within its stated scope. "
            "Do not infer additional authorization.\n\n"
            f"{self.raw.rstrip()}\n" + "\n".join(metadata) + "\n"
        )


RunStatus = Literal[
    "created",
    "starting",
    "running",
    "stopping",
    "incomplete",
    "succeeded",
    "failed",
    "stopped",
    "stopped_forced",
    "unavailable",
]


@dataclass(slots=True)
class RunRecord:
    """Persisted summary of one external run."""

    run_id: str
    profile_id: str
    model: str
    status: RunStatus
    created_at: str
    updated_at: str
    project_root: str
    reasoning_effort: str | None = None
    reasoning_source: str | None = None
    worktree: str | None = None
    pid: int | None = None
    process_group: int | None = None
    process_started_at: float | None = None
    exit_code: int | None = None
    dirty_workspace_excluded: bool = False
    cost_state: str = "pending"
    token_usage: dict[str, Any] | None = None
    stop_outcome: str | None = None
    error: str | None = None
    artifacts: dict[str, str] = field(default_factory=dict)
    rss_samples: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "RunRecord":
        allowed = {field.name for field in cls.__dataclass_fields__.values()}
        return cls(**{key: value[key] for key in allowed if key in value})
