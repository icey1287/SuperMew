# Backend AGENTS.md

本文件适用于 `backend/` 下的工作，并继承仓库根目录 `AGENTS.md`。它是后端方向的深度指南；进入 `backend/rag/` 或 `backend/auth/` 时继续读取当地指南。

同级 `CLAUDE.md` 只导入本文件。后端领域术语以 `../CONTEXT.md` 为准，架构细节以 `../docs/adr/` 为准，完整门禁以 `../docs/runbooks/repository-quality-gates.md` 为准。

## 技术栈与入口

- Python `>=3.12`，依赖由 `pyproject.toml` + `uv.lock` 管理。
- FastAPI 入口：`backend/app.py`；路由聚合：`backend/api/router.py`。
- PostgreSQL 保存领域事实；Redis 提供通知、跨实例取消和限流；Milvus 保存向量/稀疏索引。
- LangChain/LangGraph 驱动 Agent 与 checkpointed RAG；同步图通过专用 Provider Runtime bridge 调用 async Provider。
- 常驻进程：API、`backend.workers.indexing`、`backend.workers.evaluation`；`backend.auth.cleanup` 由 scheduler 周期运行。

`backend/app.py` 的 lifespan 拥有资源启动与逆序关闭。不要在模块 import 时启动线程、创建 AsyncClient、加载 Embedding 模型或连接外部基础设施。

## 目录所有权

```text
backend/
├── api/                 # HTTP 校验、鉴权、响应 schema、service 调用
├── threads/             # Thread/Message 生命周期与 append version
├── runs/                # Run 预留、幂等、owner lease/fencing、取消、HITL
├── events/              # Event v1、PostgreSQL Journal/outbox、Redis 通知、SSE
├── agent/               # Runtime factory、固定 middleware、Run request context
├── rag/                 # 检索图、证据评判、重写、合成、RAG Trace
├── documents/           # Document Catalog、Version 发布、Job 与清理
├── indexing/            # 解析、分块、Embedding、Milvus 与 parent store
├── workers/             # 独立持久 worker 入口
├── providers/           # 模型、Embedding、Rerank Adapter 与统一错误/重试
├── model_control/       # Model Profile、Assignment、Snapshot
├── evaluation/          # Dataset/Job/Case、Judge、Report
├── capabilities/        # 持久 Capability control plane
├── skills/              # 版本化 Skill 装载
├── tools/               # Tool contract、Registry、控制面和生成类型
├── guardrails/          # 确定性 policy、approval、destination capability
├── sandbox/             # 隔离执行 Adapter 与资源预算
├── sql_assistant/       # 管理员只读 SQL 边界
├── web_research/        # 搜索/抓取、URL policy、Evidence、citation
├── auth/                # Access/Refresh 生命周期与 cleanup
├── rate_limits/         # policy、HMAC identity、memory/Redis limiter
├── security/            # Origin、响应头、CSP、Milvus filter
├── infra/               # 数据库、Redis、鉴权等基础设施 Adapter
├── db/                  # SQLAlchemy ORM
├── schemas/             # HTTP Pydantic schema
└── core/                # settings、公开错误等共享基础
```

把逻辑放到拥有事实与不变量的 Module。HTTP route 应保持薄：校验输入、解析身份、调用 service、返回 schema；不要在 route 内复制事务、重试、状态机或安全策略。

## 开发命令

```bash
uv sync --dev --locked

# 聚焦测试
uv run --no-sync pytest -q tests/test_file.py
uv run --no-sync pytest -q tests/test_file.py::test_name

# 静态门禁
uv run --no-sync ruff format --check .
uv run --no-sync ruff check .
uv run --no-sync mypy

# 生成物与 Registry
uv run --no-sync python scripts/generate_contract_types.py --check
uv run --no-sync python scripts/generate_rag_eval_schemas.py --check
uv run --no-sync python -m backend.tools.registry_cli validate

# 完整覆盖率
uv run --no-sync pytest -q \
  --cov=backend --cov=scripts \
  --cov-report=term-missing:skip-covered \
  --cov-fail-under=80
```

改动迁移、Redis/Milvus 协议、生产 Compose、认证并发或真实 PostgreSQL 锁序时，运行 Runbook 中对应 integration/smoke；不要用 SQLite 或 fake Adapter 的通过结果替代真实协议证据。

## 架构规则

### Thread、Run、Message 与 Event

