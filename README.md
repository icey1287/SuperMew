# SuperMew 项目说明

Agent的项目记录，方便后续持续更新与展示。

[![zread](https://img.shields.io/badge/Ask_Zread-_.svg?style=flat&color=00b0aa&labelColor=000000&logo=data%3Aimage%2Fsvg%2Bxml%3Bbase64%2CPHN2ZyB3aWR0aD0iMTYiIGhlaWdodD0iMTYiIHZpZXdCb3g9IjAgMCAxNiAxNiIgZmlsbD0ibm9uZSIgeG1sbnM9Imh0dHA6Ly93d3cudzMub3JnLzIwMDAvc3ZnIj4KPHBhdGggZD0iTTQuOTYxNTYgMS42MDAxSDIuMjQxNTZDMS44ODgxIDEuNjAwMSAxLjYwMTU2IDEuODg2NjQgMS42MDE1NiAyLjI0MDFWNC45NjAxQzEuNjAxNTYgNS4zMTM1NiAxLjg4ODEgNS42MDAxIDIuMjQxNTYgNS42MDAxSDQuOTYxNTZDNS4zMTUwMiA1LjYwMDEgNS42MDE1NiA1LjMxMzU2IDUuNjAxNTYgNC45NjAxVjIuMjQwMUM1LjYwMTU2IDEuODg2NjQgNS4zMTUwMiAxLjYwMDEgNC45NjE1NiAxLjYwMDFaIiBmaWxsPSIjZmZmIi8%2BCjxwYXRoIGQ9Ik00Ljk2MTU2IDEwLjM5OTlIMi4yNDE1NkMxLjg4ODEgMTAuMzk5OSAxLjYwMTU2IDEwLjY4NjQgMS42MDE1NiAxMS4wMzk5VjEzLjc1OTlDMS42MDE1NiAxNC4xMTM0IDEuODg4MSAxNC4zOTk5IDIuMjQxNTYgMTQuMzk5OUg0Ljk2MTU2QzUuMzE1MDIgMTQuMzk5OSA1LjYwMTU2IDE0LjExMzQgNS42MDE1NiAxMy43NTk5VjExLjAzOTlDNS42MDE1NiAxMC42ODY0IDUuMzE1MDIgMTAuMzk5OSA0Ljk2MTU2IDEwLjM5OTlaIiBmaWxsPSIjZmZmIi8%2BCjxwYXRoIGQ9Ik0xMy43NTg0IDEuNjAwMUgxMS4wMzg0QzEwLjY4NSAxLjYwMDEgMTAuMzk4NCAxLjg4NjY0IDEwLjM5ODQgMi4yNDAxVjQuOTYwMUMxMC4zOTg0IDUuMzEzNTYgMTAuNjg1IDUuNjAwMSAxMS4wMzg0IDUuNjAwMUgxMy43NTg0QzE0LjExMTkgNS42MDAxIDE0LjM5ODQgNS4zMTM1NiAxNC4zOTg0IDQuOTYwMVYyLjI0MDFDMTQuMzk4NCAxLjg4NjY0IDE0LjExMTkgMS42MDAxIDEzLjc1ODQgMS42MDAxWiIgZmlsbD0iI2ZmZiIvPgo8cGF0aCBkPSJNNCAxMkwxMiA0TDQgMTJaIiBmaWxsPSIjZmZmIi8%2BCjxwYXRoIGQ9Ik00IDEyTDEyIDQiIHN0cm9rZT0iI2ZmZiIgc3Ryb2tlLXdpZHRoPSIxLjUiIHN0cm9rZS1saW5lY2FwPSJyb3VuZCIvPgo8L3N2Zz4K&logoColor=ffffff)](https://zread.ai/icey1287/SuperMew)

## 本地部署

### 1) 环境准备
- Python `3.12+`
- 包管理建议：`uv`（也支持 `pip`）
- Docker / Docker Compose（用于启动 Milvus 依赖）

### 2) 使用 pyproject 安装依赖
在项目根目录执行：

```bash
# 方式 A：推荐（uv）
uv sync

# 运行服务
uv run alembic upgrade head
uv run python backend/app.py
# 或
uv run uvicorn backend.app:app --host 0.0.0.0 --port 8000 --reload
```

```bash
# 方式 B：pip
python -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -e .

# 运行服务
python backend/app.py
# 或
uvicorn backend.app:app --host 0.0.0.0 --port 8000 --reload
```

### 3) 创建 `.env` 文件

```bash
cp .env.example .env
```

按需编辑 `.env` 中的 API Key、模型名与连接地址；变量说明见 `.env.example` 内注释。

### 4) Docker 部署（数据库 + 缓存 + 向量库）
当前仓库的 `docker-compose.yml` 同时承载业务依赖与 Milvus 依赖：
- 业务依赖：`postgres`、`redis`
- 向量依赖：`etcd`、`minio`、`standalone`、`attu`

```bash
# 启动向量库依赖
docker compose up -d

# 查看服务状态
docker compose ps

# 查看日志（可选）
docker compose logs -f standalone
```

端口说明：
- PostgreSQL：`5432`
- Redis：`6379`
- Milvus：`19530`
- Milvus 健康检查：`9091`
- MinIO API：`9000`
- MinIO Console：`9001`
- Attu：`8080`

### 5) 编译前端代码（首次运行及修改后必做）
首次运行或前端代码修改后，需要进行前端依赖安装和构建编译，以生成供后端托管的 `frontend/dist` 目录：

```bash
cd frontend

# 安装前端依赖
npm install

# 编译构建静态包
npm run build
```

编译完成后，构建产物会自动保存在 `frontend/dist/` 中，后端启动时会自动挂载此目录。

### 6) 启动应用并访问
在 Milvus 启动且前端编译完成后，返回项目根目录并运行后端应用：

```bash
# 若当前处于 frontend 目录下，先返回项目根目录
cd ..

# 运行后端应用
uv run uvicorn backend.app:app --host 0.0.0.0 --port 8000 --reload
```

另开一个终端启动持久化索引 worker；API 不再在请求进程内解析和向量化文档：

```bash
uv run python -m backend.workers.indexing
```

API 与 worker 必须共享同一个 `UPLOAD_DIR`。`GET /health/ready` 会检查近期 worker heartbeat；本地确实不启动 worker 时才可显式设置 `INDEX_WORKER_REQUIRED=false`。

当前 `docker-compose.yml` 与 `docker-compose.prod.yml` 只管理 PostgreSQL、Redis、Milvus 等依赖，不会启动 API 或 indexing worker。生产部署必须由 systemd、Kubernetes 或等价 supervisor 分别管理两个进程，并为它们挂载同一持久上传目录、配置自动重启和 termination grace。

启动或发布前可验证 Skill/Tool Registry。Skill 正文只在显式 slash、可信路由或 `describe_skill` 后向 Agent 披露；`tool_search` 只返回当前 Run 已授权的 deferred schema：

```bash
uv run --frozen python -m backend.tools.registry_cli validate
uv run --frozen python -m backend.tools.registry_cli list-skills --role user
uv run --frozen python -m backend.tools.registry_cli list-tools --role user
```

新增或升级 Skill 的目录、manifest、hash pin、回滚和 Secret 规则见 `docs/runbooks/skill-tool-registry.md`。

SQL Assistant 默认关闭。启用时必须使用与应用写库不同 username 的 PostgreSQL 只读账号、
显式 schema/table allowlist、RLS 和 `admin` 角色；`sql_schema` / `sql_query` 以 deferred Tool
按需披露，不满足开关、Secret 或 `private-data` policy 时不会进入模型上下文：

```dotenv
SQL_ASSISTANT_ENABLED=true
SQL_ASSISTANT_DSN=postgresql://supermew_sql_reader:<secret>@db/analytics?sslmode=require
SQL_ASSISTANT_EXPECTED_ROLE=supermew_sql_reader
SQL_ASSISTANT_ALLOWED_SCHEMAS=analytics
SQL_ASSISTANT_ALLOWED_TABLES=analytics.orders,analytics.customers
SQL_ASSISTANT_SENSITIVE_COLUMNS=analytics.customers.email
```

配置独立 reader、验证权限/成本门禁、轮换和紧急禁用步骤见
`docs/runbooks/sql-assistant.md`。启用后由管理员使用 `/sql-assistant` 激活；Skill 只允许读取
授权 catalog 和执行单条有界只读查询，不提供任何写操作。

Web Research 同样默认关闭。启用后，`user` 与 `admin` 可用 `/web-research` 激活；
`web_search` / `web_fetch` 仍是 deferred Tool，只有 feature flag、已配置的 Brave Search
Secret、active Skill 和 `restricted` network policy 同时满足时才披露。Secret 只在进程级
Runtime 内使用，不进入 prompt、Run state、checkpoint、事件或审计：

```dotenv
WEB_RESEARCH_ENABLED=true
BRAVE_SEARCH_API_KEY=<由 Secret 管理系统注入>
```

`web_fetch` 不接受模型提供的任意 URL，只接受同一 Run 内 `web_search` 返回的不可变
`evidence_id`；Runtime 再通过 SSRF policy、DNS pin、逐跳 redirect 复核、内容类型与字节预算
抓取公网页面。模型只输出 Run-local `webcite:` token，服务端终态校验后才渲染 canonical
Markdown 链接；事实必须就近引用，并披露来源冲突、检索时间与覆盖缺口。
完整预算、上线验证、轮换和紧急禁用见 `docs/runbooks/web-research.md`。

所有 Registry-bound Tool 在 handler 前还会经过确定性 Guardrail，决策只有 `ALLOW`、`DENY`
与 `REQUIRE_APPROVAL`。`shell`、`code`、`process`、`network-private`、`high-risk` group 永久
hard deny；Web fetch 的 destination capability 绑定 user、tenant、Thread、Run、Tool、network
policy 与 resource scope，模型始终只能提交 `evidence_id`。Guardrail 详细 reason 与 policy
version/hash 只进入脱敏 ToolAudit，不进入公开 Run Event/SSE。

隔离 Sandbox 默认关闭。它使用本地 digest-pinned image、固定非 root runner、只读 rootfs、
无网络、无 bind mount 和有大小上限的 tmpfs workspace；执行结束只返回有界 stdout/stderr，
不导出文件或宿主路径。生产环境必须连接专用 rootless Docker daemon：

```dotenv
SANDBOX_ENABLED=true
SANDBOX_ADAPTER=docker
SANDBOX_DOCKER_IMAGE=sha256:<local-image-sha256>
SANDBOX_DOCKER_HOST=unix:///path/to/supermew-rootless.sock
SANDBOX_REQUIRE_ROOTLESS=true
```

当前没有互动审批 UX。只有 trusted admin 能在创建 durable Run 时通过
`approved_tools=["sandbox_execute"]` 预先授权；grant 绑定该 Run 完整身份，Runtime 构造和每次
执行都会复核。Sandbox 启用但 daemon/image 不 ready 时 `/health/ready` 返回 503；关闭时不会
探测 Docker，也不影响 readiness。发布、烟雾测试、审计和事件响应见
`docs/runbooks/guardrails-and-sandbox.md`。

浏览器访问：
- 前端页面：`http://127.0.0.1:8000/` （后端静态托管编译后的 `frontend/dist` 资源）
- API 文档：`http://127.0.0.1:8000/docs`

### 7) 前端开发与调试（可选）
前端基于 Vite + Vue 3 开发。若需要进行前端代码开发与调试：

```bash
cd frontend

# 1. 启动本地开发服务（运行于 http://localhost:3000，内置反向代理至 FastAPI 后端 8000 端口）
npm run dev

# 2. 编译生产包（构建结果将输出至 frontend/dist/ 目录中，供后端静态托管）
npm run build
```

## 项目概览
- **核心能力**：
  - LangChain Agent + 自定义工具。
  - 文档上传后执行三级滑动窗口分块，叶子分块向量化写入 Milvus，父级分块写入 PostgreSQL。
  - 用户注册/登录、JWT 鉴权、基于角色的 RBAC 权限控制（admin/user）。
  - 会话记忆与摘要，聊天与历史记录落地 PostgreSQL，并引入 Redis 缓存热点会话与父文档。
- **运行形态**：FastAPI 后端 + 现代工程化前端（Vite + Vue 3 + TypeScript + Pinia）+ Milvus 向量库。

## 关键创新点
- **混合检索落地**：稠密向量 + BM25 稀疏向量，Milvus Hybrid Search + RRF 排序，兼顾语义与词匹配。
- **低延迟复杂度规划与并行 Sub-Agent 流程**：明显的单事实问题由本地规则直接进入检索；其余问题由 FAST_MODEL 一次完成复杂度判断和 2-4 个子问题规划。复杂问题通过 LangGraph `Send` 并行执行各子问题的“检索 → 证据评判”，最终在 Synthesis 节点去重合成。
- **纠错型 RAG（Corrective RAG）与单选查询重写**：检索后由独立的 GRADE_MODEL 结构化判断证据相关性、可回答性与歧义。证据不足时，FAST_MODEL 在一次结构化调用中选择 Step-back 或 HyDE，只执行选中的一次重写检索和一次复评。
- **Jina Rerank 接入**：Hybrid/Dense 召回后进行 API 级精排，支持返回 `rerank_score` 并在前端可视化。
- **双向降级**：稀疏生成或 Hybrid 调用失败时自动降级为纯稠密检索，提升稳定性。
- **可恢复流式输出**：Run Event 先写入持久 Journal，再通过 SSE 投影；前端使用 sequence 去重、`Last-Event-ID` 重连与 `/events` 补放实现打字机效果。
- **实时 RAG 过程可视化**：检索、评分与重写通过 `tool.progress`、`retrieval.*` 等 Event v1 事件展示，刷新或断网后仍可重放。
- **真实回答终止**：前端调用 `POST /v1/runs/{run_id}/cancel` 请求取消后端 Run，并等待 `run.cancelled` 或其他权威 terminal Event。
- **会话摘要记忆**：自动摘要旧消息并注入系统提示，维持上下文且控制 token。
- **文档处理链路**：上传 → 切分 → 稠密/稀疏向量同步生成 → Milvus 入库，支持重复上传自动清理旧 chunk。
- **Milvus 2.5+ 原生 BM25 混合检索**：彻底摒弃本地客户端手写 BM25 序列化和统计同步的繁琐设计。通过在 Milvus 集合 schema 中为 `text` 字段绑定 `FunctionType.BM25` 计算函数，由向量数据库在服务端原生提取稀疏特征，保证高效率的 Dense + Sparse 混合检索与完美的统计对齐。
- **三级分块 + Auto-merging**：L1/L2/L3 三层滑窗切分；检索时优先召回 L3，满足阈值后自动合并到父块（L3->L2->L1）。
- **Leaf-only 向量化存储**：仅叶子分块写入 Milvus，父块写入 DocStore，减少向量冗余并保留上下文聚合能力。
- **工具可扩展**：天气查询示例 + 知识库检索，便于按需增添第三方 API 或企业数据源。
- **RAG 过程可观测**：记录检索、评分、重写与来源信息，前端可展开查看每一步细节。
- **查询重写体系**：证据不足时由 FAST_MODEL 在 Step-back 与 HyDE 中单选一种，并只执行一次二次检索，控制模型调用次数与最坏延迟。
- **相关性评分门控**：基于结构化输出的 `grade_documents` 判断是否需要重写检索。
- **实时执行链路展示**：Agent 的工具与 RAG 阶段统一写入 Event Journal，再由前端 reducer 投影 Searching → Grading → Rewriting 等操作性步骤，不展示模型私有推理。

## 未来迭代（Todo Lists）

### RAG部分

#### 数据层、Chunk分块

1. 先做文档结构解析，按文档结构做粗拆分，再用递归字符分块兜底，保证打的主题单元不被拆分（2000-3000token）；再用语义分块做精细化拆分，控制单块大小（512-1024token）
2. 代码块、表格、图片特殊处理
3. 实现 ParentDocument/Auto-merging Retriever 策略 --done

#### 召回层

1. BM25的k1和b新增参数扫描
2. RRF额外做BM25和dense的权重，可以通过AB test确定
3. 做一个小型标注集比较dense only、sparse only、hybrid、hybrid + rerank的gold chunk

#### 生成层

1. 子问题分解（CoT、专门的分解小模型、判断分几个子问题）
2. 多文档Refine（一次拼接、串行Refine）
3. 多文档冲突处理（A文档说X，B文档说非X），回答中显式输出“来源存在冲突”

#### 其他

1. 向量嵌入：新增多模态 embedding 能力
2. 搭建 RAG 评估体系
3. Rerank 策略评估（top_k、candidate_k、召回/精排比例）

### 其他能力拓展

1. 开发 SQL assistant Skill
2. 实现暂停功能与人工介入机制 --done
3. 新增问题类型判断，简单问题跳过复杂处理流程
4. 扩展网络搜索能力 --done
5. 支持多步骤规划与任务并行执行
6. 搭建路由器节点，由 LLM 自主判断下一步动作
7. 优化 memory 管理：集成 MemO、LangMem 等方案
8. multi-agent：工具过多，把工具拆分给职责明确的专业化agent，提升工具选择的准确性和整体稳定性
9. 历史记录会话名称可修改
10. 死循环检测与恢复：_is_stuck + attempt_loop_recovery

### 后端服务建设（本轮已完成）

1. 账号体系与权限体系
- 新增注册登录接口：`/auth/register`、`/auth/login`。
- 新增用户信息接口：`/auth/me`。
- 引入 JWT 鉴权中间能力：请求通过 Bearer Token 识别当前用户。
- 权限隔离：
  - `admin`：可执行文档上传、删除、文档列表查询。
  - `user`：仅可聊天、查询和删除自己的会话历史。

2. 数据库建模与持久化迁移
- 使用 SQLAlchemy 建立核心模型：`User`、`ChatSession`、`ChatMessage`、`ParentChunk`。
- 聊天历史由本地 JSON 迁移到 PostgreSQL。
- 父级分块文档（L1/L2）由本地 JSON 迁移到 PostgreSQL。

3. Redis 缓存策略
- 会话消息缓存：按 `user + session` 维度缓存消息列表。
- 会话列表缓存：按 `user` 维度缓存会话摘要列表。
- 父文档缓存：按 `chunk_id` 缓存父级分块内容。
- 写入/删除后执行缓存失效，保证一致性。

4. 密码安全与兼容
- 新注册用户采用 PBKDF2-SHA256 存储密码哈希（避免 bcrypt 后端兼容问题）。
- 登录校验兼容历史 bcrypt 哈希，支持平滑迁移。

## 目录与架构
- 后端：`backend/`（分层包结构，统一 `from backend.xxx import`）
  - [app.py](backend/app.py)：FastAPI 入口、CORS、静态资源挂载。
  - `api/`：HTTP 层
    - [router.py](backend/api/router.py)：路由聚合。
    - `routes/`：`auth`、`sessions`、`runs`、`documents` 分文件；`chat` 仅保留返回 `410 Gone` 的退役入口。
    - [resources.py](backend/api/resources.py)：Milvus / 上传目录等共享资源。
  - `runs/`：持久化 Run 创建、幂等与 Thread 并发、owner lease/fencing、取消、HITL resume 和执行调度。
  - `events/`：Event v1 contract、PostgreSQL Journal/outbox、Redis 通知与可恢复 SSE Adapter。
  - `agent/`：`AgentRuntimeFactory`、固定中间件链与 Run-local Runtime Context。
  - `chat/`：历史对话 Implementation 与会话投影；不再作为公开执行 Interface，`service.py` 仅供内部兼容测试。
    - [request_context.py](backend/chat/request_context.py)：Run-local RAG step、RAG trace 与工具预算上下文。
    - [storage.py](backend/chat/storage.py)：append-only 消息与 Thread 历史读取。
  - `guardrails/`：Tool 调用前的确定性 policy、Run-bound approval 与 destination capability。
  - `sandbox/`：隔离执行契约、进程级 Runtime，以及 disabled/Docker Adapter。
  - `rag/`：检索增强
    - [pipeline.py](backend/rag/pipeline.py)：LangGraph RAG 工作流。
    - [utils.py](backend/rag/utils.py)：混合检索、Rerank、Auto-merging。
  - `indexing/`：文档入库与向量
    - [embedding.py](backend/indexing/embedding.py)：稠密 + BM25 稀疏向量。
    - [document_loader.py](backend/indexing/document_loader.py)：PDF/Word/Excel 分块。
    - [milvus_client.py](backend/indexing/milvus_client.py)、[milvus_writer.py](backend/indexing/milvus_writer.py)。
    - [parent_chunk_store.py](backend/indexing/parent_chunk_store.py)：父级分块 DocStore。
  - `tools/`：Tool/Skill Registry 接入 Adapter（知识库、天气、SQL、Web、Sandbox）。
  - `infra/`：[database.py](backend/infra/database.py)、[cache.py](backend/infra/cache.py)、[auth.py](backend/infra/auth.py)。
  - `db/`：[models.py](backend/db/models.py)：ORM 模型。
  - `schemas/`：Pydantic 请求/响应（auth / chat / documents）。
  - `documents/`：Document Catalog、两阶段发布、持久 indexing/cleanup worker。
  - `workers/`：[indexing.py](backend/workers/indexing.py)：独立索引 worker 进程入口。
- 前端：`frontend/`
  - 采用现代工程化设计（Vite + Vue 3 + TypeScript + Pinia + Axios + Sass）。
  - **前端工程架构与状态流**：
    - **Pinia 状态存储**：
      - `stores/auth.ts`：处理 JWT 鉴权状态、用户注册与登录，维持 Bearer 鉴权请求。
      - `stores/sessions.ts`：负责多会话历史的创建、异步载入、删除与切换。
      - `stores/runs.ts`：管理 durable Run、Event cursor、重放、HITL resume 与真实取消。
      - `stores/chat.ts`：把 Run Event 投影到对应 Thread 的 assistant 消息与 RAG 步骤。
      - `stores/documents.ts`：实现知识库文档的展示并配合接口轮询监听上传异步任务进度。
    - **精细化组件设计**：
      - `ThinkingTrace.vue` & `RetrievalTraceDetails.vue`：动态渲染子/主 Agent 思考状态（Searching, Grading, Rewriting 等步骤），支持展示每路子问题的合并与召回详情。
      - `References.vue`：折叠卡片展示知识库来源信息，含 RRF Rank、Rerank 语义得分、合并叶子块数、所处层级和页码。
      - `UploadSection.vue` & `DocumentSettings.vue`：管理员控制面板，动态轮询监听并步进展示上传的多阶段状态机进度。
    - **可恢复流与主动终止**：
      - `events/runEventStream.ts`：解析 Event v1 SSE，使用 `Last-Event-ID` 恢复并只在 reducer 成功后推进 cursor。
      - Stop 调用 `POST /v1/runs/{run_id}/cancel`；关闭本地 stream 只影响订阅，不等价于取消后端 Run。
  - 在 `frontend/` 目录下运行 `npm run dev` 即可开始开发联调（运行于 http://localhost:3000）。
  - 在 `frontend/` 目录下运行 `npm run build` 会生成生产环境编译产物输出至 `frontend/dist/`，供 FastAPI 后端无缝进行静态托管。
- 数据：`data/`
  - `documents/`：上传文档原文件。
- 向量库：Milvus（可由 `docker-compose` 或自建服务提供）。

## 核心流程

### 1) 项目全链路（端到端）
1. 客户端创建或复用 Thread，再调用 `POST /v1/threads/{thread_id}/runs`，携带 `idempotency_key`、期望 Thread version 与断连策略；响应返回 durable `run_id`。
2. `RunAgentExecutor` 使用 owner lease 与 fencing token 领取 Run，通过 `AgentRuntimeFactory` 构建固定中间件链。
3. Agent 根据问题类型决定是否调用工具：
  - 天气问题 → `get_current_weather`
  - 知识问答 → `search_knowledge_base`
4. 若命中知识库工具，进入 `backend/rag/pipeline.py`；tool progress、message delta、HITL 与 terminal 都先追加到持久 Event Journal。
5. 客户端通过 `GET /v1/runs/{run_id}/stream` 订阅 SSE；断线后携带 `Last-Event-ID` 重连，或先调用 `/events?after={sequence}` 补放缺失事件。
6. `message.completed` 是最终正文与 `rag_trace` 的权威来源；消息与 Run 终态提交后才发布 `run.completed`、`run.failed` 或 `run.cancelled`。
7. `hitl.required` 把 Run 置为 `waiting_input`，客户端调用 `/resume` 恢复同一 checkpoint；Stop 调用 `/cancel` 并继续监听权威 terminal Event。
8. 旧 `POST /chat` 与 `POST /chat/stream` 已退役，统一返回 typed `410 ENDPOINT_RETIRED`，不会再执行 Agent 或 Tool。

### 2) RAG 全链路（重点）
1. **复杂度规划**：`classify_complexity`
  - 明显的短单事实问题由本地规则直接判为 simple，不调用模型。
  - 其余问题由 FAST_MODEL 一次完成 simple/complex 判断；complex 结果同时给出 2-4 个子问题，不再追加拆题调用。
2. **检索执行**
  - simple：进入 `retrieve_initial`，执行一次标准检索。
  - complex：通过 LangGraph `Send` 并行执行各子问题的“检索 → 证据评判”，随后由 `synthesis` 去重合成。
  - 调用 `retrieve_documents`。
  - 先按 `chunk_level == 3` 执行 Milvus Hybrid 检索（Dense + Sparse + RRF），候选池大小由 `RETRIEVAL_CANDIDATE_K` 或 `RETRIEVAL_CANDIDATE_MULTIPLIER` 决定。
  - 在完整候选上对叶子块执行 Auto-merging（L3→L2→L1），父块从 DocStore 读取。
  - 对合并后的片段走 Jina Rerank 精排并截断 `top_k`（流水线：`recall_merge_rerank`）。
3. **证据评判与路由**：`grade_documents`
  - GRADE_MODEL 一次输出相关性、可回答性、歧义、置信度和 `route`。
  - 路由仅进入回答、一次重写、HITL 澄清/范围选择或无知识结束；评判失败会显式报错，不切换到其他实现。
4. **Step-back / HyDE 单选重写**：`rewrite_question`
  - FAST_MODEL 在一次结构化调用中选择一种方式并生成对应内容。
  - Step-back：生成更抽象的退步问题，与原问题组成 `rewritten_query`。
  - HyDE：生成仅用于检索的假设性答案文档，与原问题组成 `rewritten_query`；该文档不作为回答证据。
5. **二次召回**：`retrieve_rewritten`
  - 对 `rewritten_query` 再执行一次 L3 召回 → Auto-merging → Rerank。
6. **答案生成**：Agent 结合上下文生成最终回答。
7. **可观测追踪**：返回 `rag_trace`，包括
  - 评分结果与路由决策
  - `rewrite_method`、`step_back_question` / `hyde_document` 与 `rewritten_query`
  - 初次/二次检索结果
  - 三级检索与合并信息（`leaf_retrieve_level`、`auto_merge_*`）
  - 检索分数 `score` 与精排分数 `rerank_score`

### 3) 文档入库链路
1. 前端上传到 `POST /documents/upload/async`；API 保存 source object，并在 PostgreSQL 预留 DocumentVersion 与 IndexJob。
2. 独立 worker 使用 lease、heartbeat、`SKIP LOCKED`、build fingerprint capability 与 execution fence 领取任务；API 重启不会遗失任务，旧 profile worker 也不会误构建新 profile 候选。
3. `document_loader.py` 生成带稳定版本身份的三级分块；L1/L2 写入 ParentChunk staging，L3 写入隔离的 Milvus candidate scope。
4. worker 对 ParentChunk、Milvus 与 exact manifest 做完整身份核验，再用 PostgreSQL CAS 原子切换 `current_version_id`。
5. 同名旧版本在发布完成前始终可检索；新版本失败不会影响旧版本。
6. superseded/failed/delete 版本进入持久 cleanup queue，由同一 worker 以独立 lease 和数据库时钟退避策略执行 exact-version 物理清理；删除的 scope revoke、legacy tombstone 和 cleanup snapshot 在一个 PostgreSQL 事务内提交。
7. worker crash 后 RUNNING 任务可幂等重建；STAGED 任务只恢复 publish，不重复解析和向量化。

### 4) Milvus 2.5+ 原生 BM25 处理
- **机制**：项目利用了 Milvus 2.5+ 新版内置的全文检索机制。创建集合时，定义一个 `FunctionType.BM25` 类型的函数，输入字段为 `text` 字段，输出字段为 `sparse_embedding`。
- **自动对齐**：当新文本 chunk 插入或删除时，Milvus 在服务端自动进行分词、统计、稀疏特征向量计算。这实现了高效率、零客户端统计负担的密集 + 稀疏混合双塔检索。

### 5) 会话记忆链路
1. 用户消息与 assistant placeholder 在创建 Run 时 append-only 写入 PostgreSQL，并绑定 `thread_id`、`run_id` 与单调 sequence。
2. 流式 delta 只作为 Event 投影；完成、失败或取消时一次落定 assistant 消息状态，避免整段历史删除重插。
3. 当消息过长时，Runtime 在 token budget 内加载派生摘要；原始消息始终保留为事实来源。
4. 前端通过带 cursor 的会话接口分页读取自己的 Thread 历史；Thread version 负责并发写保护。

## 技术栈
- 后端：FastAPI、LangChain Agents、Pydantic、Uvicorn、SQLAlchemy、PostgreSQL、Redis。
- 向量与检索：Milvus（HNSW 稠密索引 + SPARSE_INVERTED_INDEX 稀疏索引）、RRF 融合、Jina Rerank 精排。
- 嵌入与稀疏：`langchain_huggingface` 本地稠密向量（默认 `BAAI/bge-m3`）；Milvus 2.5+ 原生 Chinese 分析器与原生 BM25 特征提取。
- 前端：Vite + Vue 3 (SFC) + TypeScript + Pinia + Axios + Marked + Highlight.js + FontAwesome，工程化编译与静态文件托管。
- 工具链：dotenv 配置、requests、langchain_text_splitters、langchain_community.loaders。

## 环境变量
需在仓库根目录或运行环境配置：
- 模型相关：`ARK_API_KEY`、`MODEL`、`FAST_MODEL`、`GRADE_MODEL`、`BASE_URL`。`FAST_MODEL` 负责复杂度规划及 Step-back / HyDE 单选重写；`GRADE_MODEL` 专门负责证据评判。两者都是显式必需配置，不会相互替代或回退到 `MODEL`。
- 稠密向量：`EMBEDDING_MODEL`、`EMBEDDING_DEVICE`、`DENSE_EMBEDDING_DIM`（需与 Milvus 集合 `dense_embedding` 维度一致）
- 密集与稀疏：Dense 由本地 embedding 生成；Sparse 由 Milvus 中文 analyzer 与 BM25 Function 自动生成和维护
- Rerank 相关：`RERANK_MODEL`、`RERANK_BINDING_HOST`、`RERANK_API_KEY`
- Milvus：`MILVUS_HOST`、`MILVUS_PORT`、`MILVUS_COLLECTION`
- 文档 build capability：`DEFAULT_TENANT_ID`、`DEFAULT_KNOWLEDGE_BASE_NAME`、`DOCUMENT_PARSER_VERSION`、`DOCUMENT_CHUNKER_VERSION`、`DOCUMENT_INDEX_VERSION`、`DOCUMENT_INDEX_CLEANUP_GRACE_SECONDS`。API 与 indexing worker 必须一致；readiness 会按 build fingerprint 拒绝仅有旧 profile worker 的部署。
- 数据库缓存：`DATABASE_URL`、`REDIS_URL`
- 鉴权相关：`JWT_SECRET_KEY`、`ADMIN_INVITE_CODE`、`JWT_ALGORITHM`、`JWT_EXPIRE_MINUTES`
- 密码参数：`PASSWORD_PBKDF2_ROUNDS`
- 隔离 Sandbox：`SANDBOX_ENABLED`、`SANDBOX_ADAPTER`、`SANDBOX_DOCKER_IMAGE`、
  `SANDBOX_DOCKER_HOST`、`SANDBOX_REQUIRE_ROOTLESS` 与各项 `SANDBOX_MAX_*` 预算；默认关闭，
  启用后参与 readiness。
- 检索候选池：`RETRIEVAL_CANDIDATE_K`（固定候选数，优先）、`RETRIEVAL_CANDIDATE_MULTIPLIER`（未设 K 时 `max(top_k × 倍数, top_k)`，默认 `3`）
- Auto-merging：`AUTO_MERGE_ENABLED`、`AUTO_MERGE_THRESHOLD`、`LEAF_RETRIEVE_LEVEL`
- 工具：`AMAP_WEATHER_API`、`AMAP_API_KEY`

## API 速览
- 鉴权
  - `POST /auth/register`：注册（支持普通用户/管理员邀请码模式）。
  - `POST /auth/login`：登录，返回 Bearer Token。
  - `GET /auth/me`：获取当前登录用户信息。
- Thread / Run / Event
  - `POST /v1/threads`：创建 Thread。
  - `POST /v1/threads/{thread_id}/runs`：幂等创建 durable Run，返回 `run_id` 与 `thread_version`。
  - `GET /v1/runs/{run_id}`：读取 Run 当前状态。
  - `GET /v1/runs/{run_id}/events?after={sequence}`：分页重放持久 Event。
  - `GET /v1/runs/{run_id}/stream`：订阅 Event v1 SSE；重连时发送 `Last-Event-ID: <sequence>`。
  - `POST /v1/runs/{run_id}/resume`：携带一次性 `hitl_token` 与幂等键恢复同一 checkpoint。
  - `POST /v1/runs/{run_id}/cancel`：请求取消真实后端 Run；客户端应等待 `run.cancelled` 或其他权威 terminal Event。
  - `POST /chat`、`POST /chat/stream`：已退役，认证后返回 JSON `410 ENDPOINT_RETIRED`，不会执行 legacy Chat Implementation。
- 会话（用户隔离）
  - `GET /sessions`：列出当前用户会话。
  - `GET /sessions/{session_id}`：拉取当前用户某会话消息。
  - `DELETE /sessions/{session_id}`：删除当前用户会话。
- 文档（管理员权限）
  - `GET /documents`：列出已入库文档及 chunk 数。
  - `POST /documents/upload/async`：保存上传并提交持久化索引任务。
  - `GET /documents/upload/jobs`：列出最近 durable 索引任务，供刷新后恢复。
  - `GET /documents/upload/jobs/{job_id}`：查询可恢复的索引状态、attempt 与退避时间。
  - `DELETE /documents/delete/async/{filename}`：立即撤销检索 scope，并提交持久化清理任务。
  - `GET /documents/delete/jobs`：列出最近 durable 删除 operation，供刷新后恢复。
  - `GET /documents/delete/jobs/{job_id}`：查询物理清理进度或 dead-letter。
  - 兼容路径 `POST /documents/upload` 与 `DELETE /documents/{filename}` 已改为 `202 Accepted` 并返回 durable `job_id`：前者只持久化提交任务，后者只原子撤销检索 scope；二者都不再表示物理索引/清理已同步完成。旧客户端升级前必须改为轮询 durable job 接口。

## 持久化 Run/Event 与可恢复流 — 技术细节

### 1. 创建 durable Run

客户端先创建 Thread，再用独立请求创建 Run；不要用一个长连接同时承担“创建工作”和“观察工作”：

```http
POST /v1/threads/{thread_id}/runs
Authorization: Bearer <token>
Content-Type: application/json

{
  "message": "请比较两份文档的结论",
  "idempotency_key": "client-generated-key",
  "expected_thread_version": 3,
  "multitask_strategy": "reject",
  "on_disconnect": "continue",
  "approved_tools": []
}
```

响应中的 `run.id`、assistant message identity 与 `thread_version` 是后续重放、取消和 HITL 恢复的稳定身份。相同用户、Thread 与幂等键只创建一个 Run；同一 Thread 的并发写入由 version、数据库约束、owner lease 与 fencing token 共同保护。

### 2. Event v1 Interface

`RunAgentExecutor` 驱动 `AgentRuntimeFactory`，并把规划、工具、检索、消息、HITL、usage 与 terminal 状态追加到 Event Journal。SSE 只是 Journal 的可恢复投影：

```text
id: 42
event: message.delta
data: {"schema_version":1,"event_id":"evt_xxx","sequence":42,"run_id":"run_xxx","thread_id":"thread_xxx","type":"message.delta","timestamp":"...","data":{}}
```

关键不变量：

- `sequence` 在单个 Run 内严格单调，客户端 reducer 按 `(run_id, sequence)` 去重并检测 gap。
- `message.delta` 只负责临时展示；`message.completed` 是最终正文与 `rag_trace` 的权威来源。
- `run.completed`、`run.failed`、`run.cancelled` 是终止事件，不再使用非结构化 `[DONE]`。
- assistant 消息与 Run 终态必须先在持久化事务中落定，再发布 terminal Event。
- Event Envelope 不暴露模型私有推理、密钥或未脱敏 ToolAudit 参数。

### 3. 重放、重连与 heartbeat

首次订阅使用：

```http
GET /v1/runs/{run_id}/stream
```

若连接中断，客户端保留最后一个已成功应用的 sequence，并通过以下任一方式恢复：

```http
GET /v1/runs/{run_id}/events?after=42
GET /v1/runs/{run_id}/stream
Last-Event-ID: 42
```

服务端先从 PostgreSQL Journal 重放缺失事件，再通过 Redis transport 等待新事件；Redis 只负责低延迟通知，不是事实来源。空闲期间发送 SSE heartbeat。客户端遇到 sequence gap 时不得推进 cursor，应先补放缺失事件；terminal 后关闭该 Run 的本地订阅。

### 4. RAG 进度与前端投影

知识库工具仍由 `backend/rag/pipeline.py` 执行 Dense + BM25 + RRF、Auto-merging、Rerank、证据评判与一次重写。不同阶段通过 `tool.started`、`tool.progress`、`retrieval.*` 与 `message.*` Event 表达，前端 `runs` store 维护 Run 状态，`chat` store 只把同一 `thread_id/run_id` 的事件投影到对应 assistant 消息，避免不同 Thread 串写。

### 5. HITL 与取消

- 收到 `hitl.required` 后，Run 已在同一事务中保存 checkpoint 并进入 `waiting_input`。客户端持久保存 `run_id`、`hitl_token` 与 `checkpoint_id`，调用 `POST /v1/runs/{run_id}/resume` 恢复同一图节点；刷新页面不需要重新执行原问题。
- Stop 调用 `POST /v1/runs/{run_id}/cancel`。响应表示“取消请求已接受或 Run 已终止”，客户端继续监听直到收到权威 terminal Event。
- 关闭页面、切换账号或断开 SSE 默认只关闭观察连接；`on_disconnect=continue` 的 Run 会在后端继续。网络断开不能冒充用户取消。

### 6. 公开 legacy Chat 入口退役

`POST /chat` 与 `POST /chat/stream` 不再是 compatibility execution Adapter。二者认证后对任意 legacy body 返回 JSON `410 ENDPOINT_RETIRED`，并给出 create/stream/resume/cancel 迁移路径；它们不会创建模型调用、ToolAudit、消息或后台任务。所有公开工具执行因此只跨越 durable Run 的 Guardrail 与 Sandbox Seam。

### 7. 混合检索（Hybrid Search）深度实现

项目并非在客户端手写复杂的 BM25 特征序列化，而是利用 Milvus 2.5+ 服务端原生分析器构建了极致的双塔检索：

- **Dense Pathway**：使用 `langchain_huggingface.HuggingFaceEmbeddings`（默认 `BAAI/bge-m3`）生成稠密向量，维度由 `DENSE_EMBEDDING_DIM` 与集合 schema 对齐（默认 1024），向量可做 L2 归一化后与 Milvus `IP` 度量配合。
- **Sparse Pathway**：
    - 文档写入时，仅需将原始文本写入启用 `chinese` 分析器分词的 `text` 字段。
    - Milvus 服务端自动运行绑定的 `FunctionType.BM25` 计算函数，动态生成对应的稀疏嵌入并同步到 `sparse_embedding` 索引中，完美对齐词表统计。
- **Milvus 融合**：
    - 使用 Milvus 的 `AnnSearchRequest` 同时发起稠密和稀疏的两个多路检索请求。
    - **RRFRanker (Reciprocal Rank Fusion)**: 采用 `k=60` 的倒数排名融合算法，将两路召回结果无参数化地合并，避免了加权求和中调节 `alpha` 参数的困难。

## 更新日志

### 2026-06-12 全面迁移至 Milvus 2.5+ 原生 BM25 与事务级可靠删除
- **服务端原生 BM25**：使用 Milvus 2.5+ 内置中文分词器与 BM25 Pipeline Function，稀疏特征和统计由向量库自动维护。
- **Schema 自动升级**：优化 `ensure_collection` 逻辑，支持自动检测旧版 Schema 并进行 drop 与无缝重建升级。
- **事务性一键删除**：实现高可靠、强一致性的 `delete_document_transactionally` 删除协调器，一键清理 Milvus 向量数据、PostgreSQL 级联分块记录和 Redis 热缓存，避免产生任何悬空脏数据。
- **企业级文本净化**：升级文本清洗逻辑，通过 Unicode NFC 标准规范化和 PUA/C0/C1 等非打印/零宽/孤立代理项的彻底过滤，解决 PostgreSQL 与 Milvus 的字符集兼容性报错。

### 2026-06-12 前端单文件 CDN 重构为 Vite + Vue 3 + TS 工程化组件架构
- **现代化架构重构**：将以前臃肿的多合一 HTML/CDN 页面重构为标准的 **Vite + Vue 3 (SFC) + TypeScript + Pinia + Axios + Sass** 现代化工程项目，全部组件和状态高度解耦。
- **状态及路由管理**：利用 Pinia 建立了 `auth`、`sessions`、`chat`、`documents` 四大 Store 共享核心数据。
- **高阶交互界面**：增加流式上传进度详情卡片、上传成功后卡片自动折叠、References 参考文献精美折叠展示、Thinking 气泡流畅过度等。

### 2026-06-03 自适应复杂问题分解、并行 Sub-Agent 与精排门控
- **低延迟复杂度规划**：明显的单事实问题由本地规则直接进入检索；其余问题由 FAST_MODEL 一次完成复杂度判断，并在 complex 时同时给出 2-4 个子问题。
- **并行子 Agent 检索**：利用 LangGraph 的 `Send` API 并行调用 `rag_sub_agent`，每个子问题只执行 retrieve 与 grade，避免不可达的嵌套图和二次改写。
- **子步骤完美分组**：前端界面重新适配并行子流程，在 RAG Step 的 SSE 数据中为子问题建立独立分组标签展示，避免交错重复建组与视觉混淆。
- **精排与明确路由**：`RERANK_MIN_SCORE` 过滤噪音；空检索直接结束，有相关信号但证据不足时才执行唯一一次 Step-back / HyDE 单选重写。

### 2026-06-02 通用 RAG 能力强化与后端生命周期重构
- **通用 RAG 功能增强**：提供会话摘要长期记忆（Context Manager Notes）、首问本地截断标题，以及多源参考文献的可视化折叠展示卡片。
- **gRPC 连接生命周期优化**：Milvus 数据库客户端访问由全局连接池改为短生命周期会话（`session()` contextmanager），按请求建立短连接会话，彻底规避连接因长期挂起产生的失效 gRPC channel 问题。
- **后端分层重组与包依赖解耦**：彻底重构 backend 代码目录包结构，剔除 re-export 导出机制，解决因交叉导入产生的循环依赖，并统一环境加载规范。

### 2026-06-01 召回-合并-精排（Rerank）流水线重构
- **模块化 Pipeline**：重构 RAG 底层实现，将 RAG 流程收拢为高可控的“召回 -> 自动合并 -> 语义重排”流水线，收口统一的参数配置与多级 RAG Trace 追踪。
- **去重合并高分保留**：修复了在执行 L3 -> L2/L1 叶子向上合并时，在循环内聚合 Rank 分数的算法，防止去重过程中丢失高置信度召回分。

### 2026-03-21 后端服务建设升级（认证 + 数据库 + 缓存）
- 新增认证与权限模块：注册、登录、JWT、管理员权限控制。
- 聊天历史从本地 JSON 迁移到 PostgreSQL，按用户隔离会话数据。
- 父级分块存储从本地 JSON 迁移到 PostgreSQL。
- 引入 Redis 缓存会话与父文档，提高读取性能并降低数据库压力。
- API 升级为 Token 驱动，移除前端直接传 `user_id` 的历史模式。
- 文档管理接口收敛到管理员角色，避免普通用户误操作知识库。
- 密码哈希方案升级为 PBKDF2-SHA256，兼容历史 bcrypt 校验。

### 2026-03-13 三级分块与 Auto-merging 升级
- 新增三级滑动窗口分块（L1/L2/L3），并为分块写入层级元数据。
- 存储策略调整为 Leaf-only：仅 L3 叶子块写入 Milvus，L1/L2 写入本地 DocStore。
- Auto-merging 改为从 DocStore 拉取父块，减少向量冗余存储。
- 思考链路新增三级检索与自动合并步骤事件。
- `rag_trace` 新增 `leaf_retrieve_level` 与 `auto_merge_*` 字段，且历史会话读取同样保留这些字段。

### 2026-02-19 RAG 实时思考链路修复
- **问题**：Agent 在执行同步工具（如 `search_knowledge_base`）时，由于运行在线程池中，无法正确获取主线程的 asyncio 事件循环，导致 `emit_rag_step` 事件丢失，前端"思考中"气泡一直静止。
- **修复**：
  1. **Backend (`service.py`)**：为每个请求创建 `ChatRequestContext`，在其中捕获主线程 `loop` 与本请求 `output_queue`。
  2. **Backend (`backend/tools/knowledge.py` + `backend/rag/pipeline.py`)**：使用 per-request tool factory 与显式 `ctx` 参数跨线程调度 RAG step，避免请求间串号。
  3. **Frontend (`stores/chat.ts`)**：在发送消息时初始化空的 `ragSteps: []` 数组，确保 Vue 响应式系统能立即追踪后续的 push 操作。
- **效果**：用户提问后，思考气泡内实时跳动显示检索步骤（如"🔍 正在检索知识库..." -> "📊 正在评估文档相关性..."），不再只有静态的"正在思考中..."。
