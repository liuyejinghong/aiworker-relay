"""Regression tests for fail-closed Profile verification persistence."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from orchestrator.config import ProfileStore
from orchestrator.models import Profile


class ProfileVerificationTests(unittest.TestCase):
    def test_put_cannot_self_assert_verified_but_reload_preserves_existing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "profiles.json"
            path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "profiles": [
                            {
                                "id": "existing",
                                "model": "provider/existing",
                                "verification": "verified",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            store = ProfileStore(path)
            self.assertEqual(store.get("existing").verification, "verified")

            candidate = Profile(
                id="candidate",
                model="provider/candidate",
                verification="verified",
            )
            returned = store.put(candidate)
            self.assertEqual(returned.verification, "unverified")
            self.assertEqual(store.get("candidate").verification, "unverified")
            self.assertEqual(store.get("existing").verification, "verified")

            reloaded = ProfileStore(path)
            self.assertEqual(reloaded.get("candidate").verification, "unverified")
            self.assertEqual(reloaded.get("existing").verification, "verified")


if __name__ == "__main__":
    unittest.main()
