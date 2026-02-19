# SuperAgent 项目说明

Agent的项目记录，方便后续持续更新与展示。

## 项目概览
- **核心能力**：
  - LangChain Agent + 自定义工具。
  - 文档上传、切分、向量化后写入 Milvus，支持混合检索（稠密+稀疏）。
  - 会话记忆与摘要，保持长对话上下文。
- **运行形态**：FastAPI 后端 + 纯前端（Vue 3 CDN 单页）+ Milvus 向量库。

## 关键创新点
- **混合检索落地**：稠密向量 + BM25 稀疏向量，Milvus Hybrid Search + RRF 排序，兼顾语义与词匹配。
- **Jina Rerank 接入**：Hybrid/Dense 召回后进行 API 级精排，支持返回 `rerank_score` 并在前端可视化。
- **双向降级**：稀疏生成或 Hybrid 调用失败时自动降级为纯稠密检索，提升稳定性。
- **流式输出（Streaming）**：后端基于 `agent.astream(stream_mode="messages")` 逐 token 推送，前端 SSE + ReadableStream 实现打字机效果。
- **实时 RAG 过程可视化**：检索过程在模型"思考中"阶段就开始展示，通过 `asyncio.Queue` + 后台任务架构实现工具执行期间的实时推送。
- **回答终止功能**：前端 `AbortController` + 后端 `StreamingResponse` 支持用户随时中断正在生成的回答。
- **会话摘要记忆**：自动摘要旧消息并注入系统提示，维持上下文且控制 token。
- **文档处理链路**：上传 → 切分 → 稠密/稀疏向量同步生成 → Milvus 入库，支持重复上传自动清理旧 chunk。
- **工具可扩展**：天气查询示例 + 知识库检索，便于按需增添第三方 API 或企业数据源。
- **RAG 过程可观测**：记录检索、评分、重写与来源信息，前端可展开查看每一步细节。
- **查询重写体系**：Step-Back 与 HyDE 两种扩展方式 + 路由选择，必要时触发重写检索。
- **相关性评分门控**：基于结构化输出的 `grade_documents` 判断是否需要重写检索。
- **实时思考链路展示**：通过 `asyncio` 事件循环穿透技术，实现 Agent 在执行 RAG、评分、重写等同步工具时，实时向前端推送思考步骤（Searching -> Grading -> Rewriting），彻底解决"静默思考"问题。

## 未来迭代（Todo Lists）

### RAG部分

1. 文本切分升级为语义分块（Semantic Chunking）————（待定、效果未必比递归字符分块好）
2. 向量嵌入：新增多模态 embedding 能力
3. 检索优化：实现多查询分解 (Multi-query)、子问题检索 ————（效果不太好，往往简单的问题也会复杂化，额外消耗token）
4. 生成优化：适配多文档场景的 refine 策略
5. 搭建 RAG 评估体系
6. Rerank 策略评估（top_k、candidate_k、召回/精排比例）

### 其他能力拓展

1. 开发 SQL assistant Skill
2. 实现暂停功能与人工介入机制
3. 新增问题类型判断，简单问题跳过复杂处理流程
4. 扩展网络搜索能力
5. 支持多步骤规划与任务并行执行
6. 搭建路由器节点，由 LLM 自主判断下一步动作
7. 优化 memory 管理：集成 MemO、LangMem 等方案
8. multi-agent：工具过多，把工具拆分给职责明确的专业化agent，提升工具选择的准确性和整体稳定性
9. 历史记录会话名称可修改

### 后端服务建设

1. 实现用户注册登录、密码加密、权限管理，基于 sqlalchemy 搭建 ORM 数据库
2. 聊天记录落地数据库，引入 redis 做缓存优化

