#!/usr/bin/env python3
"""Fail when high-risk secret material is committed to Git."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAX_TEXT_BYTES = 2_000_000
RISKY_NAMES = re.compile(
    r"(^|/)(\.env($|\.)|.*credentials.*\.json$|.*\.pem$|.*\.p12$|.*\.key$)", re.I
)
ASSIGNMENT = re.compile(
    r"(?m)^\s*(?:export\s+)?"
    r"(BIZINFO_API_KEY|SESSION_SECRET|CLOUDFLARE_API_TOKEN|VERCEL_TOKEN|OPENAI_API_KEY)"
    r"\s*[=:]\s*[\"']?([^\s\"'#]+)"
)
PRIVATE_KEY = "-----BEGIN " + "PRIVATE KEY-----"
SAFE_MARKERS = ("change-me", "replace", "example", "fixture", "fake", "test", "<", "${")


def tracked_files() -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=ROOT,
        capture_output=True,
        check=True,
    )
    return [ROOT / item.decode() for item in result.stdout.split(b"\0") if item]


def main() -> None:
    findings: list[str] = []
    for path in tracked_files():
        relative = path.relative_to(ROOT).as_posix()
        if RISKY_NAMES.search(relative) and relative != ".env.example":
            findings.append(f"high-risk tracked filename: {relative}")
            continue
        try:
            payload = path.read_bytes()
        except OSError as exc:
            findings.append(f"cannot inspect {relative}: {type(exc).__name__}")
            continue
        if len(payload) > MAX_TEXT_BYTES or b"\0" in payload:
            continue
        text = payload.decode("utf-8", errors="ignore")
        if PRIVATE_KEY in text:
            findings.append(f"private key marker: {relative}")
        for match in ASSIGNMENT.finditer(text):
            value = match.group(2).strip().lower()
            if not value or any(marker in value for marker in SAFE_MARKERS):
                continue
            findings.append(f"non-placeholder {match.group(1)} in {relative}:{text[:match.start()].count(chr(10)) + 1}")

    if findings:
        for finding in findings:
            print(f"ERROR: {finding}")
        raise SystemExit(1)
    print("tracked_secret_scan=ok")


if __name__ == "__main__":
    main()