- Thread 是连续对话容器；Run 是一次持久执行。不要用 Session/Request/Task 代替正式身份。
- 一个 Run 恰好预留一个用户 Message 和一个 assistant Message；幂等键和 Model Snapshot 在预留事务中冻结。
- Thread append version 只保护新增 Message 顺序；更新已有 assistant Message 正文/终态不推进 append version。
- Event 先写 PostgreSQL Journal，再由 outbox/Redis/SSE 投影。Redis 丢失只能影响低延迟通知，不能丢失事实。
- 所有 Event writer 都必须遵守 owner lease 与 fencing；terminal 后拒绝旧 owner 的 delta、progress 和 trace。
- `message.completed` 与 Run terminal Event 由同一权威终结事务产生。不要从模型返回值、Queue 关闭或 SSE 生命周期直接制造终态。

### HITL、取消与恢复

- `waiting_input` 只有在 Checkpoint、Run 状态和 `hitl.required` 同事务持久化后才成立。
- Resume 使用同一 Checkpoint 的 `Command(resume=...)`，并保持 Run/Thread/assistant Message 身份。
- 用户取消、进程 shutdown、ownership lost 是不同原因；取消必须在 terminal 竞态中按既定优先级落定。
- API 请求取消只建立持久取消事实；实际 Provider/Tool 检查 cancellation probe 并由 executor 原子终结。

### Request context 与并发

- Run-local 状态通过 `RunRequestContext` 显式传递：Tenant、Queue、event loop、RAG Trace、retrieval/model snapshot、deadline、cancellation、Web Evidence、预算和 destination authority。
- 不新增模块级可变请求状态、ContextVar 隐式兜底或“最后一次结果”缓存。必须共享时，按进程资源、Tenant 资源或 Run 资源明确所有权和关闭时机。
- 从 worker thread 向 asyncio Queue 投递，使用 Queue 所属 loop 的线程安全调度；不要在错误 loop 直接 `put_nowait`。
- 异步测试等待可观察条件，不依赖固定 sleep 或任务调度好运气。

### Agent Runtime

- `AgentRuntimeFactory.create()` 拥有模型、工具、基础 prompt、预算与固定 middleware chain。
- `RunAgentExecutor` 拥有领取、续租、执行、Event、取消、终结和恢复。公开执行不得绕过该 seam 直接调用 graph/tool。
- 修改 middleware 顺序前读取 `docs/adr/0011-agent-runtime-and-middleware-order.md`，更新顺序测试并说明对上下文、Tool policy、HITL 和终态的影响。
- Dynamic memory 是不可信数据；active Skill instructions 是完整可信块。上下文裁剪必须保持 tool call 与 ToolMessage 成组。

### Provider Runtime

- 统一通过 `ProviderExecutor` 分类 typed error、绝对 deadline、取消、Retry-After 与有限重试。
- OpenAI-compatible SDK 的内建重试保持关闭；答案流一旦发布 delta，不得从头重试同一答案。
- 同步 checkpoint 图只从没有 running loop 的 worker thread 通过 `ProviderLoopBridge` 调用 async Provider。
- 不要仅把 `invoke()` 改成 `ainvoke()`。整图异步迁移必须同时具备 async checkpointer 和所有节点的 async Implementation。
- Adapter 返回健康空结果前验证外层、hits/entity/index/score 形状；畸形响应是 Provider failure。

### Model Control

- Model Profile 与 Assignment 存数据库且不含 Secret；环境变量只负责首次种子。
- Run/Evaluation Job 使用创建时冻结的 Model Snapshot。执行、重试、HITL resume 和 worker 重领期间不得动态查询“当前模型”。
- 角色能力 fail-closed：Answer 需要 stream，Fast/Grader/Evaluator 需要 structured output；缺失 Assignment 或 Secret 不静默换模型。

### Document 与 Indexing

- Document 是稳定逻辑身份；Document Version 是不可变内容版本；Index Job 是持久后台工作，不是 Run。
- 新版本在 candidate scope 中解析、分块、Embedding、写索引并校验 manifest，随后以 PostgreSQL CAS 原子发布。
- 失败/取消/dead-letter candidate 不得污染 current version；删除和 retirement 必须使用完整 Tenant + Document + Version identity。
- 上传 API 只提交 durable Job。文件解析、Embedding、Milvus 写入和物理清理由 indexing worker 拥有。

### Capability、Tool 与安全

