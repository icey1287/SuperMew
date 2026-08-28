# ADR-0016：持久化索引 Worker、执行租约与清理队列

- 状态：已接受
- 日期：2026-07-16
- 更新：2026-07-17

## 背景

Index Job 与物理清理必须跨 API 重启、多进程和 worker 故障保持可恢复。`publication_fence` 只保护候选版本发布次序，不能证明某个 worker 仍拥有本次执行；无锁扫描也无法阻止多个进程重复删除同一 Document Version，或为失败提供 attempt、backoff 与 dead-letter。

## 决策

建立持久化 Indexing Worker Module，并让 `DocumentCatalog` 作为唯一 durable ledger：

1. `publication_fence` 与 `execution_fence` 正交。前者属于候选版本，后者在每次 claim/reclaim 时单调递增；所有 worker-owned Catalog mutation 都校验 worker ID、execution fence 和未过期 lease。
2. `claim_index_job()` 使用 `SELECT ... FOR UPDATE SKIP LOCKED` 领取 pending、到期 retry、STAGED 和过期 RUNNING。STAGED 只执行 publish；其它 crash recovery 从 deterministic version scope 幂等重建。
3. attempt 在 claim 时原子递增。retryable failure 写 durable `next_retry_at`；确定性失败进入 FAILED；重试耗尽进入 DEAD_LETTER。失去 ownership 的执行不得 terminalize 或清理候选。
4. `DocumentPublication.submit()` 只创建 durable reservation，`run()` 只执行 publication。retry/dead-letter 策略由 Worker Module 决定。
5. `document_cleanup_jobs` 对每个 Document Version 只有一个 ledger。supersede、failed、cancelled、dead-letter 和 retire 在同一 PostgreSQL 事务内幂等 enqueue；cleanup claim、heartbeat、retry、dead-letter 和 finalize 使用独立 execution fence。
6. `worker_heartbeats` 在空闲时也持续写入。API readiness 只接受近期且 build fingerprint 匹配的 running heartbeat；backlog 与 dead-letter 只作为诊断数据。
7. `backend.workers.indexing` 是独立进程入口，自行校验 migration、启动 Provider Runtime、响应 SIGTERM 停止领取新任务，并在当前同步阶段返回后退出。
8. 上传与删除 HTTP Adapter 只执行短事务：保存 source、reserve candidate，或撤销 Catalog scope 并 enqueue cleanup；不解析、不生成 Embedding、不写 Milvus，也不保存进程内 job 状态。
9. 每次用户删除生成独立 `document_retirement_jobs` operation ID，并冻结 exact version ID 集合。scope revoke、cleanup enqueue 与 retirement snapshot 必须原子提交；查询进度逐项核对 cleanup ledger，缺失任务 fail-closed。
10. retry delay 由 worker 计算，绝对 `next_retry_at` 由 Catalog 使用数据库时钟写入；worker 本机时钟不参与 lease、retry 或 readiness 判定。
11. worker identity 强制包含 host、PID 与随机后缀。API 与 worker 必须共享同一 `UPLOAD_DIR`，相对路径统一锚定项目根目录。
12. cleanup dead-letter 只能通过 worker CLI 的受控 requeue Interface 恢复；该 Interface 再次核验 exact version 非 current/pending、尚未 cleaned，并记录 operator requeue。
13. 发布替换产生的 previous current 固定在 publish 后 1 小时才可 claim。用户主动删除、失败、取消、dead-letter 和候选覆盖都以数据库当前时间为 due time，立即可 claim。
14. worker capability 由 Document Version `build_fingerprint` 表示。非 STAGED job 只分配给 fingerprint 匹配的 worker；STAGED 可由其它 profile 执行 publish-only，但 stored profile 自身 hash 必须一致。
15. 生产 ledger 固定使用 PostgreSQL；SQLite 只作为单元测试 Adapter。

## 不变量

- `publication_fence` 不得由 claim/reclaim 改写；`execution_fence` 不得代替发布 CAS。
- progress、manifest、publish、retry、fail、dead-letter、cleanup step 和 finalize 必须拒绝 stale execution。
- STAGED transient failure 不得重复解析和向量化。
- ownership loss 后只能停止交付结果，不能改写终态或删除候选产物。
- cleanup 只作用于任务创建时的 exact Document Version scope；current/pending 永远不能 finalize cleaned。
- 外部删除成功而 finalize 前进程崩溃时，下一次 exact-version cleanup 必须可安全重放。
- 队列为空不能等价于 worker 存活；readiness 只依据 heartbeat。
- retirement snapshot 引用的每个 version 都必须存在且只存在一个 cleanup ledger。
- `next_retry_at` 必须从数据库时钟加相对 delay 生成。
- SIGTERM 后 worker 在每次 claim 前检查 drain gate。
- 只有发布替换的 previous current 可以等待 1 小时；用户主动删除必须立即可 claim。
- 只有 heartbeat capability 与当前 API submit profile 匹配的 worker 才能满足 readiness。

## 结果

调用者跨越较小的 Catalog 与 Worker Interface，即获得 durable claim、lease、fencing、数据库时钟 retry、dead-letter、graceful drain、retirement snapshot 和 exact cleanup 的 Leverage。调度与失败知识集中在两个 Module 中，提高 Locality。

同步 parser、Milvus 和本地 Embedding 进入底层调用后仍不能硬中止。当前通过独立 heartbeat、阶段前后 ownership guard、deterministic IDs 和不可见候选 scope 限制风险。
