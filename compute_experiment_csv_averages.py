#!/usr/bin/env python3
"""
读取 LangSmith / 评测导出的实验 CSV，计算若干指标列的算术平均值（忽略非数字与空值）。

默认输入：仓库根目录下的「med experiment3-bf9b3dae.csv」。

用法：
  uv run python compute_experiment_csv_averages.py
  uv run python compute_experiment_csv_averages.py -i "other.csv"
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent

METRICS = ("latency", "answer_relevance", "correctness", "hallucination")


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="计算实验 CSV 中 latency 等列的平均值")
    p.add_argument(
        "-i",
        "--input",
        type=Path,
        default=ROOT / "med experiment3-bf9b3dae.csv",
        help="输入 CSV 路径",
    )
    return p.parse_args()


def main() -> int:
    try:
        import pandas as pd
    except ImportError:
        print("请先安装 pandas: uv pip install pandas", file=sys.stderr)
        return 1

    args = _parse_args()
    inp = args.input.expanduser().resolve()
    if not inp.is_file():
        print(f"找不到文件: {inp}", file=sys.stderr)
        return 1

    df = pd.read_csv(inp, encoding="utf-8-sig")
    missing = [c for c in METRICS if c not in df.columns]
    if missing:
        print(f"CSV 缺少列: {missing}；现有列: {list(df.columns)}", file=sys.stderr)
        return 1

    print(f"文件: {inp}")
    print(f"总行数: {len(df)}")
    print()
    for col in METRICS:
        s = pd.to_numeric(df[col], errors="coerce")
        n = int(s.notna().sum())
        if n == 0:
            print(f"{col}: 无有效数值，无法求平均")
            continue
        mean = float(s.mean())
        print(f"{col}: 平均值 = {mean:.6g}  （有效行数 {n}）")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
