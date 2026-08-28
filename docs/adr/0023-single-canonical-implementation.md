# ADR-0023：单一正式 Interface 与 Implementation

- 状态：已接受
- 日期：2026-07-17

## 背景

同一个领域动作如果同时存在多套 Interface、Adapter 或 Implementation，调用者就必须理解选择规则、状态差异、错误差异和数据同步顺序。即使其中一套只被称为兼容路径，它仍会扩大测试表面，并让后续修改有机会静默回到非权威状态。

SuperMew 的 Thread、Run、Event、Document Version 与认证生命周期都以持久化事实为核心。正式升级完成后，继续保留平行运行时路径不会增加有效 Leverage，只会降低 Locality。

## 决策

每个领域动作只保留一套正式 Interface 和一套实际使用的 Implementation：

- Thread 生命周期只由 `backend.threads` 与 `/v1/threads` 拥有；
- Agent 执行只由持久 Run/Event/Checkpoint 路径拥有；
- Document 入库、发布、检索与删除只接受完整 Document Version identity；
- 浏览器认证只提供内存 Access Token 与 HttpOnly Refresh Token；密码写入只使用
  PBKDF2-SHA256。升级期间允许在登录事务中只读验证历史哈希并立即单向改写，但不保留第二套
  Token、Session 或密码写入 Interface；
- 前端只使用 Thread、Run、Event、Document 与 Document Version 领域模型。

正式替换完成时必须同时删除：

- 不再对外的 route、schema 与请求/响应 shape；
- 仅用于转发或翻译的 Adapter；
- 不再被正式 Interface 调用的 Implementation；
- 双读、自动探测、自动迁移、运行时 allowlist/tombstone 与按旧身份清理逻辑；
- 可让故障静默切换到另一套行为的 fallback；
- 描述已删除路径、配置、字段或操作步骤的活跃文档与文案。

数据库迁移可以读取并删除待退役字段，因为这是升级到当前 schema 所必需的单向数据变换。迁移完成后，运行时模型、查询、健康检查和文档不得继续依赖这些字段。不可逆迁移必须 fail-closed，发现仍活跃或未清理的数据时拒绝升级。

无法离线转换的密码哈希是唯一的凭据迁移边界：旧哈希只允许在成功登录时验证一次，并在同一
事务中改写为 PBKDF2-SHA256。部署方确认所有环境不存在旧哈希后必须删除验证代码与依赖。

唯一允许的延迟清理是 Document Version 原子发布后 previous current 的固定 1 小时 grace。该规则封装在 `DocumentCatalog` Implementation 内，不是配置项，也不暴露给调用者。用户主动删除、失败、取消、dead-letter 与候选覆盖全部立即进入可领取的物理清理。

Provider 的能力降级不是版本回退：例如 Hybrid 明确不受支持时改用 Dense、Rerank 失败时保留召回排序，仍属于同一正式 RAG Interface，并且必须在 RAG Trace 中显式记录。任何未声明、静默或指向另一套 Implementation 的 fallback 均不允许。

## 不变量

- 一个领域动作不得存在第二个可调用的公开 Interface。
- 一个 seam 只有一个实际运行 Adapter 时，不为假设性的替代实现保留额外抽象。
- 删除正式 Implementation 后，测试不得通过 monkeypatch 或 import 重新激活另一套运行路径。
- 未知动态 HTTP path 必须按当前安全策略 fail-closed，不能因为未列入 matcher 而绕过认证或限流。
- 用户动作必须立即获得 in-flight、成功或失败反馈；异步任务必须暴露 durable job identity、可恢复进度和明确 terminal 状态。
- dead-letter 是失败，不得使用“等待中”文案掩盖，也不得误报 completed。
- 迁移历史是单向 schema 证据，不是运行时兼容授权。

## 结果

调用者学习更小的 Interface，测试直接覆盖正式 seam，维护者无需判断多套路径之间的优先级。删除冗余 Adapter 和 Implementation 提高了 Locality，也减少后续自动生成代码重新接回非权威逻辑的机会。

代价是升级需要明确的数据迁移和客户端同步发布；发现不满足迁移前提的数据时，系统会拒绝继续，而不是用运行时兼容掩盖问题。
