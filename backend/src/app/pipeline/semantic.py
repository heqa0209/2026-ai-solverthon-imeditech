from __future__ import annotations

from typing import Any

from app.pipeline.hashing import canonical_json_bytes, sha256_bytes


def semantic_answer_fingerprint(answer: dict[str, Any] | None) -> str | None:
    if answer is None:
        return None
    return sha256_bytes(canonical_json_bytes(answer))


def semantic_input_fingerprint(
    *,
    analysis_run_id: str,
    answer_fingerprints: dict[str, str],
    selected_role_key: str | None,
) -> str:
    return sha256_bytes(
        canonical_json_bytes(
            {
                "analysis_run_id": analysis_run_id,
                "answer_fingerprints": answer_fingerprints,
                "selected_role_key": selected_role_key,
            }
        )
    )
