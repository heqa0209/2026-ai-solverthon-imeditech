from __future__ import annotations

import asyncio
from typing import Annotated

import typer

from app.auth import create_user, reset_password
from app.config import get_settings
from app.db import SessionFactory
from app.pipeline.cli import register_pipeline_commands
from app.pipeline.service import DatabasePipelineCLIService

app = typer.Typer(help="AI Solverthon administration CLI")
user_app = typer.Typer(help="Manage pre-created login users")
app.add_typer(user_app, name="user")
pipeline_settings = get_settings()
register_pipeline_commands(
    app,
    DatabasePipelineCLIService(
        SessionFactory,
        fixture_root=pipeline_settings.demo_fixture_root,
        source_storage_root=pipeline_settings.source_storage_root,
    ),
)


async def _create(username: str, password: str) -> None:
    async with SessionFactory() as db:
        user = await create_user(db, username, password)
    typer.echo(f"created user {user.username} ({user.id})")


async def _reset(username: str, password: str) -> None:
    async with SessionFactory() as db:
        user = await reset_password(db, username, password)
    typer.echo(f"reset password and revoked sessions for {user.username} ({user.id})")


@user_app.command("create")
def create_command(
    username: Annotated[str, typer.Option(prompt=True)],
    password: Annotated[str, typer.Option(prompt=True, hide_input=True, confirmation_prompt=True)],
) -> None:
    asyncio.run(_create(username, password))


@user_app.command("reset-password")
def reset_command(
    username: Annotated[str, typer.Option(prompt=True)],
    password: Annotated[str, typer.Option(prompt=True, hide_input=True, confirmation_prompt=True)],
) -> None:
    asyncio.run(_reset(username, password))


if __name__ == "__main__":
    app()
