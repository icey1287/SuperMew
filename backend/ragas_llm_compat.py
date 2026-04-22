"""
RAGAS 评测用 LLM 构造。

ragas.llms.llm_factory 对 OpenAI 兼容客户端使用 instructor.from_openai(..., Mode.JSON)，
请求里会带 response_format=json_object；豆包/火山等端常报：
  InvalidParameter: json_object is not supported by this model

可通过环境变量改用 TOOLS（函数调用）模式（默认），一般可与上述端兼容。

环境变量：
  GRADE_MODEL   RAGAS 打分用模型（与流水线 grade_documents 一致）；未设置时再使用 MODEL。
  RAGAS_INSTRUCTOR_MODE   tools（默认）| json
    - tools : instructor.Mode.TOOLS，不发 json_object
    - json  : 与官方 ragas llm_factory(OpenAI) 一致，适合原生 OpenAI 等支持 JSON 模式的模型

  RAGAS_MAX_OUTPUT_TOKENS  单次生成的 max_tokens（默认 8192）
    Faithfulness 第二步会把大量陈述一次性写入结构化输出，默认 1024 极易触发
    finish_reason=length → InstructorRetryException → 分数 nan。若仍截断可改为 12288、16384 等（受模型上限约束）。
"""

from __future__ import annotations

import os

import instructor
from openai import OpenAI
from ragas.llms.base import InstructorLLM, InstructorModelArgs


def build_ragas_instructor_llm(
    *,
    api_key: str | None = None,
    base_url: str | None = None,
    model: str | None = None,
) -> InstructorLLM:
    key = (api_key if api_key is not None else os.getenv("ARK_API_KEY", "")).strip()
    url = (base_url if base_url is not None else os.getenv("BASE_URL", "")).strip() or None
    if model is not None:
        mdl = str(model).strip()
    else:
        mdl = os.getenv("GRADE_MODEL", "").strip() or os.getenv("MODEL", "").strip()
    if not key or not mdl:
        raise ValueError(
            "需要配置 ARK_API_KEY 与 GRADE_MODEL（或 MODEL），或通过参数传入 model"
        )

    client = OpenAI(api_key=key, base_url=url)
    raw = (os.getenv("RAGAS_INSTRUCTOR_MODE", "tools") or "tools").strip().lower()
    if raw in ("json", "json_object"):
        mode = instructor.Mode.JSON
    elif raw in ("tools", "tool", "functions"):
        mode = instructor.Mode.TOOLS
    else:
        raise ValueError(
            "RAGAS_INSTRUCTOR_MODE 应为 tools 或 json，当前为 {!r}".format(raw)
        )

    patched = instructor.from_openai(client, mode=mode)
    try:
        max_tokens = int(os.getenv("RAGAS_MAX_OUTPUT_TOKENS", "8192").strip() or "8192")
    except ValueError:
        max_tokens = 8192
    max_tokens = max(256, min(max_tokens, 131072))

    model_args = InstructorModelArgs(max_tokens=max_tokens)
    return InstructorLLM(
        client=patched,
        model=mdl,
        provider="openai",
        model_args=model_args,
    )
