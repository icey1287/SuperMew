from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from backend.core.settings import AppSettings, get_settings


_SEPARATOR = "\n\n---\n\n"
_TRUNCATION_MARKER = "\n…[evidence truncated by context budget]"


@dataclass(frozen=True)
class EvidencePack:
    """A bounded, citation-stable projection of ranked Evidence."""

    documents: tuple[dict[str, Any], ...]
    text: str
    omitted_count: int
    truncated_count: int
    characters: int


def rag_evidence_character_budget(settings: AppSettings | None = None) -> int:
    effective = settings or get_settings()
    return max(int(effective.rag.max_context_tokens), 1)


def agent_evidence_character_budget(settings: AppSettings | None = None) -> int:
    """Reserve at least half of the Agent input budget for instructions and history."""

    effective = settings or get_settings()
    input_budget = max(int(effective.agent.input_token_budget), 1)
    evidence_budget = max(input_budget // 2, 256)
    return min(rag_evidence_character_budget(effective), evidence_budget)


def grader_evidence_character_budget(settings: AppSettings | None = None) -> int:
    """Use a compact evidence projection for routing and answerability grading."""

    effective = settings or get_settings()
    return max(int(effective.rag.grader_evidence_characters), 1)


def grader_max_document_character_budget(
    settings: AppSettings | None = None,
) -> int:
    effective = settings or get_settings()
    return max(int(effective.rag.grader_max_document_characters), 1)


def _document_parts(
    document: Mapping[str, Any],
    *,
    max_document_characters: int | None,
) -> tuple[dict[str, Any], str, bool]:
    projected = dict(document)
    source = str(document.get("filename") or "Unknown")[:240]
    page = str(document.get("page_number") or "N/A")[:40]
    text = str(document.get("text") or "")
    truncated = False
    if max_document_characters is not None and len(text) > max_document_characters:
        text = text[: max(max_document_characters - len(_TRUNCATION_MARKER), 1)]
        text += _TRUNCATION_MARKER
        projected["text"] = text
        truncated = True
    rendered = f"{source} (Page {page}):\n{text}"
    return projected, rendered, truncated


def pack_evidence(
    documents: Sequence[Mapping[str, Any]],
    *,
    maximum_characters: int,
    max_document_characters: int | None = None,
) -> EvidencePack:
    """Pack ranked documents without cutting a chunk unless none can fit whole."""

    maximum = max(int(maximum_characters), 1)
    per_document = (
        None
        if max_document_characters is None
        else max(int(max_document_characters), 1)
    )
    selected: list[tuple[dict[str, Any], str]] = []
    used = 0
    omitted = 0
    truncated = 0

    for document in documents:
        if not isinstance(document, Mapping):
            omitted += 1
            continue
        projected, body, was_truncated = _document_parts(
            document,
            max_document_characters=per_document,
        )
        citation = len(selected) + 1
        rendered = f"[{citation}] {body}"
        required = len(rendered) + (len(_SEPARATOR) if selected else 0)
        if used + required > maximum:
            omitted += 1
            continue
        selected.append((projected, rendered))
        used += required
        truncated += int(was_truncated)

    if not selected:
        first = next(
            (item for item in documents if isinstance(item, Mapping)),
            None,
        )
        if first is not None:
            projected, body, _ = _document_parts(
                first,
                max_document_characters=None,
            )
            prefix = "[1] "
            available = max(maximum - len(prefix) - len(_TRUNCATION_MARKER), 1)
            rendered = prefix + body[:available] + _TRUNCATION_MARKER
            projected["text"] = str(first.get("text") or "")[:available]
            projected["text"] += _TRUNCATION_MARKER
            selected.append((projected, rendered[:maximum]))
            omitted = max(len(documents) - 1, 0)
            truncated = 1

    text = _SEPARATOR.join(rendered for _, rendered in selected)
    return EvidencePack(
        documents=tuple(document for document, _ in selected),
        text=text,
        omitted_count=omitted,
        truncated_count=truncated,
        characters=len(text),
    )


__all__ = [
    "EvidencePack",
    "agent_evidence_character_budget",
    "grader_evidence_character_budget",
    "grader_max_document_character_budget",
    "pack_evidence",
    "rag_evidence_character_budget",
]
