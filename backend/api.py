import re
import os
import json
import uuid
from pathlib import Path
from fastapi import APIRouter, HTTPException, UploadFile, File, Query
from fastapi.responses import StreamingResponse

from schemas import (
    ChatRequest,
    ChatResponse,
    SessionListResponse,
    SessionInfo,
    SessionMessagesResponse,
    MessageInfo,
    SessionDeleteResponse,
    DocumentListResponse,
    DocumentInfo,
    DocumentUploadResponse,
    DocumentDeleteResponse,
    ProfileUpdateRequest,
)
from pydantic import BaseModel
from agent import chat_with_agent, chat_with_agent_stream, storage, optimize_user_question, model, fast_model
from document_loader import DocumentLoader
from parent_chunk_store import ParentChunkStore
from milvus_writer import MilvusWriter
from milvus_client import MilvusManager
from embedding import embedding_service
from profile_manager import ProfileManager, UserMedicalFolder, build_folder_medical_summary
from fastapi import Form

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR.parent / "data"
UPLOAD_DIR = DATA_DIR / "documents"
PROFILE_DOC_DIR = DATA_DIR / "profile_docs"

loader = DocumentLoader()
parent_chunk_store = ParentChunkStore()
milvus_manager = MilvusManager()
milvus_writer = MilvusWriter(embedding_service=embedding_service, milvus_manager=milvus_manager)
profile_manager = ProfileManager()

router = APIRouter()


def _normalize_kb_tier(kb_tier: str | None) -> str:
    return milvus_manager.normalize_kb_tier(kb_tier)

@router.get("/profile/{user_id}")
async def get_user_profile(user_id: str):
    """获取用户个人档案"""
    try:
        profile = profile_manager.load_profile(user_id)
        return {"profile": profile}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/profile/upload")
async def upload_personal_record(user_id: str = Form(...), is_update: str = Form("false"), file: UploadFile = File(...)):
    """上传病历并提取多模态个人档案"""
    try:
        filename = file.filename
        os.makedirs(PROFILE_DOC_DIR, exist_ok=True)
        file_path = PROFILE_DOC_DIR / filename
        
        with open(file_path, "wb") as f:
            content = await file.read()
            f.write(content)
            
        update_flag = is_update.lower() == "true"
        profile_data = profile_manager.process_medical_record(user_id, str(file_path), filename, is_update=update_flag)
        return {"message": "病历解析成功", "profile": profile_data}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"病历解析失败: {str(e)}")


@router.put("/profile/{user_id}")
async def update_user_profile(user_id: str, body: ProfileUpdateRequest):
    """保存用户在前端校对、补充后的病历档案"""
    try:
        normalized = UserMedicalFolder(**body.profile).model_dump()
        normalized["medical_summary"] = build_folder_medical_summary(normalized)
        profile_manager.save_profile(user_id, normalized)
        return {"message": "档案已保存", "profile": normalized}
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"保存档案失败: {str(e)}")


@router.delete("/profile/{user_id}/records/{record_id}")
async def delete_profile_record(user_id: str, record_id: str):
    """删除病历夹中的单条记录，并重新拼接夹级长记忆（与各条简要记忆同步）。"""
    try:
        folder = profile_manager.delete_record(user_id, record_id)
        return {"message": "已删除该条病历，长记忆已更新", "profile": folder}
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/profile/{user_id}/discharge/upload")
async def upload_discharge_report(user_id: str, file: UploadFile = File(...)):
    """上传出院报告 PDF/图片，解析出院医嘱与随访日期。"""
    try:
        filename = file.filename or "discharge.pdf"
        os.makedirs(PROFILE_DOC_DIR, exist_ok=True)
        safe_name = f"discharge_{uuid.uuid4().hex[:12]}_{Path(filename).name}"
        file_path = PROFILE_DOC_DIR / safe_name
        with open(file_path, "wb") as f:
            f.write(await file.read())
        profile_data = profile_manager.process_discharge_report(user_id, str(file_path), filename)
        return {"message": "出院报告解析成功", "profile": profile_data}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"出院报告解析失败: {str(e)}")


@router.delete("/profile/{user_id}/discharge/{report_id}")
async def delete_discharge_report(user_id: str, report_id: str):
    """删除一条出院报告并更新夹级长记忆。"""
    try:
        folder = profile_manager.delete_discharge_report(user_id, report_id)
        return {"message": "已删除该份出院报告", "profile": folder}
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


