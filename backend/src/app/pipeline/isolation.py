from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class FilesystemIsolationPolicy:
    temp_root: Path
    forbidden_read_paths: tuple[Path, ...]
    forbidden_write_path: Path


def run_filesystem_isolation_self_test(policy: FilesystemIsolationPolicy) -> bool:
    """Validate OS-level permissions before a worker is allowed to claim jobs."""

    try:
        policy.temp_root.mkdir(parents=True, exist_ok=True)
        probe = policy.temp_root / f"isolation-probe-{os.getpid()}"
        probe.write_text("probe", encoding="utf-8")
        probe.unlink()
    except OSError:
        return False
    for path in policy.forbidden_read_paths:
        try:
            if path.is_dir():
                with os.scandir(path) as entries:
                    next(entries, None)
            else:
                with path.open("rb"):
                    pass
            return False
        except (PermissionError, FileNotFoundError):
            pass
    try:
        policy.forbidden_write_path.write_text("probe", encoding="utf-8")
    except OSError:
        return True
    else:
        policy.forbidden_write_path.unlink(missing_ok=True)
        return False
