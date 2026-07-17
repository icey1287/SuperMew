# ADR-0016：持久化索引 Worker、执行租约与清理队列

- 状态：已接受
- 日期：2026-07-16

## 背景

PR-15 已将上传拆成 durable reservation 与两阶段发布，但调用 `DocumentPublication.run()` 的时机仍由 FastAPI `BackgroundTasks` 决定。Web 进程重启会遗失调度，多个 API 进程也无法安全协调同一 `IndexJob`。`publication_fence` 只保护候选版本发布次序，不能证明某个 worker 仍拥有本次执行；旧执行在 lease 过期后仍可能写 progress、manifest 或 terminal 状态。

延迟物理清理同样只有无锁的 candidate scan。多个进程可能同时删除同一版本，失败没有独立 attempt、backoff 或 dead-letter。readiness 也无法在队列为空时证明 indexing worker 存活。

## 决策

建立一个持久化 Indexing Worker Module，并让 `DocumentCatalog` 继续作为唯一 durable ledger：

1. `IndexJob.publication_fence` 与 `IndexJob.execution_fence` 正交。前者属于候选版本，后者在每次 claim/reclaim 时单调递增；所有 worker-owned Catalog mutation 都同时校验 worker ID、execution fence 和未过期 lease。
2. `claim_index_job()` 使用 `SELECT ... FOR UPDATE SKIP LOCKED` 领取 pending、到期 retry、STAGED 和过期 RUNNING。STAGED claim 保持 STAGED，只执行 publish；非 STAGED crash recovery 从 deterministic version scope 幂等重建。
3. attempt 在 claim 时原子递增。retryable failure 在未耗尽时写 durable `next_retry_at`；STAGED 的 retry 保持 STAGED。确定性失败进入 FAILED；retryable exhaustion 进入 DEAD_LETTER。失去 ownership 的旧 worker不得 terminalize 或清理候选。
4. `DocumentPublication` 的外部 Interface 保留 `submit()` 与 `run()`。worker 通过可选 `IndexJobExecution` 调用 `run()`；Publication 在解析、ParentChunk、Milvus、manifest 和 publish 的外部阶段前后复核 ownership，但 retry/dead-letter 策略只由 Worker Module 决定。
5. 新增一-version一-job 的 `document_cleanup_jobs`。publish supersede、failed/dead-letter、retire 和 legacy tombstone 在同一 PostgreSQL 事务内幂等 enqueue。cleanup claim、heartbeat、retry、dead-letter 和 finalize 使用独立 execution fence；finalize 再次确认目标不是 current/pending。
6. 新增 `worker_heartbeats`。进程空闲时也持续写 heartbeat；API readiness 只读检查近期且 build capability 匹配的 `running` heartbeat。backlog 和 dead-letter 只作为诊断数据，不直接让 API 摘流。
7. `backend.workers.indexing` 是独立进程入口，自行校验 migration、启动 Provider Runtime、响应 SIGTERM 停止领新任务，并在当前同步执行返回后退出。
8. 上传与删除 endpoint 只做短事务：保存 source、reserve candidate，或撤销 Catalog scope 并 enqueue cleanup。API 进程不再解析、Embedding、写 Milvus，也不再保存进程内 job 状态。
9. 兼容上传/删除路径返回 `202 Accepted`，其成功只表示 durable submit 或 scope revoke 已持久化。现有 compose 仍只管理基础依赖；API 与 worker 的共享卷、restart 和 termination grace 由外部 supervisor 配置。
10. 每次删除生成独立 `document_retirement_jobs` operation ID，并冻结该次删除涉及的 exact version ID 集合。versioned scope revoke、legacy tombstone、cleanup enqueue 与 retirement snapshot 必须在同一个 PostgreSQL 事务提交；任一步失败都会整体回滚，不存在进程崩溃后只完成一半 scope 的状态。查询删除进度必须逐项核对 cleanup ledger；缺失任务 fail closed，不能把空查询误报为 completed。多版本中 dead-letter 诊断优先暴露真实待 requeue job ID。
11. retry backoff 只由 worker 计算相对 delay，绝对 `next_retry_at` 由 Catalog 使用数据库时钟写入；worker 本机时钟不参与 lease、retry 或 readiness 判定。没有 typed `public_error` 的 orchestration/数据库异常默认视为可重试存储故障，只有显式确定性错误才能直接 FAILED/dead-letter。
12. `INDEX_WORKER_ID` 只是可读前缀，实际 identity 强制追加 host、PID 和随机后缀。独立入口在导入 Milvus、Redis、Publication 等运行时 Module 前加载项目 `.env`；相对 `UPLOAD_DIR` 统一锚定项目根目录，防止 API 与 worker 因 CWD 不同产生 split-brain。
13. cleanup dead-letter 只能通过 worker CLI 的受控 requeue Interface 恢复；该 Interface 再次核验 exact version 仍非 current/pending、尚未 cleaned，并记录 operator requeue。readiness 的 `oldest_ready_at` 只统计已经越过 cleanup grace、retry delay 且当前可 claim 的 scope。
14. 用户显式删除不沿用 Publication cleanup grace：新查询在 scope revoke 后立即不可见，cleanup job 同时变为可领取。版本发布替换产生的旧 current 仍使用 grace，供 publish 前已取得旧 scope 的并发查询完成。
15. worker capability 由 DocumentVersion `build_fingerprint` 表示。非 STAGED claim 只分配给 fingerprint 匹配的 worker，Publication 仍在解析前复核记录字段 hash 与本地 capability；STAGED 因只执行 publish 可由新 profile worker恢复，但仍必须通过 stored profile 完整性校验。heartbeat 携带 fingerprint，API readiness 只接受与当前 submit profile 匹配的近期 worker。
16. 生产 job ledger 固定使用 PostgreSQL；`FOR UPDATE SKIP LOCKED` 是多 worker claim 正确性的一部分，SQLite 只作为单元测试 Adapter，生产配置会拒绝其它数据库 scheme。

