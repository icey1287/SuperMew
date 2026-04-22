#!/usr/bin/env python3
"""
网格扫描 Top-K × 子叶子块窗口 W，对评测集跑 RAGAS Faithfulness，并输出冷蓝配色（matplotlib Blues）热力图（与论文式 Faithfulness vs K vs Chunk size 图一致）。

对 CSV 每行调用 chat_with_agent，用 rag_trace 中的片段作为 retrieved_contexts，再只计算 Faithfulness 均值。
本脚本在 set_rag_config 中打开 skip_grade_and_rewrite，RAG 子图在首次检索后**不**做文档打分与查询重写/二次检索，直接走「生成答案」路径（与 run_ragas_eval 的完整策略不同）。

检索参数：
  - 通过 tools.set_rag_config 传入 final_top_k（即 Top-K）、candidate_k（候选池，自动 ≥ final_top_k）、think_mode=normal。
  - 子块窗口 W：需对应**单独入库**的 Milvus brief 集合。若未配置则三行 W 共用当前默认集合（热力图在 W 维度会相同）。

环境变量（可选，按窗口覆盖 Milvus 集合名）：
  MILVUS_BRIEF_W300、MILVUS_BRIEF_W400、MILVUS_BRIEF_W500
  未设置时回退到 MILVUS_COLLECTION_BRIEF；再未设置则不覆盖（使用代码默认 embeddings_collection_brief）。

依赖（eval 组）：ragas、pandas、openai、matplotlib；以及项目 backend 与 .env（ARK_API_KEY、MODEL、BASE_URL、Milvus、嵌入等）。

用法：
  uv sync --extra eval && uv pip install matplotlib
  uv run python run_ragas_faithfulness_heatmap.py
  # 默认：上述 CSV 的前 15 条、问题列 question；可用 --limit / --question-col 修改

运行日志：默认写入 out-dir 下 faithfulness_run_<时间戳>.log（与控制台同步），可用 --log-file 指定路径。
RAGAS 评测 LLM 见 backend/ragas_llm_compat.py（默认 RAGAS_INSTRUCTOR_MODE=tools，避免 json_object 不被模型支持）。
过程说明见 docs/ragas_faithfulness_heatmap.md。
"""

from __future__ import annotations

import argparse
import importlib
import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from uuid import uuid4

import numpy as np
import pandas as pd
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
_BACKEND = str(ROOT / "backend")
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

load_dotenv(ROOT / ".env")

from ragas import evaluate
from ragas.dataset_schema import EvaluationDataset, SingleTurnSample
# evaluate() 只接受 ragas.metrics.base.Metric 子类；collections.Faithfulness 为新架构 BaseMetric，会报错
from ragas.metrics._faithfulness import Faithfulness

from ragas_llm_compat import build_ragas_instructor_llm

_agent = importlib.import_module("agent")
chat_with_agent = _agent.chat_with_agent

_tools = importlib.import_module("tools")
set_rag_config = _tools.set_rag_config


TOP_K_LIST_DEFAULT = [3, 5, 7, 10]
WINDOW_SIZES_DEFAULT = [300, 400, 500]

LOG = logging.getLogger("faithfulness_grid")


