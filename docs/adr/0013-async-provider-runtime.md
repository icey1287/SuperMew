# ADR-0013：异步 Provider Runtime 与同步图兼容策略

- 状态：已接受
- 日期：2026-07-15

## 背景

RAG Module 仍以同步 LangGraph 节点和同步 `PostgresSaver` 运行。持久 Run 的知识 Tool 会由 LangChain 放入 worker thread，HITL resume 也显式使用 `asyncio.to_thread()`；因此同步图本身不会直接占用 FastAPI 主 event loop。

旧 Embedding Implementation 在模块导入时加载 BGE-M3，查询错误地复用 `embed_documents()`，没有并发限制、微批或查询缓存。旧 Rerank Implementation 则在同步检索函数中逐次调用 `requests.post()`，无法复用异步连接池，也不能主动取消在途 HTTP。

直接把同步图改为 `graph.ainvoke()` 并不能解决问题：没有 async Implementation 的 LangGraph 节点会在 event loop 上直接执行同步 `invoke()`，同步 `PostgresSaver` 也没有可用的 async checkpoint Interface。只替换调用方法会把模型、Milvus、父块读取和 checkpoint I/O 一起压到主 event loop。

## 决策

建立 `ProviderRuntime` 深 Module，集中拥有 Provider 生命周期，并在内部放置以下 seam：

1. `ProviderLoopBridge` 拥有进程级专用 event loop 和后台线程。异步 Provider Adapter、查询微批任务和 `httpx.AsyncClient` 只在该 loop 创建、调用和关闭。
2. `EmbeddingRuntime` 提供 async query/document Interface。查询固定使用 `encode_query()` 语义，文档固定使用 `encode_document()`；Implementation 隐藏 lazy model load、专用有界 executor、并发 gate、10–30ms 可配置微批、LRU、同 key inflight 去重、向量形状校验、warmup 和 readiness。
3. `RerankerProvider` 由 `HttpxRerankerAdapter` 与 `DisabledRerankerAdapter` 两个真实 Adapter 实现。HTTP Adapter 复用单一 `AsyncClient`，transport retries 固定为 0；`ProviderExecutor.acall()` 继续独占 taxonomy、deadline、取消、`Retry-After` 和有限重试。
4. 现有同步 checkpoint 图仅通过明确的 compatibility Adapter 调用 Provider Runtime。同步调用只能来自无 running event loop 的 worker thread，并通过 `run_coroutine_threadsafe()` 投递；禁止每次调用 `asyncio.run()`，禁止跨 loop 使用 AsyncClient。
5. Rerank 的条件跳过、输入上限、fallback 和 trace metadata 仍由 RAG Module 拥有。Provider Adapter 只负责可靠评分；失败保留召回排序，且不得应用 rerank score 阈值。
6. FastAPI lifespan 在 Run executor 之前启动 Provider Runtime，并在所有 Run 停止后关闭它。模块导入不得加载模型、启动线程或创建 AsyncClient。
7. async 文档 endpoint 中的 Milvus、解析、文件校验、PostgreSQL/Redis 和向量写入全部下沉到 worker thread。PR-15/PR-16 会再用 Document Catalog 和持久 indexing worker 替换这些 compatibility 路径。

## 不变量

- FastAPI 主 event loop 不执行本地 Embedding、同步 Milvus RPC、文档解析、文件 `fsync` 或同步 Rerank HTTP。
- `httpx.AsyncClient` 只属于 Provider loop；多次调用复用同一 client，shutdown 必须关闭且不得遗留 task。
- 每次外部调用只有一个重试所有者。HTTP transport、SDK、bridge 和 RAG caller 不得叠加重试。
- Provider deadline 包含 semaphore/queue 等待时间；取消必须主动取消在途 coroutine 或 bridge future。
- 已进入 PyTorch/CUDA 底层 encode 后无法保证硬中止。取消只停止等待与结果交付；有界 queue、executor 和并发 gate 必须限制尾部工作。
- Query cache key 至少包含 model、revision、规范化 query、namespace、tenant 和 index version。只有成功且形状合法的向量可进入缓存。
- Rerank malformed response、重复/越界 index、缺失或非有限 score 都是 typed Provider failure，不能表现为健康空检索。
- 同步 compatibility Adapter 在 running event loop 中必须 fail fast，不能通过阻塞等待制造死锁。
- 当前同步 checkpoint 图、`Command(resume=...)` 和已接受的 HITL 语义保持不变。

## 后续异步图迁移条件

若未来删除 compatibility Adapter，必须一次性提供 async checkpoint saver、async start/resume/outcome，以及所有模型、检索、rewrite、grader、子问题和 HITL targeted retrieval 节点的 async Implementation。不得只切换 `graph.ainvoke()` 留下同步节点。

## 结果

调用者跨越一个小的 Provider Runtime Interface，即获得连接池、查询语义、微批、缓存、并发限制、deadline、取消、重试和生命周期的 Leverage。模型与 HTTP 资源集中在一个 Module，提高 Locality；同步图只保留一个明确的兼容 seam，不再决定 Provider 的运行方式。

代价是进程内增加一个专用 loop 线程，且本地模型已经进入底层推理后仍不能硬取消。相比在每次调用创建 event loop，专用 owner loop 保持 AsyncClient 与微批状态稳定；相比当前立即迁移整张 async 图，它避免破坏同步 checkpointer 和持久 HITL 语义。
