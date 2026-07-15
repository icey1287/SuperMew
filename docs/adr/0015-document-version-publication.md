# ADR-0015：Document Catalog 与版本化两阶段发布

- 状态：已接受
- 日期：2026-07-15

## 背景

旧文档上传路径以文件名作为身份，并在解析新文件前顺序删除 Milvus 与父级分块。解析、Embedding、Milvus 或 PostgreSQL 任一阶段失败，都会让仍然有效的旧知识先消失。文档列表还需要扫描最多 10,000 条向量后在 Python 中聚合；进程内上传任务无法在重启后查询。

知识库检索已经成为 Run Runtime 的关键领域能力，因此文档身份、版本、入库状态和可检索范围不能继续由多个存储的偶然状态共同决定。需要一个深 Module 隐藏跨 PostgreSQL、Milvus、ParentChunk 与对象文件的发布顺序，并保留一个可由 PR-16 worker 替换的调度 seam。

## 决策

建立三个相邻 Module：

1. `DocumentCatalog` 的 Interface 负责文档身份、候选版本、持久 `IndexJob`、manifest、fencing、原子发布、逻辑删除和延迟清理状态。PostgreSQL 的 `documents.current_version_id` 是可检索版本的唯一真相源。
2. `DocumentPublication` 的 Interface 只有 `submit()` 与 `run()` 两段。`submit()` 保存一个 durable reservation/job；`run()` 完成版本化解析、父级分块 staging、Milvus staging、精确核验和 Catalog publish。同步 endpoint 与后台 dispatcher 都调用同一个 Interface。
3. `DocumentRetrievalScope` 把 Catalog current pointer 投影为一个或多个只读 target。每个 target 固定 collection 与服务端生成的安全 filter；RAG caller 不拼接版本表达式，也不能选择候选版本。

新候选默认写入 `${MILVUS_COLLECTION}_catalog_v1`，与 legacy filename collection 物理隔离。所有新 chunk ID 都包含 `document_version_id`，Milvus 与 ParentChunk metadata 同时携带 tenant、knowledge base、document、version、section、index 和 content hash 身份。

发布流程固定为：

```text
reserve candidate/version + durable IndexJob
→ version-scoped parse/chunk IDs
→ stage ParentChunk
→ stage Milvus
→ exact ID/count verification
→ PostgreSQL fencing/CAS publish
→ old version superseded with cleanup grace
```

`pending_version_id` 与单调 `version_number` 是发布 fencing。较早候选即使晚完成，也不能覆盖较新的 pending 候选。发布事务同时写入 manifest、更新 candidate/job、切换 current pointer、标记旧版 superseded，并递增 knowledge-base catalog revision；事务失败时检索 scope 仍指向旧 current。

相同 `content_sha256 + build_fingerprint` 的提交只在仍由 `current_version_id` 或 `pending_version_id` 持有的活跃版本上幂等复用。`failed/superseded` 是不可逆终态，其 `DocumentVersion.id` 永不重新变成候选；终态后同内容重传会获得新的 version number、version ID 与 IndexJob。数据库只对 `uploaded/parsing/indexing/staged/ready` 建立 partial unique identity，避免旧 cleanup snapshot 与新发布 scope 发生别名。`build_fingerprint` 至少覆盖 parser、chunker、Embedding 和 index layout 版本；内容相同但构建身份改变时创建新版本。

旧 current 不立即物理删除。它获得 `index_cleanup_after` grace，供已经取得旧 scope 的查询完成；清理由持久 worker 重试并记录 `index_cleaned_at` 或稳定 `cleanup_error_code`。只有 Milvus、ParentChunk cache/DB 与版本对象文件全部清理成功后，才能记录 cleanup complete；失败状态无法持久确认时必须保留候选与 source object。

## Legacy 迁移

legacy collection 不含 DocumentVersion metadata，不能与候选写入同一 collection。迁移脚本以安全文件名聚合 legacy chunk，并验证全 corpus 唯一且非空的 leaf chunk ID、leaf→parent 链、parent filename、空 legacy version identity、父级正文/hash 与拓扑，随后幂等建立 `storage_layout=legacy_filename` 的 current version。检索投影在迁移期间双读：

- versioned current 从 catalog collection 按精确 version ID 读取；
- 已 adoption 的 legacy current 从 legacy collection 按 Catalog 精确 filename allowlist 读取；
- 未 adoption、恶意或非规范 filename 的 legacy 行 fail closed，不进入 broad base target；
- 删除未 adoption 名称时先在 Catalog 原子建立 durable legacy tombstone，再执行 filename 物理清理；清理失败或后续同名重传都不能让旧向量重新可见。

