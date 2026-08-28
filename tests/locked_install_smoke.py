"""Create and verify one fresh runtime through the production installer path."""

from __future__ import annotations

import json
import sys
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
LAUNCHER_PATH = (
    REPOSITORY_ROOT
    / "plugins"
    / "aiworker-relay"
    / "scripts"
    / "launch_external_workers.py"
)


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit("usage: locked_install_smoke.py RUNTIME_ROOT")
    runtime = Path(sys.argv[1]).resolve()
    if runtime.exists():
        raise SystemExit(f"runtime target already exists: {runtime}")

    spec = spec_from_file_location("locked_install_launcher", LAUNCHER_PATH)
    if spec is None or spec.loader is None:
        raise SystemExit("could not load Plugin launcher")
    launcher = module_from_spec(spec)
    sys.modules[spec.name] = launcher
    spec.loader.exec_module(launcher)

    version = launcher.bundle_version()
    source_fingerprint = launcher.bundle_source_fingerprint()
    orch = launcher._install_runtime_at(
        runtime,
        expected_version=version,
        expected_source_fingerprint=source_fingerprint,
    )
    identity = launcher._read_runtime_identity(runtime, version=version)
    if identity is None or identity.get("schema_version") != 2:
        raise SystemExit("installed runtime identity is unavailable")
    if identity.get("source_fingerprint") != source_fingerprint:
        raise SystemExit("installed source identity does not match the Plugin bundle")
    if not orch.is_file():
        raise SystemExit("installed orch entrypoint is unavailable")

    print(
        json.dumps(
            {
                "dependency_lock": identity["dependency_lock"],
                "dependency_lock_sha256": identity["dependency_lock_sha256"],
                "package_count": len(identity["packages"]),
                "python_version": identity["python_version"],
                "source_fingerprint": source_fingerprint,
                "version": version,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
