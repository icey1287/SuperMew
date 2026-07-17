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
./scripts/start.sh
```

```bash
# 方式 B：pip
python -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -e .

# 运行服务
alembic upgrade head
python -m backend.launcher
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
在 Milvus 启动且前端编译完成后，返回项目根目录并运行统一启动器：

```bash
# 若当前处于 frontend 目录下，先返回项目根目录
cd ..

# 同时启动 API 与持久化 Index Job worker
./scripts/start.sh
```

启动器默认启用 Uvicorn 自动重载；需要关闭时运行：

```bash
./scripts/start.sh --no-reload
```

启动器使用同一个 Python 环境和项目配置拉起两个进程，确保 API 与 Index Job worker 共享 `UPLOAD_DIR`。任一进程退出时，启动器会关闭另一个进程，避免 API 仍可访问但文档任务无人消费。`GET /health/ready` 会继续检查近期 worker heartbeat。

当前 `docker-compose.yml` 与 `docker-compose.prod.yml` 只管理 PostgreSQL、Redis、Milvus 等依赖，不会启动应用进程。本地使用 `scripts/start.sh`；生产部署仍应由 systemd、Kubernetes 或等价 supervisor 分别管理 API 与 Index Job worker，并为它们挂载同一持久上传目录、配置自动重启和 termination grace。

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
Runtime 内使用，不进入 prompt、Run state、Checkpoint、Event 或审计：

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

浏览器认证采用短期 Access Token + 可轮换 Refresh Token。Access Token 只保存在当前页面的
JavaScript 内存中，受保护 API 仍使用标准 `Authorization: Bearer <access-token>`；页面刷新时
由前端以 credentials 调用 `/auth/refresh` 恢复，不从 `localStorage` 等持久存储读取凭据。
Opaque Refresh Token 只通过固定 `Path=/auth` 的 HttpOnly Cookie 传输，服务端仅保存 SHA-256
hash；每次刷新都会轮换，仍在自然有效期内的 revoked token replay 会撤销该用户所有活跃
refresh credential。
`/auth/logout` 撤销当前设备，`/auth/logout-all` 撤销全部设备。

同一标签页用 shared promise 合并 refresh；支持 Web Locks 的浏览器还会串行化跨标签页轮换。
获得锁后会重新检查 generation/revocation tombstone，refresh 响应必须保持 username 主体一致；
Axios 只对仍属于同一 Access Token 与 username 的 401 重试一次，旧账号请求不能跨账号复用新
credential。退出会等待在途 refresh response 落定后再撤销最新 Cookie。

服务端 refresh 写路径统一按 `User → RefreshToken` 获取数据库锁，避免 rotate/logout-all 并发后
残留活跃 token。ledger 在 token 自然过期后继续保留
`AUTH_REFRESH_LEDGER_RETENTION_DAYS`（默认 30 天），仅作为 forensic/audit evidence 与运维诊断；
过期 token 只返回 expired，不触发用户级 replay 撤销。API 不负责清理，部署方必须独立调度：

```bash
uv run --no-sync python -m backend.auth.cleanup
# 或安装后的 supermew-auth-cleanup
```

生产环境必须同时启用 Secure Refresh Cookie 与 Redis Rate Limit，并为 identity HMAC 使用与
JWT Secret 分离的随机 key：

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

开发/测试可使用单进程 memory Adapter；生产 Redis 不可用时入口限流 fail-closed 返回 typed
503。登录和注册在密码校验前同时执行直接 client IP 与 `IP + NFKC/casefold username` 两层
限流；复合 bucket 每次消耗两个 quota unit，以降低单一来源集中尝试同一用户名的额度，同时
避免 username-only 全局 bucket 带来的账户 DoS。Thread Run、chat retrieval、HITL resume、
上传与 general API 使用独立 policy。

Rate Limit 只读取 ASGI `scope.client`，不会信任任意 `X-Forwarded-For`。反向代理部署必须先由
受控 ProxyHeaders/forwarded allowlist 修正真实来源；若未配置，公网请求会共享代理 IP bucket，
若直接信任客户端转发头则会产生身份伪造绕过。

有效 Bearer 会先验证并解析为稳定 username subject，再进入 HMAC bucket；access token 轮换不会
刷新配额。opaque refresh 与当前设备 logout 使用 120/min client host 粗限额，logout-all 使用
稳定 subject；原始 access/refresh token 不会成为 quota identity。除
health/docs/正式静态资源/preflight 外，动态路径默认使用 general policy，因此 deprecated
`/sessions` 和未来新增 route 不会默认 fail-open。

所有 `/auth` unsafe POST 先在 Rate Limit 前校验来源、login/register JSON media type、
Content-Length 语法与声明的 16 KiB 上限。same-origin 始终可信；跨 origin 仅在
`CORS_ALLOW_CREDENTIALS=true` 且命中显式 allowlist 时可信，空 allowlist 表示
same-origin-only。畸形/`null` Origin 和无可信来源的 same-site/cross-site Fetch Metadata 会被
拒绝。Rate Limit 计费后才流式累计实际 body，以阻止无/伪 Content-Length 的慢速请求在未计费时
占用连接；超过 16 KiB 或非空非 JSON body 仍会在 route、PBKDF2/token mutation 前拒绝。
Web Locks 只在同一浏览器 Origin 内共享，因此生产 credentialed 跨源部署最多配置一个 canonical
前端 Origin；多个前端必须使用独立 Cookie/API host，或先设计服务端 refresh family 并发协议。
所有 `/auth` 成功、401 与 429 响应均为 `no-store`。Vite 开发服务器固定使用 3000，本地默认
allowlist 与之保持一致。

