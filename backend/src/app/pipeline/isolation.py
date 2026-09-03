from __future__ import annotations

import json
import tempfile
from pathlib import Path

from app.pipeline.ai import AIStage, build_codex_invocation

REQUIRED_DISABLED_FEATURES = {
    "shell_tool",
    "unified_exec",
    "apps",
    "browser_use",
    "computer_use",
    "multi_agent",
    "plugins",
}
FORBIDDEN_CHILD_ENV = {"BIZINFO_API_KEY", "DATABASE_URL", "SESSION_SECRET"}


def run_ai_runner_isolation_self_test(temp_root: Path) -> bool:
    """Verify the tool-less Codex child boundary without making a model call."""

    try:
        temp_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=temp_root, prefix="isolation-") as directory:
            job_root = Path(directory).resolve()
            schema = job_root / "self-test.schema.json"
            output = job_root / "self-test.output.json"
            schema.write_text(
                json.dumps(
                    {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {"ok": {"type": "boolean"}},
                        "required": ["ok"],
                    }
                ),
                encoding="utf-8",
            )
            invocation = build_codex_invocation(
                stage=AIStage.CONDITION_EXTRACTION,
                temp_dir=job_root,
                schema_path=schema,
                output_path=output,
                instruction="Return the self-test result.",
                structured_input={"ok": True},
                source_env={
                    "PATH": "/usr/bin:/bin",
                    "LANG": "C.UTF-8",
                    "BIZINFO_API_KEY": "must-not-pass",
                    "DATABASE_URL": "must-not-pass",
                    "SESSION_SECRET": "must-not-pass",
                },
            )
            disabled = {
                invocation.args[index + 1]
                for index, value in enumerate(invocation.args[:-1])
                if value == "--disable"
            }
            return (
                invocation.args[0:2] == ("codex", "exec")
                and "--ignore-user-config" in invocation.args
                and "--ignore-rules" in invocation.args
                and "--strict-config" in invocation.args
                and "--ephemeral" in invocation.args
                and "--sandbox" in invocation.args
                and invocation.args[invocation.args.index("--sandbox") + 1] == "read-only"
                and invocation.args[invocation.args.index("--cd") + 1] == str(job_root)
                and REQUIRED_DISABLED_FEATURES <= disabled
                and not (FORBIDDEN_CHILD_ENV & invocation.env.keys())
                and invocation.env.get("TMPDIR") == str(job_root)
            )
    except OSError, ValueError, KeyError:
        return False
