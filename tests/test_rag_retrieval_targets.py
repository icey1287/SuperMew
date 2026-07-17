from types import SimpleNamespace

import pytest

from backend.providers import ProviderCode, ProviderError, ProviderExecutor
from test_rag_latency_guards import load_utils


def _utils(**overrides):
    env = {
        "RERANK_MODEL": "",
        "RERANK_BINDING_HOST": "",
        "RERANK_API_KEY": "",
        "AUTO_MERGE_ENABLED": "false",
    }
    env.update(overrides)
    utils, _embedding = load_utils(env)
    utils._provider_executor = ProviderExecutor(sleeper=lambda _seconds: None)
    return utils


def _target(
    collection: str,
    *,
    required: bool = True,
    filter_expr: str = "chunk_level == 3",
):
    return SimpleNamespace(
        collection_name=collection,
        filter_expr=filter_expr,
        required=required,
    )


def _snapshot(*targets, index_id="index-current"):
    return SimpleNamespace(
        tenant_id="tenant-a",
        index_id=index_id,
        targets=tuple(targets),
    )


class Scope:
    def __init__(self, snapshot=None, error=None):
        self.snapshot = snapshot
        self.error = error
        self.calls = []

    def resolve(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return self.snapshot


class Embedding:
    def __init__(self):
        self.calls = []

    def embed_query(self, query, **kwargs):
        self.calls.append((query, kwargs))
        return [0.1, 0.2]


class TargetStore:
    def __init__(self, *, exists=True, hybrid=None, hybrid_error=None, dense=None):
        self.exists = exists
        self.hybrid = list(hybrid or [])
        self.hybrid_error = hybrid_error
        self.dense = list(dense or [])
        self.has_calls = 0
        self.hybrid_calls = []
        self.dense_calls = []

    def has_collection(self):
        self.has_calls += 1
        return self.exists

    def hybrid_retrieve(self, **kwargs):
        self.hybrid_calls.append(kwargs)
        if self.hybrid_error:
            raise self.hybrid_error
        return list(self.hybrid)

    def dense_retrieve(self, **kwargs):
        self.dense_calls.append(kwargs)
        return list(self.dense)


class RoutedStore:
    def __init__(self, stores):
        self.stores = dict(stores)
        self.requested = []

    def with_collection(self, name):
        self.requested.append(name)
        return self.stores[name]


def _doc(chunk_id, text, score=0.9):
    return {
        "chunk_id": chunk_id,
        "text": text,
        "filename": "guide.pdf",
        "page_number": 1,
        "score": score,
    }


def test_versioned_only_uses_snapshot_index_and_exact_target_filter():
    utils = _utils()
    target = _target(
        "catalog_v1",
        filter_expr='tenant_id == "tenant-a" and document_version_id in ["v2"]',
    )
    scope = Scope(_snapshot(target, index_id="manifest-fingerprint"))
    embedding = Embedding()
    store = TargetStore(hybrid=[_doc("v2::chunk-1", "versioned evidence")])
    utils._document_retrieval_scope = scope
    utils._embedding_service = embedding
    utils._milvus_manager = RoutedStore({"catalog_v1": store})

    result = utils.retrieve_documents("question", top_k=2, tenant_id="tenant-a")

    assert [item["chunk_id"] for item in result["docs"]] == ["v2::chunk-1"]
    assert scope.calls == [
        {
            "tenant_id": "tenant-a",
            "knowledge_base_id": None,
            "leaf_chunk_level": 3,
        }
    ]
    embedding_scope = embedding.calls[0][1]["scope"]
    assert embedding_scope.tenant_id == "tenant-a"
    assert embedding_scope.index_id == "manifest-fingerprint"
    assert store.hybrid_calls[0]["filter_expr"] == target.filter_expr
    assert result["meta"]["retrieval_index_id"] == "manifest-fingerprint"
    assert result["meta"]["retrieval_target_count"] == 1


def test_zero_target_snapshot_short_circuits_without_embedding_or_milvus():
    utils = _utils()
    embedding = Embedding()
    utils._document_retrieval_scope = Scope(_snapshot(index_id="empty-index"))
    utils._embedding_service = embedding

    class ForbiddenMilvus:
        def with_collection(self, _name):
            raise AssertionError("empty catalog must not touch Milvus")

    utils._milvus_manager = ForbiddenMilvus()

    result = utils.retrieve_documents("question", top_k=2)

    assert result["docs"] == []
    assert result["meta"]["retrieval_mode"] == "catalog_empty"
    assert result["meta"]["retrieval_index_id"] == "empty-index"
    assert result["meta"]["retrieval_target_count"] == 0
    assert embedding.calls == []


def test_multiple_catalog_collections_are_routed_and_deduplicated():
    utils = _utils()
    primary = _target("catalog_v1")
    archive = _target("archive_catalog_v1", required=False)
    utils._document_retrieval_scope = Scope(_snapshot(primary, archive))
    utils._embedding_service = Embedding()
    utils._milvus_manager = RoutedStore(
        {
            "catalog_v1": TargetStore(
                hybrid=[
                    _doc("versioned-1", "new evidence"),
                    _doc("shared", "shared evidence", 0.8),
                ]
            ),
            "archive_catalog_v1": TargetStore(
                hybrid=[
                    _doc("archive-1", "archive evidence"),
                    _doc("shared", "shared evidence", 0.7),
                ]
            ),
        }
    )

    result = utils.retrieve_documents("question", top_k=5)

    assert [item["chunk_id"] for item in result["docs"]] == [
        "versioned-1",
        "archive-1",
        "shared",
    ]
    assert result["meta"]["recall_count"] == 4
    assert result["meta"]["deduplicated_recall_count"] == 3
    assert result["meta"]["retrieval_optional_target_count"] == 1
    assert result["meta"]["retrieval_optional_missing_count"] == 0


def test_multi_collection_hybrid_fallback_is_isolated_to_one_target():
    utils = _utils()
    first = _target("catalog_a")
    second = _target("catalog_b")
    first_store = TargetStore(hybrid=[_doc("a-1", "alpha")])
    second_store = TargetStore(
        hybrid_error=utils.HybridRetrievalUnsupported(),
        dense=[_doc("b-1", "beta")],
    )
    utils._document_retrieval_scope = Scope(_snapshot(first, second))
    utils._embedding_service = Embedding()
    utils._milvus_manager = RoutedStore(
        {"catalog_a": first_store, "catalog_b": second_store}
    )

    result = utils.retrieve_documents("question", top_k=3)

    assert [item["chunk_id"] for item in result["docs"]] == ["a-1", "b-1"]
    assert first_store.dense_calls == []
    assert len(second_store.dense_calls) == 1
    assert result["meta"]["retrieval_mode"] == "hybrid_dense_fusion"
    assert result["meta"]["retrieval_degraded_code"] == ("HYBRID_RETRIEVAL_DEGRADED")


def test_multi_collection_requires_an_adapter_that_can_route_targets():
    utils = _utils()
    utils._document_retrieval_scope = Scope(
        _snapshot(_target("catalog_a"), _target("catalog_b"))
    )
    utils._embedding_service = Embedding()

    class UnroutedStore:
        calls = 0

        def hybrid_retrieve(self, **_kwargs):
            self.calls += 1
            return []

    store = UnroutedStore()
    utils._milvus_manager = store

    with pytest.raises(ProviderError) as raised:
        utils.retrieve_documents("question", top_k=1)

    assert raised.value.code == ProviderCode.VECTOR_STORE_UNAVAILABLE
    assert store.calls == 0


def test_required_missing_collection_is_typed_provider_failure():
    utils = _utils()
    utils._document_retrieval_scope = Scope(_snapshot(_target("required")))
    utils._embedding_service = Embedding()
    store = TargetStore(exists=False)
    utils._milvus_manager = RoutedStore({"required": store})

    with pytest.raises(ProviderError) as raised:
        utils.retrieve_documents("question", top_k=1)

    assert raised.value.code == ProviderCode.VECTOR_STORE_UNAVAILABLE
    assert store.has_calls == 2


def test_optional_missing_catalog_collection_is_a_healthy_skip():
    utils = _utils()
    archive = _target("archive_catalog_v1", required=False)
    utils._document_retrieval_scope = Scope(_snapshot(archive))
    utils._embedding_service = Embedding()
    utils._milvus_manager = RoutedStore(
        {"archive_catalog_v1": TargetStore(exists=False)}
    )

    result = utils.retrieve_documents("question", top_k=1)

    assert result["docs"] == []
    assert result["meta"]["retrieval_mode"] == "catalog_empty"
    assert result["meta"]["retrieval_optional_missing_count"] == 1
    assert result["meta"]["retrieval_empty"] is True


def test_catalog_failure_is_typed_before_embedding_and_never_becomes_empty():
    utils = _utils()
    scope = Scope(error=ConnectionError("catalog unavailable"))
    embedding = Embedding()
    utils._document_retrieval_scope = scope
    utils._embedding_service = embedding

    with pytest.raises(ProviderError) as raised:
        utils.retrieve_documents("question", top_k=1)

    assert raised.value.code == ProviderCode.VECTOR_STORE_UNAVAILABLE
    assert len(scope.calls) == 2
    assert embedding.calls == []


def test_parent_expansion_failure_is_typed_and_retried():
    utils = _utils(AUTO_MERGE_ENABLED="true", AUTO_MERGE_THRESHOLD="2")
    first = _doc("leaf-1", "first")
    second = _doc("leaf-2", "second")
    first["parent_chunk_id"] = "parent-1"
    second["parent_chunk_id"] = "parent-1"
    utils._document_retrieval_scope = Scope(_snapshot(_target("catalog")))
    utils._embedding_service = Embedding()
    utils._milvus_manager = RoutedStore(
        {"catalog": TargetStore(hybrid=[first, second])}
    )

    class BrokenParentStore:
        def __init__(self):
            self.calls = 0

        def get_documents_by_ids(self, _chunk_ids):
            self.calls += 1
            raise ConnectionError("parent store unavailable")

    parent_store = BrokenParentStore()
    utils._parent_chunk_store = parent_store

    with pytest.raises(ProviderError) as raised:
        utils.retrieve_documents("question", top_k=2)

    assert raised.value.code == ProviderCode.VECTOR_STORE_UNAVAILABLE
    assert parent_store.calls == 2


def test_parent_expansion_observes_shared_retrieval_deadline(monkeypatch):
    utils = _utils(AUTO_MERGE_ENABLED="true", AUTO_MERGE_THRESHOLD="2")
    first = _doc("leaf-1", "first")
    second = _doc("leaf-2", "second")
    first["parent_chunk_id"] = "parent-1"
    second["parent_chunk_id"] = "parent-1"
    utils._document_retrieval_scope = Scope(_snapshot(_target("catalog")))
    utils._embedding_service = Embedding()
    utils._milvus_manager = RoutedStore(
        {"catalog": TargetStore(hybrid=[first, second])}
    )

    class Clock:
        now = 0.0

        def monotonic(self):
            return self.now

    clock = Clock()

    class SlowParentStore:
        def get_documents_by_ids(self, _chunk_ids):
            clock.now = 11.0
            return []

    utils._parent_chunk_store = SlowParentStore()
    utils._provider_executor = ProviderExecutor(
        clock=clock.monotonic,
        sleeper=lambda seconds: setattr(clock, "now", clock.now + seconds),
    )
    monkeypatch.setattr(utils.time, "monotonic", clock.monotonic)

    with pytest.raises(ProviderError) as raised:
        utils.retrieve_documents("question", top_k=2)

    assert raised.value.code == ProviderCode.PROVIDER_TIMEOUT


def test_parent_expansion_observes_cancellation_after_store_call():
    utils = _utils(AUTO_MERGE_ENABLED="true", AUTO_MERGE_THRESHOLD="2")
    first = _doc("leaf-1", "first")
    second = _doc("leaf-2", "second")
    first["parent_chunk_id"] = "parent-1"
    second["parent_chunk_id"] = "parent-1"
    utils._document_retrieval_scope = Scope(_snapshot(_target("catalog")))
    utils._embedding_service = Embedding()
    utils._milvus_manager = RoutedStore(
        {"catalog": TargetStore(hybrid=[first, second])}
    )
    cancelled = False

    class CancellingParentStore:
        def get_documents_by_ids(self, _chunk_ids):
            nonlocal cancelled
            cancelled = True
            return []

    utils._parent_chunk_store = CancellingParentStore()

    with pytest.raises(utils.asyncio.CancelledError):
        utils.retrieve_documents(
            "question",
            top_k=2,
            cancellation=lambda: cancelled,
        )