`Referrer-Policy`、`nosniff`、`X-Frame-Options: DENY` 与 `Permissions-Policy` 应用于全部 HTTP
响应，CSP 只应用于正式前端 HTML，FastAPI `/docs` 与 `/redoc` 不附加 CSP。详细不变量与 purge
操作见 `docs/adr/0022-browser-auth-and-inbound-rate-limits.md` 和
`docs/runbooks/auth-token-lifecycle.md`。

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
  - 持久化 Thread、Run、Event 与 Checkpoint，支持断线重放、真实取消和 HITL 恢复。
  - 文档上传后由持久化 Index Job 构建不可变 Document Version；叶子分块写入 Milvus，父级分块写入 PostgreSQL。
  - 版本固定的 Skill/Tool Registry，以及只读 SQL、受控 Web Research、Guardrail 与隔离 Sandbox。
  - 内存 Access Token、HttpOnly Refresh Token rotation/replay 撤销、入口 Rate Limit 与基于角色的 RBAC 权限控制（admin/user）。
  - Thread Message、Run Event 和派生摘要以 PostgreSQL 为事实来源；Redis 只承担 Event 低延迟通知和版本绑定的父分块缓存，不缓存整段 Thread 历史。
- **运行形态**：FastAPI 后端 + 现代工程化前端（Vite + Vue 3 + TypeScript + Pinia）+ Milvus 向量库。

## 关键架构能力

- **混合检索与精排**：稠密向量 + Milvus 原生 BM25 稀疏向量，经 RRF 融合、Auto-merging 和 Jina Rerank 后生成 Evidence。
- **严格的检索降级语义**：只有 Milvus Adapter 明确报告稀疏/Hybrid 能力不兼容时，受影响的检索 target 才降级为 Dense；连接、超时、服务不可用或畸形响应会作为 typed Provider failure 结束，不会被伪装成降级成功。
- **低延迟复杂度规划与并行 Sub-Agent 流程**：明显的单事实问题由本地规则直接进入检索；其余问题由 FAST_MODEL 一次完成复杂度判断和 2-4 个子问题规划。复杂问题通过 LangGraph `Send` 并行执行各子问题的“检索 → 证据评判”，最终在 Synthesis 节点去重合成。
- **纠错型 RAG（Corrective RAG）与单选查询重写**：检索后由独立的 GRADE_MODEL 结构化判断证据相关性、可回答性与歧义。证据不足时，FAST_MODEL 在一次结构化调用中选择 Step-back 或 HyDE，只执行选中的一次重写检索和一次复评。
- **可恢复 Run/Event 流**：Event 先写入 PostgreSQL Journal，再通过 SSE 投影；前端按 sequence 去重，使用 `Last-Event-ID` 重连和 `/events` 补放，`message.completed` 与 terminal Event 保持权威。
- **同一 Run 的 HITL 与取消**：Checkpoint、Run、Thread 和 assistant Message 身份在暂停/恢复期间保持不变；取消请求不会用关闭 SSE 冒充后端终止。
- **Document Version 两阶段发布**：Index Job 在隔离 candidate scope 构建并核验 manifest，随后用 PostgreSQL CAS 原子发布；构建失败不影响当前已发布版本。
- **Agent 循环与预算保护**：固定中间件链限制模型调用、Tool 调用、递归、deadline 和重复 Tool fingerprint；相同调用超限或 A/B 交替循环返回 `TOOL_LOOP_BLOCKED`。
- **RAG 评测契约**：已实现离线纯评分 Interface、Live/Prediction Adapter、版本化 schema、baseline 比较和 CI 门禁。仓库内 20 条受控数据只标记为 `contract_smoke`，用于证明契约与基线可复现，不代表生产质量。
- **浏览器认证生命周期**：Access Token 仅驻留内存；opaque Refresh Token 仅由 HttpOnly `Path=/auth` Cookie 承载并逐次轮换；仍在自然有效期内的 revoked token replay 会撤销同一用户全部活跃 refresh credential。
- **入口保护与浏览器响应头**：Rate Limit Module 用 HMAC identity 隐藏原始凭据，生产 Redis 多实例共享计数且故障 fail-closed；登录/注册在 PBKDF2 前执行 IP 与 IP+username 二级限流。CSP 只保护正式前端 HTML，其他安全响应头全局应用。
- **只读 SQL Assistant**：默认关闭；启用后只向 `admin` 披露 allowlist 内 PostgreSQL catalog 与有界只读查询，并执行 AST、权限、RLS、成本、超时、结果脱敏和审计门禁。
- **可审计 Tool 执行**：Skill/Tool Registry 决定可见能力，Guardrail 对每次调用给出确定性决策，Sandbox 只隔离已经获准的代码执行。

