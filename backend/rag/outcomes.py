import re
from collections.abc import Mapping
from enum import StrEnum


class RetrievalOutcome(StrEnum):
    """Stable domain outcomes produced only after a healthy retrieval flow."""

    ANSWERABLE = "ANSWERABLE"
    NO_KNOWLEDGE = "NO_KNOWLEDGE"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"


def outcome_for_status(status: str) -> RetrievalOutcome:
    if status == "answerable":
        return RetrievalOutcome.ANSWERABLE
    if status == "no_knowledge":
        return RetrievalOutcome.NO_KNOWLEDGE
    return RetrievalOutcome.INSUFFICIENT_EVIDENCE


def _trace(result: Mapping) -> Mapping:
    value = result.get("rag_trace")
    return value if isinstance(value, Mapping) else {}


def coverage_gap_codes(result: Mapping) -> tuple[str, ...]:
    trace = _trace(result)
    values = trace.get("coverage_gap_codes") or result.get("coverage_gap_codes") or []
    if isinstance(values, (str, bytes)):
        return ()
    return tuple(
        dict.fromkeys(
            str(value)
            for value in values
            if re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", str(value))
        )
    )[:8]


def coverage_gap_questions(result: Mapping) -> tuple[str, ...]:
    trace = _trace(result)
    values = (
        trace.get("coverage_gap_questions")
        or result.get("coverage_gap_questions")
        or []
    )
    if isinstance(values, (str, bytes)):
        return ()
    questions = []
    for value in values:
        compact = " ".join(str(value).split())[:240]
        if compact and compact not in questions:
            questions.append(compact)
    return tuple(questions[:8])


def outcome_for_result(result: Mapping) -> RetrievalOutcome:
    trace = _trace(result)
    docs = result.get("docs") or []
    explicit = result.get("retrieval_outcome") or trace.get("retrieval_outcome")
    try:
        resolved = RetrievalOutcome(str(explicit))
    except ValueError:
        resolved = None
    if resolved is not None:
        if resolved == RetrievalOutcome.ANSWERABLE and not docs:
            return RetrievalOutcome.INSUFFICIENT_EVIDENCE
        return resolved

    status = result.get("retrieval_status") or trace.get("retrieval_status")
    route = result.get("route") or trace.get("route")
    if status == "no_knowledge" or route == "no_knowledge":
        return RetrievalOutcome.NO_KNOWLEDGE
    if (
        status in {"partial", "insufficient_evidence", "provider_failed"}
        or route in {"insufficient_evidence", "provider_failed"}
        or coverage_gap_codes(result)
        or coverage_gap_questions(result)
    ):
        return RetrievalOutcome.INSUFFICIENT_EVIDENCE
    return RetrievalOutcome.ANSWERABLE if docs else RetrievalOutcome.NO_KNOWLEDGE


def partial_evidence_instruction(result: Mapping) -> str:
    if outcome_for_result(result) != RetrievalOutcome.INSUFFICIENT_EVIDENCE:
        return ""
    details = []
    codes = coverage_gap_codes(result)
    questions = coverage_gap_questions(result)
    if codes:
        details.append("Provider 缺口代码：" + ", ".join(codes))
    if questions:
        details.append("未覆盖子问题：" + "；".join(questions))
    suffix = "；" + "；".join(details) if details else ""
    return (
        "检索证据只覆盖了问题的一部分。只能回答已有证据支持的部分，"
        f"并必须明确披露未覆盖范围，不能声称知识库没有答案{suffix}。"
    )


def retrieval_user_message(result: Mapping) -> str | None:
    if result.get("docs"):
        return None
    outcome = outcome_for_result(result)
    if outcome == RetrievalOutcome.NO_KNOWLEDGE:
        return "知识库中没有找到可靠的相关信息，暂时无法基于知识库回答这个问题。"
    if outcome == RetrievalOutcome.INSUFFICIENT_EVIDENCE:
        instruction = partial_evidence_instruction(result)
        return "当前证据不足以可靠回答这个问题。" + instruction
    return "当前没有可用于回答的可靠证据。"


__all__ = [
    "RetrievalOutcome",
    "coverage_gap_codes",
    "coverage_gap_questions",
    "outcome_for_result",
    "outcome_for_status",
    "partial_evidence_instruction",
    "retrieval_user_message",
]
