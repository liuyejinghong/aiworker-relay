"""The deliberately small Git worktree boundary for external write runs."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path


class WorktreeError(RuntimeError):
    """The source repository cannot provide the v0.1 worktree contract."""


@dataclass(frozen=True, slots=True)
class WorktreeInfo:
    project_root: Path
    path: Path
    git_common_dir: Path
    source_checkout_index: Path
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
    git_common_dir = Path(_git(path, "rev-parse", "--git-common-dir"))
    if not git_common_dir.is_absolute():
        git_common_dir = (path / git_common_dir).resolve()
    source_checkout_index = Path(
        _git(project_root, "rev-parse", "--git-path", "index")
    )
    if not source_checkout_index.is_absolute():
        source_checkout_index = (project_root / source_checkout_index).resolve()
    return WorktreeInfo(
        project_root=project_root,
        path=path,
        git_common_dir=git_common_dir,
        source_checkout_index=source_checkout_index,
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


def _git_path(worktree: Path, name: str) -> Path:
    value = Path(_git(worktree, "rev-parse", "--git-path", name))
    if not value.is_absolute():
        value = worktree / value
    return value.resolve()


def _quote_alternate_object_path(path: Path) -> str:
    """Quote one Git alternate-object path without losing legal separators."""

    value = str(path).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{value}"'


def diff_text(worktree: Path) -> str:
    """Collect the complete uncommitted binary-capable diff for evidence.

    Git's ordinary ``diff`` omits untracked files, while ``diff --cached``
    omits unstaged edits. Build a disposable index and object directory, stage
    the complete worktree there, and compare that snapshot to ``HEAD``. The
    worker's real index, status, and repository object database remain intact.
    The returned text uses UTF-8 with ``surrogateescape`` so callers can
    re-encode it without changing non-UTF-8 or newline bytes.
    """

    worktree = worktree.resolve()
    index_path = _git_path(worktree, "index")
    object_path = _git_path(worktree, "objects")
    if not index_path.is_file():
        raise WorktreeError(f"Git index is unavailable: {index_path}")
    if not object_path.is_dir():
        raise WorktreeError(f"Git object directory is unavailable: {object_path}")

    with tempfile.TemporaryDirectory(prefix="aiworker-relay-diff-") as temporary:
        temporary_root = Path(temporary)
        temporary_index = temporary_root / "index"
        temporary_objects = temporary_root / "objects"
        temporary_objects.mkdir()
        shutil.copyfile(index_path, temporary_index)

        environment = os.environ.copy()
        environment["GIT_INDEX_FILE"] = str(temporary_index)
        environment["GIT_OBJECT_DIRECTORY"] = str(temporary_objects)
        alternates = [_quote_alternate_object_path(object_path)]
        inherited_alternates = environment.get("GIT_ALTERNATE_OBJECT_DIRECTORIES")
        if inherited_alternates:
            alternates.append(inherited_alternates)
        environment["GIT_ALTERNATE_OBJECT_DIRECTORIES"] = os.pathsep.join(alternates)

        staged = subprocess.run(
            ["git", "add", "--all", "--"],
            cwd=worktree,
            env=environment,
            check=False,
            capture_output=True,
        )
        if staged.returncode:
            detail = (staged.stderr or staged.stdout).decode(
                "utf-8", errors="replace"
            ).strip()
            raise WorktreeError(detail or "git add for evidence failed")

        result = subprocess.run(
            [
                "git",
                "diff",
                "--cached",
                "--no-ext-diff",
                "--no-textconv",
                "--binary",
                "--no-color",
                "HEAD",
            ],
            cwd=worktree,
            env=environment,
            check=False,
            capture_output=True,
        )
        if result.returncode:
            detail = (result.stderr or result.stdout).decode(
                "utf-8", errors="replace"
            ).strip()
            raise WorktreeError(detail or "git diff failed")
        return result.stdout.decode("utf-8", errors="surrogateescape")