## 后续迭代

### RAG

1. 先按文档结构粗拆分，再用递归字符分块兜底和语义分块细化；补齐代码块、表格、图片等专用解析。
2. 为 BM25 `k1`/`b`、RRF 权重和 Rerank 候选比例建立可复现的 profile 对比。
3. 把人工标注集扩展到至少 200 条，在固定 Document Version/Index Manifest 上运行 Live Adapter，并逐步启用 groundedness、引用和冲突披露指标。
4. 评估多文档一次拼接与串行 Refine，并在来源冲突时显式披露。
5. 增加多模态 embedding 与 Evidence Interface。

### 平台能力

1. 把当前 RAG 子问题并行扩展为更通用的多步骤 Run 规划与专业 Agent 协作。
2. 扩展可信路由，使更多专业 Skill 可在进入 graph 前稳定激活。
3. 继续优化派生记忆管理，并保持原始 Message 为事实来源。
4. 支持修改 Thread 标题。

### 已完成的基础治理

- Access Token、可撤销 Refresh Token 与数据库角色共同保护用户隔离；`admin` 管理 Document，`user` 只能读取和删除自己的 Thread。
- PostgreSQL 持久化 Thread/Message、Run/Event/Checkpoint 和 Document/Document Version/Index Job；Message 采用 append-only sequence，Thread version 只随 Message append 递增，assistant 终态落定不会制造下一轮伪冲突。
- Redis 不保存 Thread Message 或 Thread 列表快照；它只提供 Event 通知和版本绑定的父分块缓存，事实读取始终回到 PostgreSQL。
- 新注册用户使用 PBKDF2-SHA256；登录成功时会把兼容的历史 bcrypt/bcrypt-sha256 hash 升级为当前 PBKDF2 格式。

## 目录与架构

- 后端：`backend/`（分层包结构，统一 `from backend.xxx import`）
  - [app.py](backend/app.py)：FastAPI 入口、CORS、静态资源挂载。
  - `api/`：HTTP 层
    - [router.py](backend/api/router.py)：路由聚合。
    - `routes/`：`auth`、`threads`、`runs`、`documents` 分文件；`sessions` 仅保留 deprecated 兼容 Adapter，`chat` 仅保留返回 `410 Gone` 的退役入口。
    - [resources.py](backend/api/resources.py)：Milvus / 上传目录等共享资源。
  - `threads/`：权威 Thread 生命周期、ID 约束、最近 Message 分页、Run 状态投影与兼容 Adapter 共享的 application Module。
  - `runs/`：持久化 Run 创建、幂等与 Thread 并发、owner lease/fencing、取消、HITL resume 和执行调度。
  - `events/`：Event v1 contract、PostgreSQL Journal/outbox、Redis 通知与可恢复 SSE Adapter。
  - `auth/`：Access/Refresh Token 签发、opaque token hash、rotation、replay detection 与撤销生命周期。
  - `rate_limits/`：入口 policy、identity HMAC、fixed-window limiter，以及 memory/Redis Adapter。
  - `security/`：正式前端 CSP 与全局浏览器安全响应头。
  - `agent/`：`AgentRuntimeFactory`、固定中间件链、Run-local Runtime Context，以及调用预算和 Tool 循环检测。
  - `chat/`：Thread Message 的持久化与历史投影；不再作为公开执行 Interface，`service.py` 仅供内部兼容测试。
    - [request_context.py](backend/chat/request_context.py)：Run-local RAG step、RAG trace 与工具预算上下文。
    - [storage.py](backend/chat/storage.py)：append-only Message 与 Thread 历史读取。
  - `guardrails/`：Tool 调用前的确定性 policy、Run-bound approval 与 destination capability。
  - `sandbox/`：隔离执行契约、进程级 Runtime，以及 disabled/Docker Adapter。
  - `rag/`：检索增强
    - [pipeline.py](backend/rag/pipeline.py)：LangGraph RAG 工作流。
    - [utils.py](backend/rag/utils.py)：混合检索、Rerank、Auto-merging。
  - `evaluation/`：RAG Dataset/Observation/Gate 契约、纯评分 Interface 与离线/Live Adapter。
  - `sql_assistant/`：只读 PostgreSQL Runtime、catalog allowlist、查询策略与有界结果编码。
  - `indexing/`：文档入库与向量
    - [embedding.py](backend/indexing/embedding.py)：稠密 + BM25 稀疏向量。
    - [document_loader.py](backend/indexing/document_loader.py)：PDF/Word/Excel 分块。
    - [milvus_client.py](backend/indexing/milvus_client.py)、[milvus_writer.py](backend/indexing/milvus_writer.py)。
    - [parent_chunk_store.py](backend/indexing/parent_chunk_store.py)：父级分块 DocStore。
  - `tools/`：Tool/Skill Registry 接入 Adapter（知识库、天气、SQL、Web、Sandbox）。
  - `infra/`：[database.py](backend/infra/database.py)、[cache.py](backend/infra/cache.py)、[auth.py](backend/infra/auth.py)。
  - `db/`：[models.py](backend/db/models.py)：ORM 模型。
  - `schemas/`：Pydantic 请求/响应（auth / threads / runs / documents）。
  - `documents/`：Document Catalog、Document Version 两阶段发布，以及持久 Index Job/cleanup 协调。
  - `workers/`：[indexing.py](backend/workers/indexing.py)：独立 Index Job worker 进程入口。