- Registry 决定 Tool 是否可见；Guardrail 在 handler 前做确定性 `ALLOW`/`DENY`/`REQUIRE_APPROVAL`；ToolAudit 保存脱敏细节。
- 缺失 policy、Tenant、approval 或 request-bound capability 时 fail-closed。
- 自定义 HTTP Tool 仍受 HTTPS/443、DNS pin、SSRF、redirect、Content-Type、deadline 与 byte budget；控制面不能上传 Python/Shell。
- Sandbox 固定 digest、无网络、无宿主挂载、受限资源；不要把 Sandbox 当授权机制。
- SQL Assistant 只对 admin，且依赖独立只读账号、allowlist、AST、RLS、成本/超时/结果预算和脱敏。

### 公开错误与可观测性

- HTTP、ToolResult、Run terminal 和前端投影共享稳定公开错误 taxonomy；不要把 typed code 覆盖成统一 `INTERNAL_ERROR`。
- 日志、Event、Trace、Checkpoint 不写 endpoint、响应正文、原始异常、token、Cookie、DSN 或私有推理。
- RAG Trace 只保存公开的检索路线、候选身份、评分、降级和耗时，不保存 chain of thought。

## 数据库与迁移

- ORM、Repository、migration 和测试必须同一变更集更新。
- 迁移是把旧 schema 单向转换为当前 schema 的证据，不是运行时保留旧字段的许可。
- 对 PostgreSQL 锁序、并发 CAS、partial index、JSON/时间类型等行为，增加真实 PostgreSQL 测试；SQLite 只证明受支持的转换链。
- 生产迁移前向执行并校验 `assert_schema_current()`。不可逆迁移明确拒绝 downgrade 或不满足前提的数据。

## 代码风格

- 导入统一使用 `from backend...` 绝对路径。
- 新的关键 Interface、公开函数和数据模型使用完整类型；不要以全局 `# type: ignore` 或宽泛 `Any` 绕过边界。
- 异步函数中不使用同步 HTTP、`subprocess`、内建 `open` 或 `time.sleep`；Ruff 的 ASYNC 规则只是最低门禁，仍需审查同步 SQLAlchemy、第三方 SDK 和 CPU 工作。
- 同步 PBKDF2/SQLAlchemy route 保持同步，由 FastAPI 在线程池执行整个 handler；不要包装半条调用链造成 event-loop 阻塞。
- 依赖注入优先于隐藏全局；进程级 singleton 只用于有明确 lifespan 的 Runtime/Service。

## 按变更类型验证

| 变更 | 最低验证 |
| --- | --- |
| Route/schema | 聚焦 route 测试 + 公开错误/鉴权测试 |
| Run/Event/HITL | service/repository/executor + SSE/取消/恢复测试 |
| 并发状态 | 至少两个并发请求/Run，验证身份与输出不串号 |
| Provider/RAG | Provider failure、deadline、取消、降级 + 对应评测 |
| Contract | 生成器 `--check` + 后端/前端消费者测试 |
| ORM/migration | migration 测试；需要时 PostgreSQL integration |
| Registry/Tool | Registry validate + policy/handler/audit 测试 |
| Auth/Rate limit | 读取 `auth/AGENTS.md` 并运行其聚焦回归 |
| Document/Indexing | Catalog/Job/worker/Milvus 版本过滤与清理测试 |

## Code Review Rules

### 持久事实旁路

- 阻止 route、脚本或内部 helper 直接执行 Agent、RAG、Tool 或索引并返回结果，却不建立正式 Run/Job/Event/Audit。
  安全路径：跨越对应 service/executor/worker seam，并复用正式状态机。

### 资源生命周期

- 阻止 import-time I/O、跨 event loop 复用 async client、后台 task 无 owner、关闭顺序丢资源。
  安全路径：由 FastAPI lifespan 或独立 worker main 明确 start/close，异常关闭聚合且不隐藏主错误。

### 事务与 fencing

- 阻止把状态和 Event 分事务写入、忽略 owner/fence、terminal 后继续写、或用内存锁代替多实例事实。
  安全路径：Repository 原子事务、数据库约束/锁、lease 与 stale-writer 回归测试。

### 错误语义

- 阻止 catch-all 后返回空列表/成功、泄露原始异常，或覆盖稳定 typed code。
  安全路径：Provider/PublicError taxonomy，明确 retryable/fallback，并测试终态投影。