def _remove_bm25_stats_for_filename(filename: str, kb_tier: str | None = None) -> None:
    """删除 Milvus 中该文件对应 chunk 前，先从持久化 BM25 统计中扣减（按知识库 tier 查询对应集合）。"""
    rows = milvus_manager.query_all(
        filter_expr=f'filename == "{filename}"',
        output_fields=["text"],
        kb_tier=kb_tier,
    )
    texts = [r.get("text") or "" for r in rows]
    embedding_service.increment_remove_documents(texts)


@router.get("/sessions/{user_id}/{session_id}", response_model=SessionMessagesResponse)
async def get_session_messages(user_id: str, session_id: str):
    """获取指定会话的所有消息"""
    try:
        data = storage._load()
        if user_id not in data or session_id not in data[user_id]:
            return SessionMessagesResponse(messages=[])
        
        session_data = data[user_id][session_id]
        messages = []
        for msg_data in session_data.get("messages", []):
            messages.append(MessageInfo(
                type=msg_data["type"],
                content=msg_data["content"],
                timestamp=msg_data["timestamp"],
                rag_trace=msg_data.get("rag_trace")
            ))
        
        return SessionMessagesResponse(messages=messages)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/sessions/{user_id}", response_model=SessionListResponse)
async def list_sessions(user_id: str):
    """获取用户的所有会话列表"""
    try:
        data = storage._load()
        if user_id not in data:
            return SessionListResponse(sessions=[])
        
        sessions = []
        for session_id, session_data in data[user_id].items():
            metadata = session_data.get("metadata", {})
            title = metadata.get("title")
            sessions.append(SessionInfo(
                session_id=session_id,
                updated_at=session_data.get("updated_at", ""),
                message_count=len(session_data.get("messages", [])),
                title=title
            ))
        
        # 按更新时间倒序排列
        sessions.sort(key=lambda x: x.updated_at, reverse=True)
        return SessionListResponse(sessions=sessions)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/sessions/{user_id}/{session_id}", response_model=SessionDeleteResponse)
async def delete_session(user_id: str, session_id: str):
    """删除指定会话"""
    try:
        deleted = storage.delete_session(user_id, session_id)
        if not deleted:
            raise HTTPException(status_code=404, detail="会话不存在")
        return SessionDeleteResponse(session_id=session_id, message="成功删除会话")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


class OptimizeRequest(BaseModel):
    message: str

class OptimizeResponse(BaseModel):
    questions: list[str]

@router.post("/chat/optimize", response_model=OptimizeResponse)
async def optimize_endpoint(request: OptimizeRequest):
    """优化用户的初步提问"""
    try:
        questions = await optimize_user_question(request.message, fast_model)
        return OptimizeResponse(questions=questions)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/chat", response_model=ChatResponse)
async def chat_endpoint(request: ChatRequest):
    try:
        resp = chat_with_agent(request.message, request.user_id, request.session_id)
        if isinstance(resp, dict):
            return ChatResponse(**resp)
        return ChatResponse(response=resp)
    except Exception as e:
        message = str(e)
        match = re.search(r"Error code:\s*(\d{3})", message)
        if match:
            code = int(match.group(1))
            if code == 429:
                raise HTTPException(
                    status_code=429,
                    detail=(
                        "上游模型服务触发限流/额度限制（429）。请检查账号额度/模型状态。\n"
                        f"原始错误：{message}"
                    ),
                )
            if code in (401, 403):
                raise HTTPException(status_code=code, detail=message)
            raise HTTPException(status_code=code, detail=message)
        raise HTTPException(status_code=500, detail=message)


@router.post("/chat/stream")
async def chat_stream_endpoint(request: ChatRequest):
    """跟 Agent 对话 (流式)"""
    async def event_generator():
        try:
            # chat_with_agent_stream 已经生成了 SSE 格式的字符串 (data: {...}\n\n)
            async for chunk in chat_with_agent_stream(
                request.message, 
                request.user_id, 
                request.session_id,
                request.think_mode
            ):
                yield chunk
        except Exception as e:
            error_data = {"type": "error", "content": str(e)}
            # SSE 格式错误
            yield f"data: {json.dumps(error_data)}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/documents", response_model=DocumentListResponse)
