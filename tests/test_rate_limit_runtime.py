from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import SecretStr

from backend.rate_limits.runtime import build_rate_limiter


def _settings(
    *,
    enabled: bool = True,
    backend: str = "memory",
    key: str = "",
) -> SimpleNamespace:
    return SimpleNamespace(
        rate_limits=SimpleNamespace(
            enabled=enabled,
            backend=backend,
            identity_hmac_key=SecretStr(key),
            key_prefix="test",
        ),
        storage=SimpleNamespace(redis_url=SecretStr("redis://localhost:6379/0")),
    )


@pytest.mark.asyncio
async def test_disabled_runtime_has_no_limiter_and_memory_key_can_be_ephemeral():
    assert build_rate_limiter(_settings(enabled=False)) is None

    limiter = build_rate_limiter(_settings())
    assert limiter is not None
    await limiter.close()
    await limiter.close()


def test_redis_runtime_requires_stable_hmac_key_before_client_construction():
    with pytest.raises(ValueError, match="RATE_LIMIT_HMAC_KEY"):
        build_rate_limiter(_settings(backend="redis"))
