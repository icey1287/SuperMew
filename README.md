# SuperMew

SuperMew 是一个知识库优先的 Agent 平台。它以持久化 Thread、可恢复 Run、版本化 Event 和
Document Version 为核心，提供 RAG、HITL、RAG 效果评测以及可审计的 Skill / Tool 执行。

## 核心能力

- 持久化 Thread、Run、Event 与 Checkpoint，支持断线重放、取消和 HITL 恢复。
- 文档异步索引与原子版本发布，使用 Milvus Dense + BM25、RRF、Auto-merging 和可选 Rerank。
- 持久化模型控制面，以及 Dataset / Job / Case 形式的 RAG 自动评测工作台。
- Knowledge Base、Web Research、只读 SQL Assistant 和隔离 Sandbox 等 Skill。
- 内存 Access Token、HttpOnly Refresh Token、RBAC、Redis Rate Limit 和安全响应头。
- Vue 3 + TypeScript 前端，由 FastAPI 在生产模式下托管构建后的静态资源。

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

## 正式 HTTP Interface

当前只保留 canonical Thread / Run / Event Interface，不再提供旧 `/chat`、`/chat/stream` 和
`/sessions` 兼容入口。

- `POST /v1/threads`：创建 Thread。
- `GET /v1/threads`：列出 Thread 与活跃 Run 投影。
- `GET /v1/threads/{thread_id}/messages`：分页读取 Message。
- `POST /v1/threads/{thread_id}/runs`：幂等创建 durable Run。
- `GET /v1/runs/{run_id}/events`：重放持久 Event。
- `GET /v1/runs/{run_id}/stream`：订阅 SSE；支持 `Last-Event-ID`。
- `POST /v1/runs/{run_id}/resume`：恢复同一 HITL Checkpoint。
- `POST /v1/runs/{run_id}/cancel`：请求取消后端 Run。

其他管理接口可直接查看 `/docs`，包括文档、模型、能力控制面和 RAG 评估。

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

## 测试与质量门禁

```bash
uv run --frozen pytest -q
uv run --frozen ruff check .
uv run --frozen mypy

cd frontend
npm run format:check
npm run lint
npm run typecheck
npm run test:unit
npm run build:check
```

需要浏览器回归时运行 `npm run test:e2e`。完整门禁与 CI 对应关系见
[仓库质量门禁](docs/runbooks/repository-quality-gates.md) 和
[前端质量门禁](docs/runbooks/frontend-quality-gates.md)。

## 文档索引

- [领域语言](CONTEXT.md)
- [生产部署](docs/runbooks/deployment.md)
- [持久化索引 worker](docs/runbooks/persistent-indexing-worker.md)
- [Skill / Tool Registry](docs/runbooks/skill-tool-registry.md)
- [Web Research](docs/runbooks/web-research.md)
- [SQL Assistant](docs/runbooks/sql-assistant.md)
- [Guardrail 与 Sandbox](docs/runbooks/guardrails-and-sandbox.md)
- [认证与 Refresh Ledger](docs/runbooks/auth-token-lifecycle.md)
- [架构决策记录](docs/adr/)
