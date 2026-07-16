import json
import math
import subprocess
import sys
import unittest
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError as JsonSchemaValidationError
from pydantic import ValidationError

from backend.tools.catalog import build_default_tool_registry
from backend.tools.contracts import new_tool_failure, new_tool_success
from backend.tools.generated import (
    TOOL_RESULT_V1_SCHEMA,
    ToolArtifactV1,
    ToolResultV1,
)
from scripts.generate_contract_types import render_tool_result_python


ROOT = Path(__file__).resolve().parents[1]


class ToolResultContractTests(unittest.TestCase):
    def test_success_and_failure_round_trip_as_json(self):
        artifact = ToolArtifactV1(
            artifact_id="art_report_1",
            name="report.json",
            media_type="application/json",
            uri="/api/artifacts/art_report_1",
            size_bytes=42,
            sha256="a" * 64,
            metadata={"source": "sql", "pages": [1, 2]},
        )
        success = new_tool_success(
            data={"rows": [{"answer": 42}]},
            duration_ms=15,
            artifacts=[artifact],
            observability_metadata={"trace_id": "trace_1"},
        )
        failure = new_tool_failure(
            error_code="TOOL_TIMEOUT",
            retryable=True,
            duration_ms=3000,
            data={"stage": "execute"},
        )

        success_payload = json.loads(success.model_dump_json())
        failure_payload = json.loads(failure.model_dump_json())
        self.assertEqual(success, ToolResultV1.model_validate(success_payload))
        self.assertEqual(failure, ToolResultV1.model_validate(failure_payload))
        self.assertIsNone(success_payload["error_code"])
        self.assertFalse(success_payload["retryable"])
        self.assertEqual("TOOL_TIMEOUT", failure_payload["error_code"])

    def test_outcome_invariants_are_fail_closed(self):
        schema = json.loads(
            (ROOT / "contracts" / "tool_result_v1.json").read_text(encoding="utf-8")
        )
        validator = Draft202012Validator(schema)
        base = {
            "schema_version": 1,
            "data": None,
            "duration_ms": 0,
            "artifacts": [],
            "observability_metadata": {},
        }
        invalid_outcomes = [
            {"success": True, "error_code": "TOOL_FAILED", "retryable": False},
            {"success": True, "error_code": None, "retryable": True},
            {"success": False, "error_code": None, "retryable": False},
        ]

        for outcome in invalid_outcomes:
            payload = {**base, **outcome}
            with self.subTest(outcome=outcome):
                with self.assertRaises(JsonSchemaValidationError):
                    validator.validate(payload)
                with self.assertRaises(ValidationError):
                    ToolResultV1.model_validate(payload)

    def test_extra_fields_and_unsafe_artifact_references_are_rejected(self):
        artifact = {
            "artifact_id": "art_report_1",
            "name": "report.json",
            "media_type": "application/json",
        }
        invalid_artifacts = [
            {**artifact, "host_path": "/Users/example/private/report.json"},
            {**artifact, "uri": "/Users/example/private/report.json"},
            {**artifact, "uri": "file:///tmp/report.json"},
        ]

        for payload in invalid_artifacts:
            with self.subTest(payload=payload), self.assertRaises(ValidationError):
                ToolArtifactV1.model_validate(payload)

        with self.assertRaises(ValidationError):
            ToolResultV1.model_validate(
                {
                    "schema_version": 1,
                    "success": True,
                    "data": None,
                    "error_code": None,
                    "retryable": False,
                    "duration_ms": 0,
                    "artifacts": [],
                    "observability_metadata": {},
                    "unexpected": True,
                }
            )

    def test_artifact_uri_shape_has_json_schema_and_pydantic_parity(self):
        schema = json.loads(
            (ROOT / "contracts" / "tool_result_v1.json").read_text(encoding="utf-8")
        )["$defs"]["toolArtifact"]
        payload = {
            "artifact_id": "art_report_1",
            "name": "report.json",
            "media_type": "application/json",
            "uri": "artifact://art_different",
        }

        Draft202012Validator(schema).validate(payload)
        self.assertEqual(
            payload["uri"],
            ToolArtifactV1.model_validate(payload).uri,
        )

    def test_generated_model_and_json_schema_reject_the_same_type_drift(self):
        schema = json.loads(
            (ROOT / "contracts" / "tool_result_v1.json").read_text(encoding="utf-8")
        )
        validator = Draft202012Validator(schema)
        valid = {
            "schema_version": 1,
            "success": True,
            "data": None,
            "error_code": None,
            "retryable": False,
            "duration_ms": 3,
            "artifacts": [],
            "observability_metadata": {},
        }
        invalid_payloads = [
            {key: value for key, value in valid.items() if key != "schema_version"},
            {**valid, "duration_ms": "3"},
            {**valid, "duration_ms": True},
            {**valid, "schema_version": "1"},
            {**valid, "success": 1},
            {**valid, "retryable": 0},
            {**valid, "error_code": "lowercase_code"},
        ]

        for payload in invalid_payloads:
            with self.subTest(payload=payload):
                with self.assertRaises(JsonSchemaValidationError):
                    validator.validate(payload)
                with self.assertRaises(ValidationError):
                    ToolResultV1.model_validate(payload)

    def test_control_descriptors_use_the_generated_tool_result_schema(self):
        registry = build_default_tool_registry()
        canonical_schema = json.loads(
            (ROOT / "contracts" / "tool_result_v1.json").read_text(encoding="utf-8")
        )
        self.assertEqual(canonical_schema, TOOL_RESULT_V1_SCHEMA)
        payloads = [
            new_tool_success(data={"tools": [], "count": 0}).model_dump(mode="json"),
            new_tool_failure(
                error_code="SKILL_NOT_AVAILABLE",
                retryable=False,
                data={"message": "unavailable"},
            ).model_dump(mode="json"),
        ]

        for name in ("describe_skill", "tool_search"):
            descriptor = registry.descriptor(name)
            self.assertIsNotNone(descriptor)
            self.assertEqual(canonical_schema, descriptor.output_schema)
            validator = Draft202012Validator(descriptor.output_schema)
            for payload in payloads:
                with self.subTest(tool=name, success=payload["success"]):
                    validator.validate(payload)

    def test_generator_rejects_unhandled_schema_shape_drift(self):
        schema = json.loads(
            (ROOT / "contracts" / "tool_result_v1.json").read_text(encoding="utf-8")
        )
        mutations = []
        success_type = deepcopy(schema)
        success_type["properties"]["success"]["type"] = "integer"
        mutations.append(success_type)
        missing_constraint = deepcopy(schema)
        del missing_constraint["properties"]["error_code"]["maxLength"]
        mutations.append(missing_constraint)
        relaxed_artifact = deepcopy(schema)
        relaxed_artifact["$defs"]["toolArtifact"]["additionalProperties"] = True
        mutations.append(relaxed_artifact)
        missing_invariant = deepcopy(schema)
        missing_invariant.pop("allOf")
        mutations.append(missing_invariant)

        for mutation in mutations:
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                render_tool_result_python(mutation)

    def test_non_json_values_are_rejected(self):
        for value in (datetime.now(UTC), object(), {"invalid": math.nan}):
            with (
                self.subTest(value=type(value).__name__),
                self.assertRaises(ValidationError),
            ):
                new_tool_success(data=value)  # type: ignore[arg-type]

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
