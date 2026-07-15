# ADR-0014：RAG 评测契约、离线评分与回归门禁

- 状态：已接受
- 日期：2026-07-15

## 背景

RAG 的路由、召回、Auto-merge、Rerank、重写、HITL 和无知识判断已经形成较深的执行链，但参数调整仍主要依赖人工观察。旧 `langsmith_eval.py` 在模块导入时读取环境并立即执行远程评测，只检查答案字符重合，既不能离线复现，也无法区分检索质量、Provider 故障和领域性无知识。

PR-15 之前还没有稳定的 DocumentVersion 与 IndexManifest 身份；当前回答也没有结构化 claim/evidence 映射。因此本阶段需要先固定一个不依赖模型、Milvus 或网络的评分 Interface，同时为真实运行结果保留 Adapter seam。

## 决策

建立 `backend.evaluation` RAG 评测 Module：

1. `evaluate_rag()` 是纯评分 Interface。输入严格版本化 Dataset、Observation、GatePolicy 和可选 baseline，Implementation 集中完成排名指标、路线/结果/HITL 指标、标签切片、Provider 故障统计、基线比较和门禁；Pydantic 模型生成的 JSON Schema 作为跨工具契约并由 CI 检查是否过期。
2. Dataset 与 Observation 分离。Dataset 是人工标注事实；Observation 是某次 RAG 执行的脱敏投影，不保存 chunk 正文、上游响应、endpoint、凭证或原始异常。
3. `PredictionFileAdapter` 提供完全离线评分；`LiveRagEvalAdapter` 调用当前原始 RAG 图并生成同一 Observation Interface。离线测试与 CI 不得导入或启动生产模型、Milvus、Provider Runtime。
4. Dataset 使用内容规范化后的 SHA-256 fingerprint。Observation 与 baseline 必须绑定同一 fingerprint；数据集变化时不得静默沿用旧报告。可比性还必须同时绑定 corpus 相对路径与字节、RAG 源码和依赖锁文件，以及脱敏的模型/Embedding/Rerank/检索配置 profile；PR-15 后 `index_id` 改用 IndexManifest hash。
5. 初始门禁覆盖 Recall/Precision/MRR/nDCG、document recall、route、complexity、outcome、HITL、rewrite improvement、Provider failure 和 critical case。延迟仅报告 p50/p95，不跨硬件默认门禁。
6. 当前缺少稳定 claim/evidence Interface，因此 correctness、groundedness、citation precision/recall、unsupported claim 和 conflict disclosure 显式标记为 unavailable，禁止用字符重合伪装这些指标。
7. 仓库提交一个受控 Orion HTML corpus、覆盖计划中主要问题类型的 smoke Dataset，以及明确标记为 `contract_smoke` 的 sanitized baseline。静态 Prediction Adapter 不能通过 `live_rag` provenance 门禁；真实发布报告只能由 Live Adapter 在显式 profile/index identity 下生成。
8. 本地生成的 predictions、报告和包含调试信息的 artifact 只能写入 `.artifacts/`；CI 仅保留脱敏 JSON/Markdown 摘要。

## 不变量

- `NO_KNOWLEDGE` case 不得携带 gold evidence；Provider failure 不能算作正确 abstention。
- `ANSWERABLE` case 必须至少有一个 gold document 或 gold chunk。
- case ID、observation case ID 和 rank 必须唯一；未知字段拒绝。
- chunk 指标只统计有 gold chunk 的 eligible case，空 gold case 不得稀释分母。
- critical case 从通过变为失败时零容忍；缺失 observation、执行错误和 eligible case 数下降默认失败。
- HITL resolution 必须离开等待状态、没有 Provider failure，并命中标注的最终 outcome；最终 `NO_KNOWLEDGE` 不能冒充预期为 `ANSWERABLE` 的成功恢复。
- Release CLI 拒绝关闭 critical protection、清空必需指标、降低绝对阈值或放宽回归容差的 GatePolicy。
- Dataset fingerprint、corpus/index identity 或 RAG source fingerprint 不一致时，报告必须说明并拒绝不安全比较。
- JSON 和 Markdown 报告不得包含文档正文、密钥、endpoint、原始异常或私有推理。
- 实时评测一次进程只运行一个 profile；模块级 RAG 配置变化通过新进程生效，不在同一进程热切换。

## 结果

调用者跨越一个小的评分 Interface，即获得数据校验、指标、切片、基线和门禁的 Leverage。评分知识集中在一个 Module，提高 Locality；实时 Provider 与离线 CI 通过两个 Adapter 共享同一 seam。

代价是初始 smoke baseline 只证明评测系统可复现，不代表生产质量。PR-15 会用 DocumentVersion/IndexManifest 替代临时 corpus identity；结构化引用完成后再启用答案与引用指标。最终完成审计前，应把真实人工标注集扩展到至少 200 条。
