import json
import subprocess
import sys
import unittest
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from backend.events import RunEventType, RunEventV1, new_run_event


ROOT = Path(__file__).resolve().parents[1]


class RunEventContractTests(unittest.TestCase):
    def test_schema_enum_matches_generated_python_enum(self):
        schema = json.loads(
            (ROOT / "contracts" / "run_event_v1.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            schema["properties"]["type"]["enum"],
            [item.value for item in RunEventType],
        )

    def test_valid_event_round_trips_with_json_field_names(self):
        event = new_run_event(
            sequence=1,
            run_id="run_abc",
            thread_id="thread-1",
            event_type=RunEventType.RUN_CREATED,
            data={"status": "pending"},
            timestamp=datetime(2026, 7, 14, tzinfo=UTC),
        )
        payload = event.model_dump(mode="json")
        self.assertEqual(1, payload["schema_version"])
        self.assertEqual("run.created", payload["type"])
        self.assertEqual(event, RunEventV1.model_validate(payload))

    def test_extra_fields_and_invalid_sequence_are_rejected(self):
        payload = {
            "schema_version": 1,
            "event_id": "evt_abc",
            "sequence": 0,
            "run_id": "run_abc",
            "thread_id": "thread-1",
            "type": "run.created",
            "timestamp": "2026-07-14T00:00:00Z",
            "data": {},
            "unexpected": True,
        }
        with self.assertRaises(ValidationError):
            RunEventV1.model_validate(payload)

    def test_naive_timestamp_is_rejected(self):
        with self.assertRaises(ValidationError):
            new_run_event(
                sequence=1,
                run_id="run_abc",
                thread_id="thread-1",
                event_type="run.created",
                timestamp=datetime(2026, 7, 14),
            )

    def test_generated_files_are_current(self):
        result = subprocess.run(
            [sys.executable, "scripts/generate_contract_types.py", "--check"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(0, result.returncode, result.stderr)


if __name__ == "__main__":
    unittest.main()