- 前端：`frontend/`
  - 采用现代工程化设计（Vite + Vue 3 + TypeScript + Pinia + Axios + Sass）。
  - **前端工程架构与状态流**：
    - **Pinia 状态存储**：
      - `auth/session.ts` 与 `stores/auth.ts`：只在内存中维护 Access Token；页面启动使用标签页内 single-flight 与 Web Locks 跨标签 refresh，重检 generation/tombstone/主体后才允许 401 单次重试，退出时撤销 HttpOnly Cookie 对应的 Refresh Token。
      - `stores/sessions.ts`：通过 `/v1/threads` 负责 Thread 创建、列表、权威删除与切换；文件名只保留旧 UI store 术语。
      - `stores/runs.ts`：管理 durable Run、Event cursor、重放、HITL resume 与真实取消。
      - `stores/chat.ts`：把 Run Event 投影到对应 Thread 的 assistant Message 与 RAG 步骤。
      - `stores/documents.ts`：展示知识库 Document，并轮询构建与清理 Index Job 的持久进度。
    - **精细化组件设计**：
      - `ThinkingTrace.vue` & `RetrievalTraceDetails.vue`：动态渲染子/主 Agent 的操作性状态（Searching, Grading, Rewriting 等步骤），支持展示每路子问题的合并与召回详情，不展示模型私有推理。
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

1. 客户端先调用 `POST /v1/threads` 获得服务端生成的 `thread_<uuid>`，或复用已有 Thread；只有已存在且属于当前用户的 Thread 才能调用 `POST /v1/threads/{thread_id}/runs`。Run 请求携带 `idempotency_key`、期望 Thread version 与断连策略，响应返回 durable `run_id`。
2. `RunAgentExecutor` 使用 owner lease 与 fencing token 领取 Run，通过 `AgentRuntimeFactory` 构建固定中间件链。
3. Agent 根据问题类型决定是否调用工具：
  - 天气问题 → `get_current_weather`
  - 知识问答 → `search_knowledge_base`
4. 若命中知识库工具，进入 `backend/rag/pipeline.py`；tool progress、message delta、HITL 与 terminal 都先追加到持久 Event Journal。
5. 客户端通过 `GET /v1/runs/{run_id}/stream` 订阅 SSE；断线后携带 `Last-Event-ID` 重连，或先调用 `/events?after={sequence}` 补放缺失 Event。
6. `message.completed` 是最终正文与 `rag_trace` 的权威来源；assistant Message 与 Run 终态提交后才发布 `run.completed`、`run.failed` 或 `run.cancelled`。
7. `hitl.required` 把 Run 置为 `waiting_input`，客户端调用 `/resume` 恢复同一 Checkpoint；Stop 调用 `/cancel` 并继续监听权威 terminal Event。
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
  - 只有明确的 Hybrid capability error 才对对应 target 使用 Dense fallback；其他 Provider failure 显式失败。
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

### 3) Document Version 入库链路

1. 前端上传到 `POST /documents/upload/async`；API 保存 source object，并在 PostgreSQL 预留 Document Version 与 Index Job。
2. 独立 worker 使用 lease、heartbeat、`SKIP LOCKED`、build fingerprint capability 与 execution fence 领取 Index Job；API 重启不会遗失工作，旧 profile worker 也不会误构建新 profile 候选。
3. `document_loader.py` 生成带稳定 Document Version 身份的三级分块；L1/L2 写入 ParentChunk staging，L3 写入隔离的 Milvus candidate scope。
4. worker 对 ParentChunk、Milvus 与 exact manifest 做完整身份核验，再用 PostgreSQL CAS 原子切换 `current_version_id`。
5. 同名旧版本在发布完成前始终可检索；新版本失败不会影响旧版本。
6. superseded/failed/delete Document Version 进入持久 cleanup queue，由同一 worker 以独立 lease 和数据库时钟退避策略执行 exact-version 物理清理；删除的 scope revoke、legacy tombstone 和 cleanup snapshot 在一个 PostgreSQL 事务内提交。
7. worker crash 后 RUNNING Index Job 可幂等重建；STAGED Index Job 只恢复 publish，不重复解析和向量化。

### 4) Milvus 2.5+ 原生 BM25 处理

- **机制**：项目利用了 Milvus 2.5+ 新版内置的全文检索机制。创建集合时，定义一个 `FunctionType.BM25` 类型的函数，输入字段为 `text` 字段，输出字段为 `sparse_embedding`。
- **自动对齐**：当新文本 chunk 插入或删除时，Milvus 在服务端自动进行分词、统计、稀疏特征向量计算。这实现了高效率、零客户端统计负担的密集 + 稀疏混合双塔检索。

### 5) Thread 记忆链路

