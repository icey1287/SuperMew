from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

import httpx
import pytest
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, ValidationError

from backend.agent.models import ModelRegistry
from backend.core.settings import ModelSettings
from backend.model_control import (
    ModelCatalogSnapshot,
    ModelRole,
    build_model_catalog_snapshot,
)
from backend.providers import ProviderError
from backend.rag.pipeline import _invoke_structured_model
from tests.support import TEST_MODEL_SNAPSHOT


class Decision(BaseModel):
    relevant: bool


def snapshot_with_method(method):
    return build_model_catalog_snapshot(
        {
            role: spec.model_copy(update={"structured_output_method": method})
            for role, spec in TEST_MODEL_SNAPSHOT.assignments.items()
        }
    )


@pytest.mark.parametrize("method", ["json_schema", "function_calling"])
@pytest.mark.parametrize("reject", [False, True])
def test_frozen_method_controls_wire_protocol_without_automatic_fallback(
    method, reject
):
    requests = []

    def transport(request):
        payload = json.loads(request.content)
        requests.append(payload)
        if reject:
            return httpx.Response(400, json={"error": {"message": "unsupported mode"}})
        message = {"role": "assistant", "content": '{"relevant": true}'}
        finish_reason = "stop"
        if method == "function_calling":
            message = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_check",
                        "type": "function",
                        "function": {
                            "name": "Decision",
                            "arguments": '{"relevant": true}',
                        },
                    }
                ],
            }
            finish_reason = "tool_calls"
        return httpx.Response(
            200,
            json={
                "id": "check",
                "object": "chat.completion",
                "created": 0,
                "model": "test-model",
                "choices": [
                    {"index": 0, "message": message, "finish_reason": finish_reason}
                ],
            },
        )

    with httpx.Client(transport=httpx.MockTransport(transport)) as client:

        def initialize(**kwargs):
            kwargs.pop("model_provider")
            return ChatOpenAI(**kwargs, http_client=client)

        registry = ModelRegistry(
            settings=SimpleNamespace(
                models=ModelSettings(_env_file=None, ARK_API_KEY="offline-test")
            ),
            initializer=initialize,
        )
        snapshot = snapshot_with_method(method)
        model = registry.get(ModelRole.GRADER, snapshot=snapshot)

        def invoke():
            return _invoke_structured_model(
                {"model_snapshot": snapshot.model_dump(mode="json")},
                role=ModelRole.GRADER,
                model=model,
                schema=Decision,
                messages=[{"role": "user", "content": "Is this relevant?"}],
                provider="offline-test",
                timeout_seconds=5,
            )

        if reject:
            with pytest.raises(ProviderError):
                invoke()
        else:
            assert invoke() == Decision(relevant=True)
    assert len(requests) == 1
    if method == "json_schema":
        assert requests[0]["response_format"]["type"] == "json_schema"
        assert "tools" not in requests[0]
    else:
        assert requests[0]["tool_choice"]["function"]["name"] == "Decision"
        assert "response_format" not in requests[0]


def test_v1_snapshot_keeps_existing_hash_and_rejects_method_tampering():
    payload = TEST_MODEL_SNAPSHOT.model_dump(mode="json")
    for spec in payload["assignments"].values():
        del spec["structured_output_method"]
    old_hash = hashlib.sha256(
        json.dumps(
            payload["assignments"],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
    ).hexdigest()
    assert payload["catalog_hash"] == old_hash
    restored = ModelCatalogSnapshot.model_validate(payload)
    assert restored.require(ModelRole.FAST).structured_output_method == "json_schema"
    assert restored.catalog_hash == old_hash
    assert snapshot_with_method("function_calling").catalog_hash != old_hash

    payload["assignments"]["fast"]["structured_output_method"] = "function_calling"
    with pytest.raises(ValidationError, match="hash does not match"):
        ModelCatalogSnapshot.model_validate(payload)
    payload["assignments"]["fast"]["structured_output_method"] = "auto"
    with pytest.raises(ValidationError, match="Input should be"):
        ModelCatalogSnapshot.model_validate(payload)


@pytest.mark.parametrize("method", ["json_schema", "function_calling"])
def test_query_rewrite_uses_frozen_fast_mode(monkeypatch, method):
    import backend.rag.utils as utils

    observed = []

    class Model:
        def with_structured_output(self, schema, *, method):
            observed.append(method)
            self.schema = schema
            return self

        def invoke(self, messages):
            return self.schema(
                method="step_back",
                step_back_question="What is deployment?",
                hyde_document="",
            )

    monkeypatch.setattr(utils, "_get_rewrite_model", lambda snapshot: Model())
    result = utils.rewrite_query_once(
        "How do I deploy?", model_snapshot=snapshot_with_method(method)
    )
    assert result["rewrite_method"] == "step_back"
    assert observed == [method]