## 目录与架构
- 后端：`SuperAgent/backend/`
  - [app.py](backend/app.py)：FastAPI 入口、CORS、静态资源挂载。
  - [api.py](backend/api.py)：聊天、会话管理、文档管理接口。
  - [agent.py](backend/agent.py)：LangChain Agent、会话存储、摘要逻辑。
  - [tools.py](backend/tools.py)：天气查询、知识库检索工具。
  - [embedding.py](backend/embedding.py)：稠密向量 API 调用 + BM25 稀疏向量生成。
  - [document_loader.py](backend/document_loader.py)：PDF/Word 加载与分片。
  - [milvus_writer.py](backend/milvus_writer.py)：向量写入（稠密+稀疏）。
  - [milvus_client.py](backend/milvus_client.py)：Milvus 集合定义、混合检索。
  - [schemas.py](backend/schemas.py)：Pydantic 请求/响应模型。
- 前端：`SuperAgent/frontend/`
  - [index.html](frontend/index.html) + [script.js](frontend/script.js) + [style.css](frontend/style.css)：Vue 3 + marked + highlight.js，提供聊天、历史会话、文档上传/删除界面。
- 数据：`SuperAgent/data/`
  - `customer_service_history.json`：会话落盘存储。
  - `documents/`：上传文档原文件。
- 向量库：Milvus（可由 `docker-compose` 或自建服务提供）。

## 核心流程

### 1) 项目全链路（端到端）
1. 用户在前端输入问题，调用 `POST /chat/stream`（流式）。
2. FastAPI `api.py` 返回 `StreamingResponse(media_type="text/event-stream")`。
3. LangChain Agent 根据问题类型决定是否调用工具：
  - 天气问题 → `get_current_weather`
  - 知识问答 → `search_knowledge_base`
4. 若命中知识库工具，进入 `rag_pipeline.py` 执行检索工作流，各阶段通过 `emit_rag_step()` 实时推送到前端。
5. 检索结果与 RAG Trace 一起返回，Agent 流式生成最终回答（逐 token 推送）。
6. 前端 ReadableStream 逐块解析 SSE，打字机效果实时渲染。
7. 同时消息落盘到 `customer_service_history.json`，支持历史会话回放。

### 2) RAG 全链路（重点）
1. **初次召回**：`retrieve_initial`
  - 调用 `retrieve_documents`。
  - 先做 Milvus Hybrid 检索（Dense + Sparse + RRF）。
  - 取更大候选集后走 Jina Rerank 精排。
2. **相关性打分门控**：`grade_documents`
  - 使用结构化输出打分 `yes/no`。
  - `yes` 直接进入生成回答；`no` 进入重写阶段。
3. **查询重写路由**：`rewrite_question`
  - 在 `step_back / hyde / complex` 中选择策略。
  - 生成 `rewrite_query`、`step_back_question`、`hypothetical_doc` 等中间结果。
4. **二次召回**：`retrieve_expanded`
  - 对重写后的查询（或 HyDE 文档）再次检索。
  - 结果去重后返回上下文。
5. **答案生成**：Agent 结合上下文生成最终回答。
6. **可观测追踪**：返回 `rag_trace`，包括
  - 评分结果与路由决策
  - 重写策略与重写内容
  - 初次/二次检索结果
  - 检索分数 `score` 与精排分数 `rerank_score`

### 3) 文档入库链路
1. 前端上传 PDF/Word 到 `POST /documents/upload`。
2. `document_loader.py` 解析文档并切分 chunk。
3. `embedding.py` 生成 Dense 向量与 BM25 Sparse 向量。
4. `milvus_writer.py` 将向量 + 元数据写入 Milvus。
5. 后续检索可直接利用新文档参与召回。

### 4) 会话记忆链路
1. 每轮问答按 `user_id/session_id` 写入本地存储。
2. 当消息过长时触发摘要压缩，保留长期上下文。
3. 前端可通过会话接口读取、删除历史对话。

## 技术栈
- 后端：FastAPI、LangChain Agents、Pydantic、Uvicorn。
- 向量与检索：Milvus（HNSW 稠密索引 + SPARSE_INVERTED_INDEX 稀疏索引）、RRF 融合、Jina Rerank 精排。
- 嵌入与稀疏：自定义 API 调用获取稠密向量；BM25 手写稀疏向量；同时输出双塔特征。
- 前端：Vue 3 (CDN)、marked、highlight.js、纯静态部署。
- 工具链：dotenv 配置、requests、langchain_text_splitters、langchain_community.loaders。

