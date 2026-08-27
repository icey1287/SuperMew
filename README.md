# SuperMew 项目说明

SuperMew 是一个知识库优先、面向真实运行与评测的 Agent 平台。它以持久化 Thread、可恢复
Run、版本化 Event、不可变 Document Version 和明确的 Provider 失败语义为核心，提供 RAG、
HITL、RAG 效果评测以及可审计的 Skill / Tool 执行。

[![zread](https://img.shields.io/badge/Ask_Zread-_.svg?style=flat&color=00b0aa&labelColor=000000&logo=data%3Aimage%2Fsvg%2Bxml%3Bbase64%2CPHN2ZyB3aWR0aD0iMTYiIGhlaWdodD0iMTYiIHZpZXdCb3g9IjAgMCAxNiAxNiIgZmlsbD0ibm9uZSIgeG1sbnM9Imh0dHA6Ly93d3cudzMub3JnLzIwMDAvc3ZnIj4KPHBhdGggZD0iTTQuOTYxNTYgMS42MDAxSDIuMjQxNTZDMS44ODgxIDEuNjAwMSAxLjYwMTU2IDEuODg2NjQgMS42MDE1NiAyLjI0MDFWNC45NjAxQzEuNjAxNTYgNS4zMTM1NiAxLjg4ODEgNS42MDAxIDIuMjQxNTYgNS42MDAxSDQuOTYxNTZDNS4zMTUwMiA1LjYwMDEgNS42MDE1NiA1LjMxMzU2IDUuNjAxNTYgNC45NjAxVjIuMjQwMUM1LjYwMTU2IDEuODg2NjQgNS4zMTUwMiAxLjYwMDEgNC45NjE1NiAxLjYwMDFaIiBmaWxsPSIjZmZmIi8%2BCjxwYXRoIGQ9Ik00Ljk2MTU2IDEwLjM5OTlIMi4yNDE1NkMxLjg4ODEgMTAuMzk5OSAxLjYwMTU2IDEwLjY4NjQgMS42MDE1NiAxMS4wMzk5VjEzLjc1OTlDMS42MDE1NiAxNC4xMTM0IDEuODg4MSAxNC4zOTk5IDIuMjQxNTYgMTQuMzk5OUg0Ljk2MTU2QzUuMzE1MDIgMTQuMzk5OSA1LjYwMTU2IDE0LjExMzQgNS42MDE1NiAxMy43NTk5VjExLjAzOTlDNS42MDE1NiAxMC42ODY0IDUuMzE1MDIgMTAuMzk5OSA0Ljk2MTU2IDEwLjM5OTlaIiBmaWxsPSIjZmZmIi8%2BCjxwYXRoIGQ9Ik0xMy43NTg0IDEuNjAwMUgxMS4wMzg0QzEwLjY4NSAxLjYwMDEgMTAuMzk4NCAxLjg4NjY0IDEwLjM5ODQgMi4yNDAxVjQuOTYwMUMxMC4zOTg0IDUuMzEzNTYgMTAuNjg1IDUuNjAwMSAxMS4wMzg0IDUuNjAwMUgxMy43NTg0QzE0LjExMTkgNS42MDAxIDE0LjM5ODQgNS4zMTM1NiAxNC4zOTg0IDQuOTYwMVYyLjI0MDFDMTQuMzk4NCAxLjg4NjY0IDE0LjExMTkgMS42MDAxIDEzLjc1ODQgMS42MDAxWiIgZmlsbD0iI2ZmZiIvPgo8cGF0aCBkPSJNNCAxMkwxMiA0TDQgMTJaIiBmaWxsPSIjZmZmIi8%2BCjxwYXRoIGQ9Ik00IDEyTDEyIDQiIHN0cm9rZT0iI2ZmZiIgc3Ryb2tlLXdpZHRoPSIxLjUiIHN0cm9rZS1saW5lY2FwPSJyb3VuZCIvPgo8L3N2Zz4K&logoColor=ffffff)](https://zread.ai/icey1287/SuperMew)

## 项目概览

SuperMew 不把一次聊天请求视为一个不可恢复的 HTTP 调用，而是将用户输入、Agent 执行、工具
调用、检索证据、人工确认和最终结果建模为一组可以持久化、重放和审计的领域对象。

- **对话与执行**：Thread 保存长期会话边界，Message 保存事实历史，Run 表示一次可恢复执行，
  Event Journal 保存执行过程，Checkpoint 支持 HITL 暂停后恢复同一个 Run。
- **知识库与检索**：文档上传后由持久化 Index Job 构建不可变 Document Version；叶子分块写入
  Milvus，父级分块与版本元数据写入 PostgreSQL。新版本通过两阶段发布切换，不会在构建中污染
  当前可检索版本。
- **RAG 流水线**：Dense + Milvus 原生 BM25 双路召回，经 RRF、Auto-merging、可选 Rerank、
  证据评判以及最多一次 Step-back / HyDE 重写后生成回答，并在 `rag_trace` 中保留可观测信息。
- **能力系统**：版本固定的 Skill / Tool Registry 管理知识库、天气、只读 SQL、Web Research、
  Sandbox 和受限 HTTPS JSON Tool；工具 schema 按当前 Run 的权限延迟披露。
- **模型控制面**：管理员维护不含 Secret 的 Model Profile，并为 Answer、Fast、Grader、
  Evaluator 四类角色分配模型。Run 和 Evaluation Job 创建时冻结 Model Snapshot。
- **RAG 评估**：既支持仓库内版本化 Dataset、Observation、baseline 与 CI Gate，也提供持久化
  Dataset / Job / Case API、独立 Evaluation Worker 和前端评估工作台。
- **认证与隔离**：浏览器 Access Token 只驻留内存，opaque Refresh Token 通过 HttpOnly Cookie
  轮换；RBAC、入口限流、Guardrail、SQL 只读边界和隔离 Sandbox 共同约束高风险能力。
- **前后端形态**：后端使用 FastAPI、SQLAlchemy、PostgreSQL、Redis 与 Milvus；前端使用
  Vite、Vue 3、TypeScript、Pinia 和 Axios，生产构建由 FastAPI 同源托管。

系统只保留 canonical Thread / Run / Event Interface。旧 `/chat`、`/chat/stream` 和 `/sessions`
兼容入口已经移除，不再通过双写、双读或隐藏 fallback 维持第二套执行路径。

## 关键架构能力

- **可恢复 Run/Event 流**：Event 先写入 PostgreSQL Journal，再通过 SSE 投影；前端按 sequence
  去重，使用 `Last-Event-ID` 重连和 `/events` 补放。`message.completed` 与 terminal Event 是
  最终权威结果，Redis 只负责低延迟通知。
- **同一 Run 的 HITL 与真实取消**：Checkpoint、Run、Thread 和 assistant Message 身份在暂停与
  恢复期间保持不变；Stop 会请求取消后端 Run，关闭浏览器或 SSE 只停止观察，不冒充执行终止。
- **Document Version 两阶段发布**：Index Job 在隔离 candidate scope 构建并核验 exact
  manifest，随后使用 PostgreSQL CAS 原子切换当前版本；构建失败不会影响已发布版本。
- **混合检索与精排**：稠密向量与 Milvus 原生 BM25 稀疏向量经 RRF 融合，在完整候选池上完成
  Auto-merging，再进入有输入预算、超时和熔断保护的 Rerank 阶段。
- **严格的检索降级语义**：只有 Milvus Adapter 明确报告稀疏或 Hybrid 能力不兼容时，受影响的
  target 才复用同一 query embedding 降级为 Dense。连接失败、超时、服务不可用、参数错误或
  畸形响应会显式失败，不会伪装成降级成功。
- **有界的 Rerank 降级**：Rerank 未配置时直接保留融合后的候选排序；已配置的 Rerank Provider
  在完成自身有限重试后仍失败时，回退到 RRF / Auto-merging 结果，并把错误码、尝试次数和
  `rerank_fallback_applied` 写入 RAG Trace，不触发另一套检索实现。
- **低延迟复杂度规划**：明显的短单事实问题由本地规则直接进入检索；其余问题由 Fast 模型一次
  完成复杂度判断。复杂问题同时生成 2-4 个子问题，通过 LangGraph `Send` 并行执行检索与证据
  评判，最终在 Synthesis 节点去重合成。
- **纠错型 RAG 与单选重写**：Grader 一次结构化判断相关性、可回答性、歧义与 route。证据不足
  时，Fast 模型只选择 Step-back 或 HyDE 中的一种，并只执行一次重写检索和一次复评。
- **Agent 循环与预算保护**：固定中间件链约束模型调用、Tool 调用、递归、deadline、上下文预算
  和重复 Tool fingerprint；重复调用或 A/B 交替循环超限时返回 `TOOL_LOOP_BLOCKED`。
- **持久化模型控制面**：Answer、Fast、Grader、Evaluator Assignment 存储在数据库；每个 Run
  与 Evaluation Job 冻结完整 Model Snapshot，控制面后续修改不会改变执行中或等待 HITL 的任务。
- **RAG 自动评估**：持久化 Evaluation Worker 逐 Case 执行检索与回答，并评估 correctness、
  groundedness、relevance、completeness、unsupported claim 和 conflict disclosure；报告可与
  baseline 及质量 Gate 比较。
- **浏览器认证生命周期**：Access Token 不写入 `localStorage`；Refresh Token 仅由固定
  `Path=/auth` 的 HttpOnly Cookie 承载并逐次轮换。仍在自然有效期内的 revoked token replay 会
  撤销该用户全部活跃 refresh credential。
- **入口保护与浏览器响应头**：生产使用 Redis 共享 Rate Limit，identity 进入 bucket 前使用
  HMAC；登录与注册在密码哈希校验前执行 IP 和 IP+username 两层限流。CSP 只保护正式前端 HTML，
  其他安全响应头全局应用。
- **只读 SQL Assistant**：只对 `admin` 开放，并同时受独立数据库账号、schema/table allowlist、
  AST、权限、RLS、成本、超时、结果大小和敏感字段脱敏约束，不提供 DDL 或 DML。
- **受控 Web Research**：模型不能直接提交任意抓取 URL，只能消费同一 Run 内搜索产生的
  `evidence_id`；Runtime 负责 SSRF policy、DNS pin、逐跳重定向复核和引用身份校验。
- **可审计 Tool 执行**：Registry 决定能力是否可见，Guardrail 在 handler 前执行确定性约束，
  Sandbox 只隔离已经获准的代码执行。普通 `ALLOW` 不在前端展示；用户只会看到拒绝或需要审批的
  结果，详细 policy reason 仅进入脱敏 ToolAudit。

## 前端能力中心与专业模式

登录后可通过以下入口选择当前 Thread 使用的 Skill：

- 侧边栏或欢迎页的“能力中心”：查看当前账号可见的 Skill 与 Tool，并按可用状态筛选；目录会
  展示角色要求、网络策略、资源范围、Tool 暴露方式和是否需要在创建 Run 前审批。
- 输入区的模式选择器：切换智能对话、知识库问答或专业 Skill，并显示当前模式的约束和快捷提示。
- `⌘K`（Windows/Linux 使用 `Ctrl+K`）命令面板：搜索模式或 Skill，使用方向键选择、`Enter`
  确认、`Esc` 关闭。不可用能力会标记为权限不足或尚未配置，不会创建 Run。

当前产品化入口包括：

- **Knowledge Base**：使用当前发布的 Document Version 进行 RAG 问答，并展示检索、评判、重写、
  合并与引用信息。
- **Web Research**：调查公开问题、时间范围或来源偏好。只有 feature flag、Tavily Keyless
  Runtime、角色与受限公网策略同时满足时才可用，外部事实必须引用当前 Run 的 Web Evidence。
- **SQL Assistant**：以自然语言描述指标、维度、筛选条件和时间范围，仅查询 allowlist 内的
  PostgreSQL catalog，不提供写入能力。
- **Sandbox**：选择 Python 或 Shell 后提交源码。该模式固定无网络、无宿主挂载、无持久
  workspace；发送前的确认只为即将创建的单个 Run 签发 names-only grant。

管理员侧栏提供三个全页控制面：

- **模型中心**：维护 Model Profile，检查 Stream / Structured Output 能力，并分配 Answer、Fast、
  Grader、Evaluator 角色。API Key 只存在服务端，前端仅显示“已配置/未配置”。
- **Skill / Tool**：配置 SQL Assistant、Tavily Keyless、内建与自定义 Skill，以及受限 HTTPS
  JSON Tool。Secret 值始终只保存在服务端环境中。
- **RAG 评估**：导入版本化 Dataset、选择 baseline、启动持久 Evaluation Job，并查看历史趋势、
  Gate、Case、Judge reason 与 Evidence identity。

审批确认发生在 Run 创建之前。当前没有“Run 已开始后弹窗临时扩权，再恢复执行”的审批状态机；
未随创建请求预授权的 approval-only Tool 会被拒绝。

### Artifact 展示与下载边界

前端会把 `artifact.created` Event 投影为时间线和 Artifact 卡片，但 descriptor 与下载能力分离：

- `artifact://art_*` 只表示稳定 Artifact 身份，不能直接转换成宿主路径或对象存储 key。
- 只有服务端已经持久化、完成所有权校验并发布 `/api/artifacts/art_*` URI 时，前端才会携带当前
  Access Token 通过同源请求预览或下载。
- 当前 Sandbox 不导出持久 Artifact。临时 workspace 在调用结束后销毁，文件不会暴露容器或
  宿主路径。

## 运行拓扑与责任边界

本地统一启动器和生产 supervisor 都需要管理三个常驻进程：

| 进程            | 正式入口                               | 主要职责                                                |
| --------------- | -------------------------------------- | ------------------------------------------------------- |
| API             | `uvicorn backend.app:app`              | HTTP、SSE、认证、Thread/Run 调度、控制面和生产静态资源  |
| 索引 worker     | `python -m backend.workers.indexing`   | Document Version 构建、发布、重试、清理和 heartbeat     |
| RAG 评估 worker | `python -m backend.workers.evaluation` | Evaluation Job 领取、Case 执行、Judge、报告和 heartbeat |

存储职责保持单一：

| 资源            | 权威职责                                                                                               | 不承担的职责                         |
| --------------- | ------------------------------------------------------------------------------------------------------ | ------------------------------------ |
| PostgreSQL      | User、Refresh ledger、Thread、Message、Run、Event、Checkpoint、Document Version、Job、模型与能力控制面 | 不作为向量近邻检索引擎               |
| Redis           | Event 低延迟通知、版本绑定的父分块缓存、分布式 Rate Limit                                              | 不保存 Thread 历史或最终 Run 状态    |
| Milvus          | 当前有效 Document Version 的 Dense / BM25 检索数据                                                     | 不决定版本发布，也不保存业务事务事实 |
| `UPLOAD_DIR`    | 文档 source object 与索引 worker 可读取的持久文件                                                      | 不作为公开静态下载目录               |
| `frontend/dist` | 生产前端构建产物                                                                                       | 不替代 Vite 开发服务器               |

API 与两个 worker 必须使用同一 release 和配置。API 与索引 worker 还必须共享持久化
`UPLOAD_DIR`；PostgreSQL 是任务领取与状态恢复的事实来源，进程重启不应丢失已提交的工作。

## 本地启动

### 环境要求

- Python 3.12+
- [uv](https://docs.astral.sh/uv/)
- Node.js 20.19+ 与 npm 10+
- Docker 与 Docker Compose

### 1. 配置环境

```bash
cp .env.example .env
```

至少需要修改以下配置：

- `ARK_API_KEY`、`BASE_URL`、`MODEL`、`FAST_MODEL`、`GRADE_MODEL` 和
  `EVALUATION_MODEL`；这些模型值只用于首次创建模型控制面默认值。
- `JWT_SECRET_KEY`：至少 32 字符的随机值，例如使用 `openssl rand -hex 32` 生成。
- 如需注册管理员，设置独立的 `ADMIN_INVITE_CODE`；留空会禁用公开 admin 注册。

`.env.example` 中的 PostgreSQL 与 Redis DSN 已与开发 Compose 默认值对齐。Rerank、Web
Research、SQL Assistant 和 Sandbox 都是可选能力，默认关闭或允许明确降级。

### 2. 启动依赖并安装项目

```bash
docker compose up -d
docker compose ps

uv sync --frozen

cd frontend
npm ci
npm run build
cd ..

uv run --frozen alembic upgrade head
uv run --frozen python -m backend.tools.registry_cli validate
```

开发 Compose 只启动 PostgreSQL、Redis、etcd、MinIO、Milvus 和 Attu，不启动应用进程。

### 3. 启动应用

```bash
./scripts/start.sh
```

统一启动器会管理三个进程：

- FastAPI API
- 持久化索引 worker
- RAG 评估 worker

任一进程异常退出时，启动器会关闭其余进程，避免出现 API 可访问但后台任务无人消费的状态。
关闭 Uvicorn 自动重载可使用：

```bash
./scripts/start.sh --no-reload
```

启动后访问：

- 应用：<http://127.0.0.1:8000/>
- API 文档：<http://127.0.0.1:8000/docs>
- Readiness：<http://127.0.0.1:8000/health/ready>
- Attu：<http://127.0.0.1:8080/>

前端开发时可另开终端运行 `cd frontend && npm run dev`。Vite 固定监听 3000，并代理到 8000
端口的 API。

## 生产部署

生产 Compose 同样只管理 PostgreSQL、Redis、etcd、MinIO、Milvus 和 Attu。API、索引 worker
和 RAG 评估 worker 必须由 systemd、Kubernetes 或等价 supervisor 分别管理，并使用同一版本
代码与环境配置；API 和索引 worker 还必须共享持久化 `UPLOAD_DIR`。

完整的 Secret、迁移、构建、三进程启动、健康检查、发布顺序和清理任务见
[生产部署 Runbook](docs/runbooks/deployment.md)。不要使用 `python backend/app.py`；正式 API
入口是 `uvicorn backend.app:app`。

### 生产发布要点

- `APP_ENV=production`，生产数据库、Redis、MinIO 和模型 Secret 不使用示例默认值。
- `JWT_SECRET_KEY` 与 `RATE_LIMIT_HMAC_KEY` 使用两个不同的至少 32 字符随机值。
- 启用 `AUTH_REFRESH_COOKIE_SECURE=true`、`RATE_LIMIT_BACKEND=redis` 和
  `INDEX_WORKER_REQUIRED=true`。
- 发布前备份 PostgreSQL，执行 Alembic 迁移、schema current 校验、Registry 校验和前端构建。
- 先启动索引 worker 与 RAG 评估 worker，再启动 API；通过 live、ready、登录、Thread/Run、知识库
  问答和最小 Evaluation Job 后再恢复入口。
- Refresh ledger 清理不在 API 热路径执行，需要由 scheduler 周期运行
  `python -m backend.auth.cleanup`。

## 能力配置与安全边界

### Skill / Tool Registry

启动或发布前可验证 Registry：

```bash
uv run --frozen python -m backend.tools.registry_cli validate
uv run --frozen python -m backend.tools.registry_cli list-skills --role user
uv run --frozen python -m backend.tools.registry_cli list-tools --role user
```

Skill 正文只在显式 slash、可信路由或 `describe_skill` 后向 Agent 披露；`tool_search` 只返回当前
Run 已授权的 deferred schema。管理员可在 **Skill / Tool** 控制面编辑或停用内建 Skill、创建
自定义 Skill、创建声明式公共 HTTPS JSON Tool，并配置 SQL Assistant 与 Web Research。

自定义 HTTP Tool 只支持固定 HTTPS Endpoint、GET/POST、JSON Schema、静态 Header 和环境 Secret
Header 引用，不接受任意服务端 Python、Shell 或插件代码。Registry 的目录结构、manifest、hash
pin、回滚和 Secret 规则见 [Skill / Tool Registry Runbook](docs/runbooks/skill-tool-registry.md)。

### 模型控制面

模型中心将“模型配置”和“运行时 Secret”分离：

- `ARK_API_KEY` 与 Provider endpoint Secret 保留在服务端环境中。
- `MODEL`、`FAST_MODEL`、`GRADE_MODEL`、`EVALUATION_MODEL` 与 `BASE_URL` 只用于数据库尚未初始化时
  的首次种子。
- 管理员通过 `/v1/models` 维护 Model Profile，并为 Answer、Fast、Grader、Evaluator 分配模型。
- 新 Run 和 Evaluation Job 在创建时冻结 Profile、Assignment 与能力信息；运行中修改控制面不会
  改变已经开始或正在等待 HITL 的任务。
- 删除或停用 Profile 前必须解除仍然存在的 Assignment 或其他活跃引用。

### SQL Assistant

SQL Assistant 默认关闭。启用时必须使用与应用写库不同 username 的 PostgreSQL 只读账号、显式
schema/table allowlist、RLS 和 `admin` 角色：

```dotenv
SQL_ASSISTANT_ENABLED=true
SQL_ASSISTANT_DSN=postgresql://supermew_sql_reader:<secret>@db/analytics?sslmode=require
SQL_ASSISTANT_EXPECTED_ROLE=supermew_sql_reader
SQL_ASSISTANT_ALLOWED_SCHEMAS=analytics
SQL_ASSISTANT_ALLOWED_TABLES=analytics.orders,analytics.customers
SQL_ASSISTANT_SENSITIVE_COLUMNS=analytics.customers.email
```

`sql_schema` 与 `sql_query` 以 deferred Tool 按需披露。Runtime 只接受单条有界只读查询，并执行
AST、对象 allowlist、实际数据库权限、RLS、预计成本、statement timeout、lock timeout、行数、
字节数、单元格大小和敏感字段脱敏检查。配置、验证、轮换和紧急禁用见
[SQL Assistant Runbook](docs/runbooks/sql-assistant.md)。

### Web Research

Web Research 默认关闭，可由管理员在控制面启用：

```dotenv
WEB_RESEARCH_ENABLED=true
```

`web_search` 与 `web_fetch` 只有在 feature flag、Tavily Keyless Runtime、active Skill、角色和
`restricted` network policy 同时满足时才披露。`web_fetch` 不接受模型提交的任意 URL，只接受
同一 Run 内 `web_search` 返回的不可变 `evidence_id`。Runtime 会重新执行 SSRF policy、DNS pin、
逐跳 redirect、内容类型、压缩与解压字节、并发和总 Evidence 预算检查。

模型输出 Run-local `webcite:` token，服务端终态校验通过后才渲染 canonical Markdown 链接；事实
应就近引用，并披露来源冲突、检索时间和覆盖缺口。完整上线、预算与事件响应流程见
[Web Research Runbook](docs/runbooks/web-research.md)。

### Guardrail 与 Sandbox

所有 Registry-bound Tool 在 handler 前经过确定性 Guardrail。内部决策类型为 `ALLOW`、`DENY` 和
`REQUIRE_APPROVAL`，它们的职责是约束真实 Tool handler，并不是额外调用模型：

- 正常 `ALLOW` 直接继续执行，不产生前端“策略允许”提示。
- `DENY` 才会产生用户可见的策略拒绝结果。
- `REQUIRE_APPROVAL` 只接受创建当前 Run 时携带的预授权，不支持运行中临时扩权。
- policy version/hash、详细 reason 和脱敏调用参数进入 ToolAudit，不进入公开 Event/SSE。
- `shell`、`code`、`process`、`network-private` 和 `high-risk` 等未被隔离 Runtime 承接的高风险
  group 保持 hard deny。

Sandbox 默认关闭。启用后使用本地 digest-pinned image、固定非 root runner、只读 rootfs、无网络、
无 bind mount 和有大小上限的 tmpfs workspace：

```dotenv
SANDBOX_ENABLED=true
SANDBOX_ADAPTER=docker
SANDBOX_DOCKER_IMAGE=sha256:<local-image-sha256>
SANDBOX_DOCKER_HOST=unix:///path/to/supermew-rootless.sock
SANDBOX_REQUIRE_ROOTLESS=true
```

只有 trusted admin 可以在创建 Run 前确认并提交 `approved_tools=["sandbox_execute"]`。grant 绑定
user、Tenant、Thread、Run 与 Tool 名称，不能跨 Run 复用。Sandbox 启用但 daemon 或 image 不
ready 时 `/health/ready` 返回 503；关闭时不探测 Docker。发布、烟雾测试和审计流程见
[Guardrail 与 Sandbox Runbook](docs/runbooks/guardrails-and-sandbox.md)。

### 浏览器认证与入口限流

浏览器认证采用短期 Access Token 与可轮换 Refresh Token：

- Access Token 只保存在当前页面 JavaScript 内存中，受保护 API 使用标准
  `Authorization: Bearer <access-token>`。
- 页面刷新时通过携带 credential 的 `/auth/refresh` 恢复会话，不从 `localStorage` 读取凭据。
- opaque Refresh Token 只通过 `Path=/auth` 的 HttpOnly Cookie 传输，服务端只保存 SHA-256 hash。
- 每次 refresh 都轮换 token；仍在自然有效期内的 revoked token replay 会撤销该用户全部活跃
  refresh credential。
- `/auth/logout` 撤销当前设备，`/auth/logout-all` 撤销所有设备。

同一标签页使用 shared promise 合并 refresh，支持 Web Locks 的浏览器还会串行化跨标签页轮换。
获得锁后重新检查 generation、revocation tombstone 和 username 主体；Axios 只对仍属于同一
Access Token 与 username 的 401 重试一次，避免旧账号请求复用新账号凭据。

服务端 refresh 写路径按 `User → RefreshToken` 获取数据库锁，降低 rotate 与 logout-all 并发后
残留活跃 token 的风险。ledger 在自然过期后继续保留
`AUTH_REFRESH_LEDGER_RETENTION_DAYS`，用于审计与诊断，并由独立任务清理：

```bash
uv run --no-sync python -m backend.auth.cleanup
```

新注册和新密码统一写入 PBKDF2-SHA256。为了保留既有账号，登录边界仍会只读验证历史 bcrypt /
bcrypt-sha256 哈希，并在成功签发凭据的同一事务中单向改写为 PBKDF2；这是一段可删除的一次性
数据迁移读取器，不是第二套认证或会话实现。

生产认证至少需要：

```dotenv
APP_ENV=production
AUTH_REFRESH_COOKIE_SECURE=true
AUTH_REFRESH_COOKIE_SAMESITE=lax
AUTH_REFRESH_LEDGER_RETENTION_DAYS=30
RATE_LIMIT_ENABLED=true
RATE_LIMIT_BACKEND=redis
RATE_LIMIT_HMAC_KEY=<至少 32 字符且不同于 JWT_SECRET_KEY 的随机 Secret>
RATE_LIMIT_KEY_PREFIX=supermew
```

登录和注册在 PBKDF2/bcrypt 校验前执行直接 client IP 与 `IP + NFKC/casefold username` 两层限流。
Thread Run、知识检索、HITL resume、上传与 general API 使用独立 policy。Rate Limit 只读取经过
受控代理修正的 ASGI `scope.client`，不会自行信任任意 `X-Forwarded-For`。

所有 `/auth` unsafe POST 会先校验 Origin / Fetch Metadata、JSON media type、Content-Length 和
16 KiB body 上限。`Referrer-Policy`、`nosniff`、`X-Frame-Options: DENY` 与
`Permissions-Policy` 应用于全部 HTTP 响应；CSP 只应用于正式前端 HTML，不影响 FastAPI
`/docs` 与 `/redoc`。详细生命周期和清理步骤见
[认证与 Refresh Ledger Runbook](docs/runbooks/auth-token-lifecycle.md)。

## 目录与架构

### 后端

后端代码位于 `backend/`，统一使用 `from backend.xxx import ...`：

- [backend/app.py](backend/app.py)：FastAPI 入口、生命周期、CORS、安全响应头、路由与生产静态资源
  挂载。
- `api/`：HTTP 层。
  - [backend/api/router.py](backend/api/router.py)：health、auth、capabilities、models、evaluations、
    threads、documents 和 runs 路由聚合。
  - `api/routes/`：各领域请求校验、鉴权、响应 schema 和 service 调用。
  - [backend/api/resources.py](backend/api/resources.py)：Milvus、上传目录等共享资源。
- `threads/`：Thread 生命周期、ID 约束、Message 分页、append version 和活跃 Run 投影。
- `runs/`：Run 创建与幂等、Thread 并发控制、owner lease/fencing、取消、HITL resume 和 Agent 调度。
- `events/`：Event v1 contract、PostgreSQL Journal/outbox、Redis 通知和可恢复 SSE Adapter。
- `agent/`：`AgentRuntimeFactory`、固定中间件链、Run-local Context、预算和 Tool 循环检测。
- `rag/`：复杂度规划、检索、证据评判、Step-back / HyDE、Synthesis、Rerank 和 RAG Trace。
- `indexing/`：文档解析、三级分块、Embedding、Milvus schema/read/write 和 ParentChunk Store。
- `documents/`：Document Catalog、Document Version 两阶段发布、Index Job、retirement 和 cleanup。
- `evaluation/`：Dataset / Observation / Gate 契约、离线评分、Live/Prediction Adapter、持久 Job/Case、
  Judge Runtime 和报告构建。
- `model_control/`：Model Profile、Assignment、Model Snapshot 和兼容性校验。
- `capabilities/`、`skills/` 与 `tools/`：能力目录、持久控制面、Skill 装载与 Tool Adapter。
- `guardrails/`：Tool 调用前的确定性 policy、Run-bound approval 和 destination capability。
- `sandbox/`：隔离执行契约、预算、disabled Adapter 与 Docker Adapter。
- `sql_assistant/`：只读 PostgreSQL catalog、AST policy、查询预算、结果编码和脱敏。
- `web_research/`：搜索、抓取、URL policy、Evidence 和 citation 校验。
- `providers/`：模型、Embedding、Rerank 等 Provider Adapter，以及错误分类、重试和生命周期。
- `auth/`：Access/Refresh 签发、opaque hash、rotation、replay detection、撤销和 ledger cleanup。
- `rate_limits/`：入口 policy、HMAC identity、fixed-window limiter 与 memory/Redis Adapter。
- `security/`：Origin 校验、正式前端 CSP、全局安全响应头和 Milvus filter 安全构造。
- `infra/`：数据库、Redis、鉴权依赖等基础设施 Adapter。
- `db/`：SQLAlchemy ORM 与 Alembic 对应的持久化模型。
- `schemas/`：HTTP 请求与响应 Pydantic schema。
- `workers/`：独立进程入口。
  - [backend/workers/indexing.py](backend/workers/indexing.py)：持久化 Index Job worker。
  - [backend/workers/evaluation.py](backend/workers/evaluation.py)：持久化 RAG Evaluation worker。

### 前端

前端位于 `frontend/`，使用 Vite + Vue 3 + TypeScript + Pinia + Axios + Sass：

- `src/auth/session.ts` 与 `stores/auth.ts`：内存 Access Token、页面恢复、single-flight refresh、
  Web Locks 跨标签协调、401 单次重试和退出撤销。
- `stores/threads.ts`：Thread 创建、列表、切换和权威删除。
- `stores/runs.ts`：durable Run、Event cursor、重放、HITL resume 与真实取消。
- `stores/chat.ts`：把相同 `thread_id/run_id` 的 Event 投影到对应 assistant Message 与 RAG 步骤。
- `stores/documents.ts`：Document、构建 Job 和清理 Job 的持久进度。
- `stores/models.ts`、`stores/capabilityAdmin.ts` 与 `stores/evaluations.ts`：三个管理员控制面状态。
- `events/runEventStream.ts`：解析 Event v1 SSE，使用 `Last-Event-ID` 恢复，只在 reducer 成功后推进
  cursor。
- `events/runEventReducer.ts`：按 schema version、Run 和 sequence 验证、去重并投影 Event。
- `components/Chat/ThinkingTrace.vue` 与 `RetrievalTraceDetails.vue`：展示可公开的检索、评判、重写与
  合并步骤，不展示模型私有推理。
- `components/Chat/References.vue`：展示知识库来源、RRF / Rerank 分数、层级、合并叶子块和页码。
- `components/Documents/UploadSection.vue` 与 `DocumentSettings.vue`：上传、发布和清理状态机。
- `components/Models/ModelCenter.vue`：模型控制面。
- `components/Capabilities/CapabilityCenter.vue` 与 `CapabilityAdmin.vue`：能力目录与配置。
- `components/Evaluations/RagEvaluationWorkbench.vue`：Dataset、Job、Case、趋势和 Gate 工作台。
- `components/Run/ExecutionTimeline.vue`：Tool、Guardrail、Artifact 与 terminal 状态；正常 `ALLOW`
  不显示为一条额外执行告警。

开发构建命令：

```bash
cd frontend
npm run dev
npm run build
```

`npm run dev` 默认监听 <http://localhost:3000> 并代理 API；`npm run build` 输出
`frontend/dist/`，供 FastAPI 在生产模式下静态托管。

### 数据、迁移与评测资产

- `alembic/`：PostgreSQL schema 的前向迁移。
- `data/documents/`：默认本地上传目录；生产应通过 `UPLOAD_DIR` 指向持久卷。
- `evals/rag/`：版本化 Dataset、受控 corpus、Observation、baseline、Gate 和 JSON Schema。
- `scripts/evaluate_rag.py`：离线校验、评分与报告命令。
- `docs/adr/`：架构决策记录；`docs/runbooks/`：部署与运维流程。
- `tests/`：后端单元、契约、集成、故障注入、性能保护和迁移测试。
- `frontend/e2e/` 与前端 `*.spec.ts`：浏览器 E2E 和组件/状态单测。

## 核心流程

### 1. 项目全链路

1. 客户端调用 `POST /v1/threads` 获取服务端生成的 `thread_<uuid>`，或选择已有且属于当前用户的
   Thread。
2. 客户端调用 `POST /v1/threads/{thread_id}/runs`，提交 message、`idempotency_key`、期望 Thread
   version、并发策略、断连策略和可选的 `approved_tools`。
3. Run Service 在同一事务中追加用户 Message、assistant placeholder、Run 和初始 Event，并返回
   durable `run_id` 与新的 `thread_version`。
4. `RunAgentExecutor` 使用 owner lease 与 fencing token 领取执行，通过 `AgentRuntimeFactory` 构建
   固定中间件链和冻结的 Model Snapshot。
5. Agent 根据当前 Skill 和问题决定是否调用知识库、天气、Web、SQL、Sandbox 或受限自定义 Tool。
6. Tool progress、检索状态、Message delta、HITL 和 terminal 结果先进入 PostgreSQL Event Journal。
7. 客户端通过 `GET /v1/runs/{run_id}/stream` 观察 SSE；断线后使用 `Last-Event-ID` 重连，或通过
   `/events?after={sequence}` 补放。
8. `message.completed` 是最终正文与 `rag_trace` 的权威来源；assistant Message 与 Run 终态提交后
   才发布 `run.completed`、`run.failed` 或 `run.cancelled`。
9. `hitl.required` 将 Run 置为 `waiting_input`，客户端调用 `/resume` 恢复同一 Checkpoint；Stop
   调用 `/cancel` 并继续监听权威 terminal Event。

### 2. RAG 全链路

1. **复杂度规划：`classify_complexity`**
   - 明显的短单事实问题由本地规则直接判为 simple，不调用规划模型。
   - 其余问题由 Model Snapshot 中的 Fast 角色一次完成 simple/complex 判断。
   - complex 结果同时给出最多 `RAG_MAX_SUBQUERIES` 个子问题，不再追加一次拆题模型调用。
2. **检索执行**
   - simple：进入 `retrieve_initial`，执行一次标准检索。
   - complex：通过 LangGraph `Send` 并发执行子问题的“检索 → 证据评判”，并受
     `RAG_MAX_CONCURRENT_SUBQUERIES` 限制。
   - 每个 target 先对 `chunk_level == 3` 发起 Milvus Hybrid Search：Dense + Sparse + RRF。
   - 候选池由 `RETRIEVAL_CANDIDATE_K` 控制；未显式设置时使用内部候选倍率计算。
   - 只有 `HybridRetrievalUnsupported` 才对对应 target 使用 Dense fallback；其他 Provider failure
     显式失败。
   - 在完整候选池上执行 L3 → L2 → L1 Auto-merging，父块从版本绑定的 ParentChunk Store 读取。
   - 合并后进入 Rerank；未配置或 Provider 失败时保留已有排序并记录明确 trace。
3. **证据评判：`grade_documents`**
   - Grader 一次输出相关性、可回答性、歧义、置信度与 route。
   - route 只进入回答、一次重写、HITL 澄清/范围选择或无知识结束。
   - 评判 Provider 失败会返回明确错误，不切换另一套 grader。
4. **单选重写：`rewrite_question`**
   - Fast 角色在一次结构化调用中选择 Step-back 或 HyDE。
   - Step-back 生成更抽象的问题并与原问题组合检索。
   - HyDE 生成只用于检索的假设性答案，不把该文本当作最终回答证据。
5. **二次召回：`retrieve_rewritten`**
   - 对重写结果再执行一次 L3 召回、Auto-merging、Rerank 和证据复评。
   - 流程不会无限重写；一次重写后必须进入回答、澄清或无知识终态。
6. **答案生成与追踪**
   - Answer 角色只使用最终 Evidence 生成回答。
   - `rag_trace` 保存 route、rewrite method、初次/二次结果、检索模式、降级码、层级、合并信息、
     RRF score、rerank score 和 Provider issue，不保存私有推理。

### 3. Document Version 入库链路

1. 前端上传到 `POST /documents/upload/async`；API 保存 source object，并在 PostgreSQL 预留
   Document Version 与 Index Job。
2. 索引 worker 使用 lease、heartbeat、`SKIP LOCKED`、build fingerprint 与 execution fence 领取
   Job；API 重启不会丢失任务，旧 fingerprint worker 也不能构建新 profile candidate。
3. Document Loader 生成带稳定版本身份的三级分块；L1/L2 写入 ParentChunk staging，L3 写入隔离的
   Milvus candidate scope。
4. worker 核验 ParentChunk、Milvus 与 exact manifest，再使用 PostgreSQL CAS 切换
   `current_version_id`。
5. 同名旧版本在发布前始终可检索；新版本失败不会影响旧版本。
6. superseded、failed、delete 版本进入持久 cleanup queue，由 worker 使用独立 lease 和数据库时钟
   退避执行 exact-version 物理清理。
7. worker crash 后 RUNNING Job 可幂等重建；STAGED Job 只恢复 publish，不重复解析和向量化。

### 4. RAG Evaluation Job 链路

1. 管理员通过工作台或 `/v1/rag-evaluations/datasets` 导入满足 schema 的版本化 Dataset。
2. 创建 Job 时选择 Dataset、Gate policy 和可选 baseline；服务冻结四角色 Model Snapshot、Dataset
   fingerprint 和检索 profile。
3. Evaluation Worker 通过 lease 领取 Job，为每个 Case 执行真实 RAG、生成回答并保留 Evidence
   identity 与 latency observation。
4. Evaluator 对 correctness、groundedness、relevance、completeness、unsupported claim 和 conflict
   disclosure 生成结构化 Judge 结果。
5. Job 聚合 Case 指标、Gate 与 baseline delta，持久化 report；前端刷新后可继续查看进度和结果。
6. 取消 Job 会停止领取后续 Case，并保留已完成 Case 的审计记录。

### 5. Milvus 2.5+ 原生 BM25

- Collection 定义 `FunctionType.BM25`，输入为启用中文 analyzer 的 `text`，输出为
  `sparse_embedding`。
- 文档写入时客户端只提交原始文本和 Dense embedding；Milvus 负责分词、统计、稀疏特征生成与
  SPARSE_INVERTED_INDEX 维护。
- 查询时同时发起 Dense 与 Sparse `AnnSearchRequest`，再使用 `RRFRanker` 融合两路排名。
- Document Version、Tenant、Knowledge Base 和 chunk level 过滤在安全构造的 Milvus filter 中
  同时生效，避免跨版本或跨作用域召回。

### 6. Thread 记忆链路

1. 用户 Message 与 assistant placeholder 在创建 Run 时 append-only 写入 PostgreSQL，绑定
   `thread_id`、`run_id` 与单调 sequence。
2. 流式 delta 只是 Event 投影；完成、失败或取消时只落定对应 assistant Message，不改写其他历史。
3. Runtime 在 token budget 内读取原始 Message 和派生摘要；原始 Message 始终是事实来源。
4. 前端默认读取最近一页，并使用 `before` cursor 加载更早 Message；页面按 sequence 升序呈现。
5. Thread 列表分别投影 `thread_status`、`active_run_id` 和 `active_run_status`，Run 状态不会覆盖
   Thread 自身状态。
6. Thread version 表示 Message append version；创建一轮 Run 时随用户与 assistant 两条 Message
   增加，assistant terminal finalize 不再次递增。

## 正式 HTTP Interface

当前只保留 canonical Thread / Run / Event Interface，不再提供旧 `/chat`、`/chat/stream` 和
`/sessions` 兼容入口。

### 鉴权

- `POST /auth/register`：注册并返回只供内存使用的 Access Token，同时设置 HttpOnly Refresh
  Cookie。
- `POST /auth/login`：登录并签发 Access / Refresh credential；成功登录旧 bcrypt 账号时执行单向
  PBKDF2 迁移。
- `POST /auth/refresh`：轮换 Refresh Token，并签发新的内存 Access Token。
- `POST /auth/logout`：撤销当前设备 Refresh Token 并清除 Cookie。
- `POST /auth/logout-all`：使用 Access Token 鉴权，撤销当前用户全部活跃 Refresh Token。
- `GET /auth/me`：返回当前登录用户与角色。

### Thread / Run / Event

- `POST /v1/threads`：由服务端创建 `thread_<uuid>`，可选提交标题。
- `GET /v1/threads`：列出当前用户的 Thread，并聚合非终态 Run 投影。
- `GET /v1/threads/{thread_id}/messages?before={sequence}`：读取最近一页或按 cursor 加载更早的
  canonical Message。
- `DELETE /v1/threads/{thread_id}`：权威删除 Thread；存在已知或未知非终态 Run 时返回冲突。
- `POST /v1/threads/{thread_id}/runs`：幂等创建 durable Run，返回 `run_id`、created 标记与
  `thread_version`。
- `POST /v1/threads/{thread_id}/runs/stream`：创建/复用 Run 后立即返回 canonical Event SSE，适合
  需要单请求创建并观察的客户端；仍使用同一个 Run Service 和 Event Journal。
- `GET /v1/runs/{run_id}`：读取 Run 当前权威状态。
- `GET /v1/runs/{run_id}/events?after={sequence}`：分页重放持久 Event。
- `GET /v1/runs/{run_id}/stream`：订阅 Event v1 SSE；重连支持 `Last-Event-ID`。
- `POST /v1/runs/{run_id}/resume`：携带一次性 `hitl_token` 与幂等键恢复同一 Checkpoint。
- `POST /v1/runs/{run_id}/cancel`：请求取消真实后端 Run；客户端继续等待 terminal Event。

### 模型与能力控制面

- `GET /v1/models`：管理员读取 Model Profile、四角色 Assignment 和 Provider Secret 状态。
- `POST /v1/models`、`PUT/DELETE /v1/models/{profile_id}`：管理 Model Profile。
- `PUT /v1/models/assignments/{role}`：分配 Answer、Fast、Grader 或 Evaluator 模型。
- `GET /v1/capabilities`：返回当前账号可见且不含 Secret 的 Skill / Tool 目录、可用状态、网络与
  资源策略、审批要求。
- `GET /v1/capabilities/control-plane`：管理员读取能力配置，只返回 Secret 名称与配置状态。
- `POST /v1/capabilities/skills`、`PUT/DELETE /v1/capabilities/skills/{name}`：管理自定义 Skill；
  内建 Skill 可编辑/停用但不可删除。
- `POST /v1/capabilities/tools`、`PUT/DELETE /v1/capabilities/tools/{name}`：管理声明式公共 HTTPS
  JSON Tool；仍被 Skill 引用时不能停用或删除。
- `PUT /v1/capabilities/sql-assistant`：保存 SQL Assistant Secret 引用、allowlist 和预算。
- `PUT /v1/capabilities/web-research`：切换 Web Research。

### 文档与索引任务

- `GET /documents`：列出已发布文档、当前版本与 chunk 统计。
- `POST /documents/upload/async`：保存上传并提交持久 Index Job。
- `GET /documents/upload/jobs`：列出最近的构建 Job，供页面刷新后恢复。
- `GET /documents/upload/jobs/{job_id}`：查询状态、阶段、attempt、heartbeat 与退避时间。
- `DELETE /documents/delete/async/{filename}`：撤销检索 scope 并提交持久清理 Job。
- `GET /documents/delete/jobs`：列出最近的清理 Job。
- `GET /documents/delete/jobs/{job_id}`：查询物理清理进度或 dead-letter。

### RAG 评估

- `GET/POST /v1/rag-evaluations/datasets`：列出或导入 Dataset。
- `GET /v1/rag-evaluations/datasets/{dataset_id}`：读取版本化 Dataset 与 fingerprint。
- `GET/POST /v1/rag-evaluations/jobs`：列出或创建持久 Evaluation Job。
- `GET /v1/rag-evaluations/jobs/{job_id}`：读取 Job 进度、模型/检索 snapshot、指标和 report。
- `GET /v1/rag-evaluations/jobs/{job_id}/cases`：读取 Case 结果、Judge reason 与 Evidence identity。
- `POST /v1/rag-evaluations/jobs/{job_id}/cancel`：请求取消评估任务。

完整 OpenAPI 可在运行中的 `/docs` 查看。

## RAG 效果评测

仓库保留了小型、版本化的 RAG Dataset、受控语料、离线 Observation、baseline 和质量 Gate。
这些资产属于产品功能和 CI 门禁，不是临时测试产物。

```bash
uv run --frozen python scripts/evaluate_rag.py validate \
  --dataset evals/rag/rag_smoke_v1.json

uv run --frozen python scripts/evaluate_rag.py score \
  --dataset evals/rag/rag_smoke_v1.json \
  --observations evals/rag/offline_smoke_observations_v1.json \
  --gates evals/rag/gates_v1.json \
  --baseline evals/rag/baseline_v1.json \
  --report .artifacts/rag-eval/report.json \
  --markdown .artifacts/rag-eval/report.md \
  --fail-on-regression
```

真实 RAG 运行、profile/index fingerprint 和报告约束见
[RAG 评测说明](evals/rag/README.md)。提交入库的 baseline 与 Dataset 不应被 `.gitignore`
忽略；本地生成的报告统一写入 `.artifacts/`。

需要连接当前模型、Milvus 和隔离测试索引进行真实评测时运行：

```bash
uv run --frozen python scripts/evaluate_rag.py run \
  --dataset evals/rag/rag_smoke_v1.json \
  --gates evals/rag/live_gates_v1.json \
  --observations .artifacts/rag-eval/live-observations.json \
  --report .artifacts/rag-eval/live-report.json \
  --markdown .artifacts/rag-eval/live-report.md \
  --profile-id local-orion-eval-v1 \
  --index-id your-index-manifest-or-collection-version \
  --timeout-seconds 60
```

两类评测不能混用：

- offline smoke 用于验证 Dataset / Observation / Report 契约、指标计算和 baseline Gate 可复现性，
  provenance 为 `contract_smoke`，不代表生产检索质量。
- live run 真实调用当前 RAG、Provider 和索引，provenance 为 `live_rag`；报告绑定 corpus、RAG 源码、
  lockfile、模型、Embedding、Rerank 和检索配置的脱敏 fingerprint。

报告与 Observation 不保存 chunk 正文、Provider endpoint、Secret 或原始异常。修改 Dataset 后
fingerprint 会变化，旧 Observation 和 baseline 会被拒绝。生产门禁应逐步扩展到至少 200 条人工
标注 Case，覆盖单事实、跨文档综合、多跳、时间/版本、表格、代码、歧义/HITL、无知识、来源冲突、
近似实体、错别字、长问题和文档提示注入。

前端 **RAG 评估** 工作台建立在同一评测契约之上，但使用 PostgreSQL 持久化 Dataset、Job 和
Case，并由独立 worker 执行。CLI 更适合仓库 Gate、分支比较与生成可归档报告；工作台更适合持续
运行、历史趋势、Case 诊断和模型控制面联动。

## 技术栈

- **后端与 Agent**：Python 3.12、FastAPI、Uvicorn、Pydantic、LangChain、LangGraph、
  SQLAlchemy、Alembic、psycopg。
- **事实存储与通知**：PostgreSQL 保存业务事实；Redis 提供 Event transport、父分块缓存和生产
  Rate Limit。
- **向量与检索**：Milvus 2.5+、HNSW Dense index、SPARSE_INVERTED_INDEX、原生中文 analyzer、
  BM25 Function、RRF、Auto-merging。
- **Embedding 与 Rerank**：`langchain_huggingface` 本地 Embedding，默认固定 revision 的
  `BAAI/bge-m3`；可选 Jina-compatible Rerank Provider。
- **文档解析**：PyPDF、docx2txt、Unstructured、openpyxl、msoffcrypto-tool 和多级递归分块。
- **前端**：Vite、Vue 3 SFC、TypeScript、Pinia、Axios、Marked、DOMPurify、Highlight.js、
  FontAwesome 与 Sass。
- **质量工具**：pytest、pytest-asyncio、Ruff、mypy、Vitest、ESLint、Prettier、vue-tsc、
  Playwright 和 bundle size gate。
- **运行与部署**：uv、npm、Docker Compose；生产应用进程由 systemd、Kubernetes 或等价
  supervisor 管理。

## 环境变量

`.env.example` 是当前完整模板。模型名、DSN 或路径含义不要只根据变量名猜测，发布前应同时阅读
模板注释和 [生产部署 Runbook](docs/runbooks/deployment.md)。

### 模型与 RAG

- `ARK_API_KEY`、`BASE_URL`、`MODEL`、`FAST_MODEL`、`GRADE_MODEL`、`EVALUATION_MODEL`：模型
  Secret 与首次控制面种子。
- `MODEL_TIMEOUT_SECONDS`：模型单次请求上限；SDK 内建重试关闭，由 Provider policy 统一控制。
- `RETRIEVAL_TOP_K`、`RETRIEVAL_CANDIDATE_K`、`RETRIEVAL_CANDIDATE_MULTIPLIER`：最终片段数与
  叶子层候选池。
- `RAG_MAX_SUBQUERIES`、`RAG_MAX_CONCURRENT_SUBQUERIES`：复杂问题拆分和并行度上限。
- `RAG_MAX_CONTEXT_TOKENS`、`RAG_GRADER_EVIDENCE_CHARACTERS`、
  `RAG_GRADER_MAX_DOCUMENT_CHARACTERS`：Answer 与 Grader 的独立输入预算。
- `AUTO_MERGE_ENABLED`、`AUTO_MERGE_THRESHOLD`、`LEAF_RETRIEVE_LEVEL`：三级检索与父块合并。
- `VECTOR_TIMEOUT_SECONDS`：单个检索阶段 deadline，不能越过 Run 总 deadline。

### Embedding 与 Rerank

- `EMBEDDING_MODEL`、`EMBEDDING_MODEL_REVISION`、`EMBEDDING_DEVICE`、
  `DENSE_EMBEDDING_DIM`：本地 Dense 模型与 Milvus schema 身份。
- `EMBEDDING_TIMEOUT_SECONDS`、`EMBEDDING_EXECUTOR_WORKERS`、`EMBEDDING_MAX_CONCURRENCY`：
  Embedding 执行与并发预算。
- `EMBEDDING_QUERY_MICROBATCH_MS`、`EMBEDDING_QUERY_MAX_BATCH_SIZE`、
  `EMBEDDING_QUERY_QUEUE_SIZE`、`EMBEDDING_QUERY_CACHE_SIZE`、`EMBEDDING_CACHE_NAMESPACE`：查询
  micro-batch、队列和缓存。
- `RERANK_MODEL`、`RERANK_BINDING_HOST`、`RERANK_API_KEY`：可选 Rerank Provider；占位符会被
  识别为未启用。
- `RERANK_TIMEOUT_SECONDS`、并发/连接池、候选数、单文档字符、总字符、熔断阈值和 reset 时间：
  共同限制精排对端到端时延和内存的影响。
- `RERANK_MIN_SCORE`：Rerank 后 Evidence 的最低分，需要基于固定 Dataset 标定。

### 存储、Milvus 与 Document Version

- `DATABASE_URL`、`REDIS_URL`、`REDIS_KEY_PREFIX`：业务事实、通知、缓存和限流连接。
- `MILVUS_HOST`、`MILVUS_PORT`、`MILVUS_COLLECTION`、`MILVUS_TIMEOUT`：Milvus Adapter。
- `UPLOAD_DIR`：source object 持久目录；API 与索引 worker 必须指向同一位置。
- `MAX_UPLOAD_BYTES`、`MAX_DOCUMENT_PAGES`、`MAX_PAGE_CHARACTERS`、`PARSER_TIMEOUT_SECONDS`：
  单文件与解析预算。
- `MAX_ARCHIVE_ENTRIES`、`MAX_UNCOMPRESSED_BYTES`、`MAX_COMPRESSION_RATIO`：压缩包防护。
- `DEFAULT_TENANT_ID`、`DEFAULT_KNOWLEDGE_BASE_NAME`、`DOCUMENT_PARSER_VERSION`、
  `DOCUMENT_CHUNKER_VERSION`、`DOCUMENT_INDEX_VERSION`：Document build fingerprint；API 与索引
  worker 必须一致。

### Worker、Run 与 Agent

- `INDEX_WORKER_*`：索引 worker identity、poll、lease、heartbeat、retry 和 readiness TTL。
- `EVALUATION_WORKER_*`、`EVALUATION_CASE_TIMEOUT_SECONDS`：RAG Evaluation worker 的领取、心跳、
  Case deadline 和最大尝试次数。
- `RUN_DEADLINE_SECONDS`、`RUN_HEARTBEAT_SECONDS`、`RUN_ON_DISCONNECT`、
  `RUN_MULTITASK_STRATEGY`：Run 生命周期默认策略。
- `RUN_EVENT_QUEUE_SIZE`、`RUN_EVENT_POLL_INTERVAL_SECONDS`、`RUN_EVENT_STREAM_MAXLEN`、
  `OUTBOX_BATCH_SIZE`：Event transport 与 outbox 预算。
- `AGENT_RECURSION_LIMIT`、`AGENT_MAX_MODEL_CALLS`、`AGENT_MAX_TOOL_CALLS`、
  `AGENT_MAX_REPEATED_TOOL_CALLS`：Agent 循环上限。
- `AGENT_MAX_CONTEXT_TOKENS`、`AGENT_RESPONSE_RESERVE_TOKENS`、
  `AGENT_MEMORY_MESSAGE_THRESHOLD`：上下文与派生记忆预算。

### 认证、限流与跨源

- `JWT_SECRET_KEY`、`JWT_ALGORITHM`、`JWT_EXPIRE_MINUTES`、`JWT_REFRESH_EXPIRE_DAYS`：JWT 与
  credential 生命周期。
- `ADMIN_INVITE_CODE`：留空即禁用公开 admin 注册；启用时不得复用 JWT 或 Rate Limit Secret。
- `AUTH_REFRESH_LEDGER_RETENTION_DAYS`、`AUTH_REFRESH_COOKIE_NAME`、
  `AUTH_REFRESH_COOKIE_SECURE`、`AUTH_REFRESH_COOKIE_SAMESITE`：Refresh ledger 与 Cookie policy。
- `PASSWORD_PBKDF2_ROUNDS`：新密码和旧账号迁移后的 PBKDF2 cost。
- `RATE_LIMIT_ENABLED`、`RATE_LIMIT_BACKEND`、`RATE_LIMIT_HMAC_KEY`、
  `RATE_LIMIT_KEY_PREFIX`：入口限流；生产必须使用 Redis 和独立随机 HMAC Key。
- `CORS_ORIGINS`、`CORS_ALLOW_CREDENTIALS`：显式跨源 allowlist；生产优先同源，只允许一个
  canonical credentialed 前端 Origin。

### 可选能力

- `SQL_ASSISTANT_*`：只读 DSN、预期数据库角色、allowlist、敏感列、超时、成本和结果预算。
- `WEB_RESEARCH_*`：搜索、DNS、URL、redirect、响应、Evidence、citation 和并发预算。
- `SANDBOX_*`：Docker Adapter、digest image、rootless daemon、CPU、内存、PID、workspace、源码、
  输出和清理预算。
- `SKILL_DIR`、`SKILL_MANIFEST_NAME`、`SKILL_MAX_CONTENT_BYTES`：文件型 Skill Registry 输入。
- `LOG_LEVEL`、`METRICS_ENABLED`、`TRACE_RETENTION_DAYS`：可观测性与保留周期。

## 持久化 Run/Event 与可恢复流 — 技术细节

下列 `Authorization: Bearer <token>` 表示受保护 Interface 的标准 Access Token 传输方式。浏览器
从内存认证状态读取 token；示例不授权把它写入 `localStorage`、Cookie 或其他持久客户端存储。

### 1. 创建 durable Run

客户端先创建 Thread，再独立创建 Run；Run Interface 不会为任意路径 ID 隐式创建 Thread：

```http
POST /v1/threads
Authorization: Bearer <token>
Content-Type: application/json

{
  "title": "文档结论对比"
}
```

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

响应中的 `run.id`、assistant message identity 与 `thread_version` 是后续重放、取消和 HITL 恢复的
稳定身份。相同用户、Thread 与幂等键只创建一个 Run；同一 Thread 的并发写入由 version、数据库
约束、owner lease 和 fencing token 共同保护。

`thread_version` 是本轮用户 Message 与 assistant placeholder 两次 append 后的版本。后续 delta、
正文完成或 terminal finalize 不再增加该 version，因此客户端可直接把返回值用于下一轮
optimistic write。

### 2. Event v1 Interface

`RunAgentExecutor` 驱动 `AgentRuntimeFactory`，并把规划、Tool、检索、Message、HITL、usage 和
terminal 状态追加到 Event Journal。SSE 是 Journal 的可恢复投影：

```text
id: 42
event: message.delta
data: {"schema_version":1,"event_id":"evt_xxx","sequence":42,"run_id":"run_xxx","thread_id":"thread_xxx","type":"message.delta","timestamp":"...","data":{}}
```

关键不变量：

- `sequence` 在单个 Run 内严格单调，前端 reducer 按 `(run_id, sequence)` 去重并检测 gap。
- `message.delta` 只负责临时展示；`message.completed` 是最终正文与 `rag_trace` 的权威来源。
- `run.completed`、`run.failed`、`run.cancelled` 是 terminal Event，不使用非结构化 `[DONE]`。
- assistant Message 与 Run 终态先在持久化事务中落定，再发布 terminal Event。
- Event Envelope 不暴露模型私有推理、Secret、原始 SQL、Sandbox 宿主路径或未脱敏 ToolAudit。

### 3. 重放、重连与 heartbeat

首次订阅：

```http
GET /v1/runs/{run_id}/stream
Authorization: Bearer <token>
```

连接中断后，客户端保留最后一个成功应用的 sequence，并通过任一方式恢复：

```http
GET /v1/runs/{run_id}/events?after=42

GET /v1/runs/{run_id}/stream
Last-Event-ID: 42
```

服务端先从 PostgreSQL Journal 重放缺失 Event，再通过 Redis transport 等待新 Event；空闲时发送
SSE heartbeat。客户端遇到 sequence gap 时不能推进 cursor，应先补放；terminal 后关闭该 Run 的
本地订阅。

### 4. RAG 进度与前端投影

知识库 Tool 由 `backend/rag/pipeline.py` 执行复杂度判断、检索、Rerank、证据评判、一次重写和
Synthesis。不同阶段通过 `tool.started`、`tool.progress`、`retrieval.*`、`message.*` 和 terminal
Event 表达。

前端 `runs` store 管理 Run 与 cursor，`chat` store 只把相同 `thread_id/run_id` 的 Event 投影到
对应 assistant Message。Thinking Trace 展示 Searching、Grading、Rewriting、Synthesizing 等
操作性阶段及公开 metadata，但不展示模型 chain-of-thought。

### 5. HITL 与取消

- 收到 `hitl.required` 时，Run 已保存 Checkpoint 并进入 `waiting_input`。客户端使用一次性
  `hitl_token` 和幂等键调用 `/resume`，恢复同一图节点；刷新页面不需要重新执行原问题。
- Stop 调用 `/cancel`。HTTP 响应表示取消请求已接受或 Run 已终止；客户端继续监听，直到收到
  权威 terminal Event。
- 关闭页面、切换账号或断开 SSE 默认只关闭观察连接；`on_disconnect=continue` 的 Run 在后端继续。

### 6. Hybrid Search 深度实现

- **Dense Pathway**：`langchain_huggingface.HuggingFaceEmbeddings` 使用固定模型 revision 生成
  Dense vector，维度必须与 Milvus `dense_embedding` schema 一致；默认 `BAAI/bge-m3` 为 1024。
- **Sparse Pathway**：文档只需写入启用中文 analyzer 的 `text` 字段，Milvus 通过
  `FunctionType.BM25` 生成并维护 `sparse_embedding`。
- **RRF 融合**：Milvus 使用两个 `AnnSearchRequest` 发起 Dense/Sparse 检索，并用
  `RRFRanker` 融合排名，避免在业务层维护另一份稀疏统计。
- **Auto-merging**：先完整保留叶子候选，再根据同版本父子身份和阈值向 L2/L1 合并；禁止跨
  Tenant、Knowledge Base、Document Version 或 build fingerprint 读取父块。
- **Rerank**：对合并后的有界候选调用 Provider；payload 会限制候选数、单文档字符和总字符，
  同时受 deadline、并发、连接池与熔断保护。
- **失败语义**：只有 `HybridRetrievalUnsupported` 允许 Dense fallback，并记录
  `HYBRID_RETRIEVAL_DEGRADED`；Rerank Provider failure 只回退到已经生成的融合排序并记录 issue；
  其他检索、Embedding、模型或 Grader failure 显式失败。

## 测试与质量门禁

```bash
uv run --no-sync ruff format --check .
uv run --no-sync ruff check .
uv run --no-sync mypy
uv run --no-sync python scripts/generate_contract_types.py --check
uv run --no-sync python scripts/generate_rag_eval_schemas.py --check
uv run --no-sync python -m backend.tools.registry_cli validate
uv run --no-sync pytest -q

cd frontend
npm run format:check
npm run lint
npm run typecheck
npm run test:unit
npm run build:check
```

需要浏览器回归时运行：

```bash
cd frontend
npm run test:e2e:install
npm run test:e2e
```

后端测试覆盖单元、HTTP 契约、迁移、PostgreSQL/Redis/Milvus 可选集成、故障注入、Provider 错误
语义、RAG latency guard、索引 worker、Evaluation worker、认证并发和 Sandbox 安全。前端测试覆盖
认证恢复、Store、Event reducer/stream、能力控制面、RAG 评估展示和 Playwright 浏览器链路。

非模型 Runtime 开销使用固定 fake Adapter 建立可复现门禁：

```bash
uv run --no-sync python -m scripts.benchmark_runtime \
  --check \
  --report .artifacts/runtime-benchmark.json
```

该门禁衡量 Thread HTTP、Event 首次持久投影、SSE 编码和本地取消信号，不可替代固定模型、固定
Document Version、固定硬件下的真实 RAG TTFT、总延迟、tokens/s 和正确率 E2E。

生产依赖 Compose 至少先做解析校验：

```bash
docker compose config
docker compose -f docker-compose.prod.yml config
```

完整门禁、覆盖率、依赖审计、真实存储兼容 smoke 与 CI 对应关系见
[仓库质量门禁](docs/runbooks/repository-quality-gates.md) 和
[前端质量门禁](docs/runbooks/frontend-quality-gates.md)。

## 后续迭代

### RAG

1. 在当前三级分块基础上继续完善代码块、表格、图片、扫描件和复杂 Office 文档的专用解析，并
   保持 Document Version 与 manifest 可复现。
2. 使用版本化 RAG Evaluation profile 标定 BM25、候选池、Auto-merging、RRF 与 Rerank 阈值，
   所有调参同时报告质量、p50/p95 时延和 Provider 调用次数。
3. 把人工标注集扩展到至少 200 条，并在固定 corpus、Document Version、Index Manifest、模型和
   Embedding revision 上维护可比较 baseline。
4. 增加 citation precision/recall、答案中的 claim-to-evidence 对齐，以及冲突来源的显式披露。
5. 评估多文档 Refine、多模态 Embedding 和图片/表格 Evidence，但不为尚未验证的能力增加并行
   运行时路径。

### 平台能力

1. 将当前 RAG 子问题并行扩展为有明确预算、Event contract 和恢复语义的多步骤专业 Agent 协作。
2. 扩展可信路由，使专业 Skill 在进入 graph 前稳定激活，同时继续按需披露 Tool schema。
3. 继续优化派生摘要与长期记忆，在任何优化中保持原始 Message 为事实来源。
4. 支持修改 Thread 标题、更多 Artifact 持久化 Adapter 和完整所有权下载 Interface。
5. 在历史 bcrypt 账号全部迁移后，删除一次性 legacy password verifier 及对应依赖和测试。

### 已完成的基础治理

- Access Token、可撤销 Refresh Token 与数据库角色共同保护用户隔离；`admin` 管理文档与控制面，
  `user` 只能读取和删除自己的 Thread。
- PostgreSQL 持久化 Thread/Message、Run/Event/Checkpoint、Document Version、Index Job、Evaluation
  Job 和控制面配置；Message 使用 append-only sequence。
- Redis 不保存 Thread Message 或 Thread 列表快照，只提供低延迟通知、版本绑定缓存和共享限流。
- 文档新版本通过 candidate build、exact manifest 核验和 CAS publish 发布；旧版本在成功切换前
  持续可检索。
- canonical `/v1` Thread/Run/Event Interface 已替代旧聊天兼容路由，避免两套执行链长期并存。
- 普通 Guardrail `ALLOW` 不再投影为前端告警；拒绝和审批仍保留真实安全语义与审计证据。
- 新密码统一 PBKDF2；旧 bcrypt/bcrypt-sha256 只在成功登录时单向迁移，不创建第二套会话路径。

## 文档索引

- [领域语言](CONTEXT.md)
- [生产部署](docs/runbooks/deployment.md)
- [RAG 评测说明](evals/rag/README.md)
- [持久化索引 worker](docs/runbooks/persistent-indexing-worker.md)
- [Skill / Tool Registry](docs/runbooks/skill-tool-registry.md)
- [Web Research](docs/runbooks/web-research.md)
- [SQL Assistant](docs/runbooks/sql-assistant.md)
- [Guardrail 与 Sandbox](docs/runbooks/guardrails-and-sandbox.md)
- [认证与 Refresh Ledger](docs/runbooks/auth-token-lifecycle.md)
- [仓库质量门禁](docs/runbooks/repository-quality-gates.md)
- [前端质量门禁](docs/runbooks/frontend-quality-gates.md)
- [架构决策记录目录](docs/adr/)
- [Agent Runtime 与中间件顺序](docs/adr/0011-agent-runtime-and-middleware-order.md)
- [Provider 错误与重试语义](docs/adr/0012-provider-error-and-retry-semantics.md)
- [异步 Provider Runtime](docs/adr/0013-async-provider-runtime.md)
- [RAG Evaluation Contract](docs/adr/0014-rag-evaluation-contract.md)
- [Document Version 发布](docs/adr/0015-document-version-publication.md)
- [持久化索引 worker](docs/adr/0016-persistent-indexing-worker.md)
- [Skill / Tool Registry](docs/adr/0017-skill-tool-registry.md)
- [只读 SQL Assistant](docs/adr/0018-read-only-sql-assistant.md)
- [Web Research 与引用](docs/adr/0019-web-research-and-citations.md)
- [Guardrail 与隔离 Sandbox](docs/adr/0020-guardrails-and-isolated-sandbox.md)
- [Canonical Thread Lifecycle](docs/adr/0021-canonical-thread-lifecycle.md)
- [浏览器认证与入口限流](docs/adr/0022-browser-auth-and-inbound-rate-limits.md)
- [单一正式实现](docs/adr/0023-single-canonical-implementation.md)
- [模型控制面与 RAG Evaluation Runtime](docs/adr/0024-model-control-and-rag-evaluation-runtime.md)
- [持久化能力控制面](docs/adr/0025-persistent-capability-control-plane.md)
