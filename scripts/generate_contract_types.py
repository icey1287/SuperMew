from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "contracts" / "run_event_v1.json"
PYTHON_PATH = ROOT / "backend" / "events" / "generated" / "run_event_v1.py"
TYPESCRIPT_PATH = ROOT / "frontend" / "src" / "types" / "generated" / "run-event-v1.ts"


def _load_schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def _enum_member(value: str) -> str:
    return value.upper().replace(".", "_").replace("-", "_")


def render_python(schema: dict) -> str:
    event_types = schema["properties"]["type"]["enum"]
    members = "\n".join(f'    {_enum_member(value)} = "{value}"' for value in event_types)
    return f'''# Generated from contracts/run_event_v1.json. Do not edit by hand.
from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class RunEventType(StrEnum):
{members}


class RunEventV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    event_id: str = Field(pattern=r"^evt_[A-Za-z0-9_-]+$", max_length=80)
    sequence: int = Field(ge=1)
    run_id: str = Field(pattern=r"^run_[A-Za-z0-9_-]+$", max_length=64)
    thread_id: str = Field(min_length=1, max_length=120)
    type: RunEventType
    timestamp: datetime
    data: dict[str, Any] = Field(default_factory=dict)

    @field_validator("timestamp")
    @classmethod
    def timestamp_must_have_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamp must include timezone")
        return value
'''


def render_typescript(schema: dict) -> str:
    event_types = schema["properties"]["type"]["enum"]
    union = "\n".join(f"  | '{value}'" for value in event_types)
    return f'''// Generated from contracts/run_event_v1.json. Do not edit by hand.
export type RunEventType =
{union};

export interface RunEventV1<TData extends Record<string, unknown> = Record<string, unknown>> {{
  schema_version: 1;
  event_id: string;
  sequence: number;
  run_id: string;
  thread_id: string;
  type: RunEventType;
  timestamp: string;
  data: TData;
}}
'''


def _sync(path: Path, content: str, *, check: bool) -> bool:
    current = path.read_text(encoding="utf-8") if path.exists() else None
    if current == content:
        return True
    if check:
        print(f"generated contract is stale: {path.relative_to(ROOT)}", file=sys.stderr)
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    schema = _load_schema()
    results = [
        _sync(PYTHON_PATH, render_python(schema), check=args.check),
        _sync(TYPESCRIPT_PATH, render_typescript(schema), check=args.check),
    ]
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
