"""Tests for per-host rate limiter."""

import time
import asyncio
import pytest
from aiolimiter import AsyncLimiter

from src.rate_limiter import (
    HostRateLimiter,
    RateLimitConfig,
    RATE_LIMITS,
    DEFAULT_RATE_LIMIT,
)


class TestRateLimitConfig:
    """Tests for rate limit configuration values."""

    def test_steamspy_config(self):
        """SteamSpy should have 1 req/sec rate limit."""
        config = RATE_LIMITS["steamspy.com"]
        assert config.max_rate == 1
        assert config.time_period == 1.0

    def test_steam_store_config(self):
        """Steam Store should have 1 req/1.5s rate limit (Phase 1 conservative)."""
        config = RATE_LIMITS["store.steampowered.com"]
        assert config.max_rate == 1
        assert config.time_period == 1.5

    def test_gamalytic_config(self):
        """Gamalytic should have 5 req/s rate limit (Phase 8 verified)."""
        config = RATE_LIMITS["gamalytic.com"]
        assert config.max_rate == 5
        assert config.time_period == 1.0

    def test_default_fallback(self):
        """Default rate limit should be conservative."""
        assert DEFAULT_RATE_LIMIT.max_rate == 1
        assert DEFAULT_RATE_LIMIT.time_period == 2.0


class TestHostRateLimiter:
    """Tests for HostRateLimiter class."""

    def test_get_limiter_returns_async_limiter(self):
        """get_limiter should return an AsyncLimiter instance."""
        limiter = HostRateLimiter()
        result = limiter.get_limiter("steamspy.com")
        assert isinstance(result, AsyncLimiter)

    def test_same_host_returns_same_limiter(self):
        """Multiple calls for same host should return same limiter instance."""
        limiter = HostRateLimiter()
        limiter1 = limiter.get_limiter("steamspy.com")
        limiter2 = limiter.get_limiter("steamspy.com")
        assert limiter1 is limiter2  # Same object identity

    def test_different_hosts_return_different_limiters(self):
        """Different hosts should get different limiter instances."""
        limiter = HostRateLimiter()
        steamspy_limiter = limiter.get_limiter("steamspy.com")
        store_limiter = limiter.get_limiter("store.steampowered.com")
        assert steamspy_limiter is not store_limiter

    def test_unknown_host_uses_default(self):
        """Unknown hosts should use DEFAULT_RATE_LIMIT configuration."""
        limiter = HostRateLimiter()
        unknown_limiter = limiter.get_limiter("unknown.example.com")
        assert isinstance(unknown_limiter, AsyncLimiter)
        # Verify it uses default config by checking get_config
        config = limiter.get_config("unknown.example.com")
        assert config == DEFAULT_RATE_LIMIT

    def test_host_substring_matching(self):
        """Full URLs should match host substring in config."""
        limiter = HostRateLimiter()
        # Full URL should match "steamspy.com" config
        config = limiter.get_config("https://steamspy.com/api.php?request=tag")
        assert config == RATE_LIMITS["steamspy.com"]
        assert config.max_rate == 1
        assert config.time_period == 1.0

    @pytest.mark.asyncio
    async def test_acquire_does_not_raise(self):
        """acquire method should complete without error."""
        limiter = HostRateLimiter()
        # Should not raise any exceptions
        await limiter.acquire("steamspy.com")

    @pytest.mark.asyncio
    async def test_per_host_isolation_concurrent(self):
        """Concurrent requests to different hosts should not block each other.

        Two concurrent acquires to different hosts should complete in roughly
        the time of one acquire, not two sequential acquires.
        """
        limiter = HostRateLimiter()

        # Create two hosts with known delays
        # SteamSpy: 1 req/sec (1.0s period)
        # Gamalytic: 10 req/min (60.0s period, but first acquire is instant)

        async def acquire_steamspy():
            await limiter.acquire("steamspy.com")
            return "steamspy"

        async def acquire_gamalytic():
            await limiter.acquire("gamalytic.com")
            return "gamalytic"

        # Measure concurrent execution
        start = time.monotonic()
        results = await asyncio.gather(acquire_steamspy(), acquire_gamalytic())
        elapsed = time.monotonic() - start

        # Both should complete successfully
        assert set(results) == {"steamspy", "gamalytic"}

        # First acquire for each limiter should be instant (no blocking)
        # Concurrent execution should take much less than sequential would take
        # Allow 0.1s overhead for async operations
        assert elapsed < 0.5, f"Concurrent acquires took {elapsed}s, expected < 0.5s"

    def test_get_config_returns_correct_config(self):
        """get_config should return the correct RateLimitConfig for a host."""
        limiter = HostRateLimiter()
        config = limiter.get_config("steamspy.com")
        assert config == RATE_LIMITS["steamspy.com"]
        assert config.max_rate == 1
        assert config.time_period == 1.0
