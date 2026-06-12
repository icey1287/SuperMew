"""文档加载和分片服务"""
import os
import re
import unicodedata
from typing import Dict, List

from langchain_community.document_loaders import Docx2txtLoader, PyPDFLoader, UnstructuredExcelLoader
from langchain_text_splitters import RecursiveCharacterTextSplitter

# 编译非打印 C0/C1 控制字符的正则（保留常规排版字：\t, \n, \r）
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
# 编译零宽字符和不可见格式化控制字符（零宽空白、BOM 标记、左右强排标志等）
_INVISIBLE_CHAR_RE = re.compile(r"[\u200b-\u200d\ufeff\u200f\u202a-\u202e]")


def sanitize_text(text: str) -> str:
    """
    企业级标准文本净化器 (Text Sanitizer)。
    1. 规范化 (Normalization)：统一转换为标准 NFC 格式，合并分离变音符和音调，确保多端字符表示合一。
    2. 剔除/替换不合法及不可见字节：过滤 NUL (0x00) 空字节、零宽字符、BOM 标签和不可见强排标记。
    3. 清洗非打印字符及乱码：剔除 C0/C1 控制符号，剥离 Unicode PUA 私有使用区乱码符号。
    4. 编码收敛防爆：利用 utf-8 ignore 安全剥离任何不完整的、孤立的 UTF-16 代理项（Surrogates）。
    """
    if not text:
        return ""
    
    # 1. 规范化为 Unicode NFC 格式
    text = unicodedata.normalize("NFC", text)
    
    # 2. 清除不可见零宽字符、BOM 及格式控制符
    text = _INVISIBLE_CHAR_RE.sub("", text)
    
    # 3. 清洗非打印控制符及 PUA 乱码框区字符
    text = _CONTROL_CHAR_RE.sub("", text)
    text = re.sub(r"[\ue000-\uf8ff]", "", text)
    
    # 4. 彻底擦除孤立代理项 (Surrogates)，收敛至 100% 合规的 UTF-8 (对应 PostgreSQL 的 utf8mb4 标准)
    try:
        cleaned = text.encode("utf-8", "ignore").decode("utf-8", "ignore")
    except Exception:
        chars = []
        for char in text:
            if 0xD800 <= ord(char) <= 0xDFFF:
                continue
            chars.append(char)
        cleaned = "".join(chars)
        
    return cleaned


