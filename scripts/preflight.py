#!/usr/bin/env python3
"""Local operator preflight without disclosing secret values."""

from __future__ import annotations

import os
import shutil
import socket
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REQUIRED = ("DATABASE_URL", "APP_ORIGIN", "SESSION_SECRET", "SOURCE_STORAGE_ROOT")


def load_env() -> None:
    env_path = ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        if not line or line.lstrip().startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key, value)


def port_open(host: str, port: int) -> bool:
    with socket.socket() as sock:
        sock.settimeout(0.3)
        return sock.connect_ex((host, port)) == 0


def main() -> None:
    load_env()
    failures: list[str] = []
    for key in REQUIRED:
        if not os.environ.get(key):
            failures.append(f"missing {key}")
    if len(os.environ.get("SESSION_SECRET", "")) < 32:
        failures.append("SESSION_SECRET must be at least 32 characters")
    for command in ("docker", "uv", "node", "codex", "cloudflared"):
        if not shutil.which(command):
            failures.append(f"missing command: {command}")
    storage = Path(os.environ.get("SOURCE_STORAGE_ROOT", ROOT / "storage/sources"))
    if not storage.is_absolute():
        storage = ROOT / storage
    storage.mkdir(parents=True, exist_ok=True)
    if not os.access(storage, os.W_OK):
        failures.append("source storage is not writable")
    print(f"postgres_port={'open' if port_open('127.0.0.1', 5432) else 'closed'}")
    print(f"bizinfo_credential={'present' if os.environ.get('BIZINFO_API_KEY') else 'missing'}")
    if failures:
        for failure in failures:
            print(f"ERROR: {failure}")
        raise SystemExit(1)
    print("preflight=ok")


if __name__ == "__main__":
    main()
