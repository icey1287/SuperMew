# SuperMew 后端技术报告（深度版）

**版本**：2.0  
**依据**：`backend/` 源码  
**说明**：本版删除凑行附录，按技术点展开原理、本仓库实现方式、与相关技术的联系及可改进方向。

---

## 目录

1. [总览与问题域](#1-总览与问题域)
2. [整体架构与数据流](#2-整体架构与数据流)
3. [`app.py`：ASGI 应用、静态资源与缓存策略](#3-apppyasgi-应用静态资源与缓存策略)
4. [`api.py`：路由层、SSE 与资源生命周期](#4-apipy路由层sse-与资源生命周期)
5. [`schemas.py`：契约与类型边界](#5-schemaspy契约与类型边界)
6. [`agent.py`：Agent、会话、流式与长上下文](#6-agentpyagent会话流式与长上下文)
7. [`tools.py`：工具边界、RAG 上下文与线程安全](#7-toolspy工具边界rag-上下文与线程安全)
8. [`rag_pipeline.py`：LangGraph 编排与查询扩展族](#8-rag_pipelinepylanggraph-编排与查询扩展族)
9. [`rag_utils.py`：混合检索、RRF、精排与层级合并](#9-rag_utilspy混合检索rrf精排与层级合并)
10. [`milvus_client.py`：集合 Schema、索引与检索 API](#10-milvus_clientpy集合-schema索引与检索-api)
11. [`embedding.py`：稠密嵌入与 BM25 稀疏向量](#11-embeddingpy稠密嵌入与-bm25-稀疏向量)
12. [`milvus_writer.py`：索引构建流水线](#12-milvus_writerpy索引构建流水线)
13. [`document_loader.py`：多格式解析与三级分块](#13-document_loaderpy多格式解析与三级分块)
14. [`parent_chunk_store.py`：父文档存储与 Auto-merge 的配合](#14-parent_chunk_storepy父文档存储与-auto-merge-的配合)
15. [`graph_retriever.py`：图检索在本项目中的位置与局限](#15-graph_retrieverpy图检索在本项目中的位置与局限)
16. [`profile_manager.py`：结构化病历、多模态与提示注入](#16-profile_managerpy结构化病历多模态与提示注入)
17. [`backend/scripts/build_neo4j_graph.py`：演示数据与生产图构建的差异](#17-backendscriptsbuild_neo4j_graphpy演示数据与生产图构建的差异)
18. [配置、错误处理与演进建议](#18-配置错误处理与演进建议)
19. [稠密检索的数学与工程：ANN、HNSW 与内积](#19-稠密检索的数学与工程annhnsw-与内积)
20. [稀疏路、词表与「查询–索引一致性」](#20-稀疏路词表与查询索引一致性)
21. [RRF 之后：为何还要 Auto-merge 与 Rerank](#21-rrf-之后为何还要-auto-merge-与-rerank)
22. [LangGraph 子图与 Agent 的边界：控制流在哪里](#22-langgraph-子图与-agent-的边界控制流在哪里)
23. [SSE、异步队列与「检索步骤可见」的实现原理](#23-sse异步队列与检索步骤可见的实现原理)
24. [患者档案注入与提示词安全](#24-患者档案注入与提示词安全)
25. [与相关论文/产品的概念对照（便于写文献综述）](#25-与相关论文产品的概念对照便于写文献综述)

---

## 1. 总览与问题域

### 1.1 系统目标

本后端支撑**鼻咽癌（NPC）患者及家属**场景下的问答：既要引用**上传的医学文档**（指南、文献等），又要结合**用户病历档案**做个性化回复。技术路线是经典的 **RAG（Retrieval-Augmented Generation）**：先检索、后生成；并在实现上叠加了**工具型 Agent**（强制走知识库工具）、**混合检索**（稠密 + 稀疏 + 图）、**检索后编排**（评分、查询扩展、再检索）以及**会话持久化与 SSE 流式输出**。

### 1.2 与通用聊天机器人的区别

| 维度 | 通用聊天 | 本仓库取向 |
|------|----------|------------|
| 事实来源 | 模型参数记忆 | 以 Milvus/工具返回片段为主 |
| 幻觉风险 | 高 | 通过检索 +「证据不足则承认」提示词抑制 |
| 长记忆 | 依赖上下文窗口 | 超长对话摘要 + 患者 `medical_summary` 注入 |
| 可解释性 | 弱 | `rag_trace` 记录检索阶段与片段，供前端展示 |

**延伸**：医疗场景常讨论 **Grounding**（生成是否与证据对齐）。本仓库通过「工具返回片段 + 模型内联引用 `[n]`」做弱 grounding；更强做法包括：引用对齐校验、NLI 判定句子是否被证据支持、或 **Attribution** 数据集评测——这些均未在后端硬性实现，但 `rag_trace` 为后续加模块留了数据面。

---

## 2. 整体架构与数据流

### 2.1 逻辑分层

1. **接入层**：`app.py` + `api.py`（HTTP/SSE）。
2. **对话与编排层**：`agent.py`（LangChain Agent + 流式队列）。
3. **检索编排层**：`rag_pipeline.py`（LangGraph 状态机）。
4. **检索执行层**：`rag_utils.py`（三路召回、融合、精排、父块合并）。
5. **存储层**：Milvus（向量与稀疏）、本地 JSON（会话、父块、档案）、Neo4j（可选图）、磁盘文件（原始上传）。

### 2.2 一次「流式问答」的端到端路径（概念顺序）

1. 客户端 `POST /chat/stream`，携带 `user_id`、`session_id`、`think_mode`。
2. `chat_with_agent_stream` 加载历史、注入患者档案、注册 RAG 步骤队列、启动 `agent.astream`。
3. Agent 决策调用 `search_knowledge_base`。
4. 工具内部执行 `run_rag_graph`：初检索 → LLM 评分相关性 → 若不相关则查询扩展（Step-back / HyDE / 组合）→ 扩展检索 → 返回拼接上下文。
5. `retrieve_documents` 内部：对查询编码 → Milvus 稠密/稀疏召回 → Neo4j 图召回 → **应用层 RRF** → 可选 **Rerank** → **Auto-merge** 用父块替换多子块。
6. 工具将片段文本返回模型；模型流式输出；结束时推送 `rag_trace`、追问列表、`[DONE]`。
7. 会话写入 `customer_service_history.json`。

**延伸**：这是典型的 **Tool-Augmented LLM** 流水线。与 **LangChain LCEL** 链式写法不同，这里用 **Agent + 单工具** 把「何时检索」交给模型，但用系统提示**强约束**「医疗问题必须检索且每轮最多一次工具」，从而在自主性与可控性之间折中。

---

## 3. `app.py`：ASGI 应用、静态资源与缓存策略

### 3.1 FastAPI 与 Uvicorn

FastAPI 基于 **Starlette** 和 **Pydantic**，暴露 ASGI 接口，由 **Uvicorn** 驱动。ASGI 相对 WSGI 的优势是原生支持**异步**与**WebSocket/SSE**（本项目的流式聊天走 SSE，见 `api.py`）。

### 3.2 CORS

当前 `allow_origins=["*"]` 对开发友好，但生产环境若配合 `allow_credentials=True`，浏览器对通配源有严格限制，且任意站可调用 API。**延伸**：应改为显式白名单 + 网关鉴权。

### 3.3 静态文件与 `html=True`

`StaticFiles(directory=..., html=True)` 使根路径可返回 `index.html`，适合前后端同仓部署。**延伸**：生产常把静态资源放到 CDN，API 单独子域。

### 3.4 开发用 no-cache 中间件

对 `/` 及 `.html/.js/.css` 设置 `Cache-Control: no-cache, no-store`，避免浏览器缓存旧前端导致「改了代码用户看不到」。**延伸**：发布流水线可用带 hash 的文件名 + 长期缓存，与开发设置分离。

---

## 4. `api.py`：路由层、SSE 与资源生命周期

### 4.1 模块级单例

`DocumentLoader`、`MilvusWriter`、`ProfileManager` 等在 import 时构造，**进程内单例**。优点：复用连接与配置；缺点：多 worker 进程时各进程一套内存状态，会话 JSON 仍共享同一文件则需注意**并发写**（CPython 下 GIL 可减轻同机竞争，但非原子，高并发应换 DB）。

### 4.2 REST 资源划分

- **会话**：`/sessions/...` 围绕 `user_id` + `session_id` 建模，符合资源式 URL。
- **文档**：`/documents` 列表、`/documents/upload` 创建索引、`DELETE /documents/{filename}` 按文件名删除向量（**不删本地文件**，与注释一致）。
- **档案**：`/profile/upload` 使用 `multipart/form-data`，`user_id` 与 `is_update` 用 Form 字段，适合文件上传。

### 4.3 SSE（`text/event-stream`）

`chat_stream_endpoint` 返回 `StreamingResponse`，`event_generator` 异步迭代 `chat_with_agent_stream` 产出的字符串（已是 `data: {...}\n\n` 形态）。响应头 `X-Accel-Buffering: no` 常见于 Nginx 后防止缓冲破坏实时性。

**延伸**：SSE 是**单向**服务器推送；若需双向高实时可换 WebSocket。SSE 在 HTTP/2 上通常更省心；断线重连需前端配合 `EventSource` 或 fetch stream。

### 4.4 上传文档的幂等与覆盖策略

上传同名文件会先 `delete` Milvus 中 `filename == ...` 的记录并 `parent_chunk_store.delete_by_filename`，再写入新向量——等价于**按文件名覆盖**。**延伸**：若需版本历史，应引入 `doc_id` 与软删除，而非仅靠文件名。

---

## 5. `schemas.py`：契约与类型边界

### 5.1 Pydantic 的作用

请求体 `ChatRequest`、响应体 `ChatResponse`、会话消息 `MessageInfo` 等用 Pydantic 建模，自动校验类型并在 OpenAPI 文档中暴露。**延伸**：`RagTrace` 字段极多，反映前端展示需求；若 API 对外暴露，可考虑拆分为「精简版 trace」与「调试版 trace」，减少 payload。

### 5.2 `think_mode`

`normal` / `fast` / `deep` 会传到 `rag_utils.retrieve_documents`（通过 `tools.get_rag_config`），影响候选数、是否跳过 Rerank。**延伸**：这是**延迟–质量旋钮**，类似搜索中的「快速/深度」模式，应在产品层向用户解释差异。

---

## 6. `agent.py`：Agent、会话、流式与长上下文

### 6.1 `create_agent` 与单工具设计

使用 LangChain `create_agent(model, tools=[search_knowledge_base], system_prompt=...)`。**ReAct 类**范式：模型可反复思考与调工具；本仓库用**工具调用次数守卫**（见 `tools.py`）把检索限制为每轮一次，避免费用与延迟爆炸。

**延伸**：多工具系统（计算器、联网、医院 API）需更复杂的路由；此处刻意保持**单一知识库工具**，降低调试难度。

### 6.2 系统提示中的可控性设计

要点包括：鼻咽癌人设、强制检索、深度总结+分点、引用 `[n]`、术语用 HTML `concept-tooltip`、不暴露思维链。**延伸**：

- **Citation 格式**：与前端 `parseMarkdown` 将 `[1]` 转为可点击引用联动。
- **安全**：用户若上传恶意 HTML，模型仍可能复述；前端应对 `v-html` 做 sanitize（属前端话题，此处仅提示）。

### 6.3 动态档案注入

若 `profile.medical_summary` 存在，拼入系统提示作为 **Long-term memory**。这是**显式记忆槽**，区别于仅依赖对话历史。**延伸**：与 **MemGPT**、**Zep** 等记忆架构相比，此处实现是「一段摘要文本」，没有结构化记忆更新策略或冲突消解；`profile_manager` 的 `is_update` 路径由 LLM 融合新旧 JSON，属于**单次写入层面的合并**。

### 6.4 `ConversationStorage`

- 路径：`data/customer_service_history.json`。
- 结构：`user_id -> session_id -> { messages, metadata, updated_at }`。
- `save` 合并 `metadata`：支持异步生成的 `title` 后写回而不覆盖其他字段。
- 消息序列化：`type` + `content` + `timestamp`，AI 消息可带 `rag_trace`。

**延伸**：JSON 文件适合单机 demo；生产应使用带 **ACID** 或至少 **行级锁** 的存储，并考虑 **PII 加密**。

### 6.5 长对话：`summarize_old_messages`

当消息数 > 50，对前 40 条做摘要，替换为一条 `SystemMessage` + 保留后 10 条左右原文。这是**滑动窗口 + 摘要**启发式。**延伸**：

- 与 **无限上下文模型**相比，摘要可能丢失细节；可改进为**层次化摘要**或**向量检索历史**。
- 摘要本身占用 token，需监控总长度。

### 6.6 流式架构：统一队列 + 后台 Agent 任务

核心思想：**所有事件**（模型 token、`rag_step`、标题、`trace`）进入同一 `asyncio.Queue`，主协程 `while` 取队列 `yield` SSE。这样 **RAG 步骤**可在工具线程通过 `call_soon_threadsafe` 入队，而不必等模型产出首个 token。

**延伸**：

- **背压**：若模型产出极快，队列可能堆积；当前依赖 asyncio 调度，一般可接受。
- **取消**：`GeneratorExit` 时 `cancel` agent 任务，避免客户端断连后仍消耗 GPU/API。

### 6.7 首条消息标题与追问

- `generate_session_title`：用 `fast_model` 生成短标题，经 `done_callback` 把结果放入队列（`session_title` 事件）。
- `_generate_follow_ups`：基于最近若干轮对话预测 3 个医学向追问，限制非临床后勤问题。

**延伸**：标题与追问都是 **LLM 派生元数据**，可单独缓存失败重试；`wait_for(follow_up_task, 3.0)` 避免拖慢流结束。

### 6.8 非流式 `chat_with_agent`

同步 `invoke`，适合脚本或简单客户端；同样会写 `rag_trace`。**延伸**：无 SSE 时用户看不到 `rag_step`，但 trace 仍可落库。

---

## 7. `tools.py`：工具边界、RAG 上下文与线程安全

### 7.1 `search_knowledge_base`

内部调用 `rag_pipeline.run_rag_graph(query)`，将返回文档格式化为 `Retrieved Chunks` 字符串；并把 `rag_trace` 写入 `_LAST_RAG_CONTEXT` 供 `agent` 保存。

**延伸**：工具返回的是**给模型读的字符串**，不是结构化 JSON；若要做 **tool result 解析**（例如只取 top-k 标题），需改返回类型或增加并行 channel。

### 7.2 每轮仅一次检索

`_KNOWLEDGE_TOOL_CALLS_THIS_TURN` 限制第二次调用返回 `TOOL_CALL_LIMIT_REACHED`。动机：**防止 Agent 循环检索**导致延迟与成本不可控。**延伸**：与 **Self-RAG** 中「多次检索直到满意」相反，此处用硬截断；若要迭代检索，应在 `rag_pipeline` 内闭环，而不是放开工具次数。

### 7.3 `emit_rag_step` 与事件循环

`set_rag_step_queue` 保存 `asyncio` 当前 loop；`emit_rag_step` 用 `call_soon_threadsafe` 向队列放 `dict`。**延伸**：若工具在完全无 loop 的线程执行，需确保 `set_rag_step_queue` 已在主协程调用过（流式路径满足）。

### 7.4 `set_rag_config` / `get_rag_config`

用于传递 `think_mode` 到 `rag_utils`，避免在全局隐式依赖请求上下文。**延伸**：更干净的做法是 **ContextVar** 或显式参数贯穿 `run_rag_graph` → `retrieve_documents`，减少全局可变状态。

---

## 8. `rag_pipeline.py`：LangGraph 编排与查询扩展族

### 8.1 LangGraph 状态机

`StateGraph(RAGState)` 节点：`retrieve_initial` → `grade_documents` → 条件边 → 要么结束，要么 `rewrite_question` → `retrieve_expanded` → 结束。

**注意**：名为 `generate_answer` 的边实际指向 **END**，并不在图内调用 LLM 生成答案；**最终答案由外层 Agent** 生成。图只负责**准备 `docs` 与 `context`**。

**延伸**：这是「**检索子图**」与「**生成在 Agent**」分离的架构；优点：复用同一 Agent 对话逻辑；缺点：图内状态与 Agent 消息历史不共享，扩展复杂推理需在 Agent 侧再加层。

### 8.2 `retrieve_initial`

调用 `retrieve_documents(query, top_k=15)`，组装 `rag_trace` 初值（`retrieval_stage: initial`），`retrieved_chunks` 供前端展示初检结果。

### 8.3 `grade_documents_node`：LLM 作相关性分类器

使用 `GRADE_MODEL`（结构化输出 `binary_score: yes/no`）判断**整段拼接上下文**是否与问题相关。**延伸**：

- 与 **Cross-encoder** 相比，LLM 更灵活但更慢更贵；与 **小模型 NLI** 相比，可解释性略差。
- 当前是对**合并上下文**打分，若片段很长，可能**淹没**局部相关性；可改为**逐段打分再聚合**（如 max / 投票）。

### 8.4 评分器不可用时的行为

若 `grader` 为 `None`，强制 `rewrite_question` 路径。**延伸**：这会导致**总是二次检索**，增加延迟；生产可降级为「跳过评分直接回答」或「用启发式长度/关键词」。

### 8.5 `rewrite_question_node`：三策略路由

路由器输出 `step_back` / `hyde` / `complex`：

- **step_back**：`step_back_expand`——先抽象「退步问题」再答一步，拼进 `expanded_query`；思想来自**教学中的退阶提问**，在 RAG 中用于覆盖**过于具体**的查询无法匹配泛化文档的问题。
- **hyde**：`generate_hypothetical_document`——生成假设文档再检索；对应 **HyDE**（Hypothetical Document Embeddings）论文思路：用生成文本的嵌入近似「理想答案」的语义邻域。
- **complex**：同时 step_back + HyDE，召回union。

**延伸**：HyDE 可能**幻觉**进假设文档，若领域极严谨，可约束 prompt 或降低温度；Step-back 增加两次 LLM 调用（问题+短答），需注意总延迟。

### 8.6 `retrieve_expanded`

按策略调用 `retrieve_documents` 一至两次，**extend 列表**，再按 `(filename, page_number, text)` **去重**，并重写 `rrf_rank` 为连续序号。**延伸**：去重键若过粗可能误合并不同语义块；过细则重复多。

### 8.7 `run_rag_graph` 的入口

模块级 `rag_graph = build_rag_graph().compile()`，工具内 `invoke`。**延伸**：LangGraph 支持 checkpointing、人机在环；当前未启用。

---

## 9. `rag_utils.py`：混合检索、RRF、精排与层级合并

### 9.1 为何需要混合检索

- **稠密向量（Dense）**：捕获语义相似（同义改写、上下位概念），但对**罕见专有名词、缩写、剂量数字**可能欠敏感。
- **稀疏向量（BM25 风格）**：强调词项匹配，利于**术语、药品名、指南原文**。
- **图检索**：补充**实体–关系**线索（若图谱质量好）。

经典文献与工业实践（如 **Elasticsearch hybrid**、**Azure Cognitive Search**）多采用「多路召回 + 融合 + 精排」。本仓库在 **应用层**对三路做 RRF（而非仅依赖 Milvus 内置双路 hybrid，见 `milvus_client.hybrid_retrieve` 与 `rag_utils` 的分工）。

### 9.2 召回：`dense_retrieve` 与 `sparse_retrieve`

均带 `filter_expr: chunk_level == LEAF_RETRIEVE_LEVEL`（默认 3），即**只在叶子块索引上搜**，父块存在 `ParentChunkStore` / 合并阶段使用。

**延伸**：**Hierarchical RAG** 常见策略是「先搜摘要层再 drill-down」；此处是「叶子搜 + 命中父块合并」，实现路径不同但目标类似：**在细粒度命中与粗粒度上下文之间平衡**。

### 9.3 RRF（Reciprocal Rank Fusion）

对每个文档键，来自多路的名次 \(r\) 贡献 \(\frac{1}{k+r}\)，默认 \(k=60\)。**延伸**：

- 出自信息检索中对多列表合并的研究，**无需**手动调各路的绝对分数尺度。
- 本实现**未**对不同路加权重（若需「稀疏 0.5 / 稠密 0.2 / 图 0.3」需在分数层改造）。
- `chunk_id` 缺失时退化为 `text` 键，可能过长且不稳定；生产应保证 chunk_id 唯一。

### 9.4 Rerank（Cross-encoder 式 API）

对 RRF 后的列表调用 OpenAI 兼容 `POST .../v1/rerank`，将 query 与候选 `text` 列表送服务，返回按相关性重排。**延伸**：

- 典型 **Cross-encoder** 比 **Bi-encoder**（纯向量）更准但更慢，适合**候选集已缩小**的阶段（late interaction）。
- `return_documents: false` 减少带宽，仅取 index 映射回原 doc。

### 9.5 `think_mode` 对检索预算的影响

| 模式 | candidate_k | final_top_k | Rerank |
|------|-------------|-------------|--------|
| fast | 10 | 10 | 跳过，用 `rrf_score` 占位 |
| normal | 15 | 10 | 启用（若配置） |
| deep | 30 | 15 | 启用（若配置） |

**延伸**：`fast` 将 `rerank_error` 记为 `skipped_by_think_mode`，便于监控与 A/B。

### 9.6 Auto-merge：从叶子到父文档

`_merge_to_parent_level`：若同一 `parent_chunk_id` 下命中子块数 ≥ `AUTO_MERGE_THRESHOLD`，则从 `ParentChunkStore` 取父块文本替换子块，分数取 max，并标记 `merged_from_children`。

执行**两轮**：先 L3→L2，再 L2→L1（代码注释写明）。**延伸**：对应 **Parent Document Retriever** 思想（LangChain 文档中的模式）：检索小片，返回大片上下文，减少**碎片化证据**。

### 9.7 Step-back / HyDE 辅助函数

位于 `rag_utils`，供 `rag_pipeline` 与潜在其他模块复用；使用独立 `_stepback_model`（temperature 0.2）以稳定输出。

---

## 10. `milvus_client.py`：集合 Schema、索引与检索 API

### 10.1 客户端与重连

`MilvusClient(uri=http://host:port)`；`_ensure_connection` 在 RPC 异常时关闭并重建 client，缓解 **closed channel** 类问题。

### 10.2 Schema 设计

- `dense_embedding`：`FLOAT_VECTOR`，维度参数 `dense_dim` 默认 **2560**（必须与 `EMBEDDER` 输出一致）。
- `sparse_embedding`：`SPARSE_FLOAT_VECTOR`，存 BM25 权重映射。
- 标量字段：`text`、`filename`、`file_type`、`file_path`、`page_number`、`chunk_idx`、`chunk_id`、`parent_chunk_id`、`root_chunk_id`、`chunk_level`。

**延伸**：`text` 上限 `VARCHAR(2000)`，超长块在入库前需截断或拆分（当前分块策略应避免触顶）。

### 10.3 索引

- 稠密：**HNSW**，`IP` 内积，`M=16`，`efConstruction=256`；搜索 `ef=64`。
- 稀疏：**SPARSE_INVERTED_INDEX**，`IP`，`drop_ratio_build/search` 0.2。

**延伸**：**IP** 假设向量已 L2 归一化或与训练目标一致；若实际未归一化，**余弦**与 **IP** 不等价。需与嵌入服务行为对齐。

### 10.4 `hybrid_retrieve`（类内）与 `rag_utils` 三路 RRF 的关系

`MilvusManager.hybrid_retrieve` 使用 Milvus **双路 AnnSearchRequest + RRFRanker**，是「**仅 Milvus 内**」的稠密+稀疏融合。当前主路径 `rag_utils` 改为 **dense + sparse 分别 search** 再与 **图** 在 Python 里 RRF，以便纳入 Neo4j 结果。**延伸**：两套融合并存，未来可统一策略，避免维护两套 RRF 参数（k=60）。

### 10.5 `get_chunks_by_ids`

按 `chunk_id in [...]` 批量拉取，用于父块合并。**延伸**：ID 数量大时需分批避免表达式过长。

---

## 11. `embedding.py`：稠密嵌入与 BM25 稀疏向量

### 11.1 稠密：`POST /embeddings`

OpenAI 兼容格式，`input` 为字符串列表，返回 `data[].embedding`。**延伸**：批处理可减少 QPS；注意 API 的 **batch size 限制**。

### 11.2 分词 `tokenize`

中文按**单字**、英文按**整词**；用于 BM25 与词表。**延伸**：医学英文复合词、中文分词若用 **jieba** 可能更准，但需同步索引与查询分词策略；当前实现**简单但一致**。

### 11.3 `fit_corpus`

在 `MilvusWriter.write_documents` 中对**本批文档**调用，统计 `df`、词表、平均文档长。**重要细节**：IDF 基于**当前批次**，若多批次上传，**全局 IDF 与索引时不一致**会导致查询侧稀疏向量与入库侧分布漂移。**延伸**：生产应对**全库或采样全库**维护统计，或改用 **Elasticsearch / BM25 官方实现** 的持久化统计。

### 11.4 BM25 公式实现

使用经典 BM25 形式：IDF 平滑 + 长度归一，`k1=1.5`，`b=0.75`。新词 `df=0` 时用 `log((N+1)/1)` 平滑。**延伸**：这是 **Okapi BM25** 家族；与 **Lucene** 实现细节可能略有数值差异，跨系统对比需注意。

---

## 12. `milvus_writer.py`：索引构建流水线

流程：`init_collection` → `fit_corpus` → 分批 `get_all_embeddings` → `insert`。

**延伸**：大批量时应 **bulk insert + flush 策略**；当前 `batch_size=50` 可调。失败重试、死信队列属运维增强。

---

## 13. `document_loader.py`：多格式解析与三级分块

### 13.1 加载器选择

- PDF：`PyPDFLoader`（按页）。
- Word：`Docx2txtLoader`。
- Excel：`UnstructuredExcelLoader`（表格结构差异大，质量依赖 unstructured）。

### 13.2 三级 `RecursiveCharacterTextSplitter`

对每页文本：L1 大块 → L2 中块 → L3 小块，层级尺寸由 `chunk_size=800` 推导放大。**分隔符**优先段落、句读，符合中文阅读习惯。

**延伸**：与 **semantic chunking**（按嵌入相似度切）相比，递归字符切**更快**、**确定性**，但可能在语义中间切开；医疗表格、药品列表需专门处理。

### 13.3 `chunk_id` 与父子关系

`filename::p{page}::l{level}::{index}`；L1 `parent_chunk_id` 空，L2 指 L1，L3 指 L2。**延伸**：同一文件名多版本若覆盖上传，ID 空间仍可能冲突若页码与索引复用——依赖上传前删除旧数据缓解。

---

## 14. `parent_chunk_store.py`：父文档存储与 Auto-merge 的配合

JSON 文件 `data/parent_chunks.json`，键为 `chunk_id`。写入用临时文件再 `replace`，**降低半写损坏概率**。

**延伸**：与 **Redis / Dynamo** 等相比，适合单机；并发写仍需文件锁或外部存储。

---

## 15. `graph_retriever.py`：图检索在本项目中的位置与局限

### 15.1 实现要点

通过 Neo4j HTTP `/db/neo4j/tx/commit` 执行 Cypher：`MATCH (n:Entity)-[r]-(m:Entity)`，用 `CONTAINS` 在**查询串与实体名**之间做子串匹配，结果拼成一段中文说明，封装成**单条伪 chunk**。

### 15.2 与知识图谱 RAG（GAR）的差距

典型 **GraphRAG** / **GAR** 会：实体链接、子图遍历、路径排序（如 Personalized PageRank）。此处是**轻量关键词图查询**，**无**实体抽取、**无** PageRank。

**延伸**：若要强化图路，应：构建 NPC 领域图谱、用 LLM/NER 从 query 抽实体、Cypher 参数化与索引；并可对多关系结果**拆成多条 chunk** 参与 RRF，避免单条大块稀释排序。

---

## 16. `profile_manager.py`：结构化病历、多模态与提示注入

### 16.1 结构化输出

`PatientProfile` Pydantic 模型约束字段；LLM 输出 JSON 解析后入库。**延伸**：**JSON mode** 或 **tool calling** 可提高格式可靠率。

### 16.2 多模态路径

若安装 PyMuPDF，PDF 转多页 JPEG base64 送入支持视觉的聊天模型；否则回退文本 `PyPDFLoader`。**延伸**：OCR 质量、扫描件、表格照片对模型要求高；可接入专用 OCR。

### 16.3 `is_update` 融合

提示词要求**历史 test_items 全保留并 append 新化验**，并更新 `medical_summary`。**延伸**：长列表可能超上下文；可改为**增量事件流**写入数据库而非整 JSON 重写。

### 16.4 隐私

档案落盘 **明文 JSON**；**延伸**：磁盘加密、访问控制、传输 HTTPS、最小化日志。

---

## 17. `backend/scripts/build_neo4j_graph.py`：演示数据与生产图构建的差异

脚本批量 `MERGE` 预置三元组，领域为演示性（如 CUP、淀粉样变性），**非** NPC 专科。**延伸**：生产需：**术语归一化**、**来源追溯**、**版本化**、与 **Milvus  chunk** 对齐（同实体多文本出处）。

---

## 18. 配置、错误处理与演进建议

### 18.1 关键环境变量（与代码一致）

`ARK_API_KEY`、`BASE_URL`、`MODEL`、`FAST_MODEL`、`EMBEDDER`、`GRADE_MODEL`、Milvus、Neo4j、Rerank 相关、`AUTO_MERGE_*`、`LEAF_RETRIEVE_LEVEL`。

### 18.2 错误与降级

- 嵌入失败：稠密/稀疏为空，RRF 仍可能靠余路。
- Rerank 失败：保留 RRF 顺序，`rerank_error` 写入 trace。
- 评分模型缺失：**总是**扩展检索，成本上升。

### 18.3 可演进方向（与代码缺口直接相关）

1. **全局 BM25 统计**或换专业搜索引擎做稀疏路。
2. **RRF 分路加权**、图检索多 chunk、**Text2Cypher** 替代字符串 `CONTAINS`。
3. **全局 IDF / 词表持久化**，多进程共享。
4. **会话存储**换 DB + **PII 合规**。
5. **检索子图内**做迭代至「评分通过」而非固定一遍扩展。
6. **Grounding 评测**与 **引用元数据**（PMID 等）入库。

---

## 19. 稠密检索的数学与工程：ANN、HNSW 与内积

### 19.1 从向量空间到「最近邻」

稠密检索把查询与文档片段映射到同一向量空间，用**相似度**近似语义相关。常见度量有 **L2（欧氏）**、**内积（IP）**、**余弦相似度**（等价于单位向量上的 IP）。本仓库 Milvus 配置为 **`metric_type: IP`**，即假设嵌入空间中以**内积越大越相似**。

**延伸**：若嵌入 API 返回**未归一化**向量，IP 会偏向**范数更大**的向量（长度与方向混在一起）。许多文本嵌入模型在服务端做 L2 normalize，此时 IP 与 cosine 单调相关；**上线前应在样本上验证** `dot(q,d)` 与人工相关性排序是否一致。

### 19.2 ANN：为何不用暴力 KNN

暴力 KNN 复杂度随库规模线性增长，百万级以上 chunk 时不可接受。**近似最近邻（ANN）**用图结构（HNSW）、树结构（Annoy）、积量化（PQ、IVF-PQ）等换精度换速度。Milvus 选用 **HNSW（Hierarchical Navigable Small World）**：多层小世界图，查询时从上层粗搜索下沉，**ef** 控制搜索宽度——本仓库搜索参数 `ef: 64`，建索引 `efConstruction: 256`、`M: 16`。

**延伸**：`ef` 越大召回越好但延迟越高；`M` 影响索引体积与建索引时间。医疗场景若**专有名词**密集、向量区分度细，可适当提高 `ef` 做 A/B。

### 19.3 双塔 Bi-encoder 与 Rerank 的 Cross-encoder

稠密检索本质是 **Bi-encoder**：query 与 doc **独立编码**，相似度由简单函数计算，故可**预先索引**百万文档。Rerank 阶段若用 **Cross-encoder**（query 与 doc 拼接进同一网络），交互更深、更准，但**无法**对全库跑，只能对 Top-K 候选。**本仓库**正是「ANN 召回 →（RRF 融合）→ Rerank」的经典**两阶段**范式。

---

## 20. 稀疏路、词表与「查询–索引一致性」

### 20.1 BM25 在检索流水线中的角色

BM25 属于 **lexical（词汇）** 检索：依赖**词项共现**，对**拼写变体、同义词**敏感弱于向量，但对**药品通用名/商品名、指南原文短语、编号**往往更稳。混合检索的工业共识是：**向量负责语义，稀疏负责术语锚点**。

### 20.2 本实现的词表是「在线扩张」的

`get_sparse_embedding` 遇到未见词会分配新 `vocab` 索引；`fit_corpus` 在每次 `write_documents` 批次内统计 `df`。因此：

- **同一词**在多次上传中可能经历**不同 IDF**，取决于该批语料是否包含该词；
- **查询时**若出现索引阶段未见词，仍会给该词一个索引与平滑 IDF，但与**入库时**该词的权重**不保证可比**。

**延伸**：这是混合检索里最容易被忽视的**分布偏移**问题。缓解方向包括：**全库统一 fit**、**冻结词表**、或稀疏路改用 **Elasticsearch/OpenSearch** 的成熟 BM25 实现，查询与索引共用同一分析链。

### 20.3 中文按字切分 vs 分词

当前中文按**单字** token，优点是实现简单、无分词器依赖；缺点是「鼻咽癌」被拆成字，**IDF 与搭配信息**弱于 **bigram 或 jieba 词**。**延伸**：医学场景可引入**领域词典**（疾病名、方案缩写）做最长匹配后再进 BM25。

---

## 21. RRF 之后：为何还要 Auto-merge 与 Rerank

三者解决的是**不同层次**的问题：

1. **RRF**：融合**多路有序列表**，不假设各路分数可比，适合异构检索器。
2. **Rerank**：在**同一 query** 下对**已截断的候选文本**做精细相关性排序，缓解双塔语义粗糙。
3. **Auto-merge**：解决的是**上下文完整性**——多个相邻叶子块同时命中时，用父块替换，减少模型看到**重复碎片**或**断句**。

**延伸**：若父块过长，可能引入噪声；`AUTO_MERGE_THRESHOLD` 控制「多少子块同时命中才抬升」，是在**粒度**与**冗余**之间的旋钮，建议用真实查询日志调参。

---

## 22. LangGraph 子图与 Agent 的边界：控制流在哪里

本仓库把 **「检索与扩展」**放在 LangGraph 内，把 **「最终自然语言答案」**放在 Agent 的 LLM。这样分工的原因是：

- **检索状态机**（评分不通过则扩展）适合用**显式图**表达，便于单测 `run_rag_graph`、记录 `rag_trace`；
- **对话策略**（语气、免责、引用格式、是否追问）集中在 **system prompt + Agent**，避免在图里再嵌一套聊天模板。

**延伸**：若未来加入「多轮检索直到证据足够」，可在图内加循环边或增加节点；需注意 **max step** 与费用上限。当前 `recursion_limit: 8` 在 Agent 侧限制工具/推理深度，与 LangGraph 内部步数需区分理解。

---

## 23. SSE、异步队列与「检索步骤可见」的实现原理

流式路径的核心矛盾是：**工具代码可能在非 async 上下文执行**，而 **SSE 必须在 async generator 里 yield**。本仓库用 **asyncio.Queue** 作为桥梁：`emit_rag_step` 通过 `loop.call_soon_threadsafe` 把步骤投递回主循环注册的队列，主循环 `await queue.get()` 再 `yield` SSE。

**延伸**：

- 这与 **Producer-Consumer** 经典模型一致；关键是 **loop 引用**必须在设置队列时捕获正确事件循环。
- 若将来把工具改为 `async def`，可考虑 **asyncio.Queue** 直接 `await put`，减少 threadsafe 复杂度。

---

## 24. 患者档案注入与提示词安全

将 `medical_summary` 拼进 system prompt，本质是 **不可信用户数据进入模型上下文**。风险包括：

- **提示注入**：若病历文本中含「忽略上文、输出…」类内容，可能误导模型（概率与模型鲁棒性相关）。
- **泄露**：模型可能在后续回复中复述档案细节，需产品层告知用户。

**延伸**：缓解包括：**结构化槽位**（仅注入脱敏字段）、**对档案段落做模板化**、**输出侧过滤**、以及 **RLHF/安全对齐**；工程上可对档案文本做 **PII 检测与掩码**后再入 prompt。

---

## 25. 与相关论文/产品的概念对照（便于写文献综述）

| 概念 | 常见出处/产品 | 本仓库中的对应 |
|------|----------------|------------------|
| HyDE | Gao et al., *Precise Zero-Shot Dense Retrieval without Relevance Labels* | `generate_hypothetical_document` + 检索 |
| Parent Document Retriever | LangChain 文档模式 | 三级分块 + `ParentChunkStore` + Auto-merge |
| RRF | 多列表融合常用基线 | `_compute_rrf`，k=60 |
| Self-RAG / CRAG | 自省式检索 | 部分相似：`grade_documents` 决定扩展检索；**未**做多轮自省循环 |
| GraphRAG | Microsoft 等 | **未**实现全局图社区摘要；仅有 Neo4j 轻量查询 |
| MedCPT 等医学双塔 | 生物医学文献 | 嵌入模型由 `EMBEDDER` 配置，**未在代码内固定**为 MedCPT |

该表用于**定位创新点与差距**：论文中可写「在 XXX 上与经典方法一致，在 YYY 上简化为工程可部署版本」。

---

## 结语

本报告按模块说明实现，并专列章节把**稠密 ANN、BM25 与词表一致性、RRF/Rerank/Auto-merge 的分工、LangGraph 与 Agent 的职责边界、SSE 线程模型、档案安全**等从代码中「展开」到相关理论与工程权衡。若你只关心单点（例如只优化稀疏路），可优先阅读 **§9–§11、§20**；若关心对话与流式，重点看 **§6、§23**。