## 环境变量
需在仓库根目录或运行环境配置：
- 模型相关：`ARK_API_KEY`、`MODEL`、`BASE_URL`、`EMBEDDER`
- Rerank 相关：`RERANK_MODEL`、`RERANK_BINDING_HOST`、`RERANK_API_KEY`
- Milvus：`MILVUS_HOST`、`MILVUS_PORT`、`MILVUS_COLLECTION`
- 工具：`AMAP_WEATHER_API`、`AMAP_API_KEY`

## API 速览
- `POST /chat`：聊天（非流式），入参 `message`、`user_id`、`session_id`。
- `POST /chat/stream`：聊天（流式 SSE），入参同上，返回 `text/event-stream`。
- `GET /sessions/{user_id}`：列出会话。
- `GET /sessions/{user_id}/{session_id}`：拉取某会话消息。
- `DELETE /sessions/{user_id}/{session_id}`：删除会话。
- `GET /documents`：列出已入库文档及 chunk 数。
- `POST /documents/upload`：上传并向量化 PDF/Word。
- `DELETE /documents/{filename}`：删除指定文档的向量数据。

## 流式输出与实时检索过程 — 技术细节

#### 1. 跨线程事件调度（Cross-Thread Event Scheduling）
这是一个解决 **"同步工具阻塞异步事件循环"** 问题的关键架构设计，常用于 Python 异步 Web 服务与 CPU 密集型/IO 密集型任务的混合场景。

**痛点**：
FastAPI 运行在单线程的 asyncio Event Loop 上。为了不阻塞主线程，LangChain 通常将同步工具（如 `search_knowledge_base`）放到 `ThreadPoolExecutor` 中运行。但在子线程中，无法直接访问主线程的 `asyncio.Queue`，且 `asyncio.get_event_loop()` 通常会失败。

**解决方案**：
我们采用了 **"Global Loop Capture + Threadsafe Callback"** 模式：

1.  **Loop 捕获 (Main Thread)**:
    在 Agent 开始生成前，主线程调用 `set_rag_step_queue()`。此时我们捕获当前的运行循环：`_RAG_STEP_LOOP = asyncio.get_running_loop()` 并保存为全局变量。
2.  **跨线程发射 (Worker Thread)**:
    当 RAG 工具在子线程运行时，调用 `emit_rag_step()`。
    函数内部使用 `_RAG_STEP_LOOP.call_soon_threadsafe(queue.put_nowait, step_data)`。
3.  **原理**:
    `call_soon_threadsafe` 是 asyncio 唯一允许从其他线程向 Loop 注入回调的方法。它相当于向主 Loop 的"待办事项箱"投递了一个任务（即 `queue.put_nowait`），主 Loop 会在下一次 tick 立即执行它，从而实现数据的平滑流转。

```python
# 核心代码摘要 (tools.py)
def set_rag_step_queue(queue):
    global _RAG_STEP_QUEUE, _RAG_STEP_LOOP
    _RAG_STEP_QUEUE = queue
    # 关键：在主线程捕获 Loop
    _RAG_STEP_LOOP = asyncio.get_running_loop()

def emit_rag_step(icon, label):
    # 关键：从子线程安全调度回主 Loop
    if _RAG_STEP_LOOP and not _RAG_STEP_LOOP.is_closed():
        _RAG_STEP_LOOP.call_soon_threadsafe(
            _RAG_STEP_QUEUE.put_nowait, 
            {"icon": icon, "label": label}
        )
```

### 2. 混合检索（Hybrid Search）深度实现
项目并非简单调用 Milvus 接口，而是手动构建了工业级的稀疏-稠密双塔检索：