1. 用户 Message 与 assistant placeholder 在创建 Run 时 append-only 写入 PostgreSQL，并绑定 `thread_id`、`run_id` 与单调 sequence。
2. 流式 delta 只作为 Event 投影；完成、失败或取消时只落定对应 assistant Message，不改写其他历史 Message。
3. Runtime 在 token budget 内加载派生摘要；原始 Message 始终保留为事实来源。
4. 前端通过 `GET /v1/threads/{thread_id}/messages` 默认读取最近一页，并用 `before` cursor 按需加载更早 Message；页面始终按 sequence 升序呈现，不再串行拉完整历史。
5. Thread 列表分别投影 `thread_status`、`active_run_id` 与 `active_run_status`；Run 的 `creating/running/waiting_input` 不会覆盖 Thread 自身状态。
6. Thread version 表示 append version：创建一轮 Run 时随用户与 assistant 两条 Message 增加，assistant 正文或终态更新不再递增。

## 技术栈

- 后端：FastAPI、LangChain Agents、Pydantic、Uvicorn、SQLAlchemy、PostgreSQL、Redis。
- 向量与检索：Milvus（HNSW 稠密索引 + SPARSE_INVERTED_INDEX 稀疏索引）、RRF 融合、Jina Rerank 精排。
- 嵌入与稀疏：`langchain_huggingface` 本地稠密向量（默认 `BAAI/bge-m3`）；Milvus 2.5+ 原生 Chinese 分析器与原生 BM25 特征提取。
- 前端：Vite + Vue 3 (SFC) + TypeScript + Pinia + Axios + Marked + Highlight.js + FontAwesome，工程化编译与静态文件托管。
- 工具链：dotenv 配置、requests、langchain_text_splitters、langchain_community.loaders。

## 环境变量

需在仓库根目录或运行环境配置：

- 模型相关：`ARK_API_KEY`、`MODEL`、`FAST_MODEL`、`GRADE_MODEL`、`BASE_URL`。`FAST_MODEL` 负责复杂度规划及 Step-back / HyDE 单选重写；`GRADE_MODEL` 专门负责证据评判。两者都是显式必需配置，不会相互替代或回退到 `MODEL`。
- 稠密向量：`EMBEDDING_MODEL`、不可变 commit `EMBEDDING_MODEL_REVISION`、`EMBEDDING_DEVICE`、`DENSE_EMBEDDING_DIM`（需与 Milvus 集合 `dense_embedding` 维度一致）
- 密集与稀疏：Dense 由本地 embedding 生成；Sparse 由 Milvus 中文 analyzer 与 BM25 Function 自动生成和维护
- Rerank 相关：`RERANK_MODEL`、`RERANK_BINDING_HOST`、`RERANK_API_KEY`
- Milvus：`MILVUS_HOST`、`MILVUS_PORT`、`MILVUS_COLLECTION`
- 文档 build capability：`DEFAULT_TENANT_ID`、`DEFAULT_KNOWLEDGE_BASE_NAME`、`DOCUMENT_PARSER_VERSION`、`DOCUMENT_CHUNKER_VERSION`、`DOCUMENT_INDEX_VERSION`、`DOCUMENT_INDEX_CLEANUP_GRACE_SECONDS`。其中 cleanup grace 只保护版本替换后的旧版本；用户主动删除会在 scope 撤销后立即进入物理清理。API 与 indexing worker 必须一致；readiness 会按 build fingerprint 拒绝仅有旧 profile worker 的部署。
- 持久化与通知：`DATABASE_URL`、`REDIS_URL`。PostgreSQL 保存事实，Redis 只用于低延迟 Event transport 和父分块缓存；DSN 中的用户名和密码包含 `@:/#%` 等保留字符时必须先 percent-encode，不能直接拼接原始 Secret。
- 鉴权相关：`JWT_SECRET_KEY`、`ADMIN_INVITE_CODE`、`JWT_ALGORITHM`、`JWT_EXPIRE_MINUTES`、
  `JWT_REFRESH_EXPIRE_DAYS`、`AUTH_REFRESH_LEDGER_RETENTION_DAYS`、
  `AUTH_REFRESH_COOKIE_NAME`、`AUTH_REFRESH_COOKIE_SECURE` 与 `AUTH_REFRESH_COOKIE_SAMESITE`。
  生产必须启用 Secure；`SameSite=None` 必须同时启用 Secure。`ADMIN_INVITE_CODE` 留空即禁用
  公开 admin 注册；启用时不得与 JWT/Rate Limit Secret 相同。
- 入口限流：`RATE_LIMIT_ENABLED`、`RATE_LIMIT_BACKEND`、`RATE_LIMIT_HMAC_KEY` 与
  `RATE_LIMIT_KEY_PREFIX`。开发/测试可用 `memory`；生产强制 `redis`，HMAC key 至少 32 字符且
  不得与 `JWT_SECRET_KEY` 相同。
- 密码参数：`PASSWORD_PBKDF2_ROUNDS`
- 隔离 Sandbox：`SANDBOX_ENABLED`、`SANDBOX_ADAPTER`、`SANDBOX_DOCKER_IMAGE`、
  `SANDBOX_DOCKER_HOST`、`SANDBOX_REQUIRE_ROOTLESS` 与各项 `SANDBOX_MAX_*` 预算；默认关闭，
  启用后参与 readiness。
