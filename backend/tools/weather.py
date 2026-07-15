import os
import time
from typing import Optional

import requests

from backend.chat.request_context import ChatRequestContext
from backend.providers import (
    ProviderCode,
    ProviderCallContext,
    ProviderError,
    ProviderExecutor,
    ProviderOperation,
    ProviderPolicy,
)

try:
    from langchain_core.tools import tool
except ImportError:
    from langchain_core.tools import tool

AMAP_WEATHER_API = os.getenv("AMAP_WEATHER_API")
AMAP_API_KEY = os.getenv("AMAP_API_KEY")
WEATHER_TIMEOUT_SECONDS = 10.0
WEATHER_REQUEST_TIMEOUT_SECONDS = 4.0
_provider_executor = ProviderExecutor()
_provider_policy = ProviderPolicy(max_attempts=2)


def _tool_error(error: ProviderError) -> str:
    code = getattr(error.code, "value", str(error.code))
    return f"{code}: {error.message}"


def _call_context(ctx: ChatRequestContext | None = None) -> ProviderCallContext:
    request_deadline = None
    cancellation = None
    if ctx is not None:
        request_deadline, cancellation = ctx.provider_runtime()
    stage_deadline = time.monotonic() + WEATHER_TIMEOUT_SECONDS
    return ProviderCallContext(
        provider="amap-weather",
        operation=ProviderOperation.TOOL,
        deadline=(
            min(request_deadline, stage_deadline)
            if request_deadline is not None
            else stage_deadline
        ),
        cancellation=cancellation,
    )


def _remaining_timeout(context: ProviderCallContext) -> float:
    if context.deadline is None:
        return WEATHER_REQUEST_TIMEOUT_SECONDS
    return max(
        min(context.deadline - time.monotonic(), WEATHER_REQUEST_TIMEOUT_SECONDS),
        0.001,
    )


def _query_weather(
    location: str,
    extensions: Optional[str],
    *,
    context: ProviderCallContext,
) -> str:
    if not location:
        return "location参数不能为空"
    if extensions not in ("base", "all"):
        return "extensions参数错误，请输入base或all"

    if not AMAP_WEATHER_API or not AMAP_API_KEY:
        raise ProviderError(ProviderCode.TOOL_UNAVAILABLE, context=context)

    params = {
        "key": AMAP_API_KEY,
        "city": location,
        "extensions": extensions,
        "output": "json",
    }

    def _request() -> str:
        resp = requests.get(
            AMAP_WEATHER_API,
            params=params,
            timeout=_remaining_timeout(context),
        )
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, dict):
            raise ValueError("invalid weather response")
        if data.get("status") != "1":
            raise ValueError("weather provider returned an unsuccessful status")

        if extensions == "base":
            lives = data.get("lives", [])
            if not isinstance(lives, list) or not lives:
                return f"未查询到 {location} 的天气数据"
            w = lives[0]
            if not isinstance(w, dict):
                raise ValueError("invalid weather live result")
            return (
                f"【{w.get('city', location)} 实时天气】\n"
                f"天气状况：{w.get('weather', '未知')}\n"
                f"温度：{w.get('temperature', '未知')}℃\n"
                f"湿度：{w.get('humidity', '未知')}%\n"
                f"风向：{w.get('winddirection', '未知')}\n"
                f"风力：{w.get('windpower', '未知')}级\n"
                f"更新时间：{w.get('reporttime', '未知')}"
            )

        forecasts = data.get("forecasts", [])
        if not isinstance(forecasts, list) or not forecasts:
            return f"未查询到 {location} 的天气预报数据"
        f0 = forecasts[0]
        if not isinstance(f0, dict):
            raise ValueError("invalid weather forecast result")
        out = [
            f"【{f0.get('city', location)} 天气预报】",
            f"更新时间：{f0.get('reporttime', '未知')}",
            "",
        ]
        casts = f0.get("casts") or []
        if not isinstance(casts, list):
            raise ValueError("invalid weather casts result")
        today = casts[0] if casts else {}
        if not isinstance(today, dict):
            raise ValueError("invalid weather cast result")
        out += [
            "今日天气：",
            f"  白天：{today.get('dayweather', '未知')}",
            f"  夜间：{today.get('nightweather', '未知')}",
            f"  气温：{today.get('nighttemp', '未知')}~{today.get('daytemp', '未知')}℃",
        ]
        return "\n".join(out)

    return _provider_executor.call(
        _request,
        context=context,
        policy=_provider_policy,
    )


def get_current_weather(location: str, extensions: Optional[str] = "base") -> str:
    """Compatibility adapter returning a safe string instead of raising."""

    try:
        return _query_weather(location, extensions, context=_call_context())
    except ProviderError as exc:
        return _tool_error(exc)
    except Exception:
        return "TOOL_UNAVAILABLE: 天气服务返回了无效数据"


@tool("get_current_weather")
def get_current_weather_tool(location: str, extensions: Optional[str] = "base") -> str:
    """Get current weather for a city in China."""
    return get_current_weather(location, extensions)


def make_weather_tool(ctx: ChatRequestContext):
    """Build a request-owned weather Adapter with Run deadline/cancellation."""

    @tool("get_current_weather")
    def request_weather(location: str, extensions: Optional[str] = "base") -> str:
        """Get current weather for a city in China."""

        return _query_weather(location, extensions, context=_call_context(ctx))

    return request_weather
