"""Tests for TTL cache with stampede prevention."""

import asyncio
import pytest
from datetime import datetime, timezone
from unittest.mock import AsyncMock

from src.cache import TTLCache, make_cache_key, CacheEntry


class TestTTLCache:
    """Tests for TTLCache hit, miss, expiry, and stampede prevention."""

    @pytest.mark.asyncio
    async def test_get_or_fetch_calls_fetch_on_miss(self):
        """First call to get_or_fetch invokes fetch_func and returns result."""
        cache = TTLCache(default_ttl=3600)
        fetch_func = AsyncMock(return_value="fetched_value")

        entry = await cache.get_or_fetch("key1", fetch_func)

        assert entry.value == "fetched_value"
        assert isinstance(entry.fetched_at, datetime)
        assert entry.fetched_at.tzinfo == timezone.utc
        fetch_func.assert_called_once()

    @pytest.mark.asyncio
    async def test_get_or_fetch_returns_cached_on_hit(self):
        """Second call with same key returns cached value, fetch_func called only once."""
        cache = TTLCache(default_ttl=3600)
        fetch_func = AsyncMock(return_value="fetched_value")

        # First call - fetches
        entry1 = await cache.get_or_fetch("key1", fetch_func)
        # Second call - cached
        entry2 = await cache.get_or_fetch("key1", fetch_func)

        assert entry1.value == entry2.value
        assert entry1.fetched_at == entry2.fetched_at
        fetch_func.assert_called_once()  # Only called once

    @pytest.mark.asyncio
    async def test_get_or_fetch_refetches_after_ttl_expires(self):
        """Set very short TTL (0.1s), sleep past it, verify fetch_func called again."""
        cache = TTLCache(default_ttl=0.1)
        fetch_func = AsyncMock(side_effect=["first_value", "second_value"])

        # First fetch
        entry1 = await cache.get_or_fetch("key1", fetch_func, ttl=0.1)
        assert entry1.value == "first_value"

        # Wait for TTL to expire
        await asyncio.sleep(0.15)

        # Second fetch after expiry
        entry2 = await cache.get_or_fetch("key1", fetch_func, ttl=0.1)
        assert entry2.value == "second_value"
        assert fetch_func.call_count == 2

    @pytest.mark.asyncio
    async def test_get_or_fetch_custom_ttl_overrides_default(self):
        """Pass ttl=0.1 to get_or_fetch on a cache with default_ttl=3600, verify it expires quickly."""
        cache = TTLCache(default_ttl=3600)
        fetch_func = AsyncMock(side_effect=["first_value", "second_value"])

        # Fetch with custom short TTL
        entry1 = await cache.get_or_fetch("key1", fetch_func, ttl=0.1)
        assert entry1.value == "first_value"

        # Wait for custom TTL to expire
        await asyncio.sleep(0.15)

        # Should refetch despite high default TTL
        entry2 = await cache.get_or_fetch("key1", fetch_func, ttl=0.1)
        assert entry2.value == "second_value"
        assert fetch_func.call_count == 2

    @pytest.mark.asyncio
    async def test_stampede_prevention(self):
        """Launch 10 concurrent get_or_fetch calls for same key, verify fetch_func called exactly once."""
        cache = TTLCache(default_ttl=3600)
        call_count = 0

        async def slow_fetch():
            nonlocal call_count
            call_count += 1
            await asyncio.sleep(0.05)  # Simulate slow API call
            return f"value_{call_count}"

        # Launch 10 concurrent fetches for same key
        tasks = [cache.get_or_fetch("key1", slow_fetch) for _ in range(10)]
        results = await asyncio.gather(*tasks)

        # All should get same value (first fetch)
        assert all(entry.value == "value_1" for entry in results)
        assert call_count == 1  # Only called once due to per-key lock

    @pytest.mark.asyncio
    async def test_different_keys_fetch_independently(self):
        """Two different keys each call their own fetch_func."""
        cache = TTLCache(default_ttl=3600)
        fetch1 = AsyncMock(return_value="value1")
        fetch2 = AsyncMock(return_value="value2")

        entry1 = await cache.get_or_fetch("key1", fetch1)
        entry2 = await cache.get_or_fetch("key2", fetch2)

        assert entry1.value == "value1"
        assert entry2.value == "value2"
        fetch1.assert_called_once()
        fetch2.assert_called_once()

    @pytest.mark.asyncio
    async def test_invalidate_removes_entry(self):
        """After invalidate(key), next get_or_fetch calls fetch_func again."""
        cache = TTLCache(default_ttl=3600)
        fetch_func = AsyncMock(side_effect=["first_value", "second_value"])

        # First fetch
        entry1 = await cache.get_or_fetch("key1", fetch_func)
        assert entry1.value == "first_value"

        # Invalidate
        cache.invalidate("key1")

        # Should fetch again
        entry2 = await cache.get_or_fetch("key1", fetch_func)
        assert entry2.value == "second_value"
        assert fetch_func.call_count == 2

    @pytest.mark.asyncio
    async def test_clear_removes_all(self):
        """After clear(), cache.size == 0."""
        cache = TTLCache(default_ttl=3600)
        fetch_func = AsyncMock(return_value="value")

        # Add multiple entries
        await cache.get_or_fetch("key1", fetch_func)
        await cache.get_or_fetch("key2", fetch_func)
        await cache.get_or_fetch("key3", fetch_func)

        assert cache.size == 3

        # Clear all
        cache.clear()
        assert cache.size == 0

    @pytest.mark.asyncio
    async def test_get_returns_none_for_missing_key(self):
        """cache.get("nonexistent") returns None."""
        cache = TTLCache(default_ttl=3600)
        result = cache.get("nonexistent")
        assert result is None

    @pytest.mark.asyncio
    async def test_get_returns_none_for_expired_entry(self):
        """Cache entry exists but TTL expired, get() returns None."""
        cache = TTLCache(default_ttl=0.1)
        fetch_func = AsyncMock(return_value="value")

        # Fetch with short TTL
        await cache.get_or_fetch("key1", fetch_func, ttl=0.1)

        # Wait for expiry
        await asyncio.sleep(0.15)

        # get() should return None for expired
        result = cache.get("key1")
        assert result is None

    @pytest.mark.asyncio
    async def test_cache_entry_has_fetched_at(self):
        """CacheEntry.fetched_at is a datetime with UTC timezone."""
        cache = TTLCache(default_ttl=3600)
        fetch_func = AsyncMock(return_value="value")

        entry = await cache.get_or_fetch("key1", fetch_func)

        assert isinstance(entry.fetched_at, datetime)
        assert entry.fetched_at.tzinfo == timezone.utc

    @pytest.mark.asyncio
    async def test_cache_age_seconds_increases(self):
        """After caching, wait briefly, verify cache_age_seconds > 0 on get."""
        cache = TTLCache(default_ttl=3600)

        # Mock object with cache_age_seconds attribute
        class MockResponse:
            def __init__(self):
                self.cache_age_seconds = 0

        mock_response = MockResponse()
        fetch_func = AsyncMock(return_value=mock_response)

        # Fetch
        entry1 = await cache.get_or_fetch("key1", fetch_func)
        assert entry1.value.cache_age_seconds == 0  # Fresh

        # Wait more than 0.5s so round(age) >= 1
        await asyncio.sleep(0.6)

        # Get again - cache_age_seconds should be updated
        entry2 = cache.get("key1")
        assert entry2 is not None
        assert entry2.value.cache_age_seconds >= 1  # Should be >= 1 after rounding


