# 持久化索引 Worker 上线与恢复手册

## 前置条件

- PostgreSQL、Redis、Milvus 与 API/worker 需要访问相同环境。
- durable claim 依赖 PostgreSQL 的 `FOR UPDATE SKIP LOCKED`；SQLite 只用于单元测试，生产启动会拒绝非 PostgreSQL `DATABASE_URL`。
- API 与 worker 的 `UPLOAD_DIR` 必须是同一个共享目录或同一持久卷。
- 环境已完成 Document Catalog legacy adoption，且 `/health/ready` 的 `document_catalog` 项通过。
- 禁止旧版 FastAPI BackgroundTask writer 与 PR-16 worker 混跑。
- 仓库现有 compose 文件只启动基础依赖；API 与 worker 由外部 supervisor 管理。两者必须共享上传持久卷，并分别配置 restart 与 termination grace。

## 本地联合启动

本地开发使用统一入口同时管理 API 与 indexing worker：

```bash
./scripts/start.sh
```

该入口默认为 API 启用自动重载，并在任一子进程退出时终止另一个子进程，避免形成“API 可访问但 Index Job 无消费者”的半健康状态。需要更接近生产的本地运行方式时使用 `./scripts/start.sh --no-reload`。生产环境仍按下文使用 systemd、Kubernetes 或等价 supervisor 分别管理两个进程。

## 升级顺序

1. 暂停上传入口，并等待旧 API 进程内的上传任务结束。
2. 停止所有仍包含旧 BackgroundTask writer 的 API 进程。
3. 执行迁移：

   ```bash
   uv run alembic upgrade head
   ```

4. 确认 schema head 为 `0008_indexing_worker`：

   ```bash
   uv run python -c "from backend.infra.database import assert_schema_current; assert_schema_current()"
   ```

5. 启动独立 worker：

   ```bash
   uv run python -m backend.workers.indexing
   ```

6. 启动新 API。生产环境保持 `INDEX_WORKER_REQUIRED=true`。
7. 检查 `/health/ready`：`indexing_worker.ready=true`、`fresh_workers>=1`。
8. 恢复上传入口，提交一个测试文档并确认任务最终进入 `completed`。

兼容上传路径 `POST /documents/upload` 也只返回 `202 Accepted` 和 durable job ID，不再表示版本已经发布。兼容删除路径同样只撤销 scope、把物理清理交给 worker。升级前必须 drain 所有旧进程内 upload/delete poll handle，并同步升级仍把 HTTP 200 当终态的客户端。

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

`INDEX_WORKER_HEARTBEAT_SECONDS` 必须小于 lease。readiness TTL 必须严格大于 `2 × max(INDEX_WORKER_POLL_SECONDS, INDEX_WORKER_HEARTBEAT_SECONDS)`，同时应小于运维可接受的故障发现时间。
`INDEX_WORKER_ID` 只是可读前缀；进程入口会强制追加 host、PID 和随机后缀。不要让两个活跃进程共享完整 worker identity。
worker heartbeat 会携带当前 Document build fingerprint。API readiness 只统计与自身 parser、chunker、Embedding revision 和 index version 完全匹配的 worker；`incompatible_fresh_workers>0` 表示仍有旧 profile 进程存活，不能用它替代匹配 worker。

## 正常停止与重启测试

向 worker 发送 SIGTERM。进程会停止领取新任务，当前同步阶段返回后写入安全状态并退出。同步 parser、Embedding 与 Milvus 调用无法由 Python 安全硬中止，因此退出上限由 systemd、Kubernetes 或其他 supervisor 的 termination grace 控制；超时强制终止后，RUNNING/STAGED/cleanup RUNNING 会在 lease 到期后由其他 worker reclaim。

验证恢复：

1. 上传文档并等待任务进入 RUNNING。
2. 终止 worker。
3. 等待 lease 到期并启动新 worker。
4. 确认 `execution_fence` 增加、旧 execution 写入被拒绝，任务最终 completed 或按策略 retry/dead-letter。
5. 对 STAGED 任务重复此流程，确认恢复只发生 publish，不重新解析。

滚动升级可以让新 profile worker 对旧 STAGED job 执行 publish-only；尚未 STAGED 的任务只会被 fingerprint 匹配的 worker领取。若 `/health/ready` 显示只有 incompatible worker，先启动匹配 worker，再恢复上传流量。

## 诊断

`GET /health/ready` 返回：

- `latest_heartbeat_at`：最近 capability 匹配的 worker heartbeat。
- `queue_counts`：index 与 cleanup 各状态数量。
- `oldest_ready_at`：当前可领取 backlog 的最早创建时间。

用户显式删除会先原子撤销新查询的 Catalog scope，并立即允许 worker 领取物理清理任务。`DOCUMENT_INDEX_CLEANUP_GRACE_SECONDS` 只用于版本发布替换后的旧版本，保护 publish 前已经取得旧 Retrieval Scope 的并发查询；`oldest_ready_at` 不包含这类尚在 grace 或 retry wait 内的任务。

上传任务可通过 `/documents/upload/jobs/{job_id}` 查看 attempts、max_attempts、next_retry_at 和 execution_fence。FAILED/DEAD_LETTER 的索引候选不可复活；修复根因后重新上传会创建新的 DocumentVersion。cleanup DEAD_LETTER 表示检索 scope 已撤销但物理数据仍保留，必须先修复对应 Milvus、ParentChunk 或对象存储故障，再用以下受控命令恢复；不要直接修改数据库，也不要把版本改回 current/pending。

```bash
uv run python -m backend.workers.indexing list-cleanup --status dead_letter
uv run python -m backend.workers.indexing requeue-cleanup --job-id cleanup_xxx

# 需要为这次人工恢复调整预算时显式指定；不传则保留原 max_attempts
uv run python -m backend.workers.indexing requeue-cleanup \
  --job-id cleanup_xxx \
  --max-attempts 5
```

`requeue-cleanup` 只接受仍为 dead-letter、尚未 cleaned 且不属于 current/pending 的 exact version scope；校验失败会拒绝操作。命令把 attempts 清零、递增 operator requeue 记录，并由下一次 worker claim 生成新的 execution fence。

## 回滚

应用回滚到旧 writer 前必须先停止新 worker。若 `document_cleanup_jobs` 或 `index_jobs` 仍有 RUNNING owner，等待其退出或 lease 到期。`alembic downgrade 0007_document_publication` 会删除 worker heartbeat、cleanup queue 与 execution fence，因此只允许在确认没有待恢复任务且已备份 PostgreSQL 后执行。
