from __future__ import annotations

import json
import re
from collections.abc import Iterable


_FIELD_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _field(name: str) -> str:
    if not _FIELD_RE.fullmatch(name):
        raise ValueError(f"invalid Milvus field name: {name!r}")
    return name


def string_literal(value: str) -> str:
    """使用 JSON 字符串转义生成 Milvus 可接受的安全字符串字面量。"""
    return json.dumps(str(value), ensure_ascii=False)


def _normalized_document_version_ids(values: Iterable[str]) -> list[str]:
    normalized_values: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, str):
            raise TypeError("document_version_ids must contain strings")
        normalized = value.strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        normalized_values.append(normalized)
    return normalized_values


def eq_filter(field: str, value: str | int | float | bool) -> str:
    safe_field = _field(field)
    if isinstance(value, str):
        literal = string_literal(value)
    elif isinstance(value, bool):
        literal = "true" if value else "false"
    elif isinstance(value, (int, float)):
        literal = str(value)
    else:
        raise TypeError(f"unsupported Milvus filter value: {type(value)!r}")
    return f"{safe_field} == {literal}"


def in_filter(field: str, values: Iterable[str | int | float]) -> str:
    safe_field = _field(field)
    literals: list[str] = []
    for value in values:
        if isinstance(value, str):
            literals.append(string_literal(value))
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            literals.append(str(value))
        else:
            raise TypeError(f"unsupported Milvus filter value: {type(value)!r}")
    if not literals:
        return "id < 0"
    return f"{safe_field} in [{', '.join(literals)}]"


def not_in_filter(field: str, values: Iterable[str | int | float]) -> str:
    """构造安全排除表达式；空集合等价于不排除任何记录。"""

    safe_field = _field(field)
    literals: list[str] = []
    for value in values:
        if isinstance(value, str):
            literals.append(string_literal(value))
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            literals.append(str(value))
        else:
            raise TypeError(f"unsupported Milvus filter value: {type(value)!r}")
    if not literals:
        return "id >= 0"
    return f"{safe_field} not in [{', '.join(literals)}]"


def and_filter(*expressions: str | None) -> str:
    """安全组合多个 Milvus 表达式；空组合默认拒绝全部记录。"""

    normalized = [
        expression.strip()
        for expression in expressions
        if expression is not None and expression.strip()
    ]
    if not normalized:
        return "id < 0"
    return " and ".join(f"({expression})" for expression in normalized)


def or_filter(*expressions: str | None) -> str:
    """安全组合备选表达式；没有备选项时默认拒绝全部记录。"""

    normalized = [
        expression.strip()
        for expression in expressions
        if expression is not None and expression.strip()
    ]
    if not normalized:
        return "id < 0"
    return " or ".join(f"({expression})" for expression in normalized)


def version_identity_filter(
    document_version_ids: Iterable[str],
    *,
    index_version: str | None = None,
) -> str:
    """构造可与租户/ACL scope 组合的版本身份表达式。"""

    expressions = [
        in_filter(
            "document_version_id",
            _normalized_document_version_ids(document_version_ids),
        )
    ]
    if index_version is not None:
        if not isinstance(index_version, str) or not index_version.strip():
            raise ValueError("index_version must be a non-empty string")
        expressions.append(eq_filter("index_version", index_version.strip()))
    return and_filter(*expressions)


def version_scope_filter(
    *,
    tenant_id: str,
    knowledge_base_id: str,
    document_id: str,
    document_version_ids: Iterable[str],
    index_version: str | None = None,
) -> str:
    """构造租户隔离的版本 scope；空版本集合会 fail-closed。"""

    scope = {
        "tenant_id": tenant_id,
        "knowledge_base_id": knowledge_base_id,
        "document_id": document_id,
    }
    for field, value in scope.items():
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{field} must be a non-empty string")

    version_ids = _normalized_document_version_ids(document_version_ids)

    expressions = [
        eq_filter("tenant_id", tenant_id.strip()),
        eq_filter("knowledge_base_id", knowledge_base_id.strip()),
        eq_filter("document_id", document_id.strip()),
        in_filter("document_version_id", version_ids),
    ]
    if index_version is not None:
        if not isinstance(index_version, str) or not index_version.strip():
            raise ValueError("index_version must be a non-empty string")
        expressions.append(eq_filter("index_version", index_version.strip()))
    return and_filter(*expressions)
