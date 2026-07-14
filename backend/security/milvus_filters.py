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
