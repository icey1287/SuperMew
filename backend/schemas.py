from pydantic import BaseModel, Field
from typing import Optional, List


class ChatRequest(BaseModel):
    """聊天请求模型"""

    message: str = Field(..., description="用户发送的消息内容")
    user_id: str = Field(default="default_user", description="用户唯一标识，用于区分不同用户")
    session_id: str = Field(default="default_session", description="会话唯一标识，用于关联对话历史")


class RetrievedChunk(BaseModel):
    """检索到的文档块模型"""

    filename: str = Field(..., description="文档文件名")
    page_number: Optional[str | int] = Field(default=None, description="文档页码")
    text: Optional[str] = Field(default=None, description="文档块文本内容")
    score: Optional[float] = Field(default=None, description="检索相关性分数")
    rrf_rank: Optional[int] = Field(default=None, description="RRF 融合排序排名")
    rerank_score: Optional[float] = Field(default=None, description="Rerank 精排分数")


class RagTrace(BaseModel):
    """RAG 检索追踪信息模型"""

    tool_used: bool = Field(..., description="是否使用了 RAG 工具")
    tool_name: str = Field(..., description="调用的工具名称")
    query: Optional[str] = Field(default=None, description="原始查询问题")
    expanded_query: Optional[str] = Field(default=None, description="扩展后的查询")
    step_back_question: Optional[str] = Field(default=None, description="后退一步的抽象问题")
    step_back_answer: Optional[str] = Field(default=None, description="抽象问题的回答")
    expansion_type: Optional[str] = Field(default=None, description="查询扩展类型 (step_back/hyde)")
    hypothetical_doc: Optional[str] = Field(default=None, description="HyDE 生成的假设文档")
    retrieval_stage: Optional[str] = Field(default=None, description="检索阶段 (initial/expanded)")
    grade_score: Optional[str] = Field(default=None, description="相关性评分结果 (yes/no)")
    grade_route: Optional[str] = Field(default=None, description="评分路由决定")
    rewrite_needed: Optional[bool] = Field(default=None, description="是否需要重写查询")
    rewrite_strategy: Optional[str] = Field(default=None, description="重写策略")
    rewrite_query: Optional[str] = Field(default=None, description="重写后的查询内容")
    rerank_enabled: Optional[bool] = Field(default=None, description="是否启用 Rerank")
    rerank_applied: Optional[bool] = Field(default=None, description="Rerank 是否已应用")
    rerank_model: Optional[str] = Field(default=None, description="Rerank 模型名称")
    rerank_endpoint: Optional[str] = Field(default=None, description="Rerank API 端点")
    rerank_error: Optional[str] = Field(default=None, description="Rerank 错误信息")
    retrieval_mode: Optional[str] = Field(default=None, description="检索模式 (dense/sparse/hybrid)")
    candidate_k: Optional[int] = Field(default=None, description="召回候选数量")
    leaf_retrieve_level: Optional[int] = Field(default=None, description="叶子块检索层级")
    auto_merge_enabled: Optional[bool] = Field(default=None, description="是否启用自动合并")
    auto_merge_applied: Optional[bool] = Field(default=None, description="自动合并是否已应用")
    auto_merge_threshold: Optional[int] = Field(default=None, description="自动合并阈值")
    auto_merge_replaced_chunks: Optional[int] = Field(default=None, description="被合并替换的块数量")
    auto_merge_steps: Optional[int] = Field(default=None, description="自动合并步数")
    retrieved_chunks: Optional[List[RetrievedChunk]] = Field(default=None, description="当前检索结果块")
    initial_retrieved_chunks: Optional[List[RetrievedChunk]] = Field(default=None, description="初次检索结果块")
    expanded_retrieved_chunks: Optional[List[RetrievedChunk]] = Field(default=None, description="扩展检索结果块")


class ChatResponse(BaseModel):
    """聊天响应模型"""

    response: str = Field(..., description="Agent 返回的回复内容")
    rag_trace: Optional[RagTrace] = Field(default=None, description="RAG 检索追踪信息")


class MessageInfo(BaseModel):
    """单条消息的信息模型"""

    type: str = Field(..., description="消息类型 (user/assistant)")
    content: str = Field(..., description="消息正文内容")
    timestamp: str = Field(..., description="消息时间戳 (ISO 格式)")
    rag_trace: Optional[RagTrace] = Field(default=None, description="RAG 追踪信息")


class SessionMessagesResponse(BaseModel):
    """会话消息列表响应模型"""

    messages: List[MessageInfo] = Field(default_factory=list, description="该会话的所有消息")


class SessionInfo(BaseModel):
    """会话摘要信息模型"""

    session_id: str = Field(..., description="会话唯一标识")
    updated_at: str = Field(..., description="最后更新时间 (ISO 格式)")
    message_count: int = Field(..., ge=0, description="会话中的消息总数")


class SessionListResponse(BaseModel):
    """会话列表响应模型"""

    sessions: List[SessionInfo] = Field(default_factory=list, description="会话信息列表")


class SessionDeleteResponse(BaseModel):
    """删除会话响应模型"""

    session_id: str = Field(..., description="被删除的会话 ID")
    message: str = Field(..., description="操作结果描述")


class DocumentInfo(BaseModel):
    """文档信息模型"""

    filename: str = Field(..., description="文件名")
    file_type: str = Field(..., description="文件类型 (pdf/docx 等)")
    chunk_count: int = Field(..., ge=0, description="该文档被分割的块数量")
    uploaded_at: Optional[str] = Field(default=None, description="上传时间 (ISO 格式)")


class DocumentListResponse(BaseModel):
    """文档列表响应模型"""

    documents: List[DocumentInfo] = Field(default_factory=list, description="文档信息列表")


class DocumentUploadResponse(BaseModel):
    """文档上传响应模型"""

    filename: str = Field(..., description="上传的文件名")
    chunks_processed: int = Field(..., ge=0, description="处理的块数量")
    message: str = Field(..., description="操作结果描述")


class DocumentDeleteResponse(BaseModel):
    """文档删除响应模型"""

    filename: str = Field(..., description="被删除的文件名")
    chunks_deleted: int = Field(..., ge=0, description="删除的块数量")
    message: str = Field(..., description="操作结果描述")
