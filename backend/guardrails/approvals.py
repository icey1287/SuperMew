"""Run-bound approval grant contract used at the Tool execution Seam."""

from __future__ import annotations

import re
from dataclasses import dataclass, field


_TOOL_NAME_RE = re.compile(r"[a-z][a-z0-9_.-]{0,127}")


def _complete(value: object, *, field_name: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value.encode("utf-8", errors="replace")) > 512
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
    ):
        raise ValueError(f"{field_name} must be complete")
    return value


@dataclass(frozen=True, slots=True, repr=False)
class RunToolApprovalGrant:
    """Trusted names-only approval snapshot bound to one complete Run identity."""

    user_id: str
    tenant_id: str
    thread_id: str
    run_id: str
    tool_names: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        for field_name in ("user_id", "tenant_id", "thread_id", "run_id"):
            object.__setattr__(
                self,
                field_name,
                _complete(getattr(self, field_name), field_name=field_name),
            )
        try:
            names = frozenset(self.tool_names)
        except TypeError as exc:
            raise TypeError("tool_names must be an iterable of strings") from exc
        if any(
            not isinstance(name, str) or _TOOL_NAME_RE.fullmatch(name) is None
            for name in names
        ):
            raise ValueError("tool_names contains an invalid tool identifier")
        object.__setattr__(self, "tool_names", names)

    def is_bound_to(
        self,
        *,
        user_id: str,
        tenant_id: str,
        thread_id: str,
        run_id: str,
    ) -> bool:
        return (
            self.user_id == user_id
            and self.tenant_id == tenant_id
            and self.thread_id == thread_id
            and self.run_id == run_id
        )

    def allows(
        self,
        tool_name: str,
        *,
        user_id: str,
        tenant_id: str,
        thread_id: str,
        run_id: str,
    ) -> bool:
        return (
            self.is_bound_to(
                user_id=user_id,
                tenant_id=tenant_id,
                thread_id=thread_id,
                run_id=run_id,
            )
            and tool_name in self.tool_names
        )

    def __repr__(self) -> str:
        return f"RunToolApprovalGrant(tool_count={len(self.tool_names)}, bound=True)"


__all__ = ["RunToolApprovalGrant"]
