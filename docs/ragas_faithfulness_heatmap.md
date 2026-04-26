# RAGAS Faithfulness 网格热力图（Top-K × 子块窗口 W）

## 目的

在多种 **Top-K** 与 **子叶子块窗口 W**（对应不同 Milvus brief 集合）组合下，用 [RAGAS](https://docs.ragas.io/) 的 **Faithfulness** 指标测 RAG 答案与检索上下文的一致性，并输出冷蓝配色（Blues）热力图与汇总表。

## 过程概览

1. **读入评测 CSV**（默认 `RAG DATA_INTRO - Copy of test.csv` 前 15 行，问题列 `question`）。
2. 对网格中每个 **(W, K)** 单元：
   - 通过 `set_rag_config` 设置 `final_top_k=K`、`candidate_k`（保证 ≥ K）、可选 `milvus_collection_brief`（见下），以及 **`skip_grade_and_rewrite=True`**。
   - **不走** RAG 图里的文档相关性打分（grade）与查询重写 / HyDE / 二次检索（rewrite + retrieve_expanded）；初次检索完成后直接进入答案生成（与线上「打分通过」分支一致）。实现见 `backend/rag_pipeline.py` 中 `grade_documents_node`。
   - 对每一行问题调用 `chat_with_agent`，从 `rag_trace` 取 `expanded_retrieved_chunks` / `retrieved_chunks` 作为 `retrieved_contexts`，模型回复作为 `response`。
   - 将该批样本交给 RAGAS **仅 Faithfulness**，得到该单元的均值。
3. **写出结果**：汇总 CSV、矩阵 CSV、热力图 PNG、各单元逐条分的 JSON。

底层检索逻辑见 `backend/rag_utils.py`：`retrieve_documents` 支持通过 `get_rag_config()` 覆盖 Top-K 与 Milvus 集合名。

## 环境与依赖

- 项目根目录 `.env`：`ARK_API_KEY`、`BASE_URL`；RAGAS 打分默认使用 **`GRADE_MODEL`**（未设则用 `MODEL`）；以及 Milvus、嵌入等与线上一致。
- 安装：`uv sync --extra eval`（含 `ragas`、`pandas`、`matplotlib` 等）。

### RAGAS LLM 与豆包 / 火山

默认 RAGAS `llm_factory` 会使用 `response_format=json_object`，部分模型会报错导致 Faithfulness 为 **nan**。评测脚本改为使用 `backend/ragas_llm_compat.build_ragas_instructor_llm`，默认 **`RAGAS_INSTRUCTOR_MODE=tools`**（Instructor 函数调用模式，不发 `json_object`）。

| 变量 | 含义 |
|------|------|
| `RAGAS_INSTRUCTOR_MODE=tools` | **默认**，适合豆包/火山等 OpenAI 兼容端 |
| `RAGAS_INSTRUCTOR_MODE=json` | 与官方 ragas 一致，适合原生 OpenAI 等支持 JSON 模式的模型 |
| `RAGAS_MAX_OUTPUT_TOKENS` | 默认 **8192**。Faithfulness 默认仅 1024 时极易因输出截断得到 **nan**；仍不够时可加大（受模型上限限制） |

## 子块窗口 W 与 Milvus

不同 **W** 需要**分别入库**到不同的 brief 集合；通过环境变量指定（未设置则所有 W 共用默认 brief，热力图在 **W 维度会相同**）：

| 变量 | 含义 |
|------|------|
| `MILVUS_BRIEF_W300` | W=300 时使用的 brief 集合名 |
| `MILVUS_BRIEF_W400` | W=400 |
| `MILVUS_BRIEF_W500` | W=500 |
| `MILVUS_COLLECTION_BRIEF` | 上述未设置时的回退 |

## 运行命令

```bash
cd /path/to/SuperMew
uv run python run_ragas_faithfulness_heatmap.py
```

常用参数：

| 参数 | 说明 |
|------|------|
| `--csv` | 评测表路径 |
| `--limit` | 行数；`0` 表示全表（默认 15） |
| `--question-col` | 问题列（默认 `question`） |
| `--ref-col` | 参考列；不设则自动选 `回答要点总结` → `answer_key` → `answer` |
| `--windows` | W 列表，默认 `300 400 500` |
| `--topk` | Top-K 列表，默认 `3 5 7 10` |
| `--out-dir` | 输出目录（默认 `ragas_faithfulness_grid_out/`） |
| `--log-file` | 运行日志路径；**不设则写入** `out-dir/faithfulness_run_<时间戳>.log` |

## 输出文件

均在 `--out-dir` 下：

| 文件 | 说明 |
|------|------|
| `faithfulness_run_<时间戳>.log` | **运行时日志**（UTF-8），与控制台内容一致 |
| `faithfulness_grid_summary.csv` | 每单元 Faithfulness 均值与样本数 |
| `faithfulness_matrix.csv` | 矩阵形式 |
| `faithfulness_heatmap.png` | 热力图 |
| `scores_W{w}_K{k}.json` | 该单元 RAGAS 逐条分数 |

## 运行时日志说明

- 脚本使用 Python `logging`：**同时写入上述 `.log` 文件并打印到 stdout**。
- 日志包含：命令行、CSV/列名、网格参数、每个 **(W,K)** 单元内**每一行**的 Agent 调用开始/结束（问题预览、上下文片段数、回复长度）、RAGAS 评测起止、均值、写出路径。
- **RAGAS `evaluate(..., show_progress=True)`** 自带的 **tqdm 进度条**通常输出到 **stderr**，一般**不会**进入 `.log` 文件；如需完整抓取可把 shell 重定向为 `python ... >out.log 2>&1`。

## 工作量提示

近似调用次数：**样本行数 × len(W) × len(K)** 次 Agent，外加每单元一次 RAGAS Faithfulness（内含多次 LLM）。全表 × 12 格时请预留时间与费用。
