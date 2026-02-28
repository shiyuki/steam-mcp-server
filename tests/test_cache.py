"""Tests for TTL cache with stampede prevention."""

import asyncio
import time
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


class TestGetOrFetchStale:
    """Tests for TTLCache.get_or_fetch_stale (stale-while-revalidate semantics)."""

    @pytest.mark.asyncio
    async def test_stale_fresh_hit(self):
        """Entry within fresh TTL returns (entry, False) without calling fetch_func again."""
        cache = TTLCache(default_ttl=3600)
        fetch_func = AsyncMock(return_value="initial_value")

        # Populate the cache
        entry1, is_stale1 = await cache.get_or_fetch_stale("key", fetch_func, ttl=3600, stale_ttl=7200)
        assert entry1.value == "initial_value"
        assert is_stale1 is False

        # Immediately re-request — still within fresh TTL
        entry2, is_stale2 = await cache.get_or_fetch_stale("key", fetch_func, ttl=3600, stale_ttl=7200)
        assert entry2.value == "initial_value"
        assert is_stale2 is False
        fetch_func.assert_called_once()  # fetch_func not called again

    @pytest.mark.asyncio
    async def test_stale_revalidate_success(self):
        """Entry past fresh TTL but within stale TTL: successful fetch returns fresh entry, is_stale=False."""
        cache = TTLCache(default_ttl=3600)
        fetch_func = AsyncMock(side_effect=["stale_value", "fresh_value"])

        # Populate with very short fresh TTL
        await cache.get_or_fetch_stale("key", fetch_func, ttl=0.05, stale_ttl=10.0)

        # Wait past fresh TTL but well within stale window
        await asyncio.sleep(0.1)

        # Revalidation attempt should succeed and return fresh value
        entry, is_stale = await cache.get_or_fetch_stale("key", fetch_func, ttl=0.05, stale_ttl=10.0)
        assert entry.value == "fresh_value"
        assert is_stale is False
        assert fetch_func.call_count == 2

    @pytest.mark.asyncio
    async def test_stale_revalidate_failure(self):
        """Entry past fresh TTL, within stale window, fetch raises: returns stale entry, is_stale=True."""
        cache = TTLCache(default_ttl=3600)

        # First call populates the cache
        populate_func = AsyncMock(return_value="stale_value")
        await cache.get_or_fetch_stale("key", populate_func, ttl=0.05, stale_ttl=10.0)

        # Wait past fresh TTL
        await asyncio.sleep(0.1)

        # Revalidation fetch raises — should serve stale data
        failing_fetch = AsyncMock(side_effect=RuntimeError("network error"))
        entry, is_stale = await cache.get_or_fetch_stale("key", failing_fetch, ttl=0.05, stale_ttl=10.0)
        assert entry.value == "stale_value"
        assert is_stale is True
        failing_fetch.assert_called_once()

    @pytest.mark.asyncio
    async def test_stale_expired(self):
        """Entry past stale TTL: successful fetch returns fresh entry, is_stale=False."""
        cache = TTLCache(default_ttl=3600)
        fetch_func = AsyncMock(side_effect=["old_value", "new_value"])

        # Populate with very short ttl AND stale_ttl
        await cache.get_or_fetch_stale("key", fetch_func, ttl=0.03, stale_ttl=0.06)

        # Wait past stale window entirely
        await asyncio.sleep(0.1)

        entry, is_stale = await cache.get_or_fetch_stale("key", fetch_func, ttl=0.03, stale_ttl=0.06)
        assert entry.value == "new_value"
        assert is_stale is False
        assert fetch_func.call_count == 2

    @pytest.mark.asyncio
    async def test_stale_expired_fetch_fails(self):
        """Entry past stale TTL, fetch raises: exception propagates (no stale to serve)."""
        cache = TTLCache(default_ttl=3600)

        # Populate with very short ttl AND stale_ttl
        populate_func = AsyncMock(return_value="old_value")
        await cache.get_or_fetch_stale("key", populate_func, ttl=0.03, stale_ttl=0.06)

        # Wait past stale window entirely
        await asyncio.sleep(0.1)

        failing_fetch = AsyncMock(side_effect=RuntimeError("network dead"))
        with pytest.raises(RuntimeError, match="network dead"):
            await cache.get_or_fetch_stale("key", failing_fetch, ttl=0.03, stale_ttl=0.06)

    @pytest.mark.asyncio
    async def test_stale_no_entry_fetch_fails(self):
        """No cached entry at all, fetch raises: exception propagates."""
        cache = TTLCache(default_ttl=3600)
        failing_fetch = AsyncMock(side_effect=ConnectionError("no network"))

        with pytest.raises(ConnectionError, match="no network"):
            await cache.get_or_fetch_stale("key", failing_fetch, ttl=3600, stale_ttl=7200)

    @pytest.mark.asyncio
    async def test_stale_default_no_stale_window(self):
        """stale_ttl=None defaults to ttl — behaves identically to get_or_fetch (no stale serving)."""
        cache = TTLCache(default_ttl=3600)
        fetch_func = AsyncMock(side_effect=["old_value", "new_value"])

        # Populate with very short TTL, no stale window
        await cache.get_or_fetch_stale("key", fetch_func, ttl=0.05, stale_ttl=None)

        # Wait for TTL to expire
        await asyncio.sleep(0.1)

        # Past TTL with no stale window — fetch fresh
        entry, is_stale = await cache.get_or_fetch_stale("key", fetch_func, ttl=0.05, stale_ttl=None)
        assert entry.value == "new_value"
        assert is_stale is False
        assert fetch_func.call_count == 2

    @pytest.mark.asyncio
    async def test_stale_return_type(self):
        """get_or_fetch_stale always returns a 2-tuple of (CacheEntry, bool)."""
        cache = TTLCache(default_ttl=3600)
        fetch_func = AsyncMock(return_value={"data": 42})

        result = await cache.get_or_fetch_stale("key", fetch_func, ttl=3600, stale_ttl=7200)

        assert isinstance(result, tuple)
        assert len(result) == 2
        entry, is_stale = result
        assert isinstance(entry, CacheEntry)
        assert isinstance(is_stale, bool)
        assert entry.value == {"data": 42}
        assert is_stale is False


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