- **Dense Pathway**: 使用 OpenAI `text-embedding-3-small` 生成 1536 维稠密向量，捕捉语义匹配。
- **Sparse Pathway**:
    - 在 `embedding.py` 中实现了基于 `jieba` 分词的自定义 BM25 算法。
    - 生成 `{word_id: tf_idf_score}` 格式的稀疏向量，模拟 ElasticSearch 的关键词匹配能力。
- **Milvus 融合**:
    - 使用 Milvus 的 `AnnSearchRequest` 同时发起两个请求。
    - **RRFRanker (Reciprocal Rank Fusion)**: 采用 `k=60` 的倒数排名融合算法，将两路召回结果无参数化地合并，避免了加权求和中调节 `alpha` 参数的困难。

### 3. 前端 "Thinking State Machine"
前端 `script.js` 维护了一个微型状态机来处理通过 SSE 传回的复杂混合流：

1.  **Idle**: 等待用户输入。
2.  **Thinking (Initial)**: 收到请求，创建消息气泡，`isThinking=true`，显示默认动画。
3.  **Thinking (Active RAG)**: 收到 `type: rag_step` 事件。
    - 状态机保持 `isThinking=true`。
    - 动态更新 Header 文字（如 "正在重写查询..."）。
    - 向 `ragSteps` 数组追加步骤，触发 Vue 列表渲染。
4.  **Streaming**: 收到第一个 `type: content` 事件。
    - **立即切换**: 设置 `isThinking=false`。
    - 并不销毁气泡，而是隐藏思考 header，开始在同一气泡内追加 Markdown 文本。
    - 这样实现了从"思考"到"回答"的无缝视觉过渡，没有突兀的 UI 抖动。

## 整体架构

```
用户发送消息
    │
    ▼
POST /chat/stream → StreamingResponse(text/event-stream)
    │
    ▼
chat_with_agent_stream()
    │
    ├── 创建统一输出队列 (asyncio.Queue)
    ├── 设置 _RagStepProxy → emit_rag_step() 的输出直接入队
    ├── 启动 _agent_worker 后台任务 (asyncio.create_task)
    │     └── agent.astream(stream_mode="messages") 逐 token 产出
    │           ├── AIMessageChunk (文本) → {"type": "content"} 入队
    │           └── tool_call_chunks (工具调用) → 跳过
    │
    └── 主循环：await output_queue.get() → yield SSE
          ▲
          │ (并发) RAG 工具在线程池中执行
          │ emit_rag_step() → loop.call_soon_threadsafe → 入队
          │ {"type": "rag_step"} 立即从队列取出并推送到前端
```

### 后端实现

#### 1) 流式生成 (`agent.py`)
- 使用 LangGraph `agent.astream(stream_mode="messages")` 获取逐 token 的 `AIMessageChunk`。
- 过滤 `tool_call_chunks`，只转发文本内容给前端。
- **关键设计**：Agent 流式循环运行在 `asyncio.create_task` 后台任务中，主生成器只负责从统一 `output_queue` 取事件并 yield。这样 RAG 步骤在工具执行期间（agent 阻塞等待工具返回时）仍然可以实时推送到前端。

#### 2) 实时 RAG 步骤推送 (`tools.py` + `rag_pipeline.py`)
- `emit_rag_step(icon, label, detail)` 通过 `asyncio.get_event_loop().call_soon_threadsafe()` 将步骤从同步线程安全地推送到异步队列。
- `_RagStepProxy` 代理对象将原始 step dict 包装为 `{"type": "rag_step", "step": {...}}` 后放入统一输出队列，**无需额外 relay 任务**。
- `rag_pipeline.py` 在每个关键节点发射步骤：
  - `retrieve_initial` → "正在检索知识库..."
  - `grade_documents` → "正在评估文档相关性..."
  - `rewrite_question` → "正在重写查询..."（含策略选择）
  - `retrieve_expanded` → "使用扩展查询重新检索..."

#### 3) SSE 协议格式
每个事件格式：`data: {JSON}\n\n`，类型字段：
- `content`：文本 token（打字机效果）
- `rag_step`：实时检索步骤（`{icon, label, detail}`）
- `trace`：完整 RAG 追踪信息（回答完成后发送）
- `error`：错误信息
- `[DONE]`：流结束标记

