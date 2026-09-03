from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Protocol

import typer

from app.pipeline.fixtures import load_fixture_manifest


@dataclass(frozen=True)
class CommandPreview:
    target_id: str
    target_version: str | None
    expected_count: int
    transmitted_fields: tuple[str, ...] = ()
    model_efforts: tuple[str, ...] = ()


class PipelineCLIService(Protocol):
    async def preview(self, operation: str, target_id: str | None) -> CommandPreview: ...

    async def execute(self, operation: str, target_id: str | None) -> int: ...

    async def job_status(self, job_id: str) -> str: ...

    async def retry_job(self, job_id: str) -> bool: ...

    async def load_fixture(self, manifest_path: Path) -> int: ...

    async def run_holdout(self, manifest_path: Path) -> tuple[int, bool]: ...


def _preview(preview: CommandPreview) -> None:
    typer.echo(f"target={preview.target_id}")
    typer.echo(f"version={preview.target_version or '-'}")
    typer.echo(f"expected_count={preview.expected_count}")
    if preview.transmitted_fields:
        typer.echo(f"transmitted_fields={','.join(preview.transmitted_fields)}")
    if preview.model_efforts:
        typer.echo(f"model_efforts={','.join(preview.model_efforts)}")


def register_pipeline_commands(root: typer.Typer, service: PipelineCLIService) -> None:
    """Attach pipeline commands without owning the application's central CLI module."""

    collect = typer.Typer(help="Collect official Bizinfo announcements")
    announcement = typer.Typer(help="Analyze one announcement")
    job = typer.Typer(help="Inspect and retry background jobs")
    fixture = typer.Typer(help="Load immutable demo fixtures")
    acceptance = typer.Typer(help="Run acceptance datasets")
    root.add_typer(collect, name="collect")
    root.add_typer(announcement, name="announcement")
    root.add_typer(job, name="job")
    root.add_typer(fixture, name="fixture")
    root.add_typer(acceptance, name="acceptance")

    def execute_scoped(operation: str, target_id: str | None, yes: bool) -> None:
        preview = asyncio.run(service.preview(operation, target_id))
        _preview(preview)
        if preview.expected_count == 0:
            raise typer.Exit(code=2)
        if not yes and not typer.confirm("Proceed with exactly this scope?"):
            raise typer.Abort()
        count = asyncio.run(service.execute(operation, target_id))
        typer.echo(f"processed={count}")
        if count == 0:
            raise typer.Exit(code=2)

    @collect.command("run")
    def collect_run(yes: bool = typer.Option(False, "--yes")) -> None:
        execute_scoped("collect.run", None, yes)

    @collect.command("reconcile")
    def collect_reconcile(yes: bool = typer.Option(False, "--yes")) -> None:
        execute_scoped("collect.reconcile", None, yes)

    @announcement.command("analyze")
    def analyze(
        announcement_id: str = typer.Option(..., "--announcement-id"),
        yes: bool = typer.Option(False, "--yes"),
    ) -> None:
        execute_scoped("announcement.analyze", announcement_id, yes)

    @announcement.command("reanalyze")
    def reanalyze(
        announcement_id: str = typer.Option(..., "--announcement-id"),
        yes: bool = typer.Option(False, "--yes"),
    ) -> None:
        execute_scoped("announcement.reanalyze", announcement_id, yes)

    @job.command("status")
    def job_status(job_id: str = typer.Option(..., "--job-id")) -> None:
        typer.echo(asyncio.run(service.job_status(job_id)))

    @job.command("retry")
    def job_retry(
        job_id: str = typer.Option(..., "--job-id"),
        yes: bool = typer.Option(False, "--yes"),
    ) -> None:
        preview = asyncio.run(service.preview("job.retry", job_id))
        _preview(preview)
        if not yes and not typer.confirm("Retry exactly this job?"):
            raise typer.Abort()
        if not asyncio.run(service.retry_job(job_id)):
            raise typer.Exit(code=2)

    @fixture.command("load")
    def fixture_load(manifest: Annotated[Path, typer.Option("--manifest")]) -> None:
        load_fixture_manifest(manifest)
        count = asyncio.run(service.load_fixture(manifest))
        typer.echo(f"processed={count}")
        if count == 0:
            raise typer.Exit(code=2)

    @acceptance.command("holdout")
    def holdout(manifest: Annotated[Path, typer.Option("--manifest")]) -> None:
        sample_size, passed = asyncio.run(service.run_holdout(manifest))
        typer.echo(f"sample_size={sample_size} passed={str(passed).lower()}")
        if sample_size < 5 or not passed:
            raise typer.Exit(code=2)