因此单个文档完成 adoption 后即可与 versioned current 一起双读；尚未 adoption 的数据需要先通过安全扫描接管。候选永远不会因为 legacy 双读而提前可见。`index_id` 同时绑定 READY current 的 exact manifest 与 legacy suppression/allowlist 投影，使可见集合变化必然改变检索身份。

升级部署必须遵循 `0007 migrate → adoption dry-run（unsafe/invalid 均为 0）→ adoption apply → readiness 通过 → 新应用接流量`。`document_catalog_states` 以 tenant 为单位持久化 legacy collection、目标 KnowledgeBase、脱敏 corpus fingerprint 与单调 adoption fence；它不是每个 KnowledgeBase 各自竞争共享 collection 的 marker。非空 corpus 只允许接管到已存在的目标，或由 operator 通过显式 `--create-target-knowledge-base` 确认创建；dry-run 永远不写数据库。未完成接管、目标/collection 配置不一致或 fence 失效时 readiness 与 Retrieval Scope 均 fail closed，具体操作见 `docs/runbooks/document-catalog-v1-rollout.md`。

adoption 完成后 legacy collection 是只读快照，旧 writer 必须停止。后续 apply 若观察到 leaf、parent 或 linkage fingerprint 漂移，会先清空 completion marker、递增 fence 并关闭 readiness；只有持有当前 fence 且所有 legacy source 再次满足 Catalog claim 后才写入新 marker。授权键固定为 `legacy:source:v1:sha256(collection + canonical_name)`，内容 hash 不参与所有权身份，只用于返回 `legacy_content_drift`。`(vector_collection, legacy_identity)` 的全局唯一 claim 同时承载 adoption、versioned suppression 与 durable tombstone，防止同一 filename-scoped 语料被不同 tenant/knowledge base 重复授权。

readiness 不得根据一次 `has_collection == false` 自动持久化 fresh marker；升级中的配置错误、恢复顺序或临时 collection 缺失不能被误认成新安装。新安装也通过显式 adoption CLI 登记安全空 corpus。

## 不变量

- 解析、ParentChunk、Milvus、核验或数据库发布任一点失败，都不得修改旧 `current_version_id`。
- V2/V3 并发乱序完成时，只有仍持有 document pending fence 的版本可以发布。
- PostgreSQL current pointer 是检索真相源；Milvus 不维护 `is_current`。
- candidate 发布前不可被任何 RAG target 选中。
- manifest、ParentChunk 与 Milvus receipt 的 ID 和数量必须精确一致，不能只检查非空。
- Catalog 故障是基础设施失败，不能降级成 `NO_KNOWLEDGE`。
- 文档列表只查询 Catalog，不扫描 Milvus。
- 删除先原子撤销 current scope，再做可重试物理清理；清理失败不能让已删除文档重新可见。
- legacy 检索只能读取 Catalog allowlist；不得用“全部 filename 减 tombstone”的 broad target。
- legacy parent expansion 必须再次核对 parent 与 child 的 filename/tenant/knowledge-base/document/version/index scope；scope 不一致或正文为空时保留 child，不得跨文档合并。
- tenant adoption fence、collection、目标 KnowledgeBase 与 corpus fingerprint 必须在每个 source claim 和最终 completion 事务中重新校验。
- failed/superseded 版本身份不可复活；stale cleanup 只能删除创建它时的版本 scope。
- 旧 current 在 cleanup grace 内可供已经开始的查询读取；新查询在 publish commit 后立即只读新 current。
- dispatcher 只负责选择何时调用 `DocumentPublication.run()`。PR-16 用 lease worker Adapter 替换 inline/BackgroundTask Adapter，不改变 publication Interface 与状态机。

## 结果

调用者跨越很小的 Catalog、Publication 与 Retrieval Scope Interface，即获得幂等、fencing、精确核验、原子切换、durable progress、legacy 双读和延迟清理的 Leverage。跨存储顺序与失败知识集中在这些 Module 中，提高 Locality；文档 endpoint、RAG 和后续 worker 不再各自重新实现发布规则。

代价是候选数据在失败或 supersede 后可能短期占用 Milvus、PostgreSQL 和对象存储空间，并需要持久 cleanup worker。相比同步物理删除，该代价换取了发布零不可用窗口和可恢复的一致性语义。
