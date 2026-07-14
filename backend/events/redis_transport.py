from __future__ import annotations

import json

import redis.asyncio as redis_async
from redis.exceptions import ResponseError

from backend.core.settings import get_settings
from backend.events.generated.run_event_v1 import RunEventV1


class RedisEventTransport:
    """Redis Streams transport adapter；durability 仍由 PostgreSQL journal 提供。"""

    def __init__(self, redis_url: str | None = None, *, key_prefix: str | None = None):
        settings = get_settings()
        self.redis_url = redis_url or settings.storage.redis_url.get_secret_value()
        self.key_prefix = key_prefix or settings.storage.redis_key_prefix
        self.maxlen = settings.runs.redis_stream_maxlen
        self._client = None

    def _stream_key(self, run_id: str) -> str:
        return f"{self.key_prefix}:run_events:v1:{run_id}"

    def _get_client(self):
        if self._client is None:
            self._client = redis_async.Redis.from_url(
                self.redis_url,
                decode_responses=True,
                socket_connect_timeout=1,
                socket_timeout=2,
            )
        return self._client

    async def publish(self, event: RunEventV1) -> None:
        try:
            await self._get_client().xadd(
                self._stream_key(event.run_id),
                {"event": event.model_dump_json()},
                id=f"{event.sequence}-0",
                maxlen=self.maxlen,
                approximate=True,
            )
        except ResponseError as exc:
            message = str(exc).lower()
            if "equal or smaller" in message or "id specified" in message:
                return
            raise

    async def wait_after(
        self,
        *,
        run_id: str,
        after: int,
        block_ms: int,
        count: int = 100,
    ) -> list[RunEventV1]:
        response = await self._get_client().xread(
            {self._stream_key(run_id): f"{max(after, 0)}-0"},
            count=count,
            block=max(1, block_ms),
        )
        events: list[RunEventV1] = []
        for _stream, entries in response:
            for _entry_id, fields in entries:
                raw = fields.get("event")
                if not raw:
                    continue
                payload = json.loads(raw)
                events.append(RunEventV1.model_validate(payload))
        return events

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