class DocumentLoader:
    """文档加载和分片服务"""

    def __init__(self, chunk_size: int = 800, chunk_overlap: int = 100):
        level_1_size = max(2000, chunk_size * 3)
        level_1_overlap = max(400, chunk_overlap * 3)
        level_2_size = max(1000, chunk_size * 2)
        level_2_overlap = max(200, chunk_overlap * 2)
        level_3_size = max(600, chunk_size)
        level_3_overlap = max(100, chunk_overlap)

        self._splitter_level_1 = RecursiveCharacterTextSplitter(
            chunk_size=level_1_size,
            chunk_overlap=level_1_overlap,
            add_start_index=True,
            separators=["\n\n", "。", "！", "？", "\n", "，", "、", " ", ""],
        )
        self._splitter_level_2 = RecursiveCharacterTextSplitter(
            chunk_size=level_2_size,
            chunk_overlap=level_2_overlap,
            add_start_index=True,
            separators=["\n\n", "。", "！", "？", "\n", "，", "、", " ", ""],
        )
        self._splitter_level_3 = RecursiveCharacterTextSplitter(
            chunk_size=level_3_size,
            chunk_overlap=level_3_overlap,
            add_start_index=True,
            separators=["\n\n", "。", "！", "？", "\n", "，", "、", " ", ""],
        )

    @staticmethod
    def _build_chunk_id(filename: str, page_number: int, level: int, index: int) -> str:
        return f"{filename}::p{page_number}::l{level}::{index}"

    def _split_page_to_three_levels(
        self,
        text: str,
        base_doc: Dict,
        page_global_chunk_idx: int,
    ) -> List[Dict]:
        if not text:
            return []

        root_chunks: List[Dict] = []
        page_number = int(base_doc.get("page_number", 0))
        filename = base_doc["filename"]

        level_1_docs = self._splitter_level_1.create_documents([text], [base_doc])
        level_1_counter = 0
        level_2_counter = 0
        level_3_counter = 0

        for level_1_doc in level_1_docs:
            level_1_text = (level_1_doc.page_content or "").strip()
            if not level_1_text:
                continue
            level_1_id = self._build_chunk_id(filename, page_number, 1, level_1_counter)
            level_1_counter += 1

            level_1_chunk = {
                **base_doc,
                "text": level_1_text,
                "chunk_id": level_1_id,
                "parent_chunk_id": "",
                "root_chunk_id": level_1_id,
                "chunk_level": 1,
                "chunk_idx": page_global_chunk_idx,
            }
            page_global_chunk_idx += 1
            root_chunks.append(level_1_chunk)

            level_2_docs = self._splitter_level_2.create_documents([level_1_text], [base_doc])
            for level_2_doc in level_2_docs:
                level_2_text = (level_2_doc.page_content or "").strip()
                if not level_2_text:
                    continue
                level_2_id = self._build_chunk_id(filename, page_number, 2, level_2_counter)
                level_2_counter += 1

                level_2_chunk = {
                    **base_doc,
                    "text": level_2_text,
                    "chunk_id": level_2_id,
                    "parent_chunk_id": level_1_id,
                    "root_chunk_id": level_1_id,
                    "chunk_level": 2,
                    "chunk_idx": page_global_chunk_idx,
                }
                page_global_chunk_idx += 1
                root_chunks.append(level_2_chunk)

                level_3_docs = self._splitter_level_3.create_documents([level_2_text], [base_doc])
                for level_3_doc in level_3_docs:
                    level_3_text = (level_3_doc.page_content or "").strip()
                    if not level_3_text:
                        continue
                    level_3_id = self._build_chunk_id(filename, page_number, 3, level_3_counter)
                    level_3_counter += 1
                    root_chunks.append({
                        **base_doc,
                        "text": level_3_text,
                        "chunk_id": level_3_id,
                        "parent_chunk_id": level_2_id,
                        "root_chunk_id": level_1_id,
                        "chunk_level": 3,
                        "chunk_idx": page_global_chunk_idx,
                    })
                    page_global_chunk_idx += 1

        return root_chunks

    def _load_from_langchain_docs(
        self,
        raw_docs: list,
        file_path: str,
        filename: str,
        doc_type: str,
    ) -> list[dict]:
        documents: list[dict] = []
        page_global_chunk_idx = 0
        for doc in raw_docs:
            meta = getattr(doc, "metadata", None) or {}
            page_num = meta.get("page", 0)
            if page_num is None:
                page_num = 0
            try:
                page_num = int(page_num)
            except (TypeError, ValueError):
                page_num = 0
            base_doc = {
                "filename": sanitize_text(filename),
                "file_path": sanitize_text(file_path),
                "file_type": sanitize_text(doc_type),
                "page_number": page_num,
            }
            page_chunks = self._split_page_to_three_levels(
                text=sanitize_text((doc.page_content or "").strip()),
                base_doc=base_doc,
                page_global_chunk_idx=page_global_chunk_idx,
            )
            page_global_chunk_idx += len(page_chunks)
            documents.extend(page_chunks)
        return documents

    def load_document(self, file_path: str, filename: str) -> list[dict]:
        file_lower = filename.lower()

        if file_lower.endswith(".pdf"):
            doc_type = "PDF"
            loader = PyPDFLoader(file_path)
        elif file_lower.endswith((".docx", ".doc")):
            doc_type = "Word"
            loader = Docx2txtLoader(file_path)
        elif file_lower.endswith((".xlsx", ".xls")):
            doc_type = "Excel"
            loader = UnstructuredExcelLoader(file_path)
        elif file_lower.endswith((".html", ".htm")):
            doc_type = "HTML"
            from backend.indexing.html_processor import load_html_for_document_loader

            raw_docs = load_html_for_document_loader(file_path, filename)
            return self._load_from_langchain_docs(raw_docs, file_path, filename, doc_type)
        else:
            raise ValueError(f"不支持的文件类型: {filename}")

        try:
            raw_docs = loader.load()
            return self._load_from_langchain_docs(raw_docs, file_path, filename, doc_type)
        except Exception as e:
            raise Exception(f"处理文档失败: {str(e)}") from e

    def load_documents_from_folder(self, folder_path: str) -> list[dict]:
        all_documents = []

        for filename in os.listdir(folder_path):
            file_lower = filename.lower()
            if not (
                file_lower.endswith(".pdf")
                or file_lower.endswith((".docx", ".doc"))
                or file_lower.endswith((".xlsx", ".xls"))
                or file_lower.endswith((".html", ".htm"))
            ):
                continue

            file_path = os.path.join(folder_path, filename)
            try:
                documents = self.load_document(file_path, filename)
                all_documents.extend(documents)
            except Exception:
                continue

        return all_documents
