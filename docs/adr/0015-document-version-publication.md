# ADR-0015：Document Catalog 与版本化两阶段发布

- 状态：已接受
- 日期：2026-07-15
- 更新：2026-07-17

## 背景

知识库检索是 Run Runtime 的关键领域能力。Document 身份、Document Version、入库状态和可检索范围不能由 PostgreSQL、Milvus、ParentChunk 与对象文件的偶然状态共同决定，也不能在新内容构建完成前破坏当前可用版本。

需要一个深 Module 隐藏跨存储发布顺序，让调用者只面对稳定的 Document、Document Version 与 Index Job Interface。

## 决策

建立三个相邻 Module：

1. `DocumentCatalog` 负责 Document 身份、候选 Document Version、持久 Index Job、manifest、fencing、原子发布、逻辑删除和物理清理 ledger。PostgreSQL 的 `documents.current_version_id` 是可检索版本的唯一真相源。
2. `DocumentPublication` 的 Interface 只有 `submit()` 与 `run()`。`submit()` 保存 durable reservation；`run()` 完成版本化解析、ParentChunk staging、Milvus staging、精确核验和 Catalog publish。
3. `DocumentRetrievalScope` 把 current pointer 投影为只读 target。每个 target 固定 collection 与服务端生成的安全 filter；RAG caller 不能选择候选版本或拼接版本表达式。

所有 chunk、ParentChunk 与 Evidence 都必须携带完整的 Tenant、Knowledge Base、Document、Document Version、section、index 与 content hash 身份。缺少 Document Version identity 的数据不进入检索、父级展开或 RAG Trace。

发布流程固定为：

```text
reserve candidate/version + durable Index Job
→ version-scoped parse/chunk IDs
→ stage ParentChunk
→ stage Milvus
→ exact ID/count verification
→ PostgreSQL fencing/CAS publish
→ previous current superseded
→ previous current cleanup due after exactly 1 hour
```

`pending_version_id`、`publication_fence` 与单调 `version_number` 共同保护发布顺序。较早候选即使晚完成，也不能覆盖较新的 pending 候选。发布事务同时切换 current pointer、标记 previous current 为 superseded、创建 cleanup ledger 并递增 Knowledge Base catalog revision；事务失败时检索仍指向原 current。

相同 `content_sha256 + build_fingerprint` 只在 current 或 pending 活跃版本上幂等复用。`failed` 与 `superseded` 是不可逆终态；同内容再次提交会获得新的 Document Version 与 Index Job。`build_fingerprint` 至少覆盖 parser、chunker、Embedding 和 index layout 版本。

唯一延迟物理清理的场景是：新 Document Version 原子发布成功后，previous current 固定保留 1 小时，供 publish commit 前已经取得其 Retrieval Scope 的并发查询完成。这个时长是 Catalog 内部不变量，不是配置项，也不暴露为调用参数。

以下路径全部使用数据库当前时间作为 cleanup due time，立即允许 worker claim：

- 用户主动删除 Document；
- 候选被更新的上传覆盖；
- 解析、索引或发布失败；
- Index Job 取消或进入 dead-letter；
- 任何不再属于 current/pending 的终态候选。

用户主动删除时，current/pending scope revoke、cleanup ledger 与 retirement snapshot 在同一事务提交。新查询立即不可见，物理清理由持久 worker 立刻领取。Milvus、ParentChunk cache/DB 与版本对象全部成功清理后才能记录 complete；dead-letter 必须作为明确失败反馈，并通过受控 requeue Interface 恢复。

## 不变量

- 解析、ParentChunk、Milvus、核验或数据库发布任一点失败，都不得修改 current pointer。
- candidate 发布前不可被任何 RAG target 选中。
- manifest、ParentChunk 与 Milvus receipt 的 ID 和数量必须精确一致。
- Catalog 故障是基础设施失败，不能降级成 `NO_KNOWLEDGE`。
- Document 列表只查询 Catalog，不扫描 Milvus。
- failed/superseded Document Version 身份不可复活；cleanup 只能删除任务创建时的 exact version scope。
- current/pending 永远不能 finalize cleaned。
- 发布替换的 previous current 固定使用 1 小时 grace；其它路径不得等待 grace。
- 用户删除先原子撤销检索 scope，再立即进入可领取的物理清理；清理失败不能让 Document 重新可见。
- 正式运行时只接受完整 Document Version identity，不双读、不按 filename 清理、不回退到无版本数据。

## 结果

调用者跨越很小的 Catalog、Publication 与 Retrieval Scope Interface，即获得幂等、fencing、精确核验、原子切换、durable progress 和 exact-version cleanup 的 Leverage。跨存储顺序与失败知识集中在这些 Module 中，提高 Locality。

代价是发布替换后的 previous current 会额外占用最多 1 小时存储空间；失败候选和删除任务也需要持久 cleanup worker。这个代价换取了零不可用发布和可恢复的一致性语义。