- 检索候选池：`RETRIEVAL_CANDIDATE_K`（固定候选数，优先）、`RETRIEVAL_CANDIDATE_MULTIPLIER`（未设 K 时 `max(top_k × 倍数, top_k)`，默认 `3`）
- Auto-merging：`AUTO_MERGE_ENABLED`、`AUTO_MERGE_THRESHOLD`、`LEAF_RETRIEVE_LEVEL`
- 工具：`AMAP_WEATHER_API`、`AMAP_API_KEY`

## API 速览

- 鉴权
  - `POST /auth/register`：注册并返回只供内存使用的 Access Token，同时设置 HttpOnly Refresh Cookie。
  - `POST /auth/login`：登录并返回只供内存使用的 Access Token，同时设置 HttpOnly Refresh Cookie。
  - `POST /auth/refresh`：轮换 Refresh Token，并签发新的内存 Access Token。
  - `POST /auth/logout`：撤销当前 Refresh Token 并清除 Cookie。
  - `POST /auth/logout-all`：使用 Access Token 鉴权，撤销当前用户全部活跃 Refresh Token。
  - `GET /auth/me`：获取当前登录用户信息。
- Thread / Run / Event
  - `POST /v1/threads`：由服务端创建 `thread_<uuid>`；可选提交标题。
  - `GET /v1/threads`：列出当前用户的 Thread，并聚合当前非终态 Run 投影。
  - `GET /v1/threads/{thread_id}/messages?before={sequence}`：读取最近一页或按 cursor 加载更早的 canonical Message。
  - `DELETE /v1/threads/{thread_id}`：权威删除当前用户的 Thread；任何未知或已知非终态 Run 都会 fail-closed 返回冲突。
  - `POST /v1/threads/{thread_id}/runs`：幂等创建 durable Run，返回 `run_id` 与 `thread_version`。
  - `GET /v1/runs/{run_id}`：读取 Run 当前状态。
  - `GET /v1/runs/{run_id}/events?after={sequence}`：分页重放持久 Event。
  - `GET /v1/runs/{run_id}/stream`：订阅 Event v1 SSE；重连时发送 `Last-Event-ID: <sequence>`。
  - `POST /v1/runs/{run_id}/resume`：携带一次性 `hitl_token` 与幂等键恢复同一 Checkpoint。
  - `POST /v1/runs/{run_id}/cancel`：请求取消真实后端 Run；客户端应等待 `run.cancelled` 或其他权威 terminal Event。
  - `POST /chat`、`POST /chat/stream`：已退役，认证后返回 JSON `410 ENDPOINT_RETIRED`，不会执行 legacy Chat Implementation。
- Deprecated Thread 兼容路径
  - `GET /sessions`、`GET /sessions/{session_id}`、`DELETE /sessions/{session_id}`：仅供旧客户端迁移，OpenAPI 已标记 deprecated；正式前端不再调用。
- 文档（管理员权限）
  - `GET /documents`：列出已入库文档及 chunk 数。
  - `POST /documents/upload/async`：保存上传并提交持久化 Index Job。
  - `GET /documents/upload/jobs`：列出最近的 Index Job，供刷新后恢复。
  - `GET /documents/upload/jobs/{job_id}`：查询 Index Job 状态、attempt 与退避时间。
  - `DELETE /documents/delete/async/{filename}`：立即撤销检索 scope，并提交持久化清理 Index Job。
  - `GET /documents/delete/jobs`：列出最近的清理 Index Job，供刷新后恢复。
  - `GET /documents/delete/jobs/{job_id}`：查询清理 Index Job 的物理清理进度或 dead-letter。
  - 兼容路径 `POST /documents/upload` 与 `DELETE /documents/{filename}` 已改为 `202 Accepted` 并返回 durable `job_id`：前者只提交 Index Job，后者只原子撤销检索 scope；二者都不再表示物理索引/清理已同步完成。旧客户端升级前必须改为轮询对应的 Index Job 接口。

## 持久化 Run/Event 与可恢复流 — 技术细节

下列 `Authorization: Bearer <token>` 表示受保护 HTTP Interface 的标准 Access Token 传输方式。
正式浏览器从内存认证状态读取 token；示例不授权把它写入 `localStorage`、Cookie 或其他持久
客户端存储。

### 1. 创建 durable Run

客户端先创建 Thread，再用独立请求创建 Run；Run Interface 不会为任意路径 ID 隐式创建 Thread。Thread ID 统一满足 `^[A-Za-z0-9][A-Za-z0-9_.:-]{0,119}$`，正式客户端使用服务端生成值。不要用一个长连接同时承担“创建工作”和“观察工作”：

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

响应中的 `run.id`、assistant message identity 与 `thread_version` 是后续重放、取消和 HITL 恢复的稳定身份。相同用户、Thread 与幂等键只创建一个 Run；同一 Thread 的并发写入由 version、数据库约束、owner lease 与 fencing token 共同保护。`thread_version` 是两条新 Message append 后的版本，后续 terminal finalize 不会再改变它，因此客户端可直接用于下一轮 optimistic write。

### 2. Event v1 Interface

`RunAgentExecutor` 驱动 `AgentRuntimeFactory`，并把规划、Tool、检索、Message、HITL、usage 与 terminal 状态追加到 Event Journal。SSE 只是 Journal 的可恢复投影：

