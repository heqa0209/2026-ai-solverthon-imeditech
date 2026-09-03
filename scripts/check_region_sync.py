#!/usr/bin/env python3
"""Verify provenance and byte-identical deployed region copies."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CANONICAL = ROOT / "data/legal-regions-20260720.json"
META = ROOT / "data/legal-regions-20260720.meta.json"
COPIES = (
    ROOT / "backend/src/app/data/legal-regions-20260720.json",
    ROOT / "frontend/src/data/legal-regions-20260720.json",
)


def main() -> None:
    content = CANONICAL.read_bytes()
    expected = json.loads(META.read_text(encoding="utf-8"))["sha256"]
    actual = hashlib.sha256(content).hexdigest()
    if actual != expected:
        raise SystemExit(f"canonical SHA-256 mismatch: expected {expected}, got {actual}")
    for copy in COPIES:
        if copy.read_bytes() != content:
            raise SystemExit(f"region copy differs: {copy.relative_to(ROOT)}")
    payload = json.loads(content)
    if len(payload["regions"]) < 200:
        raise SystemExit("region catalog is unexpectedly incomplete")
    print(f"region data OK: {len(payload['regions'])} rows, sha256={actual}")


if __name__ == "__main__":
    main()
