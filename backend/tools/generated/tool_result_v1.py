# Generated from contracts/tool_result_v1.json. Do not edit by hand.
from __future__ import annotations

import json
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    model_validator,
)


TOOL_RESULT_V1_SCHEMA: dict[str, object] = json.loads(
    r"""{
  "$defs": {
    "toolArtifact": {
      "additionalProperties": false,
      "properties": {
        "artifact_id": {
          "maxLength": 80,
          "pattern": "^art_[A-Za-z0-9_-]+$",
          "type": "string"
        },
        "media_type": {
          "maxLength": 120,
          "pattern": "^[A-Za-z0-9][A-Za-z0-9.+-]*/[A-Za-z0-9][A-Za-z0-9.+-]*$",
          "type": "string"
        },
        "metadata": {
          "additionalProperties": true,
          "type": "object"
        },
        "name": {
          "maxLength": 255,
          "minLength": 1,
          "type": "string"
        },
        "sha256": {
          "pattern": "^[a-f0-9]{64}$",
          "type": [
            "string",
            "null"
          ]
        },
        "size_bytes": {
          "minimum": 0,
          "type": [
            "integer",
            "null"
          ]
        },
        "uri": {
          "maxLength": 200,
          "pattern": "^(artifact://art_[A-Za-z0-9_-]+|/api/artifacts/art_[A-Za-z0-9_-]+)$",
          "type": [
            "string",
            "null"
          ]
        }
      },
      "required": [
        "artifact_id",
        "name",
        "media_type"
      ],
      "type": "object"
    }
  },
  "$id": "https://supermew.local/contracts/tool_result_v1.json",
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "additionalProperties": false,
  "allOf": [
    {
      "else": {
        "properties": {
          "error_code": {
            "type": "string"
          }
        }
      },
      "if": {
        "properties": {
          "success": {
            "const": true
          }
        },
        "required": [
          "success"
        ]
      },
      "then": {
        "properties": {
          "error_code": {
            "const": null
          },
          "retryable": {
            "const": false
          }
        }
      }
    }
  ],
  "properties": {
    "artifacts": {
      "items": {
        "$ref": "#/$defs/toolArtifact"
      },
      "type": "array"
    },
    "data": {},
    "duration_ms": {
      "minimum": 0,
      "type": "integer"
    },
    "error_code": {
      "maxLength": 64,
      "pattern": "^[A-Z][A-Z0-9_]{0,63}$",
      "type": [
        "string",
        "null"
      ]
    },
    "observability_metadata": {
      "additionalProperties": true,
      "type": "object"
    },
    "retryable": {
      "type": "boolean"
    },
    "schema_version": {
      "const": 1,
      "type": "integer"
    },
    "success": {
      "type": "boolean"
    }
  },
  "required": [
    "schema_version",
    "success",
    "data",
    "error_code",
    "retryable",
    "duration_ms",
    "artifacts",
    "observability_metadata"
  ],
  "title": "SuperMew Tool Result v1",
  "type": "object"
}"""
)


class ToolArtifactV1(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, strict=True)

    artifact_id: str = Field(
        pattern="^art_[A-Za-z0-9_-]+$",
        max_length=80,
    )
    name: str = Field(
        min_length=1,
        max_length=255,
    )
    media_type: str = Field(
        pattern="^[A-Za-z0-9][A-Za-z0-9.+-]*/[A-Za-z0-9][A-Za-z0-9.+-]*$",
        max_length=120,
    )
    uri: str | None = Field(
        default=None,
        pattern="^(artifact://art_[A-Za-z0-9_-]+|/api/artifacts/art_[A-Za-z0-9_-]+)$",
        max_length=200,
    )
    size_bytes: int | None = Field(default=None, ge=0)
    sha256: str | None = Field(default=None, pattern="^[a-f0-9]{64}$")
    metadata: dict[str, JsonValue] = Field(default_factory=dict)


class ToolResultV1(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, strict=True)

    schema_version: Literal[1]
    success: bool
    data: JsonValue
    error_code: str | None = Field(
        pattern="^[A-Z][A-Z0-9_]{0,63}$",
        max_length=64,
    )
    retryable: bool
    duration_ms: int = Field(ge=0)
    artifacts: list[ToolArtifactV1]
    observability_metadata: dict[str, JsonValue]

    @model_validator(mode="after")
    def outcome_fields_must_be_consistent(self) -> ToolResultV1:
        if self.success:
            if self.error_code is not None:
                raise ValueError("successful tool result cannot have error_code")
            if self.retryable:
                raise ValueError("successful tool result cannot be retryable")
        elif self.error_code is None:
            raise ValueError("failed tool result must have error_code")
        return self
