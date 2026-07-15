# RAG 评测

本目录保存版本化、可离线复现的 RAG 评测资产。

## 目录

- `rag_smoke_v1.json`：受控 Orion 语料的人工标注 smoke Dataset。
- `corpus/`：可上传到专用测试知识库的 HTML 语料。
- `offline_smoke_observations_v1.json`：用于验证评分 Module 的脱敏 Observation。
- `gates_v1.json`：质量回归门禁。
- `baseline_v1.json`：由上述 Dataset 与 Observation 生成的 offline smoke baseline。
- `schema/`：Dataset、Observation、GatePolicy 与 Report 的 JSON Schema。

offline smoke baseline 只证明数据契约、指标和回归门禁可以稳定复现，不代表生产知识库质量。真实评测应把 `corpus/` 上传到隔离的测试索引，使用 live Adapter 生成新的 Observation；不要手工修改 Observation 来美化指标。

## 常用命令

```bash
uv run python scripts/evaluate_rag.py validate \
  --dataset evals/rag/rag_smoke_v1.json

uv run python scripts/generate_rag_eval_schemas.py --check

uv run python scripts/evaluate_rag.py score \
  --dataset evals/rag/rag_smoke_v1.json \
  --observations evals/rag/offline_smoke_observations_v1.json \
  --gates evals/rag/gates_v1.json \
  --baseline evals/rag/baseline_v1.json \
  --report .artifacts/rag-eval/report.json \
  --markdown .artifacts/rag-eval/report.md \
  --fail-on-regression

uv run python scripts/evaluate_rag.py run \
  --dataset evals/rag/rag_smoke_v1.json \
  --gates evals/rag/live_gates_v1.json \
  --observations .artifacts/rag-eval/live-observations.json \
  --report .artifacts/rag-eval/live-report.json \
  --markdown .artifacts/rag-eval/live-report.md \
  --profile-id local-orion-eval-v1 \
  --index-id your-index-manifest-or-collection-version \
  --timeout-seconds 60
```

`run` 会懒加载生产 RAG，并使用当前本地 Provider、Milvus 与模型配置。一次进程只运行一个 profile；修改模型、索引或 RAG 参数后请启动新进程。`profile-id` 与 `index-id` 必须显式填写；报告还会自动绑定 corpus 字节、RAG 源码、依赖锁文件以及脱敏后的模型/Embedding/Rerank/检索配置 fingerprint。报告和 Observation 不保存 chunk 正文、endpoint、密钥或原始异常。

`gates_v1.json` 只接受 `contract_smoke` provenance；静态 Observation 不能冒充实时质量结果。`live_gates_v1.json` 只接受由 `run` 产生的 `live_rag` provenance。GitHub 的默认 workflow 验证评分器、baseline 可重建性和基准分支差异；没有配置模型、Milvus 与隔离测试索引的托管 runner 时，它不会宣称已经执行生产 RAG。RAG 发布前仍必须在受控索引上运行 live 命令并审查对应报告。

## 扩充标注集

最终生产门禁应至少有 200 条人工标注 case，覆盖：单事实、定义、参数、跨文档综合、对比、多跳、时间/版本、表格、代码、歧义/HITL、无知识、来源冲突、近似实体、错别字、长问题和文档提示注入。当前 `prompt_injection` case 带有 `retrieval_only` 标签，只验证安全说明能被正确检索；在结构化答案与泄密探针接入前，不得据此宣称完整 Guardrail 已通过。

新增或修改 Dataset 后 fingerprint 会变化，旧 Observation 与 baseline 会被拒绝。真实 corpus/index identity 应在 PR-15 后切换到 DocumentVersion 与 IndexManifest hash。