def _configure_logging(log_file: Path) -> None:
    """控制台 + UTF-8 日志文件（同一套格式与时间戳）。"""
    LOG.setLevel(logging.INFO)
    LOG.handlers.clear()
    LOG.propagate = False
    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setLevel(logging.INFO)
    fh.setFormatter(fmt)
    LOG.addHandler(fh)
    sh = logging.StreamHandler(sys.stdout)
    sh.setLevel(logging.INFO)
    sh.setFormatter(fmt)
    LOG.addHandler(sh)


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="RAGAS Faithfulness 网格热力图（Top-K × 子块窗口 W）")
    p.add_argument(
        "--csv",
        default="RAG DATA_INTRO - Copy of test.csv",
        help="评测 CSV",
    )
    p.add_argument(
        "--question-col",
        default="question",
        help="问题列名（默认 question；旧表可用「问题」）",
    )
    p.add_argument(
        "--ref-col",
        default="",
        help="参考文本列（Faithfulness 可选）；默认自动：回答要点总结 → answer_key → answer",
    )
    p.add_argument("--limit", type=int, default=3, help="仅用前 N 条；0 表示全部（默认 15）")
    p.add_argument(
        "--skip-empty-ref",
        action="store_true",
        help="跳过「回答要点总结」与「回答」均为空的行",
    )
    p.add_argument(
        "--topk",
        type=int,
        nargs="+",
        default=TOP_K_LIST_DEFAULT,
        help="Top-K 列表，默认 3 5 7 10",
    )
    p.add_argument(
        "--windows",
        type=int,
        nargs="+",
        default=WINDOW_SIZES_DEFAULT,
        help="子叶子窗口 W（字符尺度，需对应入库集合），默认 300 400 500",
    )
    p.add_argument(
        "--out-dir",
        type=Path,
        default=ROOT / "ragas_faithfulness_grid_out",
        help="输出目录（CSV + PNG + 默认运行日志）",
    )
    p.add_argument(
        "--log-file",
        type=Path,
        default=None,
        help="运行日志路径；默认写入 out-dir 下 faithfulness_run_<时间戳>.log",
    )
    p.add_argument("--show", action="store_true", help="保存图像后在窗口中显示（默认仅保存 PNG）")
    p.add_argument(
        "--plot-only",
        action="store_true",
        help="不跑评测，仅从 out-dir 下的 faithfulness_matrix.csv 重画热力图（含 faithfulness_heatmap.png）",
    )
    return p.parse_args()


def _build_ragas_llm():
    try:
        return build_ragas_instructor_llm()
    except ValueError as e:
        raise SystemExit(str(e))


def _contexts_from_rag_trace(rag_trace: dict | None) -> list[str]:
    if not rag_trace:
        return ["(无 rag_trace)"]
    chunks = rag_trace.get("expanded_retrieved_chunks") or rag_trace.get("retrieved_chunks") or []
    if not isinstance(chunks, list):
        return ["(rag_trace 中无片段列表)"]
    texts: list[str] = []
    for c in chunks:
        if isinstance(c, dict):
            t = str(c.get("text") or "").strip()
            if t:
                texts.append(t)
    return texts if texts else ["(检索片段为空)"]


def _brief_collection_for_window(w: int) -> str | None:
    """返回该窗口对应的 Milvus brief 集合名；None 表示不覆盖。"""
    key = f"MILVUS_BRIEF_W{w}"
    v = os.getenv(key, "").strip()
    if v:
        return v
    base = os.getenv("MILVUS_COLLECTION_BRIEF", "").strip()
    return base if base else None


def _candidate_k_for_final(k: int) -> int:
    """Dense/Sparse 候选数，需不小于最终 Top-K。"""
    return max(k * 3, 24, k + 8)


def _resolve_ref_column(df: pd.DataFrame, preferred: str) -> str:
    """解析 RAGAS reference 列名。"""
    c = (preferred or "").strip()
    if c and c in df.columns:
        return c
    for name in ("回答要点总结", "answer_key", "answer"):
        if name in df.columns:
            return name
    return "answer_key"


def _reference_text(row: pd.Series, col_ref: str) -> str:
    ref = str(row.get(col_ref, "") or "").strip()
    if not ref and col_ref != "回答要点总结" and "回答要点总结" in row.index:
        ref = str(row.get("回答要点总结", "") or "").strip()
    if not ref and col_ref != "answer_key" and "answer_key" in row.index:
        ref = str(row.get("answer_key", "") or "").strip()
    if not ref and "回答" in row.index:
        ref = str(row.get("回答", "") or "").strip()
    return ref