```text
id: 42
event: message.delta
data: {"schema_version":1,"event_id":"evt_xxx","sequence":42,"run_id":"run_xxx","thread_id":"thread_xxx","type":"message.delta","timestamp":"...","data":{}}
```

关键不变量：

- `sequence` 在单个 Run 内严格单调，客户端 reducer 按 `(run_id, sequence)` 去重并检测 gap。
- `message.delta` 只负责临时展示；`message.completed` 是最终正文与 `rag_trace` 的权威来源。
- `run.completed`、`run.failed`、`run.cancelled` 是 terminal Event，不再使用非结构化 `[DONE]`。
- assistant Message 与 Run 终态必须先在持久化事务中落定，再发布 terminal Event。
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

服务端先从 PostgreSQL Journal 重放缺失 Event，再通过 Redis transport 等待新 Event；Redis 只负责低延迟通知，不是事实来源。空闲期间发送 SSE heartbeat。客户端遇到 sequence gap 时不得推进 cursor，应先补放缺失 Event；terminal 后关闭该 Run 的本地订阅。

### 4. RAG 进度与前端投影

知识库 Tool 仍由 `backend/rag/pipeline.py` 执行 Dense + BM25 + RRF、Auto-merging、Rerank、证据评判与一次重写。不同阶段通过 `tool.started`、`tool.progress`、`retrieval.*` 与 `message.*` Event 表达，前端 `runs` store 维护 Run 状态，`chat` store 只把同一 `thread_id/run_id` 的 Event 投影到对应 assistant Message，避免不同 Thread 串写。

### 5. HITL 与取消

- 收到 `hitl.required` 后，Run 已在同一事务中保存 Checkpoint 并进入 `waiting_input`。客户端持久保存 `run_id`、`hitl_token` 与 `checkpoint_id`，调用 `POST /v1/runs/{run_id}/resume` 恢复同一图节点；刷新页面不需要重新执行原问题。
- Stop 调用 `POST /v1/runs/{run_id}/cancel`。响应表示“取消请求已接受或 Run 已终止”，客户端继续监听直到收到权威 terminal Event。
- 关闭页面、切换账号或断开 SSE 默认只关闭观察连接；`on_disconnect=continue` 的 Run 会在后端继续。网络断开不能冒充用户取消。

### 6. 公开 legacy Chat 入口退役

`POST /chat` 与 `POST /chat/stream` 不再是 compatibility execution Adapter。二者认证后对任意 legacy body 返回 JSON `410 ENDPOINT_RETIRED`，并给出 create/stream/resume/cancel 迁移路径；它们不会创建模型调用、ToolAudit、Message 或后台 Run。所有公开 Tool 执行因此只跨越 durable Run 的 Guardrail 与 Sandbox Seam。

### 7. 混合检索（Hybrid Search）深度实现

项目使用 Milvus 2.5+ 服务端原生分析器维护 BM25 稀疏特征，客户端只负责构造 Dense/Sparse 检索请求与解释结果：

- **Dense Pathway**：使用 `langchain_huggingface.HuggingFaceEmbeddings`（默认 `BAAI/bge-m3`）生成稠密向量，维度由 `DENSE_EMBEDDING_DIM` 与集合 schema 对齐（默认 1024），向量可做 L2 归一化后与 Milvus `IP` 度量配合。
- **Sparse Pathway**：
    - 文档写入时，仅需将原始文本写入启用 `chinese` 分析器分词的 `text` 字段。
    - Milvus 服务端运行绑定的 `FunctionType.BM25` 计算函数，生成稀疏嵌入并维护 `sparse_embedding` 索引统计。
- **Milvus 融合**：
    - 使用 Milvus 的 `AnnSearchRequest` 同时发起稠密和稀疏的两个多路检索请求。
    - **RRFRanker (Reciprocal Rank Fusion)**: 采用 `k=60` 的倒数排名融合算法，将两路召回结果无参数化地合并，避免了加权求和中调节 `alpha` 参数的困难。
- **失败语义**：仅 `HybridRetrievalUnsupported` 一类能力不兼容错误允许对应 target 复用同一 query embedding 走 Dense fallback，并在 RAG Trace 记录 `HYBRID_RETRIEVAL_DEGRADED`；连接、超时、服务不可用、参数或响应结构错误不会触发该 fallback。

## 更新日志

### 2026-06-12 全面迁移至 Milvus 2.5+ 原生 BM25 与可靠删除

- **服务端原生 BM25**：使用 Milvus 2.5+ 内置中文分词器与 BM25 Pipeline Function，稀疏特征和统计由向量库自动维护。
- **Schema 自动升级**：优化 `ensure_collection` 逻辑，支持自动检测旧版 Schema 并进行 drop 与无缝重建升级。
- **删除协调**：当时收敛了 Milvus、PostgreSQL 与父分块缓存的删除一致性；当前实现已进一步演进为 scope revoke + 持久 cleanup job，不再把物理清理描述成同步完成。
- **企业级文本净化**：升级文本清洗逻辑，通过 Unicode NFC 标准规范化和 PUA/C0/C1 等非打印/零宽/孤立代理项的彻底过滤，解决 PostgreSQL 与 Milvus 的字符集兼容性报错。

