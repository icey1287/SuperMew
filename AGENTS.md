# AGENTS.md

本文件是 SuperMew 仓库的 **Agent 总导航与仓库级约束**，适用于 Codex、Claude Code 等编码 Agent。它不是 README 的替代品；产品说明留在 `README.md`，领域语言留在 `CONTEXT.md`，架构决策留在 `docs/adr/`，运维步骤留在 `docs/runbooks/`。

同级 `CLAUDE.md` 仅通过 `@AGENTS.md` 导入本文件，不要在两个文件中维护重复规则。

## 指南分层

根文件只负责跨仓库规则。进入具体模块前，读取对应指南；从仓库根目录启动 Agent 时也要主动读取，而不要假设子目录文件已自动进入上下文。

- 后端：[`backend/AGENTS.md`](backend/AGENTS.md)
- RAG：[`backend/rag/AGENTS.md`](backend/rag/AGENTS.md)
- 认证与入口安全：[`backend/auth/AGENTS.md`](backend/auth/AGENTS.md)
- 前端：[`frontend/AGENTS.md`](frontend/AGENTS.md)
- Run Event 前端投影：[`frontend/src/events/AGENTS.md`](frontend/src/events/AGENTS.md)
- 跨端契约：[`contracts/AGENTS.md`](contracts/AGENTS.md)
- RAG 评测资产：[`evals/rag/AGENTS.md`](evals/rag/AGENTS.md)
- 文档：[`docs/AGENTS.md`](docs/AGENTS.md)
- 测试：[`tests/AGENTS.md`](tests/AGENTS.md)

## SuperMew 是什么

SuperMew 是知识库优先、面向真实运行与评测的 Agent 平台。它以持久化 **Thread**、可恢复 **Run**、版本化 **Event**、不可变 **Document Version**、冻结的 **Model Snapshot** 和可审计 **Tool** 执行为核心，让 RAG、HITL、Skill、Guardrail、Sandbox 与 RAG Evaluation 共享明确的生命周期和事实来源。

主要技术栈：

- 后端：Python 3.12、FastAPI、Pydantic、SQLAlchemy、PostgreSQL、Redis、Milvus、LangChain/LangGraph、`uv`。
- 前端：Vue 3、TypeScript、Pinia、Axios、Vite、Sass、Vitest、Playwright、`npm`。
- 生产形态：API、Indexing Worker、Evaluation Worker 分进程运行；PostgreSQL、Redis、Milvus 为共享基础设施；前端构建由 FastAPI 同源托管。

## 事实来源与阅读顺序

遇到冲突时不要凭记忆猜测，按以下顺序定位权威内容：

1. `CONTEXT.md`：正式领域术语、对象关系和禁止混用的名称。
2. `contracts/*.json`：跨后端/前端的版本化 wire contract 真源。
3. `docs/adr/`：已接受的架构决策与不变量。
4. `docs/runbooks/`：当前可执行的开发、发布、清理和故障处理流程。
5. `README.md`：面向用户与贡献者的当前能力、目录和使用说明。
6. 代码与测试：实际实现和可执行证据。

不要把上述内容整段复制进 AGENTS；这里应保存“到哪里找”和“每次都必须遵守”的规则。

## 仓库地图

```text
SuperMew/
├── backend/                 # API、Agent、RAG、持久运行、能力与安全实现
├── frontend/                # Vue 3 前端与浏览器状态投影
├── contracts/               # Run Event / Tool Result JSON Schema 真源
├── migrations/              # PostgreSQL 前向迁移（Alembic script_location）
├── evals/rag/               # 版本化 Dataset、Observation、Gate、baseline、schema
├── scripts/                 # 生成器、评测、benchmark、兼容性 smoke
├── tests/                   # 后端单元、契约、集成、迁移与故障注入测试
├── frontend/e2e/            # Playwright 浏览器测试
├── docs/adr/                # 架构决策记录
├── docs/runbooks/           # 运维和质量门禁
├── docker/                  # 应用镜像与容器辅助文件
├── docker-compose*.yml      # 本地/生产基础设施编排
├── pyproject.toml           # Python 依赖、Ruff、mypy、pytest、coverage
├── uv.lock                  # 锁定的 Python 依赖图
└── frontend/package*.json   # 前端脚本与锁定依赖图
```

## 开始工作前

1. 查看 `git status --short` 和现有 diff。不要覆盖、回滚或格式化掉用户尚未提交的修改。
2. 确认本次变更的权威 seam、对应测试、相关 ADR/Runbook，以及是否存在更近的 `AGENTS.md`。
3. 对架构、并发、安全、迁移、Contract 或 RAG 质量变更，先写出必须保持的不变量，再改代码。
4. 优先修改现有正式实现；不要为了“兼容”另建第二条 route、第二套状态、双读/双写或异常时静默切换的实现。
5. 除非用户明确要求，不创建提交、不推送分支、不改写 Git 历史。

## 常用命令

### 后端

```bash
uv sync --dev --locked
uv run --no-sync pytest -q tests/path/to/test.py
uv run --no-sync ruff format --check .
uv run --no-sync ruff check .
uv run --no-sync mypy
```

跨模块或发布前按 `docs/runbooks/repository-quality-gates.md` 运行完整门禁，包括生成契约、Registry、迁移、RAG baseline、benchmark、覆盖率和依赖审计。

### 前端

```bash
cd frontend
npm ci
npm run format:check
npm run lint
npm run typecheck
npm run test:unit
npm run build:check
npm run test:e2e
```

先运行最聚焦的测试，再扩大到受影响模块和 CI 同等门禁。不要把未运行的命令写成“已通过”；说明未运行原因和剩余风险。