def _build_samples_for_cell(
    df: pd.DataFrame,
    col_q: str,
    col_ref: str,
    skip_empty_ref: bool,
    *,
    cell_label: str,
) -> list[SingleTurnSample]:
    samples: list[SingleTurnSample] = []
    total = len(df)
    for i, (_, row) in enumerate(df.iterrows(), start=1):
        q = str(row.get(col_q, "") or "").strip()
        if not q:
            LOG.info("[%s] 行 %s/%s 跳过：问题为空", cell_label, i, total)
            continue
        ref = _reference_text(row, col_ref)
        if skip_empty_ref and not ref:
            LOG.info("[%s] 行 %s/%s 跳过：参考为空（--skip-empty-ref）", cell_label, i, total)
            continue

        LOG.info("[%s] 行 %s/%s Agent 调用开始 | 问题预览: %s", cell_label, i, total, q[:120].replace("\n", " "))
        session_id = f"ragas_grid_{uuid4().hex}"
        result = chat_with_agent(
            user_text=q,
            user_id="ragas_grid_user",
            session_id=session_id,
        )
        answer = ""
        rag_trace: dict = {}
        if isinstance(result, dict):
            answer = str(result.get("response", "") or "").strip()
            rag_trace = result.get("rag_trace") or {}
        else:
            answer = str(result).strip()

        if not answer or "Retrieved Chunks:" in answer:
            LOG.warning("[%s] 行 %s/%s 跳过：未得到有效 Agent 回复", cell_label, i, total)
            continue

        contexts = _contexts_from_rag_trace(rag_trace if isinstance(rag_trace, dict) else None)
        LOG.info(
            "[%s] 行 %s/%s 完成 | 上下文片段数=%s | 回复长度=%s",
            cell_label,
            i,
            total,
            len(contexts),
            len(answer),
        )

        samples.append(
            SingleTurnSample(
                user_input=q,
                retrieved_contexts=contexts,
                response=answer,
                reference=ref,
            )
        )
    return samples


def _faithfulness_mean(result) -> float:
    """从 RAGAS EvaluationResult 取 faithfulness 列均值。"""
    key = "faithfulness"
    if key not in result.scores[0]:
        # 兼容大小写 / 命名差异
        keys = list(result.scores[0].keys())
        cand = [k for k in keys if "faith" in k.lower()]
        if not cand:
            raise RuntimeError(f"结果中无 faithfulness 列，仅有: {keys}")
        key = cand[0]
    vals = [row.get(key) for row in result.scores]
    arr = np.array([v for v in vals if v is not None and not (isinstance(v, float) and np.isnan(v))], dtype=float)
    if arr.size == 0:
        return float("nan")
    return float(np.nanmean(arr))


def _load_faithfulness_matrix_csv(path: Path) -> tuple[np.ndarray, list[int], list[int]]:
    """读取 faithfulness_matrix.csv（index 形如 w300，列为 Top-K）。"""
    df = pd.read_csv(path, index_col=0, encoding="utf-8-sig")
    for col in df.columns:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    def _parse_w(idx: object) -> int:
        s = str(idx).strip()
        if s.lower().startswith("w"):
            return int(s[1:])
        return int(float(s))

    row_labels = [_parse_w(ix) for ix in df.index]
    col_labels = [int(float(str(c).strip())) for c in df.columns]
    matrix = df.to_numpy(dtype=float)
    return matrix, row_labels, col_labels