async def list_documents(kb_tier: str = Query(default="brief")):
    """获取已上传的文档列表"""
    try:
        tier = _normalize_kb_tier(kb_tier)
        milvus_manager.init_collection(kb_tier=tier)

        results = milvus_manager.query(
            filter_expr='id > 0',
            output_fields=["filename", "file_type"],
            limit=10000,
            kb_tier=tier,
        )
        
        # 按文件名分组统计
        file_stats = {}
        for item in results:
            filename = item.get("filename", "")
            file_type = item.get("file_type", "")
            if filename not in file_stats:
                file_stats[filename] = {
                    "filename": filename,
                    "file_type": file_type,
                    "chunk_count": 0
                }
            file_stats[filename]["chunk_count"] += 1
        
        documents = [DocumentInfo(**stats) for stats in file_stats.values()]
        return DocumentListResponse(documents=documents)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"获取文档列表失败: {str(e)}")


@router.post("/documents/upload", response_model=DocumentUploadResponse)
async def upload_document(file: UploadFile = File(...), kb_tier: str = Form(default="brief")):
    """上传文档并进行embedding"""
    try:
        tier = _normalize_kb_tier(kb_tier)
        filename = file.filename
        file_lower = filename.lower()
        if not (
            file_lower.endswith(".pdf")
            or file_lower.endswith((".docx", ".doc"))
            or file_lower.endswith((".xlsx", ".xls"))
            or file_lower.endswith((".html", ".htm"))
        ):
            raise HTTPException(status_code=400, detail="仅支持 PDF、Word、Excel 与 HTML 文档")

        target_upload_dir = UPLOAD_DIR / tier
        os.makedirs(target_upload_dir, exist_ok=True)
        milvus_manager.init_collection(kb_tier=tier)

        delete_expr = f'filename == "{filename}"'
        try:
            _remove_bm25_stats_for_filename(filename, tier)
        except Exception:
            pass
        try:
            milvus_manager.delete(delete_expr, kb_tier=tier)
        except Exception:
            pass
        try:
            parent_chunk_store.delete_by_filename(filename, kb_tier=tier)
        except Exception:
            pass

        file_path = target_upload_dir / filename
        with open(file_path, "wb") as f:
            content = await file.read()
            f.write(content)

        try:
            new_docs = loader.load_document(str(file_path), filename)
        except Exception as doc_err:
            raise HTTPException(status_code=500, detail=f"文档处理失败: {doc_err}")

        if not new_docs:
            raise HTTPException(status_code=500, detail="文档处理失败，未能提取内容")

        parent_docs = [doc for doc in new_docs if int(doc.get("chunk_level", 0) or 0) in (1, 2)]
        leaf_docs = [doc for doc in new_docs if int(doc.get("chunk_level", 0) or 0) == 3]
        if not leaf_docs:
            raise HTTPException(status_code=500, detail="文档处理失败，未生成可检索叶子分块")

        parent_chunk_store.upsert_documents(parent_docs, kb_tier=tier)
        milvus_writer.write_documents(leaf_docs, kb_tier=tier)

        return DocumentUploadResponse(
            filename=filename,
            chunks_processed=len(leaf_docs),
            message=(
                f"成功上传并处理到{('简要' if tier == 'brief' else '详细')}知识库：{filename}，叶子分块 {len(leaf_docs)} 个，"
                f"父级分块 {len(parent_docs)} 个（存入docstore）"
            ),
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"文档上传失败: {str(e)}")


@router.delete("/documents/{filename}", response_model=DocumentDeleteResponse)
async def delete_document(filename: str, kb_tier: str = Query(default="brief")):
    """删除文档在 Milvus 中的向量（保留本地文件）"""
    try:
        tier = _normalize_kb_tier(kb_tier)
        milvus_manager.init_collection(kb_tier=tier)

        delete_expr = f'filename == "{filename}"'
        _remove_bm25_stats_for_filename(filename, tier)
        result = milvus_manager.delete(delete_expr, kb_tier=tier)
        parent_chunk_store.delete_by_filename(filename, kb_tier=tier)

        return DocumentDeleteResponse(
            filename=filename,
            chunks_deleted=result.get("delete_count", 0) if isinstance(result, dict) else 0,
            message=f"成功从{('简要' if tier == 'brief' else '详细')}知识库删除文档 {filename} 的向量数据（本地文件已保留）",
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"删除文档失败: {str(e)}")
