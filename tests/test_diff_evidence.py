"""Regression tests for complete, non-mutating run diff evidence."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from orchestrator.worktree import diff_text


def git(repository: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


class DiffEvidenceTests(unittest.TestCase):
    def test_diff_includes_every_change_without_mutating_linked_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            worktree = root / "worktree"
            source.mkdir()
            git(source, "init")
            git(source, "config", "user.email", "review@example.com")
            git(source, "config", "user.name", "Review Test")
            (source / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
            (source / "deleted.txt").write_text("delete me\n", encoding="utf-8")
            (source / "staged.txt").write_text("before staged\n", encoding="utf-8")
            (source / "unstaged.txt").write_text(
                "before unstaged\n", encoding="utf-8"
            )
            git(source, "add", ".")
            git(source, "commit", "-m", "initial")
            git(source, "worktree", "add", "--detach", str(worktree), "HEAD")

            (worktree / "staged.txt").write_text("after staged\n", encoding="utf-8")
            git(worktree, "add", "staged.txt")
            (worktree / "unstaged.txt").write_text(
                "after unstaged\n", encoding="utf-8"
            )
            (worktree / "deleted.txt").unlink()
            (worktree / "new file.txt").write_text("new content\n", encoding="utf-8")
            (worktree / "empty.txt").touch()
            (worktree / "binary.bin").write_bytes(b"\x00\x01\x02\xff")
            (worktree / "ignored.txt").write_text("not evidence\n", encoding="utf-8")

            status_before = git(
                worktree, "status", "--porcelain=v1", "--untracked-files=all"
            )
            source_status_before = git(
                source, "status", "--porcelain=v1", "--untracked-files=all"
            )
            object_files_before = {
                path.relative_to(source / ".git" / "objects")
                for path in (source / ".git" / "objects").rglob("*")
                if path.is_file()
            }

            patch = diff_text(worktree)

            status_after = git(
                worktree, "status", "--porcelain=v1", "--untracked-files=all"
            )
            source_status_after = git(
                source, "status", "--porcelain=v1", "--untracked-files=all"
            )
            object_files_after = {
                path.relative_to(source / ".git" / "objects")
                for path in (source / ".git" / "objects").rglob("*")
                if path.is_file()
            }

        self.assertEqual(status_after, status_before)
        self.assertEqual(source_status_after, source_status_before)
        self.assertEqual(object_files_after, object_files_before)
        self.assertIn("after staged", patch)
        self.assertIn("after unstaged", patch)
        self.assertIn("deleted file mode", patch)
        self.assertIn("new content", patch)
        self.assertIn("diff --git a/empty.txt b/empty.txt", patch)
        self.assertIn("GIT binary patch", patch)
        self.assertNotIn("ignored.txt", patch)


if __name__ == "__main__":
    unittest.main()
