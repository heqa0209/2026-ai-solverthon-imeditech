from __future__ import annotations

from pathlib import Path

import typer
from typer.testing import CliRunner

from app.pipeline.cli import CommandPreview, register_pipeline_commands


class FakeService:
    def __init__(self, expected_count: int = 1):
        self.expected_count = expected_count
        self.executed: list[tuple[str, str | None]] = []

    async def preview(self, operation: str, target_id: str | None) -> CommandPreview:
        return CommandPreview(
            target_id=target_id or "daily",
            target_version="v1",
            expected_count=self.expected_count,
            transmitted_fields=("companyName",),
            model_efforts=("gpt-5.6-luna:medium",),
        )

    async def execute(self, operation: str, target_id: str | None) -> int:
        self.executed.append((operation, target_id))
        return self.expected_count

    async def job_status(self, job_id: str) -> str:
        return f"job={job_id} status=QUEUED"

    async def retry_job(self, job_id: str) -> bool:
        return True

    async def load_fixture(self, manifest_path: Path) -> int:
        return 1

    async def run_holdout(self, manifest_path: Path) -> tuple[int, bool]:
        return 5, True


def _app(service: FakeService) -> typer.Typer:
    app = typer.Typer()
    register_pipeline_commands(app, service)
    return app


def test_analysis_command_prints_scope_models_and_transmitted_fields() -> None:
    service = FakeService()
    result = CliRunner().invoke(
        _app(service),
        ["announcement", "analyze", "--announcement-id", "announcement-1", "--yes"],
    )
    assert result.exit_code == 0, result.output
    assert "target=announcement-1" in result.output
    assert "expected_count=1" in result.output
    assert "transmitted_fields=companyName" in result.output
    assert "model_efforts=gpt-5.6-luna:medium" in result.output
    assert service.executed == [("announcement.analyze", "announcement-1")]
    assert "enqueued=1" in result.output


def test_scoped_command_does_not_report_zero_as_success() -> None:
    service = FakeService(expected_count=0)
    result = CliRunner().invoke(_app(service), ["collect", "run", "--yes"])
    assert result.exit_code == 2
    assert service.executed == []