## 仓库级不变量

### 单一正式 Interface

- Thread 只由 `backend.threads` 与 `/v1/threads` 生命周期拥有。
- Agent 执行只走持久化 Run/Event/Checkpoint 路径。
- SSE 是 Event Journal 的可恢复投影，不是事实来源。
- 正式替换完成后删除旧 route、schema、Adapter、双读/双写和隐藏 fallback；迁移历史不构成运行时兼容授权。

### 持久运行与并发隔离

- 一个 Run 绑定用户、Tenant、Thread、幂等键、一个用户 Message 和一个 assistant Message。
- HITL 必须恢复同一 Run、同一 Checkpoint 和同一 assistant Message；不能重新提问来模拟恢复。
- 终态只能由持久事务写入 `message.completed` 与权威 terminal Event；关闭 SSE 或浏览器不等于取消 Run。
- Run-local Queue、loop、trace、budget、Evidence、Model Snapshot 和 capability 必须由显式 request context 持有。禁止模块级“当前请求”“最后一次 trace”或共享可变 Queue。

### Provider 与降级

- 每次外部调用只有一个重试所有者；关闭 SDK/transport 的隐式重试，避免乘法重试和越过绝对 deadline。
- Provider 失败不能伪装成 `NO_KNOWLEDGE`。只有健康检索确实为空时才是无知识。
- Hybrid 仅在 Adapter 明确报告能力不兼容时降级为 Dense；Rerank 失败可保留召回排序，但必须记录稳定 code、attempts 和 fallback 标志。
- 模块导入不得启动 Provider loop、加载本地模型或创建跨 loop 的 AsyncClient。

### 身份、Secret 与权限

- Access Token 只驻留浏览器内存；Refresh Token 只通过 HttpOnly `Path=/auth` Cookie 传输。
- API Key、DSN 密码、Header Secret 和原始 credential 不进入数据库公开字段、Run、Checkpoint、Event、评测报告或前端状态。
- Tool 可见性由 Registry/Skill session 决定，执行前仍必须经过 Guardrail；Sandbox 只隔离已获准执行，不替代授权。
- Web fetch 只消费同一 Run 内已验证的 Evidence identity；不得重新开放任意 URL 抓取。

### 版本化事实

- Document 内容通过不可变 Document Version 构建并原子发布；失败 candidate 不污染 current version。
- Run 与 Evaluation Job 创建时冻结 Model Snapshot；控制面变更只影响之后创建的工作。
- Contract schema、生成的 Python/TypeScript 类型、RAG Dataset fingerprint 和 baseline 必须同步，禁止手工修补生成物来绕过检查。

## 依赖、生成物与迁移

- 新增生产依赖必须有明确收益和边界；使用 `uv`/`npm` 更新 manifest 与 lockfile，不手改锁文件。
- 带“generated / do not edit”标记的文件必须从真源重新生成。
- Schema 变更必须有 Alembic 迁移和迁移测试；不可逆转换应明确 fail-closed，不通过运行时双读掩盖旧 schema。
- 测试和离线门禁不能隐式联网、下载大型模型或依赖开发者机器上的真实 Secret。

## 文档更新策略

- 用户可见能力、安装或使用方式变化：更新 `README.md`。
- 领域术语变化：更新 `CONTEXT.md`，并同步相关 Contract、代码和测试。
- 架构决策或不变量变化：更新现有 ADR 的实施细节，或为实质性反转新增 superseding ADR。
- 部署、清理、门禁或故障处理变化：更新对应 Runbook。
- Agent 的目录路由、命令或反复出现的开发错误变化：更新最近的 `AGENTS.md`。

不要要求“每次代码改动都更新 AGENTS”；只有指导本身发生变化时才更新。

## 完成标准

提交结果前必须说明：

- 改动的正式 seam 和保持的不变量；
- 新增或更新的测试；
- 实际运行的命令与结果；
- 未运行的集成/在线检查及原因；
- Contract、迁移、评测资产和文档是否需要同步。

提交信息沿用仓库现有 Conventional Commit 风格，例如 `fix(runs): 修复取消竞态`。除非用户要求，不自动 commit 或 push。

## Code Review Rules

### 第二套事实来源

- 阻止新增平行 route、内存镜像状态、双写、自动探测旧实现或 catch-all fallback。
  安全路径：迁移调用方和数据后删除旧路径；允许的能力降级必须留在同一正式 Interface 内并显式记录。

### Run/Event 终态

- 阻止由 SSE producer、浏览器断连或前端本地状态提前宣布完成/取消。
  安全路径：等待持久 `message.completed` 和 terminal Run Event，并按 sequence 重放。

### 跨 Run 状态泄漏

- 阻止模块级 Queue、全局 trace、可重绑 snapshot、共享 capability 或未清理 request context。
  安全路径：使用显式 Run-owned context，并增加两个并发 Run 的隔离回归测试。

### 静默故障与乘法重试

- 阻止把 Provider/存储/网络故障解释为空结果，或在 SDK、transport、caller 多层重复重试。
  安全路径：传播 typed error；仅在已声明的阶段应用有界降级，并把结果写入公开错误或 trace。

### 权限与网络边界扩大

- 阻止任意 URL、私网访问、任意命令、Secret 入库/出端、客户端伪造授权上下文。
  安全路径：Registry + Guardrail + request-bound capability + 固定网络策略，生产异常 fail-closed。

### Contract、迁移和生成物漂移

- 阻止只改一端类型、手改生成文件、原地破坏 v1、无迁移改 ORM，或用手工 baseline 掩盖回归。
  安全路径：修改真源、运行生成器、增加兼容/迁移测试并提交全部确定性输出。
