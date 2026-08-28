"""`RedisCache` unit tests. See BLUEPRINT §5.5.

Uses a hand-rolled fake Redis client (BLUEPRINT §9: "NO mocks, NO MagicMock") so these stay
`unit` — no real Redis needed. `test_cache_returns_none_on_redis_error` additionally points a
real `redis.asyncio.Redis` at an address nothing listens on, to prove the degrade-on-error path
against the real client's actual exception types, not just a fake that raises on cue.
"""

from __future__ import annotations

from redis.asyncio import Redis

from graphrag.adapters.redis_cache import RedisCache


class _FakeRedisClient:
    """Records whether SCAN (never KEYS) drives delete_prefix, and simulates a store."""

    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}
        self.scan_calls: list[int] = []
        self.keys_called = False

    async def get(self, name: str) -> bytes | None:
        return self.store.get(name)

    async def set(self, name: str, value: bytes, *, ex: int | None = None) -> None:
        self.store[name] = value

    async def scan(
        self, cursor: int, *, match: str | None = None, count: int | None = None
    ) -> tuple[int, list[bytes]]:
        self.scan_calls.append(cursor)
        prefix = (match or "").removesuffix("*")
        matched = [k.encode() for k in self.store if k.startswith(prefix)]
        return 0, matched

    async def unlink(self, *names: bytes) -> None:
        for name in names:
            self.store.pop(name.decode() if isinstance(name, bytes) else name, None)

    async def keys(self, pattern: str) -> list[bytes]:  # pragma: no cover - must never be called
        self.keys_called = True
        return []


class _AlwaysFailsClient:
    async def get(self, name: str) -> bytes | None:
        raise ConnectionError("redis is down")

    async def set(self, name: str, value: bytes, *, ex: int | None = None) -> None:
        raise ConnectionError("redis is down")

    async def scan(
        self, cursor: int, *, match: str | None = None, count: int | None = None
    ) -> tuple[int, list[bytes]]:
        raise ConnectionError("redis is down")

    async def unlink(self, *names: bytes) -> None:
        raise ConnectionError("redis is down")


async def test_cache_set_then_get_roundtrips() -> None:
    cache = RedisCache(_FakeRedisClient())
    await cache.set("emb:foo", b"payload", ttl_s=60)
    assert await cache.get("emb:foo") == b"payload"


async def test_cache_get_missing_returns_none() -> None:
    cache = RedisCache(_FakeRedisClient())
    assert await cache.get("emb:missing") is None


async def test_cache_delete_prefix_uses_scan() -> None:
    client = _FakeRedisClient()
    cache = RedisCache(client)
    await cache.set("ret:a", b"1", ttl_s=60)
    await cache.set("ret:b", b"2", ttl_s=60)
    await cache.set("emb:c", b"3", ttl_s=60)

    deleted = await cache.delete_prefix("ret:")

    assert deleted == 2
    assert client.store == {"emb:c": b"3"}
    assert client.scan_calls  # SCAN was actually invoked
    assert client.keys_called is False


async def test_cache_returns_none_on_redis_error() -> None:
    # Nothing listens on this address — a real connection failure, not a scripted one.
    client: Redis = Redis(host="127.0.0.1", port=1, socket_connect_timeout=1)
    cache = RedisCache(client)

    assert await cache.get("any-key") is None


async def test_cache_set_degrades_on_error() -> None:
    cache = RedisCache(_AlwaysFailsClient())
    await cache.set("k", b"v", ttl_s=1)  # must not raise


async def test_cache_delete_prefix_degrades_on_error() -> None:
    cache = RedisCache(_AlwaysFailsClient())
    assert await cache.delete_prefix("k:") == 0
