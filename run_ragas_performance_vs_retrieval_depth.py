#!/usr/bin/env python3
"""
固定子块窗口 W，扫描多个 Top-K（检索深度，**默认 K=2,4,6,8,10**），对评测集跑 RAGAS **Faithfulness** 与 **Context Recall**，
并输出论文式双轴折线图（左轴 Faithfulness、右轴 Context Recall）。**默认每轮取 CSV 前 3 条问题**（`--limit 3`）。

流程与 `run_ragas_faithfulness_heatmap.py` 一致：`set_rag_config` 打开 `skip_grade_and_rewrite`，
每格用 `chat_with_agent` 取答案与 `rag_trace` 片段构造 `SingleTurnSample`，再 `ragas.evaluate`。

Milvus brief 集合：与 heatmap 相同，`MILVUS_BRIEF_W{W}` → `MILVUS_COLLECTION_BRIEF` → 默认 brief。

- **日志**（独立目录，默认 **`ragas_retrieval_depth_logs/`**）：`retrieval_depth_run_<时间戳>.log`（可用 `--log-dir` / `--log-file` 改）
- **数据与图**（默认仍与 heatmap 同目录 **`ragas_faithfulness_grid_out/`**）：
  - `retrieval_depth_grid_summary.csv`、`scores_W{W}_K{k}.json`、`retrieval_depth_vs_k.png`

依赖：ragas、pandas、matplotlib、openai、HF 嵌入（与 `run_ragas_eval.py` 一致）。

用法示例：
  uv run python run_ragas_performance_vs_retrieval_depth.py --window 300
  uv run python run_ragas_performance_vs_retrieval_depth.py --plot-only  # 从 retrieval_depth_grid_summary.csv 重画
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
from ragas.embeddings import HuggingFaceEmbeddings
from ragas.metrics._context_recall import ContextRecall
from ragas.metrics._faithfulness import Faithfulness

from ragas_llm_compat import build_ragas_instructor_llm

_agent = importlib.import_module("agent")
chat_with_agent = _agent.chat_with_agent
_tools = importlib.import_module("tools")
set_rag_config = _tools.set_rag_config

LOG = logging.getLogger("ragas_perf_vs_k")


def _configure_logging(log_file: Path) -> None:
    LOG.setLevel(logging.INFO)
    LOG.handlers.clear()
    LOG.propagate = False
    fmt = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setLevel(logging.INFO)
    fh.setFormatter(fmt)
    LOG.addHandler(fh)
    sh = logging.StreamHandler(sys.stdout)
    sh.setLevel(logging.INFO)
    sh.setFormatter(fmt)
    LOG.addHandler(sh)


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="RAGAS Faithfulness + Context Recall vs 检索深度 K（固定窗口 W）"
    )
    p.add_argument(
        "--csv",
        default="RAG DATA_INTRO - Copy of test.csv",
        help="评测 CSV（与 heatmap 默认相同）",
    )
    p.add_argument(
        "--question-col",
        default="question",
        help="问题列名（默认 question；旧表可用「问题」）",
    )
    p.add_argument(
        "--ref-col",
        default="",
        help="参考文本列；默认自动：回答要点总结 → answer_key → answer",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=3,
        help="每轮仅用前 N 条问题；默认 3。0 表示全部 CSV",
    )
    p.add_argument(
        "--skip-empty-ref",
        action="store_true",
        help="跳过「回答要点总结」与「回答」均为空的行",
    )
    p.add_argument(
        "--window",
        type=int,
        default=300,
        help="子叶子窗口 W（默认 300 与 heatmap 第一档一致）",
    )
    p.add_argument(
        "--topk",
        type=int,
        nargs="+",
        default=[2, 4, 6, 8, 10],
        help="检索深度 K 列表（默认 2 4 6 8 10）",
    )
    p.add_argument(
        "--out-dir",
        type=Path,
        default=ROOT / "ragas_faithfulness_grid_out",
        help="输出目录（默认与 heatmap 相同）",
    )
    p.add_argument(
        "--out-png",
        type=Path,
        default=None,
        help="PNG 路径（默认 out-dir/retrieval_depth_vs_k.png）",
    )
    p.add_argument(
        "--out-csv",
        type=Path,
        default=None,
        help="汇总 CSV（默认 out-dir/retrieval_depth_grid_summary.csv）",
    )
    p.add_argument(
        "--log-dir",
        type=Path,
        default=ROOT / "ragas_retrieval_depth_logs",
        help="运行日志目录（默认项目下 ragas_retrieval_depth_logs/，与数据输出目录分离）",
    )
    p.add_argument(
        "--log-file",
        type=Path,
        default=None,
        help="若指定则直接作为日志文件路径（忽略 --log-dir）；否则在 log-dir 下写入 retrieval_depth_run_<时间戳>.log",
    )
    p.add_argument("--show", action="store_true", help="保存图像后在窗口中显示（默认仅保存 PNG）")
    p.add_argument(
        "--plot-only",
        action="store_true",
        help="不跑评测，仅从 out-dir 下 retrieval_depth_grid_summary.csv 重画 PNG（可用 --out-csv 覆盖）",
    )
    p.add_argument(
        "--auto-y",
        action="store_true",
        help="纵轴按数据自适应（忽略默认 --ylim-left / --ylim-right）",
    )
    p.add_argument(
        "--ylim-left",
        type=float,
        nargs=2,
        default=(0.65, 0.85),
        metavar=("LOW", "HIGH"),
        help="左轴 Faithfulness y 范围（默认 0.65 0.85）",
    )
    p.add_argument(
        "--ylim-right",
        type=float,
        nargs=2,
        default=(0.4, 0.8),
        metavar=("LOW", "HIGH"),
        help="右轴 Context Recall y 范围（默认 0.4 0.8）",
    )
    p.add_argument(
        "--y-tick-step-left",
        type=float,
        default=0.05,
        help="左轴主刻度间隔（默认 0.05）",
    )
    p.add_argument(
        "--y-tick-step-right",
        type=float,
        default=0.1,
        help="右轴主刻度间隔（默认 0.1）",
    )
    return p.parse_args()


def _build_ragas_llm():
    try:
        return build_ragas_instructor_llm()
    except ValueError as e:
        raise SystemExit(str(e))


def _build_embeddings() -> HuggingFaceEmbeddings:
    model = os.getenv("EMBEDDING_MODEL", "BAAI/bge-m3")
    device = os.getenv("EMBEDDING_DEVICE", "cpu")
    return HuggingFaceEmbeddings(
        model=model,
        device=device,
        normalize_embeddings=True,
    )


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
    key = f"MILVUS_BRIEF_W{w}"
    v = os.getenv(key, "").strip()
    if v:
        return v
    base = os.getenv("MILVUS_COLLECTION_BRIEF", "").strip()
    return base if base else None


def _candidate_k_for_final(k: int) -> int:
    return max(k * 3, 24, k + 8)


def _resolve_ref_column(df: pd.DataFrame, preferred: str) -> str:
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


def _build_samples(
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

        LOG.info(
            "[%s] 行 %s/%s Agent 调用开始 | 问题预览: %s",
            cell_label,
            i,
            total,
            q[:120].replace("\n", " "),
        )
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


def _nanmean_metric_key(result, key_substrings: tuple[str, ...]) -> float:
    """从 result.scores 中取与 key_substrings 之一最匹配的列名并 nanmean。"""
    keys = list(result.scores[0].keys())
    key = None
    for sub in key_substrings:
        sub_l = sub.lower()
        for k in keys:
            if sub_l in k.lower().replace(" ", "_"):
                key = k
                break
        if key:
            break
    if not key:
        raise RuntimeError(f"结果中无匹配列 {key_substrings}，仅有: {keys}")
    vals = [row.get(key) for row in result.scores]
    arr = np.array(
        [v for v in vals if v is not None and not (isinstance(v, float) and np.isnan(v))],
        dtype=float,
    )
    if arr.size == 0:
        return float("nan")
    return float(np.nanmean(arr))


def _finite_series(values: list[float]) -> np.ndarray:
    a = np.asarray(values, dtype=float)
    return a[np.isfinite(a)]


def _autoscale_ylim(values: list[float]) -> tuple[float, float]:
    """按数据 min/max 加边距，避免折线贴边。"""
    a = _finite_series(values)
    if a.size == 0:
        return (0.0, 1.0)
    mn, mx = float(np.min(a)), float(np.max(a))
    if mn == mx:
        d = max(abs(mn) * 0.08, 0.04) if mn != 0 else 0.05
        return (mn - d, mx + d)
    span = mx - mn
    pad = max(span * 0.15, 0.03)
    return (mn - pad, mx + pad)


def _plot_dual_axis(
    ks: list[int],
    faith: list[float],
    recall: list[float],
    window: int,
    out_path: Path,
    *,
    ylim_left: tuple[float, float] | None,
    ylim_right: tuple[float, float] | None,
    y_tick_step_left: float,
    y_tick_step_right: float,
    show: bool,
) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.ticker import MaxNLocator, MultipleLocator

    # 偏窄版面，接近论文图比例；上边框去掉（与参考图一致）
    fig, ax1 = plt.subplots(figsize=(6.2, 4.5))
    fig.patch.set_facecolor("white")
    ax1.set_facecolor("white")

    (line_faith,) = ax1.plot(
        ks,
        faith,
        "-o",
        color="black",
        linewidth=1.8,
        markersize=8,
        markerfacecolor="black",
        markeredgecolor="black",
        clip_on=False,
        label="Faithfulness",
    )
    ax1.set_xlabel(r"Retrieval Depth ($K$)")
    ax1.set_ylabel("Faithfulness")
    if ylim_left is not None:
        ax1.set_ylim(*ylim_left)
        if y_tick_step_left > 0:
            ax1.yaxis.set_major_locator(MultipleLocator(y_tick_step_left))
    else:
        ax1.set_ylim(*_autoscale_ylim(faith))
        ax1.yaxis.set_major_locator(MaxNLocator(nbins=8))
    ax1.set_xticks(ks)
    ax1.tick_params(axis="y")
    ax1.grid(False)
    ax1.spines["top"].set_visible(False)

    ax2 = ax1.twinx()
    (line_recall,) = ax2.plot(
        ks,
        recall,
        "--s",
        color="grey",
        linewidth=1.8,
        markersize=7,
        markerfacecolor="grey",
        markeredgecolor="grey",
        clip_on=False,
        label="Context Recall",
    )
    ax2.set_ylabel("Context Recall")
    if ylim_right is not None:
        ax2.set_ylim(*ylim_right)
        if y_tick_step_right > 0:
            ax2.yaxis.set_major_locator(MultipleLocator(y_tick_step_right))
    else:
        ax2.set_ylim(*_autoscale_ylim(recall))
        ax2.yaxis.set_major_locator(MaxNLocator(nbins=8))
    ax2.tick_params(axis="y")
    ax2.grid(False)
    ax2.spines["top"].set_visible(False)

    ax1.set_title(rf"Performance vs. Retrieval Depth ($W = {window}$)", pad=10)
    # 图例在绘图区右上角，不压标题与轴标签（纵轴范围保持 CLI 默认）
    ax1.legend(
        [line_faith, line_recall],
        [line_faith.get_label(), line_recall.get_label()],
        loc="upper right",
        bbox_to_anchor=(0.99, 0.97),
        bbox_transform=ax1.transAxes,
        ncol=2,
        frameon=True,
        fontsize=7,
        columnspacing=0.9,
        handletextpad=0.35,
        borderaxespad=0.35,
    )
    fig.tight_layout(rect=(0, 0, 1, 0.92))
    fig.savefig(out_path, dpi=160, bbox_inches="tight", facecolor="white")
    LOG.info("已保存折线图: %s", out_path)
    if show:
        plt.show()
    plt.close(fig)


def main() -> int:
    args = _parse_args()
    out_dir = args.out_dir.expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    summary_csv = (
        args.out_csv.expanduser().resolve()
        if args.out_csv
        else out_dir / "retrieval_depth_grid_summary.csv"
    )
    out_png = (
        args.out_png.expanduser().resolve()
        if args.out_png
        else out_dir / "retrieval_depth_vs_k.png"
    )

    if args.plot_only:
        if not summary_csv.is_file():
            raise SystemExit(f"--plot-only 需要汇总 CSV: {summary_csv}")
        df = pd.read_csv(summary_csv, encoding="utf-8-sig")
        w = int(df["window_w"].iloc[0])
        ks = [int(x) for x in df["top_k"].tolist()]
        faith = [float(x) for x in df["mean_faithfulness"].tolist()]
        recall = [float(x) for x in df["mean_context_recall"].tolist()]
        try:
            _plot_dual_axis(
                ks,
                faith,
                recall,
                w,
                out_png,
                ylim_left=None if args.auto_y else tuple(args.ylim_left),
                ylim_right=None if args.auto_y else tuple(args.ylim_right),
                y_tick_step_left=args.y_tick_step_left,
                y_tick_step_right=args.y_tick_step_right,
                show=args.show,
            )
        except ImportError:
            raise SystemExit("请安装 matplotlib: uv pip install matplotlib") from None
        print(f"折线图已写入: {out_png}", file=sys.stderr)
        return 0

    _cp = Path(args.csv)
    csv_path = _cp.resolve() if _cp.is_absolute() else (ROOT / _cp).resolve()
    if not csv_path.is_file():
        raise SystemExit(f"找不到 CSV: {csv_path}")

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    if args.log_file:
        log_path = args.log_file.expanduser().resolve()
        log_path.parent.mkdir(parents=True, exist_ok=True)
    else:
        log_dir = args.log_dir.expanduser().resolve()
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / f"retrieval_depth_run_{ts}.log"
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

    w = int(args.window)
    topks = [int(k) for k in args.topk]
    brief_col = _brief_collection_for_window(w)
    if brief_col:
        LOG.info("[W=%s] Milvus brief 集合: %s", w, brief_col)
    else:
        LOG.info(
            "[W=%s] 未设置 MILVUS_BRIEF_W%s / MILVUS_COLLECTION_BRIEF，使用 MilvusManager 默认 brief",
            w,
            w,
        )

    ragas_llm = _build_ragas_llm()
    embeddings = _build_embeddings()
    metrics = [
        Faithfulness(llm=ragas_llm),
        ContextRecall(llm=ragas_llm),
    ]

    rows: list[dict] = []
    faith_series: list[float] = []
    recall_series: list[float] = []

    LOG.info(
        "固定 W=%s | Top-K=%s | 样本行数=%s | 输出目录=%s | CSV=%s",
        w,
        topks,
        len(df_all),
        out_dir,
        csv_path,
    )

    for k in topks:
        cfg = {
            "think_mode": "normal",
            "final_top_k": k,
            "candidate_k": _candidate_k_for_final(k),
            "skip_grade_and_rewrite": True,
        }
        if brief_col:
            cfg["milvus_collection_brief"] = brief_col
        set_rag_config(cfg)

        label = f"W={w},K={k}"
        LOG.info("---------- 单元 %s | final_top_k=%s candidate_k=%s ----------", label, k, cfg.get("candidate_k"))
        samples = _build_samples(df_all, col_q, col_ref, args.skip_empty_ref, cell_label=label)
        if not samples:
            LOG.warning("[%s] 无可用样本，跳过 RAGAS。", label)
            rows.append(
                {
                    "window_w": w,
                    "top_k": k,
                    "n_samples": 0,
                    "mean_faithfulness": float("nan"),
                    "mean_context_recall": float("nan"),
                }
            )
            faith_series.append(float("nan"))
            recall_series.append(float("nan"))
            continue

        dataset = EvaluationDataset(samples=samples)
        LOG.info("[%s] RAGAS 评测开始（Faithfulness + Context Recall，样本数=%s）", label, len(samples))
        result = evaluate(
            dataset,
            metrics=metrics,
            llm=ragas_llm,
            embeddings=embeddings,
            raise_exceptions=False,
            show_progress=True,
        )
        mf = _nanmean_metric_key(result, ("faithfulness",))
        mr = _nanmean_metric_key(result, ("context_recall", "context recall"))
        faith_series.append(mf)
        recall_series.append(mr)
        rows.append(
            {
                "window_w": w,
                "top_k": k,
                "n_samples": len(samples),
                "mean_faithfulness": mf,
                "mean_context_recall": mr,
            }
        )
        LOG.info("[%s] Faithfulness 均值 = %.4f (n=%s)", label, mf, len(samples))
        LOG.info("[%s] Context Recall 均值 = %.4f (n=%s)", label, mr, len(samples))

        detail_path = out_dir / f"scores_W{w}_K{k}.json"
        try:
            with open(detail_path, "w", encoding="utf-8") as f:
                json.dump(result.scores, f, ensure_ascii=False, indent=2)
            LOG.info("[%s] 逐条分已写入 %s", label, detail_path)
        except OSError as e:
            LOG.warning("写入 %s 失败: %s", detail_path, e)

    set_rag_config({})

    df_out = pd.DataFrame(rows)
    df_out.to_csv(summary_csv, index=False, encoding="utf-8-sig")
    LOG.info("已写入汇总: %s", summary_csv)

    try:
        _plot_dual_axis(
            topks,
            faith_series,
            recall_series,
            w,
            out_png,
            ylim_left=None if args.auto_y else tuple(args.ylim_left),
            ylim_right=None if args.auto_y else tuple(args.ylim_right),
            y_tick_step_left=args.y_tick_step_left,
            y_tick_step_right=args.y_tick_step_right,
            show=args.show,
        )
    except ImportError:
        LOG.error("未安装 matplotlib，跳过作图。可执行: uv pip install matplotlib")
        return 1

    LOG.info("全部完成。日志: %s", log_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
