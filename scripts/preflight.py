#!/usr/bin/env python3
"""Local operator preflight without disclosing secret values."""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REQUIRED = ("DATABASE_URL", "APP_ORIGIN", "SESSION_SECRET", "SOURCE_STORAGE_ROOT")
EXPECTED_API_HOST = "api.ai-solverthon-2026-imt.party"
EXPECTED_API_SERVICE = "http://127.0.0.1:8000"


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


def run_check(command: list[str], *, cwd: Path = ROOT) -> tuple[bool, str]:
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            capture_output=True,
            check=False,
            text=True,
            timeout=30,
            env=os.environ,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, type(exc).__name__
    output = "\n".join(part.strip() for part in (completed.stdout, completed.stderr) if part.strip())
    return completed.returncode == 0, output


def resolve_repo_path(raw_path: str) -> Path:
    path = Path(raw_path)
    return path if path.is_absolute() else ROOT / path


def main() -> None:
    load_env()
    os.environ.setdefault("UV_CACHE_DIR", "/tmp/solverthon-uv-cache")
    failures: list[str] = []
    for key in REQUIRED:
        if not os.environ.get(key):
            failures.append(f"missing {key}")
    if len(os.environ.get("SESSION_SECRET", "")) < 32:
        failures.append("SESSION_SECRET must be at least 32 characters")
    commands = ("docker", "uv", "node", "codex", "cloudflared")
    for command in commands:
        if not shutil.which(command):
            failures.append(f"missing command: {command}")
    storage = Path(os.environ.get("SOURCE_STORAGE_ROOT", ROOT / "storage/sources"))
    if not storage.is_absolute():
        storage = ROOT / storage
    storage.mkdir(parents=True, exist_ok=True)
    if not os.access(storage, os.W_OK):
        failures.append("source storage is not writable")
    postgres_open = port_open("127.0.0.1", 5432)
    print(f"postgres_port={'open' if postgres_open else 'closed'}")
    if not postgres_open:
        failures.append("PostgreSQL is not reachable on 127.0.0.1:5432")

    if shutil.which("docker"):
        db_ok, _ = run_check(
            [
                "docker",
                "compose",
                "exec",
                "-T",
                "db",
                "pg_isready",
                "-U",
                "solverthon",
                "-d",
                "solverthon",
            ]
        )
        print(f"postgres_health={'ok' if db_ok else 'error'}")
        if not db_ok:
            failures.append("PostgreSQL Compose health check failed")

    if shutil.which("uv"):
        migration_ok, migration_output = run_check(
            ["uv", "run", "alembic", "check"], cwd=ROOT / "backend"
        )
        print(f"alembic_check={'ok' if migration_ok else 'error'}")
        if not migration_ok:
            last_line = migration_output.splitlines()[-1:] or ["unknown error"]
            failures.append(f"Alembic check failed: {last_line[0]}")

    if shutil.which("codex"):
        codex_ok, _ = run_check(["codex", "login", "status"])
        print(f"codex_login={'ok' if codex_ok else 'error'}")
        if not codex_ok:
            failures.append("Codex CLI is not logged in")

    tunnel_raw = os.environ.get(
        "CLOUDFLARE_TUNNEL_CONFIG", "./infra/cloudflared/config.local.yml"
    )
    tunnel_config = resolve_repo_path(tunnel_raw)
    if not tunnel_config.is_file():
        print("cloudflare_ingress=missing")
        failures.append("Cloudflare Tunnel config is missing")
    elif shutil.which("cloudflared"):
        tunnel_ok, _ = run_check(
            ["cloudflared", "tunnel", "--config", str(tunnel_config), "ingress", "validate"]
        )
        config_text = tunnel_config.read_text(encoding="utf-8")
        route_ok = (
            EXPECTED_API_HOST in config_text
            and EXPECTED_API_SERVICE in config_text
            and "http_status:404" in config_text
        )
        ingress_ok = tunnel_ok and route_ok
        print(f"cloudflare_ingress={'ok' if ingress_ok else 'error'}")
        if not ingress_ok:
            failures.append("Cloudflare ingress must target the API hostname and end in 404")

    vercel_config = ROOT / "frontend" / "vercel.json"
    if not vercel_config.is_file():
        print("vercel_rewrite=missing")
        failures.append("frontend/vercel.json is missing")
    else:
        vercel_text = vercel_config.read_text(encoding="utf-8")
        rewrite_ok = "/api/v1/:path*" in vercel_text and EXPECTED_API_HOST in vercel_text
        print(f"vercel_rewrite={'ok' if rewrite_ok else 'error'}")
        if not rewrite_ok:
            failures.append("Vercel API rewrite target is invalid")

    print(f"bizinfo_credential={'present' if os.environ.get('BIZINFO_API_KEY') else 'missing'}")
    if failures:
        for failure in failures:
            print(f"ERROR: {failure}")
        raise SystemExit(1)
    print("preflight=ok")


if __name__ == "__main__":
    main()