#### 4) StreamingResponse 配置 (`api.py`)
```python
StreamingResponse(
    event_generator(),
    media_type="text/event-stream",
    headers={
        "Cache-Control": "no-cache, no-store, must-revalidate",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",  # 禁用 Nginx 缓冲
    },
)
```

### 前端实现

#### 1) ReadableStream 解析 (`script.js`)
- 使用 `response.body.getReader()` + `TextDecoder` 逐块读取。
- 手动按 `\n\n` 分割 SSE 事件，解析 `data: ` 前缀后的 JSON。
- `content` 事件追加到消息文本；`rag_step` 事件追加到检索步骤数组并同步更新思考状态文字。

#### 2) 思考气泡二合一
- 发送消息后立即创建带 `isThinking: true` 的气泡，显示跳动圆点 + 动态文字。
- 收到 `rag_step` 时，`thinkingLabel` 更新为当前步骤（如"正在检索知识库..."）。
- 收到第一个 `content` token 时，`isThinking = false`，同一气泡无缝切换为正常文本流。
- **不存在两个分离的气泡**，从思考 → 检索 → 回答全程在同一个气泡内完成。

#### 3) Vue 3 响应式注意事项
- 通过 `this.messages[botMsgIdx]` 索引访问（而非缓存对象引用），确保拿到 Vue 的 reactive proxy。
- `ragSteps` 数组通过 `push()` 触发响应式更新。

### 终止功能

#### 前端
- 发送按钮在 `isLoading` 期间切换为红色终止按钮（`v-if/v-else`）。
- 点击调用 `AbortController.abort()`，取消正在进行的 `fetch` 请求。
- 捕获 `AbortError`，在气泡中显示"(已终止回答)"。

#### 后端
- FastAPI 的 `StreamingResponse` 在客户端断开连接（如浏览器触发 `abort()` 或关闭标签页）时，会检测到 socket 断开。
- Python 的生成器协议会向响应生成器抛出 `GeneratorExit` 异常。
- **实现细节**：采用**主动防御式编程**，显式捕获 `GeneratorExit` 并执行 `agent_task.cancel()`。
- **为什么不依赖框架自动取消？**：虽然 Starlette/FastAPI 拥有基于 `BaseHTTPMiddleware` 的级联取消机制（Cascading Cancellation），但在复杂的后台任务结构或特定中间件配置下，取消信号可能延迟或在传递链中丢失。显式调用 `.cancel()` 提供了**确定性的资源回收**保证。
- **即时止损原理**：`agent_task.cancel()` 会立即在任务挂起点注入 `asyncio.CancelledError`。对于流式 LLM 请求，这会触发 `httpx` 关闭 TCP 连接。服务端（OpenAI 等）检测到 client 掉线后会立即停止推理，从而实现**真正的 Token 节省**。

## 更新日志

### 2026-02-19 RAG 实时思考链路修复
- **问题**：Agent 在执行同步工具（如 `search_knowledge_base`）时，由于运行在线程池中，无法正确获取主线程的 asyncio 事件循环，导致 `emit_rag_step` 事件丢失，前端"思考中"气泡一直静止。
- **修复**：
  1. **Backend (`tools.py`)**：在 `set_rag_step_queue` 中显式捕获主线程的 `loop`。
  2. **Backend (`tools.py`)**：更新 `emit_rag_step` 使用捕获的 `_RAG_STEP_LOOP.call_soon_threadsafe` 跨线程调度事件。
  3. **Frontend (`script.js`)**：在发送消息时初始化空的 `ragSteps: []` 数组，确保 Vue 响应式系统能立即追踪后续的 push 操作。
- **效果**：用户提问后，思考气泡内实时跳动显示检索步骤（如"🔍 正在检索知识库..." -> "📊 正在评估文档相关性..."），不再只有静态的"正在思考中..."。


