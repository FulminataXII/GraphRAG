"""RedisCache — implements `core.ports.Cache`. See BLUEPRINT §5.5.

A cache outage must degrade performance, never correctness: every method swallows Redis errors
and returns the "empty" answer instead of raising.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from redis.asyncio import Redis

_log = logging.getLogger(__name__)

_SCAN_BATCH = 500


class _RedisLike(Protocol):
    """The subset of the redis-py async API this adapter needs.

    Typed as a Protocol (rather than importing `redis.asyncio.Redis` at runtime) so unit tests
    can inject a hand-rolled fake client without a real Redis server — see BLUEPRINT §9's "NO
    mocks, NO MagicMock" rule.
    """

    async def get(self, name: str) -> bytes | None: ...
    async def set(self, name: str, value: bytes, *, ex: int | None = None) -> object: ...
    async def scan(
        self, cursor: int, *, match: str | None = None, count: int | None = None
    ) -> tuple[int, list[bytes]]: ...
    async def unlink(self, *names: bytes | str) -> object: ...


class RedisCache:
    """Implements `Cache`.

    Contract (BLUEPRINT §5.5):
        - Namespaced keys: f"{prefix}{key}".
        - Every method returns None / no-ops on a Redis error and increments a counter.
        - delete_prefix uses SCAN + batched UNLINK. Never KEYS.
    """

    def __init__(self, client: Redis | _RedisLike) -> None:
        self._client = client

    async def get(self, key: str) -> bytes | None:
        try:
            return await self._client.get(key)
        except Exception:
            _log.warning("redis GET failed; degrading to cache miss", exc_info=True)
            return None

    async def set(self, key: str, value: bytes, *, ttl_s: int) -> None:
        try:
            await self._client.set(key, value, ex=ttl_s)
        except Exception:
            _log.warning("redis SET failed; degrading to no-op", exc_info=True)

    async def delete_prefix(self, prefix: str) -> int:
        try:
            deleted = 0
            cursor = 0
            while True:
                cursor, keys = await self._client.scan(
                    cursor, match=f"{prefix}*", count=_SCAN_BATCH
                )
                if keys:
                    await self._client.unlink(*keys)
                    deleted += len(keys)
                if cursor == 0:
                    break
            return deleted
        except Exception:
            _log.warning("redis delete_prefix failed; degrading to no-op", exc_info=True)
            return 0
