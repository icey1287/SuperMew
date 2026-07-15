# Document Catalog v1 发布手册

PR-15 将 legacy filename collection 改为 Catalog 精确 allowlist。升级环境必须先完成安全接管，再让新应用实例接收流量；否则 `/health/ready` 返回 503，RAG 检索也会以稳定的存储不可用错误 fail closed。

## 升级顺序

1. 停止旧应用写入，保留 PostgreSQL、Milvus、Redis 与上传对象存储。
2. 执行 Alembic `0006_native_checkpoints -> 0007_document_publication`。
3. 用生产相同配置运行只读扫描：

   ```bash
   uv run python scripts/adopt_legacy_document_catalog.py \
     --owner-username <admin> \
     --tenant-id default \
     --knowledge-base-name 默认知识库 \
     --dry-run
   ```

4. 仅当输出满足以下条件时继续：

   - `status` 不是 `error`；
   - `invalid_rows_skipped == 0`；
   - `unsafe_documents_skipped == 0`；
   - `adoption_ready == true`。

   dry-run 是纯只读操作。若正常升级形态中尚无 KnowledgeBase，它仍会完成扫描并返回 `target_knowledge_base_creation_required == true`，但不会创建目录。

5. 若存在 unsafe/invalid 行，先在隔离副本中按文档整组清理或修复 Milvus 与 ParentChunk 数据；不要直接把原始文件名、正文或 Provider 异常写进工单/日志。重新 dry-run 直到全部为零。
6. 执行 apply（移除 `--dry-run`）。若 dry-run 报告目标 KnowledgeBase 不存在，必须显式增加 `--create-target-knowledge-base`；脚本不会因名称拼写自动创建目标：

   ```bash
   uv run python scripts/adopt_legacy_document_catalog.py \
     --owner-username <admin> \
     --tenant-id default \
     --knowledge-base-name 默认知识库 \
     --create-target-knowledge-base
   ```

   成功输出必须包含 `adoption_complete == true`。脚本把 collection、目标 KnowledgeBase、脱敏 corpus fingerprint 与单调 adoption fence 持久化到租户级 `document_catalog_states`。
7. 启动新应用，确认 `/health/live` 为 200，且 `/health/ready` 中 `document_catalog.legacy_adoption_complete == true`。
8. 做一次已 adoption legacy 文档与一个 versioned 文档的检索 smoke test，再恢复流量。
9. adoption 完成后把 legacy collection 视为只读快照，停止旧 writer。若 leaf、leaf→parent linkage、parent 内容或 corpus fingerprint 变化，apply 会先清空 completion marker 并递增 fence，使 readiness/RAG fail closed，直到新扫描全部 reconcile 完成。

## 失败与恢复

- apply 可重复执行；已接管版本返回 `already_adopted`，不会创建重复 current。
- 同一 legacy source 的授权键固定为 `(collection, canonical_name)`；内容 hash 只用于漂移检测。相同 filename 不能被另一个租户或 KnowledgeBase 再次 claim。
- 已有 READY versioned current 会建立稳定 suppression claim；仅有 pending candidate 会让 apply 失败并保持 readiness 503。
- 两个 apply 即使交错运行，也只有持有当前 tenant adoption fence 的进程可以写 claim 或完成 marker；旧 fence 会以脱敏冲突失败。
- leaf chunk ID 必须非空且在整个 corpus 中唯一。leaf 指向的 ParentChunk 必须存在、filename 一致、保持 legacy 空版本身份，父级正文/hash/拓扑也进入 fingerprint。
- 删除未 adoption 名称会先写 durable legacy tombstone，再做物理清理。即使 Milvus/Redis 清理失败，该名称也不会进入检索 allowlist。
- 删除另一个 KnowledgeBase 中同名的 versioned 文档不会触碰已经由其他目录 claim 的 legacy filename corpus。
- 迁移 downgrade 会把 PR-15 同内容的 build/history 版本折叠回 PR-03 单一内容身份：优先保留 current，其次 pending，最后保留最高 version number。执行 downgrade 前必须备份 PostgreSQL、Milvus 与对象存储。

## 新安装

readiness 是只读门禁，不会根据“当前看不到 Milvus collection”自动写入 empty marker。新安装也必须显式运行一次 dry-run + apply；collection 不存在时 apply 会登记一个安全空 corpus。这样可以避免升级恢复顺序或临时配置错误把旧 corpus 永久误判为 fresh。
