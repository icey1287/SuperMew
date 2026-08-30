# RAG AGENTS.md

本文件适用于 `backend/rag/`。同时遵守根目录和 `backend/AGENTS.md`。开始修改前至少阅读：

- `../../docs/adr/0012-provider-error-and-retry-semantics.md`
- `../../docs/adr/0013-async-provider-runtime.md`
- `../../docs/adr/0014-rag-evaluation-contract.md`
- `../../docs/adr/0024-model-control-and-rag-evaluation-runtime.md`
- `../../evals/rag/README.md`

## 当前正式流水线

生产 RAG 的正式入口位于 `pipeline.py` 与 `checkpoint_runner.py`，并使用 `RunRequestContext`、冻结的 Model Snapshot 和 Provider Runtime：

1. 本地快速规则或 Fast 模型判断复杂度。
2. 简单问题直接检索；复杂问题生成 2–4 个子问题并行检索。
3. Dense + Milvus 原生 BM25 召回，经 RRF 融合和 Auto-merging。
4. 可选 Rerank 在有界输入、deadline 和熔断内执行。
5. Grader 结构化判断相关性、可回答性、歧义和 route。
6. 证据不足时只选择 Step-back 或 HyDE 之一，最多一次重写、一次复评。
7. Synthesis 去重并生成带 Evidence identity 的答案；HITL 使用 checkpoint 恢复。

不要新增第二套“简化 RAG”或在 Tool、评测、HTTP 中复制图逻辑。离线评测 Adapter 可以观察正式图，但不能成为另一套生产实现。

## 领域结果与故障

- `NO_KNOWLEDGE`：Provider 成功且健康检索确实为空。
- `INSUFFICIENT_EVIDENCE`：检索成功但证据不足、部分覆盖、冲突或需要澄清。
- Provider failure：Embedding、Milvus、模型、网络或响应形状失败；必须保留 typed code。

禁止把异常捕获后返回 `[]`。复杂问题允许部分子查询成功：有证据但覆盖不全时返回 partial，并记录 `coverage_gap_codes/questions`；全部子查询 Provider 失败时传播 typed error。

## 降级边界

- Hybrid 只有在 Adapter 明确报告 Sparse/Hybrid 能力不兼容时才复用同一 query embedding 降级 Dense。
- 连接失败、超时、服务不可用、参数错误和 malformed response 不属于能力不兼容。
- Rerank 未配置时保留融合排序；已配置但最终失败时允许回退 RRF/Auto-merging，必须写入 `rerank_error_code`、attempts 和 `rerank_fallback_applied`，且不再应用 rerank threshold。
- 答案 delta 已发布后不得重试整个答案生成。

## 图、HITL 与 async bridge

- 当前 checkpoint 图是同步图；知识 Tool 在 worker thread 执行，Provider 通过 `ProviderLoopBridge` 进入专用 async loop。
- 不在有 running event loop 的线程阻塞等待 bridge；不每次调用 `asyncio.run()`；不跨 loop 复用 AsyncClient。
- 只把 `invoke()` 改成 `ainvoke()` 是错误迁移。整图异步化必须同时提供 async saver、start/resume/outcome 和所有节点的 async 实现。
- `waiting_input` 的 resume state 必须可校验、可持久化且绑定同一 Run/Checkpoint；不要重跑原问题模拟 HITL。

## Evidence 与 RAG Trace

- Evidence 必须携带 Document Version、chunk、来源和内容哈希身份；不要只传无身份的 context text。
- Trace 可以记录 route、query、候选 identity、RRF/Rerank 分数、rewrite、覆盖缺口、降级和耗时。
- Trace 不保存 chunk 全文、endpoint、Secret、原始异常、模型私有推理或 chain of thought。
- 并行子问题的聚合必须确定性排序和去重；Run-local trace 只能写入当前 request context。

## Model 与检索快照

- Answer/Fast/Grader 从当前 Run 的 Model Snapshot 解析；不得在节点内读取环境变量或全局“当前模型”。
- Document retrieval snapshot 在一次 Run 内只解析一次；发布新 Document Version 不改变已开始的 Run。
- Cache key 至少包含模型/revision、规范化 query、Tenant、namespace 与 index/version identity；只有合法成功结果进入缓存。

## 修改后的最低验证

```bash
# 路由、重写、HITL 与故障语义
uv run --no-sync pytest -q \
  tests/test_rag_short_circuit.py \
  tests/test_rag_fault_injection.py \
  tests/test_native_checkpoint_hitl.py

# Provider bridge、重试与 Rerank 降级
uv run --no-sync pytest -q \
  tests/test_provider_loop_bridge.py \
  tests/test_provider_retry_policy.py \
  tests/test_rerank_stage.py

# 检索身份、Trace 与评测适配
uv run --no-sync pytest -q \
  tests/test_rag_retrieval_targets.py \
  tests/test_rag_trace_schema.py \
  tests/test_rag_evidence.py \
  tests/test_rag_eval_adapters.py \
  tests/test_rag_eval_cli.py

# 静态与评测生成物
uv run --no-sync ruff check backend/rag backend/providers tests
uv run --no-sync python scripts/generate_rag_eval_schemas.py --check
uv run --no-sync python scripts/evaluate_rag.py score \
  --dataset evals/rag/rag_smoke_v1.json \
  --observations evals/rag/offline_smoke_observations_v1.json \
  --gates evals/rag/gates_v1.json \
  --report /tmp/rebuilt-rag-baseline.json \
  --fail-on-regression
cmp /tmp/rebuilt-rag-baseline.json evals/rag/baseline_v1.json
```

测试文件名随仓库演进时，以 `tests/` 中实际存在的同领域文件为准。不要为了满足命令而新建空壳测试。

修改会影响 RAG source fingerprint、Dataset、Observation 或 Gate 时，继续遵守 `../../evals/rag/AGENTS.md`；不能只改 baseline hash 让 CI 变绿。

## Code Review Rules

### 无知识与故障混淆

- 阻止 `except: return []`、Provider error 转 `NO_KNOWLEDGE`、或 partial coverage 声称完整回答。
  安全路径：typed ProviderError、`INSUFFICIENT_EVIDENCE` 和明确 coverage metadata。

### 无界图与重写

- 阻止重复规划、两个 rewrite 同时执行、无限 Tool/RAG loop 或对同一故障多次打击 Provider。
  安全路径：固定预算、单选一次重写、一个重试所有者和绝对 deadline。

### 快照漂移

- 阻止运行中重新读取当前模型、当前 Document Version 或动态环境配置。
  安全路径：从 RunRequestContext 读取冻结的 Model/Retrieval Snapshot。

### 评测伪证据

- 阻止手工美化 Observation/baseline、用 offline smoke 宣称生产质量或省略 provenance。
  安全路径：确定性重建 offline contract；发布前在隔离索引运行 live evaluation 并审查报告。
