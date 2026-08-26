"""The deliberately small Git worktree boundary for external write runs."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path


class WorktreeError(RuntimeError):
    """The source repository cannot provide the v0.1 worktree contract."""


@dataclass(frozen=True, slots=True)
class WorktreeInfo:
    project_root: Path
    path: Path
    source_head: str
    dirty_workspace_excluded: bool


def _git(project_root: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=project_root,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        raise WorktreeError(detail or f"git {' '.join(arguments)} failed")
    return result.stdout.strip()


def source_state(project_root: Path) -> tuple[str, bool]:
    """Return ``HEAD`` and whether the source workspace has dirty changes."""

    project_root = project_root.resolve()
    head = _git(project_root, "rev-parse", "--verify", "HEAD")
    dirty = bool(
        _git(project_root, "status", "--porcelain=v1", "--untracked-files=all")
    )
    return head, dirty


def create_worktree(
    project_root: Path,
    run_id: str,
    *,
    root: Path | None = None,
) -> WorktreeInfo:
    """Create a detached worktree from the current ``HEAD``.

    The source workspace is never modified and its dirty state is only
    recorded, not copied into the run.
    """

    project_root = project_root.resolve()
    head, dirty = source_state(project_root)
    worktree_root = (root or project_root / ".orch" / "worktrees").resolve()
    worktree_root.mkdir(parents=True, exist_ok=True)
    path = worktree_root / run_id
    if path.exists():
        raise WorktreeError(f"worktree path already exists: {path}")
    _git(project_root, "worktree", "add", "--detach", str(path), head)
    return WorktreeInfo(
        project_root=project_root,
        path=path,
        source_head=head,
        dirty_workspace_excluded=dirty,
    )


def changed_files(worktree: Path) -> list[str]:
    """Return paths reported by Git as changed in an external worktree."""

    output = _git(worktree, "status", "--porcelain=v1", "--untracked-files=all")
    paths: list[str] = []
    for line in output.splitlines():
        if not line:
            continue
        value = line[3:] if len(line) >= 3 else line
        if " -> " in value:
            value = value.split(" -> ", 1)[1]
        paths.append(value)
    return paths


def diff_text(worktree: Path) -> str:
    """Collect an uncommitted binary-capable diff for evidence."""

    result = subprocess.run(
        ["git", "diff", "--no-ext-diff", "--binary", "--no-color"],
        cwd=worktree,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode:
        raise WorktreeError(result.stderr.strip() or "git diff failed")
    return result.stdout
