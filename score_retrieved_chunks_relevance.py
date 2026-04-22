#!/usr/bin/env python3
"""
读取 langsmith_eval_retrieved_chunks.csv（或指定路径），用 .env 中的 MODEL 逐行判断
「text」是否与「question」相关（用于 RAG 召回相关性 / 近似召回率标注）。

规则：
  - 相关：score 列写 1；不相关：score 列写 0。
  - 首次运行会把原 score（如 rerank 分数）备份到 retrieval_score 列（若尚无该列），再覆盖 score。
  - text 为空或为「(无召回记录)」时直接 score=0，不调模型。
  - 模型调用失败时该行 score=-1，便于事后重跑。

依赖：pandas、langchain-openai、python-dotenv。

用法（仓库根目录）：
  uv pip install pandas   # 若尚未安装
  uv run python score_retrieved_chunks_relevance.py
  uv run python score_retrieved_chunks_relevance.py -i langsmith_eval_retrieved_chunks.csv -o out.csv
  uv run python score_retrieved_chunks_relevance.py --start 0 --limit 100
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="用 MODEL 标注召回 text 与 question 是否相关，score 写 0/1")
    p.add_argument(
        "-i",
        "--input",
        type=Path,
        default=ROOT / "langsmith_eval_retrieved_chunks.csv",
        help="输入 CSV",
    )
    p.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        help="输出 CSV（默认：输入文件名加 _scored）",
    )
    p.add_argument("--start", type=int, default=0, help="从第几行开始（0-based，不含表头）")
    p.add_argument("--limit", type=int, default=0, help="最多处理多少行；0 表示全部")
    p.add_argument("--sleep", type=float, default=0.0, help="每行请求后休眠秒数，防限流")
    p.add_argument("--text-max", type=int, default=12000, help="送入模型的 text 最大字符数")
    return p.parse_args()


def _build_llm():
    from langchain_openai import ChatOpenAI

    api_key = os.getenv("ARK_API_KEY", "").strip()
    model = os.getenv("MODEL", "").strip()
    base_url = os.getenv("BASE_URL", "").strip() or None
    if not api_key or not model:
        raise SystemExit("请在 .env 中配置 ARK_API_KEY 与 MODEL")
    return ChatOpenAI(
        model=model,
        api_key=api_key,
        base_url=base_url,
        temperature=0.0,
        timeout=120,
    )


def _drop_duplicate_score_columns(df):
    """pandas 读入重复表头 score 时会产生 score、score.1 等，保留 score，删掉 score.*。"""
    df = df.copy()
    for c in list(df.columns):
        if c.startswith("score.") and c != "score":
            df.drop(columns=[c], inplace=True, errors="ignore")
    return df


def _strip_json_fence(text: str) -> str:
    s = (text or "").strip()
    if s.startswith("```"):
        s = re.sub(r"^```\w*\s*", "", s)
        s = re.sub(r"\s*```$", "", s).strip()
    return s


def _relevant_value_to_bool(v) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().lower() in ("true", "yes", "1", "是")
    return False


def _parse_relevant_from_reply(content: str) -> bool:
    """从模型回复中解析 relevant；兼容豆包等不支持 json_schema 的接口。"""
    raw = _strip_json_fence(content)
    data = None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        m = re.search(r"\{[^{}]*\"relevant\"[^{}]*\}", raw, re.DOTALL)
        if m:
            data = json.loads(m.group(0))
    if isinstance(data, dict):
        return _relevant_value_to_bool(data.get("relevant"))
    return False


def _judge_one(llm, question: str, text: str, text_max: int) -> int:
    from langchain_core.messages import HumanMessage

    snippet = (text or "").strip()
    if not snippet or snippet == "(无召回记录)":
        return 0
    if len(snippet) > text_max:
        snippet = snippet[:text_max] + "\n…(以下已截断)"

    prompt = (
        "你是检索相关性评判员。判断「资料片段」是否有助于回答「用户问题」。\n"
        "只要片段中出现与问题主题直接相关的事实、定义、建议或可支撑回答的内容，即判为相关。\n"
        "片段仅为弱相关广告、完全无关话题、或与问题无关的泛泛而谈，判为不相关。\n\n"
        f"用户问题：\n{question.strip()}\n\n"
        f"资料片段：\n{snippet}\n\n"
        "只输出一行 JSON，格式严格为：{\"relevant\": true} 或 {\"relevant\": false}。"
        "不要输出其它解释或 markdown。"
    )
    resp = llm.invoke([HumanMessage(content=prompt)])
    content = getattr(resp, "content", None) or str(resp)
    try:
        return 1 if _parse_relevant_from_reply(content) else 0
    except (json.JSONDecodeError, TypeError, ValueError):
        # 再试：整段里找 true/false
        low = content.strip().lower()
        if '"relevant": true' in low or "'relevant': true" in low:
            return 1
        if '"relevant": false' in low or "'relevant': false" in low:
            return 0
        raise


def main() -> int:
    try:
        import pandas as pd
    except ImportError:
        print("请先安装 pandas: uv pip install pandas", file=sys.stderr)
        return 1

    args = _parse_args()
    inp = args.input.expanduser().resolve()
    if not inp.is_file():
        raise SystemExit(f"找不到输入文件: {inp}")

    out = args.output
    if out is None:
        out = inp.parent / f"{inp.stem}_scored{inp.suffix}"
    else:
        out = out.expanduser().resolve()

    df = pd.read_csv(inp, encoding="utf-8-sig", dtype=str, keep_default_na=False)
    df = _drop_duplicate_score_columns(df)

    for col in ("question", "text", "score"):
        if col not in df.columns:
            raise SystemExit(f"CSV 缺少必需列: {col}，当前列: {list(df.columns)}")

    if "retrieval_score" not in df.columns:
        df["retrieval_score"] = df["score"].astype(str)

    # dtype=str 时 score 为 string[pyarrow]，不能写入 int；改为 object 以写入 0/1/-1
    df["score"] = df["score"].astype(object)

    n = len(df)
    start = max(0, args.start)
    end = n if args.limit <= 0 else min(n, start + args.limit)
    row_indices = range(start, end)

    llm = _build_llm()

    print(f"输入: {inp}\n输出: {out}\n处理行索引: {start}..{end - 1}（共 {len(row_indices)} 行）\n")

    done = 0
    for i in row_indices:
        q = str(df.at[i, "question"])
        t = str(df.at[i, "text"])
        try:
            score = _judge_one(llm, q, t, args.text_max)
        except Exception as e:
            print(f"[行 {i}] 模型调用失败，score=-1: {e}", file=sys.stderr)
            score = -1
        df.at[i, "score"] = score
        done += 1
        print(f"[行 {i}] score={score}  进度 {done}/{len(row_indices)}")
        if args.sleep > 0:
            time.sleep(args.sleep)

    df.to_csv(out, index=False, encoding="utf-8-sig")
    print(f"\n已写入: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
