# ADR-0012：Provider 错误、重试与 RAG 领域结果

- 状态：已接受
- 日期：2026-07-15

## 背景

旧 RAG Module 在 Embedding 或 Milvus 失败时返回空文档，调用者随后把空结果解释为“知识库无答案”。Rerank Module 则自行决定超时、降级和错误文本，并可能把上游响应正文与 endpoint 写入 trace。模型、天气、检索等调用也各自实现错误处理，调用者必须理解多个不一致的 Interface，缺少 Locality。

Provider 故障与健康检索后的领域结论具有不同语义：前者需要失败、重试或显式降级，后者才可以产生 `NO_KNOWLEDGE` 或 `INSUFFICIENT_EVIDENCE`。

## 决策

建立 `backend.providers.ProviderExecutor` 作为统一 Provider 调用 seam。它的 Interface 接受操作、Provider 标识、绝对 deadline、取消探针和有限重试策略；Implementation 集中完成：

- 按异常类型与 HTTP 状态分类，不从原始正文猜测公开错误；
- 使用稳定 code、安全消息、retryable 和 `Retry-After`；
- 仅重试明确可重试且适合重复执行的调用；
- 退避和下一次尝试不得越过 Run deadline；
- 取消在重试与退避检查点优先传播；
- checkpoint 只保存安全 Provider 错误快照，不保存异常对象、URL、正文或凭证。

重试所有权遵循“每次外部调用只有一层”的不变量：OpenAI-compatible 模型 Adapter 显式设置原生 `timeout` 且 `max_retries=0`，由 `ProviderExecutor` 独占分类和重试。Planner、Grader、Rewrite 等结构化、未发布输出的模型调用可以有限重试；答案模型可能在失败前已经发布 delta，因此 Runtime middleware 固定 `max_attempts=1`，不得重试整个答案流。

RAG Module 采用下列语义：

1. Embedding 与向量检索耗尽重试后抛出 typed `ProviderError`，不得返回空文档。
2. `NO_KNOWLEDGE` 只表示 Provider 调用成功且健康检索确实为空。
3. `INSUFFICIENT_EVIDENCE` 表示检索成功但证据不足、部分覆盖或需要澄清。
4. Hybrid 仅在 Adapter 明确报告稀疏/混合能力不兼容时降级到 Dense；连接、超时和服务不可用不额外重复打击同一故障端。
5. Rerank 是可降级阶段。失败时保留召回排序，记录稳定 `rerank_error_code`、attempts 与 fallback 标志；不得记录 endpoint、响应正文或原始异常。
6. 复杂问题允许部分子查询成功。成功证据与 Provider 故障并存时返回部分结果和 `coverage_gap_codes`；健康空结果与 Provider 故障并存且没有证据时返回 `INSUFFICIENT_EVIDENCE`，不能声称 `NO_KNOWLEDGE`；只有所有子查询均因 Provider 失败时才重抛 typed error。
   只要已有证据但任一规划子问题未被覆盖（包括健康空结果），整体也必须是 partial，并记录 `coverage_gap_questions`。
7. HTTP、Run、terminal Event 和前端 reducer 共用同一 `PublicError` 形状；`error` 是唯一权威错误投影。
8. request-owned Tool Adapter 必须继承 Run deadline 与 cancellation probe。Runtime 路径的 Provider 故障必须抛出 typed error，使 `tool.failed` 与 Run terminal 保持一致。
9. 已经发布的 partial answer 必须保留，并追加稳定 code 的安全终态说明。
10. `RetrievalOutcome` Module 统一持久 Run resume 与 Tool renderer 对 `NO_KNOWLEDGE` / `INSUFFICIENT_EVIDENCE` 的解释。具体 coverage metadata 只能作为 untrusted data 进入 Human/Tool message，不能提升为 SystemMessage。
11. Milvus 与 Rerank Adapter 在返回“健康空结果”前必须验证外层、hits、entity、index 与 score 形状；malformed response 必须成为 typed Provider failure。通用参数错误不能伪装成 hybrid capability 不兼容。
12. FAST memory model 也跨越 Provider seam；其降级只保留当前已持久化笔记，并且日志只记录稳定 code。

## 不变量

- 除“Provider 调用成功且结果确实为空”外，任何故障注入都不得表现为 `NO_KNOWLEDGE`。
- `CancelledError`、认证失败、策略拒绝和确定性 4xx 不重试。
- Provider 错误的公开字段只能来自固定 taxonomy 和安全数字/标识。
- Rerank 降级不能把 Run 误终结为失败；Embedding、向量与最终模型不可用不能伪装成成功。
- Run terminal 必须保留 typed code，不能再统一覆盖为 `RUN_EXECUTION_FAILED`。
- 一旦答案 delta 对客户端或 Event log 可见，同一次答案调用不得从头重试。
- 模型 SDK 内建重试必须关闭；不得与 `ProviderExecutor` 形成乘法重试或越过绝对 deadline。
- 持久化 `CANCELLING` 是 durable cancellation source；完成、ProviderError 与取消竞态中取消必须胜出，且 cancellation reason 优先级为 `ownership_lost > user > shutdown`。

## 结果

调用者跨越一个较小的 Provider Interface，即获得错误分类、重试、deadline、取消与脱敏的 Leverage；故障知识集中在一个 Module 中，提高 Locality。RAG 的“无知识”成为可评测的领域结果，而不是基础设施异常的副作用。

同步 Adapter 仍不能可靠强制中止已经进入底层 C/C++、gRPC 或阻塞 HTTP 的调用。PR-13 将在此 seam 后提供 async-native Embedding/Rerank Adapter、连接池与主动超时；本 ADR 的公开错误和领域结果语义保持不变。