def _plot_heatmap(
    matrix: np.ndarray,
    row_labels: list[int],
    col_labels: list[int],
    out_path: Path,
    *,
    show: bool,
) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.colors import Normalize

    fig, ax = plt.subplots(figsize=(8, 4.5))
    fig.patch.set_facecolor("white")
    ax.set_facecolor("white")

    finite = matrix[~np.isnan(matrix)]
    if finite.size == 0:
        mn, mx = 0.0, 1.0
    else:
        mn, mx = float(np.nanmin(matrix)), float(np.nanmax(matrix))
        if mn == mx:
            mn, mx = mn - 0.01, mx + 0.01

    # 冷酷蓝：低分浅蓝白、高分深蓝（标注按格内真实 RGB 亮度选深字/浅字）
    cmap = plt.cm.Blues
    norm = Normalize(vmin=mn, vmax=mx)
    im = ax.imshow(matrix, cmap=cmap, norm=norm, aspect="equal")

    def _text_color_for_cell(val: float) -> str:
        """按格内实际着色亮度：浅底黑字、深底白字。"""
        if val != val:
            return "#0d47a1"
        rgba = cmap(norm(float(val)))
        lum = float(np.dot(np.asarray(rgba[:3], dtype=float), [0.299, 0.587, 0.114]))
        return "#0a0a0a" if lum >= 0.52 else "#f5f9ff"

    for i in range(matrix.shape[0]):
        for j in range(matrix.shape[1]):
            v = matrix[i, j]
            txt = "nan" if v != v else f"{v:.2f}"
            color = _text_color_for_cell(v)
            ax.text(
                j,
                i,
                txt,
                ha="center",
                va="center",
                color=color,
                fontsize=11,
                fontweight="medium",
            )

    ax.set_xticks(np.arange(len(col_labels)))
    ax.set_xticklabels([str(x) for x in col_labels])
    ax.set_yticks(np.arange(len(row_labels)))
    ax.set_yticklabels([str(x) for x in row_labels])
    ax.set_xlabel(r"Top-$K$")
    ax.set_ylabel(r"Window Size ($W$)")
    ax.set_title("Interaction: Chunk Size vs. $K$ (Faithfulness)")

    ax.set_xticks(np.arange(-0.5, len(col_labels), 1), minor=True)
    ax.set_yticks(np.arange(-0.5, len(row_labels), 1), minor=True)
    # 浅格上用略深蓝线、深格上线条仍勉强可辨（避免纯白领地上消失）
    ax.grid(which="minor", color="#90caf9", linestyle="-", linewidth=1.8)
    ax.tick_params(which="minor", bottom=False, left=False)

    cbar = fig.colorbar(im, ax=ax, fraction=0.046, pad=0.04)
    cbar.set_label("Faithfulness Score")

    fig.tight_layout()
    fig.savefig(out_path, dpi=160, bbox_inches="tight")
    LOG.info("已保存热力图: %s", out_path)
    if show:
        plt.show()
    plt.close(fig)


