from pathlib import Path

from app.config import REPO_ROOT, Settings


def test_repository_relative_runtime_paths_are_independent_of_process_cwd() -> None:
    settings = Settings(
        source_storage_root=Path("storage/sources"),
        demo_fixture_root=Path("fixtures/demo"),
    )

    assert settings.source_storage_root == REPO_ROOT / "storage/sources"
    assert settings.demo_fixture_root == REPO_ROOT / "fixtures/demo"
