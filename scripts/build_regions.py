#!/usr/bin/env python3
"""Build the deployable region catalog from the official legal-dong text export."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

SOURCE_URL = "https://www.code.go.kr/etc/codeFullDown.do"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_regions(source: Path) -> list[dict[str, str | None]]:
    lines = source.read_text(encoding="cp949").splitlines()
    regions: list[dict[str, str | None]] = []
    province_names: dict[str, str] = {}
    for line in lines[1:]:
        parts = line.split("\t")
        if len(parts) < 3 or parts[2].strip() != "존재":
            continue
        code, full_name = parts[0].strip(), " ".join(parts[1].split())
        if len(code) != 10 or not code.isdigit():
            continue
        if code[2:] == "00000000":
            province_names[code[:2]] = full_name
            regions.append(
                {
                    "code": code,
                    "name": full_name,
                    "parentCode": None,
                    "parentName": None,
                    "level": "SIDO",
                }
            )
        elif code[2:5] != "000" and code[5:] == "00000":
            parent_code = f"{code[:2]}00000000"
            regions.append(
                {
                    "code": code,
                    "name": full_name,
                    "parentCode": parent_code,
                    "parentName": province_names.get(code[:2]),
                    "level": "SIGUNGU",
                }
            )
    return sorted(regions, key=lambda item: item["code"] or "")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("--source-archive", type=Path, required=True)
    parser.add_argument("--effective-date", default="2026-07-20")
    parser.add_argument("--retrieved-at", default="2026-09-03")
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    args = parser.parse_args()

    regions = parse_regions(args.source)
    if len(regions) < 200:
        raise SystemExit(f"refusing incomplete region catalog: {len(regions)} rows")

    payload = {
        "schemaVersion": 1,
        "effectiveDate": args.effective_date,
        "regions": regions,
    }
    encoded = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode()
    canonical = args.repo / "data/legal-regions-20260720.json"
    canonical.parent.mkdir(parents=True, exist_ok=True)
    canonical.write_bytes(encoded)

    digest = hashlib.sha256(encoded).hexdigest()
    meta = {
        "sourceUrl": SOURCE_URL,
        "sourceRetrievedAt": args.retrieved_at,
        "sourceArchiveSha256": sha256(args.source_archive),
        "effectiveDate": args.effective_date,
        "generatedAt": args.retrieved_at,
        "sha256": digest,
        "regionCount": len(regions),
    }
    (args.repo / "data/legal-regions-20260720.meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    for destination in (
        args.repo / "backend/src/app/data/legal-regions-20260720.json",
        args.repo / "frontend/src/data/legal-regions-20260720.json",
    ):
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(encoded)


if __name__ == "__main__":
    main()