def main() -> int:
    args = _parse_args()
    out_dir = args.out_dir.expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.plot_only:
        matrix_path = out_dir / "faithfulness_matrix.csv"
        if not matrix_path.is_file():
            raise SystemExit(f"--plot-only 需要矩阵文件: {matrix_path}")
        matrix, windows, topks = _load_faithfulness_matrix_csv(matrix_path)
        png_path = out_dir / "faithfulness_heatmap.png"
        try:
            _plot_heatmap(matrix, windows, topks, png_path, show=args.show)
        except ImportError:
            raise SystemExit("未安装 matplotlib，请: uv pip install matplotlib") from None
        print(f"热力图已写入: {png_path}", file=sys.stderr)
        return 0

    _cp = Path(args.csv)
    csv_path = _cp.resolve() if _cp.is_absolute() else (ROOT / _cp).resolve()
    if not csv_path.is_file():
        raise SystemExit(f"找不到 CSV: {csv_path}")

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = (args.log_file.expanduser().resolve() if args.log_file else out_dir / f"faithfulness_run_{ts}.log")
    _configure_logging(log_path)
    LOG.info("运行日志文件: %s", log_path)
    LOG.info("命令行等价: %s", " ".join(sys.argv))

    df_all = pd.read_csv(csv_path, encoding="utf-8")
    col_q = args.question_col.strip()
    if col_q not in df_all.columns:
        if col_q == "question" and "问题" in df_all.columns:
            col_q = "问题"
            LOG.info("CSV 无「question」列，改用「问题」列。")
        else:
            raise SystemExit(f"CSV 缺少问题列「{col_q}」，实际列: {list(df_all.columns)}")
    col_ref = _resolve_ref_column(df_all, args.ref_col)
    LOG.info("使用参考列: %s", col_ref)

    if args.limit and args.limit > 0:
        df_all = df_all.head(args.limit)

    ragas_llm = _build_ragas_llm()
    metric = Faithfulness(llm=ragas_llm)

    windows = list(args.windows)
    topks = list(args.topk)
    matrix = np.full((len(windows), len(topks)), np.nan, dtype=float)
    rows_log: list[dict] = []

    LOG.info(
        "网格 W=%s Top-K=%s | 样本行数=%s | 输出目录=%s | CSV=%s",
        windows,
        topks,
        len(df_all),
        out_dir,
        csv_path,
    )

    for wi, w in enumerate(windows):
        brief_col = _brief_collection_for_window(w)
        if brief_col:
            LOG.info("[W=%s] Milvus brief 集合: %s", w, brief_col)
        else:
            LOG.info(
                "[W=%s] 未设置 MILVUS_BRIEF_W%s / MILVUS_COLLECTION_BRIEF，使用 MilvusManager 默认 brief",
                w,
                w,
            )

        for ki, k in enumerate(topks):
            cfg = {
                "think_mode": "normal",
                "final_top_k": int(k),
                "candidate_k": _candidate_k_for_final(int(k)),
                # RAG 图：不打分、不重写查询，retrieve_initial 后直接生成答案（见 rag_pipeline.grade_documents_node）
                "skip_grade_and_rewrite": True,
            }
            if brief_col:
                cfg["milvus_collection_brief"] = brief_col
            set_rag_config(cfg)

            label = f"W={w},K={k}"
            LOG.info("---------- 单元 %s | final_top_k=%s candidate_k=%s ----------", label, k, cfg.get("candidate_k"))

            samples = _build_samples_for_cell(
                df_all,
                col_q,
                col_ref,
                args.skip_empty_ref,
                cell_label=label,
            )
            if not samples:
                LOG.warning("[%s] 无可用样本，跳过 Faithfulness。", label)
                rows_log.append(
                    {"window_w": w, "top_k": k, "mean_faithfulness": None, "n_samples": 0}
                )
                continue

            dataset = EvaluationDataset(samples=samples)
            LOG.info("[%s] RAGAS Faithfulness 评测开始（样本数=%s）", label, len(samples))
            try:
                result = evaluate(
                    dataset,
                    metrics=[metric],
                    llm=ragas_llm,
                    embeddings=None,
                    raise_exceptions=False,
                    show_progress=True,
                )
            except Exception:
                LOG.exception("[%s] RAGAS evaluate 失败", label)
                raise
            mean_f = _faithfulness_mean(result)
            matrix[wi, ki] = mean_f
            rows_log.append(
                {
                    "window_w": w,
                    "top_k": k,
                    "mean_faithfulness": mean_f,
                    "n_samples": len(samples),
                }
            )
            LOG.info("[%s] Faithfulness 均值 = %.4f (n=%s)", label, mean_f, len(samples))

            # 附加保存该单元详细分（可选）
            detail_path = out_dir / f"scores_W{w}_K{k}.json"
            try:
                with open(detail_path, "w", encoding="utf-8") as f:
                    json.dump(result.scores, f, ensure_ascii=False, indent=2)
                LOG.info("[%s] 逐条分已写入 %s", label, detail_path)
            except Exception as e:
                LOG.warning("写入 %s 失败: %s", detail_path, e)

    set_rag_config({})

    df_out = pd.DataFrame(rows_log)
    csv_path_out = out_dir / "faithfulness_grid_summary.csv"
    df_out.to_csv(csv_path_out, index=False, encoding="utf-8-sig")
    LOG.info("已写入汇总: %s", csv_path_out)

    matrix_df = pd.DataFrame(matrix, index=[f"w{w}" for w in windows], columns=[str(k) for k in topks])
    matrix_df.to_csv(out_dir / "faithfulness_matrix.csv", encoding="utf-8-sig")
    LOG.info("已写入矩阵: %s", out_dir / "faithfulness_matrix.csv")

    png_path = out_dir / "faithfulness_heatmap.png"
    try:
        _plot_heatmap(matrix, windows, topks, png_path, show=args.show)
    except ImportError:
        LOG.error("未安装 matplotlib，跳过作图。可执行: uv pip install matplotlib")
        return 1

    LOG.info("全部完成。日志: %s", log_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
