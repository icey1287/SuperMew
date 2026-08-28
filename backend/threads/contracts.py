from __future__ import annotations

import re
from typing import Annotated, Final
from uuid import uuid4

from pydantic import StringConstraints


THREAD_ID_PATTERN: Final = r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,119}$"
_THREAD_ID = re.compile(THREAD_ID_PATTERN)

ThreadId = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=120,
        pattern=THREAD_ID_PATTERN,
    ),
]


def validate_thread_id(value: str) -> str:
    if _THREAD_ID.fullmatch(value) is None:
        raise ValueError(
            "thread_id 必须以字母或数字开头，且只能包含字母、数字、_、.、:、-"
        )
    return value


def new_thread_id() -> str:
    return f"thread_{uuid4().hex}"


__all__ = [
    "THREAD_ID_PATTERN",
    "ThreadId",
    "new_thread_id",
    "validate_thread_id",
]
