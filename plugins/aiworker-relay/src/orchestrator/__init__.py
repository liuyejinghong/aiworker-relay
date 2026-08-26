"""Codex-led coding-agent orchestration package."""

from pathlib import Path


__version__ = Path(__file__).with_name("VERSION").read_text(encoding="utf-8").strip()
