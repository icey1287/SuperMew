# 持久化索引 Worker 上线与恢复手册

## 前置条件

- PostgreSQL、Redis、Milvus 与 API/worker 访问同一环境。
- durable claim 依赖 PostgreSQL `FOR UPDATE SKIP LOCKED`；SQLite 只用于单元测试。
- API 与 worker 的 `UPLOAD_DIR` 指向同一个共享目录或持久卷。
- 数据库已执行 `alembic upgrade head`，且 Document、Document Version、ParentChunk 与索引数据都具有完整版本身份。
- API 与 worker 使用相同的 parser、chunker、Embedding revision 与 index version。

## 本地联合启动

```bash
./scripts/start.sh
```

启动器同时管理 API、indexing worker 与 RAG evaluation worker。任一子进程退出时会终止其余
进程，避免形成“API 可访问但后台任务无消费者”的半健康状态。需要关闭 API 自动重载时使用：

```bash
./scripts/start.sh --no-reload
```

生产环境使用 systemd、Kubernetes 或等价 supervisor 分别管理 API、indexing worker 与 RAG
evaluation worker；API 与 indexing worker 必须共享持久上传目录。完整流程见
`docs/runbooks/deployment.md`。

## 发布顺序

1. 暂停文档上传入口。
2. 执行迁移：

   ```bash
   uv run alembic upgrade head
   ```

3. 校验 schema：

   ```bash
   uv run python -c "from backend.infra.database import assert_schema_current; assert_schema_current()"
   ```

4. 启动 indexing worker：

   ```bash
   uv run python -m backend.workers.indexing
   ```

5. 启动 RAG evaluation worker：

   ```bash
   uv run python -m backend.workers.evaluation
   ```

6. 启动 API，生产保持 `INDEX_WORKER_REQUIRED=true`。
7. 检查 `/health/ready`：`indexing_worker.ready=true`、`fresh_workers>=1`。
8. 恢复上传入口，提交测试 Document 并确认 Index Job 最终 completed。

## 必要配置

```text
INDEX_WORKER_ID=
INDEX_WORKER_REQUIRED=true
INDEX_WORKER_POLL_SECONDS=1
INDEX_WORKER_LEASE_SECONDS=90
INDEX_WORKER_HEARTBEAT_SECONDS=15
INDEX_WORKER_RETRY_BASE_SECONDS=5
INDEX_WORKER_RETRY_MAX_SECONDS=300
INDEX_WORKER_RETRY_JITTER_RATIO=0.2
INDEX_WORKER_READINESS_TTL_SECONDS=45
```

`INDEX_WORKER_HEARTBEAT_SECONDS` 必须小于 lease。readiness TTL 必须严格大于 `2 × max(INDEX_WORKER_POLL_SECONDS, INDEX_WORKER_HEARTBEAT_SECONDS)`。

`INDEX_WORKER_ID` 只是可读前缀；进程入口会追加 host、PID 与随机后缀。两个活跃进程不能共享完整 worker identity。

worker heartbeat 携带当前 Document build fingerprint。API readiness 只统计 fingerprint 匹配的 worker；不匹配的进程不能满足 readiness，也不能领取非 STAGED Index Job。

## 正常停止与恢复验证

向 worker 发送 SIGTERM。进程停止领取新任务，在当前同步阶段返回后写入安全状态并退出。同步 parser、Embedding 与 Milvus 调用无法安全硬中止，因此强制终止后，RUNNING/STAGED/cleanup RUNNING 会在 lease 到期后由其它 worker reclaim。

验证步骤：

1. 上传 Document 并等待 Index Job 进入 RUNNING。
2. 终止 worker。
3. 等待 lease 到期并启动新 worker。
4. 确认 `execution_fence` 增加，stale execution 写入被拒绝，任务最终 completed、retry 或 dead-letter。
5. 对 STAGED 任务重复此流程，确认只恢复 publish，不重新解析和向量化。

## 清理时序

- 新 Document Version 原子发布后，previous current 固定在 1 小时后才允许物理清理。这是唯一 grace。
- 用户主动删除时，Catalog scope 立即撤销，cleanup job 使用数据库当前时间作为 due time，worker 可立即 claim。
- 失败、取消、dead-letter 与候选覆盖同样立即可 claim。
- `oldest_ready_at` 只统计当前已经可 claim 的 backlog，不包含发布替换 grace 或 retry wait 内的任务。

删除 HTTP 响应只表示 scope revoke 与 durable cleanup ledger 已提交。前端必须立即展示“删除中”，并轮询 `/documents/delete/jobs/{job_id}`。状态含义：

- `running`：物理清理正在执行或等待 worker claim；
- `completed`：Milvus、ParentChunk 与版本对象均已确认清理；
- `failed`：retirement transaction 或 ledger 不完整，不能确认删除；
- `cleanup_failed`：Document 已不可检索，但物理清理进入 dead-letter，需要管理员受控重试。

## 诊断与 dead-letter 恢复

`GET /health/ready` 提供：

- `latest_heartbeat_at`：最近 capability 匹配的 worker heartbeat；
- `queue_counts`：index 与 cleanup 各状态数量；
- `oldest_ready_at`：当前可领取 backlog 的最早创建时间。

上传 Index Job 可通过 `/documents/upload/jobs/{job_id}` 查看 attempts、max_attempts、next_retry_at 与 execution_fence。FAILED/DEAD_LETTER 的候选不可复活；修复根因后重新上传会创建新的 Document Version。

cleanup dead-letter 通过受控 CLI 恢复：

```bash
uv run python -m backend.workers.indexing list-cleanup --status dead_letter
uv run python -m backend.workers.indexing requeue-cleanup --job-id cleanup_xxx

uv run python -m backend.workers.indexing requeue-cleanup \
  --job-id cleanup_xxx \
  --max-attempts 5
```

`requeue-cleanup` 只接受仍为 dead-letter、尚未 cleaned 且不属于 current/pending 的 exact Document Version scope。校验失败会拒绝操作；禁止直接修改数据库状态。

## 回退策略

当前 Document Version identity 迁移不可逆。若发布失败，应修复当前版本并向前发布；不得恢复已删除的 Interface、Adapter、Implementation、schema 字段或运行时双读。数据库迁移前必须完成备份，并在隔离环境验证 `upgrade head`。
