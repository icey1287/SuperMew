from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from backend.rag.evidence import (
    agent_evidence_character_budget,
    grader_evidence_character_budget,
    grader_max_document_character_budget,
    pack_evidence,
)
from backend.tools.knowledge import _render_rag_result


def _doc(name: str, text: str) -> dict:
    return {
        "filename": name,
        "page_number": 1,
        "text": text,
    }


def test_pack_evidence_keeps_complete_ranked_chunks_and_renumbers_citations():
    documents = [
        _doc("first.md", "a" * 60),
        _doc("oversized.md", "b" * 500),
        _doc("third.md", "c" * 40),
    ]

    packed = pack_evidence(documents, maximum_characters=180)

    assert len(packed.text) <= 180
    assert [item["filename"] for item in packed.documents] == [
        "first.md",
        "third.md",
    ]
    assert "[1] first.md" in packed.text
    assert "[2] third.md" in packed.text
    assert "oversized.md" not in packed.text
    assert packed.omitted_count == 1
    assert packed.truncated_count == 0


def test_pack_evidence_truncates_only_when_no_complete_chunk_can_fit():
    packed = pack_evidence(
        [_doc("large.md", "证据" * 500)],
        maximum_characters=160,
    )

    assert len(packed.text) <= 160
    assert packed.truncated_count == 1
    assert packed.omitted_count == 0
    assert "[evidence truncated by context budget]" in packed.text


def test_agent_evidence_budget_reserves_room_for_prompt_and_history():
    settings = SimpleNamespace(
        rag=SimpleNamespace(max_context_tokens=12_000),
        agent=SimpleNamespace(input_token_budget=9_952),
    )

    assert agent_evidence_character_budget(settings) == 4_976


def test_grader_evidence_budget_is_independent_from_answer_context():
    settings = SimpleNamespace(
        rag=SimpleNamespace(
            max_context_tokens=12_000,
            grader_evidence_characters=4_800,
            grader_max_document_characters=1_200,
        )
    )

    assert grader_evidence_character_budget(settings) == 4_800
    assert grader_max_document_character_budget(settings) == 1_200


def test_knowledge_tool_result_is_bounded_before_entering_agent_history():
    context = SimpleNamespace(
        store_checkpoint_pause=lambda _pause: None,
        store_rag_trace=lambda _trace, _resume: None,
    )
    result = {
        "docs": [_doc(f"doc-{index}.md", str(index) * 900) for index in range(6)],
        "route": "answer",
        "retrieval_status": "answerable",
        "retrieval_outcome": "ANSWERABLE",
        "rag_trace": {
            "route": "answer",
            "retrieval_status": "answerable",
            "retrieval_outcome": "ANSWERABLE",
        },
    }

    with patch(
        "backend.tools.knowledge.agent_evidence_character_budget",
        return_value=1_100,
    ):
        rendered = _render_rag_result(context, result)

    assert len(rendered) < 1_400
    assert "[1] doc-0.md" in rendered
    assert "doc-2.md" not in rendered
    assert "EVIDENCE_CONTEXT_NOTICE" in rendered
