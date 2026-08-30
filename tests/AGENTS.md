# Tests AGENTS.md

本文件适用于根目录 `tests/`，继承根目录与相关模块 `AGENTS.md`。测试不是实现细节的快照，而是正式 Interface、不变量、故障方向和竞态的可执行证据。

## 基本原则

- Bug fix 先增加能稳定失败的回归，再修实现；新功能覆盖成功、拒绝/失败和边界路径。
- 测试命名描述行为和条件，不描述“调用了某私有函数”。优先通过公开 service/module seam 观察结果。
- 单元测试默认离线、确定性、无真实 Secret、无公网、无大型模型下载。
- 普通模型调用使用 fake model；Provider、Redis、Milvus、PostgreSQL 的真实协议由明确 integration/smoke 负责。
- 不通过放宽断言、增加任意 sleep、吞异常或全局 monkeypatch 让测试偶然通过。

## 测试分层

### Unit / contract

- 纯状态机、schema、policy、错误映射、序列化、Repository 边界。
- 使用固定 clock/ID、fake Adapter 和最小 fixture。
- 不依赖测试顺序、全局 singleton 残留或开发者环境变量。

### Integration

- PostgreSQL 锁、事务、迁移、partial index 与多连接行为。
- Redis Lua、TIME、TTL、认证和跨 Adapter 共享。
- Milvus schema、Tenant/Version filter、Dense/BM25 Hybrid 和删除。
- 使用专用临时资源；测试应拒绝明显的共享/生产 DSN。

### Browser/E2E

前端 Playwright 位于 `frontend/e2e/`。通过 route mock 测真实页面投影，不要求本地模型/数据库；真实后端 E2E 应另有显式环境和清理策略。

## 并发与异步测试

- 等待明确条件：Event/Queue、状态字段、数据库行、Future 或 mock 调用；不要假设一次 `await asyncio.sleep(0)` 就完成。
- 使用 `asyncio.Event`/barrier 控制竞态顺序，证明两个请求或 owner 真正重叠。
- 跨 Run 隔离至少创建两个不同 `run_id/thread_id/user_id`，交错发送 delta/trace/cancel，断言无串号。
- cancellation、terminal、ownership lost 竞态同时断言状态、Event、Message、writer fence 和公开错误，而不只断言异常类型。
- timeout 测试使用 fake clock 或短且有界的同步点，避免依赖机器负载。

## 数据库与迁移测试

- SQLite 可验证 Alembic 转换链和通用 Repository 行为，但不能证明 PostgreSQL 行锁、`FOR UPDATE`、advisory lock、JSONB、partial index 或隔离级别。
- 新 migration 测试从历史 schema 升级到 head，并校验数据/约束；不可逆 downgrade 明确验证 fail-closed。
- PostgreSQL integration 的清理在 `finally`/fixture teardown 执行，失败时也不留下共享状态。

## Provider 与外部系统

- Fake Adapter 覆盖 taxonomy、deadline、取消、retryable、Retry-After、malformed response 和降级决策。
- 每次外部调用只有一个重试 owner；测试调用次数防止 SDK × executor 乘法重试。
- Embedding smoke 只在预热且 offline 的模型缓存运行，revision 固定 40 位 commit SHA；缓存缺失直接失败，不临时联网。
- live RAG/模型评测与普通 pytest 分离，报告明确 provenance。

## 安全测试

安全测试应证明失败方向，而不只证明 happy path：

- Origin/body metadata 在 Rate Limit 前拒绝，streaming 超限在计费后拒绝。
- Rate Limit 存储异常 typed 503，未知 path 默认受保护。
- raw token/IP/username 不进入 Redis key、日志或公开 payload。
- Web URL policy 复核 DNS/redirect/私网；Evidence/capability 不能跨 Run 重用。
- Guardrail deny/approval 在 Tool handler 前；Sandbox disabled/not-ready fail-closed。
- SQL Assistant 拒绝 DDL/DML、多语句、越界对象、过高成本和敏感字段泄露。

## 常用命令

```bash
# 单个文件/测试
uv run --no-sync pytest -q tests/test_file.py
uv run --no-sync pytest -q tests/test_file.py::test_name

# 迁移兼容
uv run --no-sync pytest -q \
  tests/test_migrations.py \
  tests/test_document_catalog_migration.py \
  tests/test_indexing_worker_migration.py

# 全覆盖率
uv run --no-sync pytest -q \
  --cov=backend --cov=scripts \
  --cov-report=term-missing:skip-covered \
  --cov-fail-under=80
```

完整命令、环境变量和 smoke 见 `../docs/runbooks/repository-quality-gates.md`。聚焦命令中的文件若因仓库演进改名，选择实际对应测试，不创建空壳兼容文件。

## Fixture 与 monkeypatch

- Fixture 只构造必要状态，默认最小权限和 fail-closed 配置。
- Patch 使用点而不是定义点；恢复全局 singleton、settings cache、registry 和 environment。
- 不 monkeypatch 私有实现来重新激活已经删除的第二套 Interface。
- 生成的 ID/时间若参与 fingerprint 或排序，使用固定值；比较结构化对象而非整段易变日志。

## Code Review Rules

### 测试未触发真实风险

- 阻止名为“并发/取消/恢复”的测试实际串行执行，或只断言 HTTP 200。
  安全路径：barrier 控制交错，断言持久状态、Event、identity 和 stale writer。

### 用 fake 证明真实协议

- 阻止以 SQLite/fake Redis/fake Milvus 结论替代 PostgreSQL 锁、Lua/TTL 或 Hybrid schema 兼容。
  安全路径：保留快速 contract 测试，并增加专用真实 integration/smoke。

### Flaky 修补

- 阻止增加 sleep、重试整条测试、放宽到“最终任意状态”或吞 background exception。
  安全路径：等待可观察条件、固定 clock、收集 task exception 并清理资源。

### 隐式外部依赖

- 阻止普通 CI 测试联网、下载模型、读取个人 `.env` 或共享服务。
  安全路径：fake/offline 默认；live 测试显式 marker、credential、隔离资源和 provenance。