## 不变量

- `publication_fence` 不得由 claim/reclaim 改写；`execution_fence` 不得代替发布 CAS。
- progress、manifest、publish、retry、fail、dead-letter、cleanup step 和 finalize 必须拒绝 stale execution。
- STAGED transient failure 不得退回 RUNNING/RETRY_WAIT，也不得重复解析和向量化。
- 旧 worker ownership loss 后只能停止交付结果；不能把候选标失败，也不能删除候选产物。
- cleanup 只作用于创建任务时的 exact DocumentVersion scope；current/pending 永远不能 finalize cleaned。
- 外部删除成功而 finalize 前进程崩溃时，下一次 exact-version cleanup 必须可安全重放。
- API 与 worker 必须挂载同一个 `UPLOAD_DIR`；分进程本地盘不是有效部署。
- 旧版 API BackgroundTask writer 与新 worker 不得混跑。升级必须先停止旧 writer，再启动新 worker。
- 队列为空不能等价于 worker 存活；readiness 只依据独立进程 heartbeat。
- retirement snapshot 引用的每个 version 都必须存在且只存在一个 cleanup ledger；缺失时对外状态必须失败，不能完成。
- `next_retry_at` 必须从数据库时钟加相对 delay 生成；worker wall clock 漂移不得缩短或延长 backoff。
- SIGTERM 后 worker 在每次 claim 前检查 drain gate，不得在第一个空队列检查后继续领取另一类新任务。
- 版本替换 cleanup grace 内的 pending 任务不得被 claim，也不得出现在 `oldest_ready_at`；用户显式删除以数据库当前时间作为 cleanup due time。
- 非 STAGED job 不能被 build fingerprint 不匹配的 worker claim；STAGED 可以跨 profile publish-only 恢复，但 stored profile 自身 hash 必须一致。
- 只有 heartbeat capability 与当前 API submit profile 匹配的 indexing worker 才能满足 readiness。

## 结果

调用者跨越较小的 Catalog 与 Worker Interface，即获得 durable claim、lease、fencing、数据库时钟 retry、dead-letter、graceful drain、retirement snapshot 和 exact cleanup 的 Leverage。调度与失败知识集中在两个 Module 中，提高 Locality；HTTP handler、Publication 与物理存储 Adapter 不再各自决定任务生命周期。

同步 parser、Milvus 和本地 Embedding 进入底层调用后仍不能硬中止。当前通过独立 heartbeat、阶段前后 ownership guard、deterministic IDs 和不可见候选 scope 限制风险；若未来需要在数据库连接完全隔离的网络分区下提供严格的外部写 fencing，应把 attempt generation 纳入物理 staging identity，并在 publish manifest 中固定 generation。
