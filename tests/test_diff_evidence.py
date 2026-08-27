"""Regression tests for complete, non-mutating run diff evidence."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from orchestrator.results import EvidenceStore
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
    def _repository_with_worktree(
        self,
        root: Path,
        *,
        source_name: str = "source",
        initial_content: bytes = b"before\n",
    ) -> tuple[Path, Path]:
        source = root / source_name
        worktree = root / "worktree"
        source.mkdir()
        git(source, "init")
        git(source, "config", "user.email", "review@example.com")
        git(source, "config", "user.name", "Review Test")
        (source / "file.txt").write_bytes(initial_content)
        git(source, "add", ".")
        git(source, "commit", "-m", "initial")
        git(source, "worktree", "add", "--detach", str(worktree), "HEAD")
        return source, worktree

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

    def test_diff_preserves_non_utf8_bytes_through_evidence_persistence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _source, worktree = self._repository_with_worktree(root)
            (worktree / "file.txt").write_bytes(b"after secret \x80\n")

            patch = diff_text(worktree)
            patch_bytes = patch.encode("utf-8", errors="surrogateescape")
            self.assertIn(b"+after secret \x80\n", patch_bytes)

            evidence = EvidenceStore(root / "run")
            evidence.write_diff(patch)
            self.assertEqual((root / "run" / "diff.patch").read_bytes(), patch_bytes)

            redacted = EvidenceStore(root / "redacted-run", secret="secret")
            redacted.write_diff(patch)
            redacted_bytes = (root / "redacted-run" / "diff.patch").read_bytes()
            self.assertIn(b"+after [REDACTED] \x80\n", redacted_bytes)
            self.assertNotIn(b"secret", redacted_bytes)

    def test_diff_preserves_crlf_bytes_for_applicable_patch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, worktree = self._repository_with_worktree(
                root, initial_content=b"before\r\nline\r\n"
            )
            (worktree / "file.txt").write_bytes(b"after\r\nline\r\n")

            patch = diff_text(worktree)
            patch_bytes = patch.encode("utf-8", errors="surrogateescape")
            self.assertIn(b"-before\r\n", patch_bytes)
            self.assertIn(b"+after\r\n", patch_bytes)
            patch_path = root / "change.patch"
            patch_path.write_bytes(patch_bytes)
            checked = subprocess.run(
                ["git", "apply", "--check", str(patch_path)],
                cwd=source,
                check=False,
                capture_output=True,
            )
            self.assertEqual(
                checked.returncode,
                0,
                checked.stderr.decode("utf-8", errors="replace"),
            )

    def test_diff_quotes_colon_in_git_object_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _source, worktree = self._repository_with_worktree(
                root, source_name="source:repo"
            )
            (worktree / "file.txt").write_text("after\n", encoding="utf-8")

            patch = diff_text(worktree)

            self.assertIn("+after", patch)


if __name__ == "__main__":
    unittest.main()