class TestMakeCacheKey:
    """Tests for cache key generation from host, endpoint, and params."""

    def test_key_without_params(self):
        """make_cache_key("steamspy.com", "/api.php") returns "steamspy.com:/api.php"."""
        key = make_cache_key("steamspy.com", "/api.php")
        assert key == "steamspy.com:/api.php"

    def test_key_with_params(self):
        """Includes param hash suffix when params provided."""
        key = make_cache_key("steamspy.com", "/api.php", {"request": "tag", "tag": "rpg"})
        # Should have format: host:endpoint:hash
        assert key.startswith("steamspy.com:/api.php:")
        assert len(key.split(":")) == 3  # host, endpoint, hash
        assert len(key.split(":")[-1]) == 8  # Hash is 8 chars

    def test_same_params_same_key(self):
        """Deterministic: same params produce same key."""
        key1 = make_cache_key("steamspy.com", "/api.php", {"request": "tag", "tag": "rpg"})
        key2 = make_cache_key("steamspy.com", "/api.php", {"request": "tag", "tag": "rpg"})
        assert key1 == key2

    def test_different_params_different_key(self):
        """Different params produce different keys."""
        key1 = make_cache_key("steamspy.com", "/api.php", {"request": "tag", "tag": "rpg"})
        key2 = make_cache_key("steamspy.com", "/api.php", {"request": "tag", "tag": "fps"})
        assert key1 != key2

    def test_param_order_irrelevant(self):
        """{"a": 1, "b": 2} and {"b": 2, "a": 1} produce same key (sort_keys=True)."""
        key1 = make_cache_key("api.com", "/endpoint", {"a": 1, "b": 2})
        key2 = make_cache_key("api.com", "/endpoint", {"b": 2, "a": 1})
        assert key1 == key2
