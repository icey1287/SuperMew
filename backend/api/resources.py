from pathlib import Path

from backend.core.settings import get_settings
from backend.documents import DocumentCatalog
from backend.documents.publication import (
    DocumentPublication,
    DocumentRetirementOutcome,
)
from backend.documents.retrieval import DocumentRetrievalScope
from backend.indexing import (
    DocumentLoader,
    MilvusWriter,
    ParentChunkStore,
    embedding_service,
)
from backend.indexing.milvus_client import get_milvus_store


UPLOAD_DIR = get_settings().storage.upload_dir

loader = DocumentLoader()
parent_chunk_store = ParentChunkStore()
milvus_manager = get_milvus_store()
milvus_writer = MilvusWriter(
    embedding_service=embedding_service,
    milvus_manager=milvus_manager,
)
document_catalog = DocumentCatalog()
document_publication = DocumentPublication(
    catalog=document_catalog,
    loader=loader,
    parent_store=parent_chunk_store,
    writer=milvus_writer,
)
document_retrieval_scope = DocumentRetrievalScope(document_catalog)


def delete_document_transactionally(
    filename: str,
    job_manager=None,
    job_id=None,
    owner_id: int | None = None,
) -> DocumentRetirementOutcome:
    """先原子撤销 Catalog current scope，再物理清理对应版本。"""

    if job_manager and job_id:
        job_manager.update_step(
            job_id,
            "prepare",
            50,
            "running",
            "正在原子撤销文档的可检索版本",
        )
    outcome = document_publication.retire(filename, owner_id=owner_id)

    if job_manager and job_id:
        job_manager.complete_step(
            job_id,
            "prepare",
            "Catalog scope 与 legacy 删除封印均已持久化",
        )
    return outcome


def ensure_upload_dir() -> None:
    Path(UPLOAD_DIR).mkdir(parents=True, exist_ok=True)
