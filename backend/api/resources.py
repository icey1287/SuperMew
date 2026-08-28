from pathlib import Path

from backend.core.settings import get_settings
from backend.documents import DocumentCatalog
from backend.documents.publication import (
    DocumentPublication,
    DocumentRetirementOutcome,
)
from backend.documents.retrieval import DocumentRetrievalScope
from backend.indexing.document_loader import DocumentLoader
from backend.indexing.embedding import embedding_service
from backend.indexing.milvus_client import get_milvus_store
from backend.indexing.milvus_writer import MilvusWriter
from backend.indexing.parent_chunk_store import ParentChunkStore


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
) -> DocumentRetirementOutcome:
    """原子撤销 Catalog scope，并把物理清理留给持久 worker。"""

    return document_publication.retire(filename)


def ensure_upload_dir() -> None:
    Path(UPLOAD_DIR).mkdir(parents=True, exist_ok=True)
