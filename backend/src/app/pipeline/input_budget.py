from __future__ import annotations

from dataclasses import dataclass

MAX_AI_INPUT_CHARS = 180_000


@dataclass(frozen=True)
class InputDocument:
    source_file_id: str
    source_priority: int
    text: str
    relevance_rank: int = 0


@dataclass(frozen=True)
class BoundedInput:
    text: str
    included_source_file_ids: tuple[str, ...]
    omitted_source_file_ids: tuple[str, ...]
    truncated: bool
    error_code: str | None


def build_bounded_input(
    documents: list[InputDocument],
    *,
    max_chars: int = MAX_AI_INPUT_CHARS,
) -> BoundedInput:
    if max_chars <= 0:
        raise ValueError("max_chars must be positive")
    ordered = sorted(
        documents,
        key=lambda item: (
            -item.relevance_rank,
            -item.source_priority,
            len(item.text),
            item.source_file_id,
        ),
    )
    chunks: list[str] = []
    included: list[str] = []
    omitted: list[str] = []
    used = 0
    for document in ordered:
        delimiter = f"<source id={document.source_file_id!r}>\n"
        suffix = "\n</source>"
        chunk = f"{delimiter}{document.text}{suffix}"
        remaining = max_chars - used
        if len(chunk) <= remaining:
            chunks.append(chunk)
            included.append(document.source_file_id)
            used += len(chunk)
        else:
            omitted.append(document.source_file_id)
    truncated = bool(omitted)
    return BoundedInput(
        text="\n".join(chunks),
        included_source_file_ids=tuple(included),
        omitted_source_file_ids=tuple(omitted),
        truncated=truncated,
        error_code="ATTACHMENT_INPUT_TRUNCATED" if truncated else None,
    )