### 2026-06-12 前端单文件 CDN 重构为 Vite + Vue 3 + TS 工程化组件架构
- **现代化架构重构**：将以前臃肿的多合一 HTML/CDN 页面重构为标准的 **Vite + Vue 3 (SFC) + TypeScript + Pinia + Axios + Sass** 现代化工程项目，全部组件和状态高度解耦。
- **状态及路由管理**：利用 Pinia 建立 `auth`、`sessions`、`chat`、`documents` 等 Store；当前另由 `runs` Store 管理 Run/Event 生命周期。
- **高阶交互界面**：增加流式上传进度详情卡片、上传成功后卡片自动折叠、References 参考文献精美折叠展示、Thinking 气泡流畅过度等。

### 2026-06-03 自适应复杂问题分解、并行 Sub-Agent 与精排门控
- **低延迟复杂度规划**：明显的单事实问题由本地规则直接进入检索；其余问题由 FAST_MODEL 一次完成复杂度判断，并在 complex 时同时给出 2-4 个子问题。
- **并行子 Agent 检索**：利用 LangGraph 的 `Send` API 并行调用 `rag_sub_agent`，每个子问题只执行 retrieve 与 grade，避免不可达的嵌套图和二次改写。
- **子步骤完美分组**：前端界面重新适配并行子流程，在 RAG Step 的 SSE 数据中为子问题建立独立分组标签展示，避免交错重复建组与视觉混淆。
- **精排与明确路由**：`RERANK_MIN_SCORE` 过滤噪音；空检索直接结束，有相关信号但证据不足时才执行唯一一次 Step-back / HyDE 单选重写。

### 2026-06-02 通用 RAG 能力强化与后端生命周期重构
- **通用 RAG 功能增强**：提供 Thread 派生摘要（Context Manager Notes）、首问本地截断标题，以及多源参考文献的可视化折叠展示卡片。
- **gRPC 连接生命周期优化**：Milvus 客户端访问由全局连接池改为短生命周期连接（`session()` contextmanager），降低长期挂起后 gRPC channel 失效的风险。
- **后端分层重组与包依赖解耦**：彻底重构 backend 代码目录包结构，剔除 re-export 导出机制，解决因交叉导入产生的循环依赖，并统一环境加载规范。

### 2026-06-01 召回-合并-精排（Rerank）流水线重构
- **模块化 Pipeline**：重构 RAG 底层实现，将 RAG 流程收拢为高可控的“召回 -> 自动合并 -> 语义重排”流水线，收口统一的参数配置与多级 RAG Trace 追踪。
- **去重合并高分保留**：修复了在执行 L3 -> L2/L1 叶子向上合并时，在循环内聚合 Rank 分数的算法，防止去重过程中丢失高置信度召回分。

### 2026-03-21 后端服务建设升级（认证 + 数据库 + 缓存）
- 新增认证与权限模块：注册、登录、JWT、管理员权限控制。
- Thread 历史从本地 JSON 迁移到 PostgreSQL，按用户隔离 Message。
- 父级分块存储从本地 JSON 迁移到 PostgreSQL。
- Redis 当前只缓存版本绑定的父分块并承担 Event 通知；Thread 列表与 Message 不使用整段 Redis 快照。
- API 升级为 Token 驱动，移除前端直接传 `user_id` 的历史模式。
- 文档管理接口收敛到管理员角色，避免普通用户误操作知识库。
- 密码哈希方案升级为 PBKDF2-SHA256，兼容历史 bcrypt 校验。

### 2026-03-13 三级分块与 Auto-merging 升级
- 新增三级滑动窗口分块（L1/L2/L3），并为分块写入层级元数据。
- 存储策略调整为 Leaf-only：仅 L3 叶子块写入 Milvus，L1/L2 写入本地 DocStore。
- Auto-merging 改为从 DocStore 拉取父块，减少向量冗余存储。
- 思考链路新增三级检索与自动合并步骤事件。
- `rag_trace` 新增 `leaf_retrieve_level` 与 `auto_merge_*` 字段，且 Thread 历史读取同样保留这些字段。

### 2026-02-19 RAG 实时思考链路修复
- **问题**：Agent 在执行同步工具（如 `search_knowledge_base`）时，由于运行在线程池中，无法正确获取主线程的 asyncio 事件循环，导致 `emit_rag_step` 事件丢失，前端"思考中"气泡一直静止。
- **修复**：
  1. **Backend (`service.py`)**：为每个请求创建 `ChatRequestContext`，在其中捕获主线程 `loop` 与本请求 `output_queue`。
  2. **Backend (`backend/tools/knowledge.py` + `backend/rag/pipeline.py`)**：使用 per-request tool factory 与显式 `ctx` 参数跨线程调度 RAG step，避免请求间串号。
  3. **Frontend (`stores/chat.ts`)**：在发送消息时初始化空的 `ragSteps: []` 数组，确保 Vue 响应式系统能立即追踪后续的 push 操作。
- **效果**：用户提问后，思考气泡内实时跳动显示检索步骤（如"🔍 正在检索知识库..." -> "📊 正在评估文档相关性..."），不再只有静态的"正在思考中..."。
