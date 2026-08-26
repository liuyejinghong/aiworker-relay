"""Validation and loading for the fixed Markdown Task Packet v1."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable

from orchestrator.models import TaskPacket


REQUIRED_FIELDS: tuple[str, ...] = (
    "Task",
    "Scope",
    "Do Not Touch",
    "Existing Behavior",
    "Expected Behavior",
    "Constraints",
    "Acceptance Criteria",
    "Verification",
    "Deliverables",
)


class PacketValidationError(ValueError):
    """A Task Packet is missing a required field or contains no value."""

    def __init__(self, message: str, *, missing: Iterable[str] = ()):
        super().__init__(message)
        self.missing = tuple(missing)


def _heading_name(value: str) -> str:
    value = value.strip().rstrip("#").strip()
    value = re.sub(r"\s+", " ", value)
    return value


def _parse_sections(text: str) -> dict[str, str]:
    headings: list[tuple[int, str, int]] = []
    lines = text.splitlines(keepends=True)
    for index, line in enumerate(lines):
        match = re.match(r"^(#{1,6})\s+(.+?)\s*$", line)
        if match:
            headings.append((len(match.group(1)), _heading_name(match.group(2)), index))

    sections: dict[str, str] = {}
    for position, (_, name, start) in enumerate(headings):
        end = len(lines)
        for next_level, _, next_start in headings[position + 1 :]:
            if next_level <= headings[position][0]:
                end = next_start
                break
        value = "".join(lines[start + 1 : end]).strip()
        if name in sections:
            raise PacketValidationError(f"duplicate Task Packet heading: {name}")
        sections[name] = value
    return sections


def parse_packet(
    text: str,
    *,
    run_id: str,
    profile_id: str | None = None,
    profile_model: str | None = None,
    reasoning_effort: str | None = None,
    selection_source: str | None = None,
) -> TaskPacket:
    """Parse and validate a Task Packet without imposing a JSON schema."""

    if not text.strip():
        raise PacketValidationError("Task Packet is empty")
    sections = _parse_sections(text)
    missing = [field for field in REQUIRED_FIELDS if field not in sections]
    if missing:
        raise PacketValidationError(
            f"Task Packet is missing required headings: {', '.join(missing)}",
            missing=missing,
        )
    empty = [field for field in REQUIRED_FIELDS if not sections[field]]
    if empty:
        raise PacketValidationError(
            f"Task Packet headings must contain text: {', '.join(empty)}"
        )
    return TaskPacket(
        run_id=run_id,
        fields={field: sections[field] for field in REQUIRED_FIELDS},
        raw=text,
        profile_id=profile_id,
        profile_model=profile_model,
        reasoning_effort=reasoning_effort,
        selection_source=selection_source,
    )


def load_packet(
    path: Path,
    *,
    run_id: str,
    profile_id: str | None = None,
    profile_model: str | None = None,
    reasoning_effort: str | None = None,
    selection_source: str | None = None,
) -> TaskPacket:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PacketValidationError(f"unable to read Task Packet: {path}") from exc
    return parse_packet(
        text,
        run_id=run_id,
        profile_id=profile_id,
        profile_model=profile_model,
        reasoning_effort=reasoning_effort,
        selection_source=selection_source,
    )
