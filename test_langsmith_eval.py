from typing import Any, Optional
import importlib
import os
import sys
from uuid import uuid4

from dotenv import load_dotenv
from langsmith import evaluate

# 将 backend 路径添加到 sys.path，以便导入你的 Agent 模块
backend_path = os.path.join(os.path.dirname(__file__), "backend")
if backend_path not in sys.path:
    sys.path.append(backend_path)

chat_with_agent = importlib.import_module("agent").chat_with_agent

load_dotenv()


def _extract_answer(outputs: Any) -> str:
    if isinstance(outputs, dict):
        # 优先取真实最终回复字段
        answer = outputs.get("response") or outputs.get("answer") or outputs.get("output")
        return str(answer or "").strip()
    if hasattr(outputs, "outputs") and isinstance(outputs.outputs, dict):
        answer = (
            outputs.outputs.get("response")
            or outputs.outputs.get("answer")
            or outputs.outputs.get("output")
        )
        return str(answer or "").strip()
    return ""


def _extract_reference(reference_outputs: Optional[dict]) -> str:
    if not isinstance(reference_outputs, dict):
        return ""
    for key in ("response", "answer", "output", "expected_answer"):
        value = reference_outputs.get(key)
        if value:
            return str(value).strip()
    return ""

# 1. Select your dataset
dataset_name = "RAG"

# 2. Define an evaluator (评估最终答案，不评估检索块)
def custom_evaluator(run_outputs: dict, reference_outputs: dict) -> bool:
    answer = _extract_answer(run_outputs)
    if not answer:
        return False
    if "Retrieved Chunks:" in answer:
        return False

    reference = _extract_reference(reference_outputs)
    if not reference:
        return True

    # 有参考答案时，至少保证存在一定语义重合（使用字符集合重合率做轻量检查）
    answer_chars = {ch for ch in answer if not ch.isspace()}
    ref_chars = {ch for ch in reference if not ch.isspace()}
    if not answer_chars or not ref_chars:
        return False

    overlap = len(answer_chars & ref_chars) / max(1, len(ref_chars))
    return overlap >= 0.2

# 直接调用你现有的完整 Agent 流程作为评估对象
def target_function(inputs: dict) -> dict:
    question = inputs["question"]
    # 每条评估样本使用独立会话，避免上下文串扰
    session_id = f"langsmith_eval_{uuid4().hex}"
    result = chat_with_agent(
        user_text=question,
        user_id="langsmith_eval_user",
        session_id=session_id,
    )

    response_text = ""
    rag_trace = {}
    if isinstance(result, dict):
        response_text = str(result.get("response", "") or "")
        rag_trace = result.get("rag_trace", {}) or {}
    else:
        response_text = str(result)

    return {
        "response": response_text,
        "rag_trace": rag_trace,
    }

# 3. Run an evaluation
# For more info on evaluators, see: https://docs.langchain.com/langsmith/evaluation-concepts
evaluate(
    target_function,
    data=dataset_name,
    evaluators=[custom_evaluator],
    experiment_prefix="RAG Pipeline Real Evaluation"
)
