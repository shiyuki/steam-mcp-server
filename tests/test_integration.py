"""Integration tests for full infrastructure stack (cache + rate limiter + HTTP client)."""

import asyncio
import time
import pytest
import httpx
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

from src.http_client import CachedAPIClient
from src.cache import TTLCache
from src.rate_limiter import HostRateLimiter
from src.steam_api import SteamSpyClient, SteamStoreClient


class TestCacheIntegration:
    """Tests verifying cache hit/miss behavior with real TTLCache."""

    @pytest.mark.asyncio
    async def test_same_appid_returns_cached_response(self):
        """Second request for same AppID should return cached data (single fetch)."""
        cache = TTLCache(default_ttl=3600)
        rate_limiter = HostRateLimiter()
        api_client = CachedAPIClient(cache=cache, rate_limiter=rate_limiter)

        # Mock the httpx client
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "646570": {
                "success": True,
                "data": {
                    "name": "Slay the Spire",
                    "genres": [{"description": "Roguelike"}],
                    "developers": ["Mega Crit"],
                    "short_description": "A deckbuilder"
                }
            }
        }
        mock_response.raise_for_status = MagicMock()

        api_client.client.get = AsyncMock(return_value=mock_response)

        steam_store = SteamStoreClient(api_client)

        # First call - should fetch
        result1 = await steam_store.get_app_details(646570)
        # Second call - should use cache
        result2 = await steam_store.get_app_details(646570)

        # Verify both results are valid
        assert result1.name == "Slay the Spire"
        assert result2.name == "Slay the Spire"

        # Verify mock was called only once (cache hit on second call)
        assert api_client.client.get.call_count == 1

        await api_client.close()

    @pytest.mark.asyncio
    async def test_different_appids_fetch_independently(self):
        """Different AppIDs should trigger separate fetches."""
        cache = TTLCache(default_ttl=3600)
        rate_limiter = HostRateLimiter()
        api_client = CachedAPIClient(cache=cache, rate_limiter=rate_limiter)

        # Mock responses for two different AppIDs
        async def mock_get(url, params=None):
            mock_response = MagicMock()
            mock_response.status_code = 200

            # Return different data based on appid param
            appid = params.get("appids") if params else None
            if appid == "646570":
                mock_response.json.return_value = {
                    "646570": {"success": True, "data": {"name": "Slay the Spire"}}
                }
            else:
                mock_response.json.return_value = {
                    "2379780": {"success": True, "data": {"name": "Balatro"}}
                }

            mock_response.raise_for_status = MagicMock()
            return mock_response

        api_client.client.get = AsyncMock(side_effect=mock_get)

        steam_store = SteamStoreClient(api_client)

        # Fetch two different AppIDs
        result1 = await steam_store.get_app_details(646570)
        result2 = await steam_store.get_app_details(2379780)

        assert result1.name == "Slay the Spire"
        assert result2.name == "Balatro"

        # Both should trigger fetches (different cache keys)
        assert api_client.client.get.call_count == 2

        await api_client.close()

    @pytest.mark.asyncio
    async def test_cached_response_has_freshness_fields(self):
        """API responses should include fetched_at and cache_age_seconds."""
        cache = TTLCache(default_ttl=3600)
        rate_limiter = HostRateLimiter()
        api_client = CachedAPIClient(cache=cache, rate_limiter=rate_limiter)

        # Mock response
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "646570": {
                "success": True,
                "data": {"name": "Slay the Spire"}
            }
        }
        mock_response.raise_for_status = MagicMock()
        api_client.client.get = AsyncMock(return_value=mock_response)

        steam_store = SteamStoreClient(api_client)
        result = await steam_store.get_app_details(646570)

        # Verify freshness fields exist
        assert hasattr(result, 'fetched_at')
        assert isinstance(result.fetched_at, datetime)
        assert hasattr(result, 'cache_age_seconds')
        assert isinstance(result.cache_age_seconds, int)
        assert result.cache_age_seconds >= 0

        await api_client.close()


class TestPerHostIsolation:
    """Tests verifying per-host rate limiter independence."""

    @pytest.mark.asyncio
    async def test_steamspy_and_store_use_different_limiters(self):
        """SteamSpy and Steam Store should have independent rate limiters."""
        rate_limiter = HostRateLimiter()

        # Get limiters for both hosts
        steamspy_limiter = rate_limiter.get_limiter("steamspy.com")
        store_limiter = rate_limiter.get_limiter("store.steampowered.com")

        # Verify they are different instances
        assert steamspy_limiter is not store_limiter

        # Verify configs are correct
        steamspy_config = rate_limiter.get_config("steamspy.com")
        assert steamspy_config.max_rate == 1
        assert steamspy_config.time_period == 1.0

        store_config = rate_limiter.get_config("store.steampowered.com")
        assert store_config.max_rate == 1
        assert store_config.time_period == 1.5

    @pytest.mark.asyncio
    async def test_concurrent_different_hosts_not_serialized(self):
        """Requests to different hosts should execute concurrently."""
        cache = TTLCache(default_ttl=3600)
        rate_limiter = HostRateLimiter()
        api_client = CachedAPIClient(cache=cache, rate_limiter=rate_limiter)

        # Mock responses
        async def mock_get(url, params=None):
            # Simulate network delay
            await asyncio.sleep(0.1)
            mock_response = MagicMock()
            mock_response.status_code = 200

            if "steamspy.com" in url:
                # Return 5000+ games to bypass cross-validation (for timing accuracy)
                mock_response.json.return_value = {str(i): {"name": f"Game{i}"} for i in range(5000)}
            else:
                mock_response.json.return_value = {
                    "646570": {"success": True, "data": {"name": "Game2"}}
                }

            mock_response.raise_for_status = MagicMock()
            return mock_response

        api_client.client.get = AsyncMock(side_effect=mock_get)

        steamspy = SteamSpyClient(api_client)
        steam_store = SteamStoreClient(api_client)

        # Measure time for concurrent requests to different hosts
        start = time.time()
        results = await asyncio.gather(
            steamspy.search_by_tag("roguelike", limit=1),
            steam_store.get_app_details(646570)
        )
        elapsed = time.time() - start

        # Should complete in roughly 0.1s (concurrent), not 0.2s (sequential)
        # Allow some tolerance for overhead
        assert elapsed < 0.3, f"Concurrent requests took {elapsed}s, expected < 0.3s"

        await api_client.close()


class TestErrorBehavior:
    """Tests verifying error handling doesn't cache failures."""

    @pytest.mark.asyncio
    async def test_api_error_not_cached(self):
        """API errors should not be cached (refetch on retry)."""
        cache = TTLCache(default_ttl=3600)
        rate_limiter = HostRateLimiter()
        api_client = CachedAPIClient(cache=cache, rate_limiter=rate_limiter)

        call_count = 0

        async def mock_get(url, params=None):
            nonlocal call_count
            call_count += 1

            # First 3 calls fail (exhaust retries), calls 4+ succeed
            if call_count <= 3:
                response = httpx.Response(
                    500,
                    request=httpx.Request("GET", url)
                )
                raise httpx.HTTPStatusError(
                    "Server error",
                    request=response.request,
                    response=response
                )
            else:
                mock_response = MagicMock()
                mock_response.status_code = 200
                mock_response.json.return_value = {
                    "646570": {"success": True, "data": {"name": "Slay the Spire"}}
                }
                mock_response.raise_for_status = MagicMock()
                return mock_response

        api_client.client.get = AsyncMock(side_effect=mock_get)

        steam_store = SteamStoreClient(api_client)

        # First call - error (retries exhaust after 3 attempts, returns APIError)
        result1 = await steam_store.get_app_details(646570)
        from src.schemas import APIError
        assert isinstance(result1, APIError)
        assert call_count == 3  # Retried 3 times

        # Second call - should try fetching again (error not cached)
        result2 = await steam_store.get_app_details(646570)

        # Second call should succeed
        from src.schemas import GameMetadata
        assert isinstance(result2, GameMetadata)
        assert result2.name == "Slay the Spire"
        assert call_count == 4  # One more successful fetch

        await api_client.close()

    @pytest.mark.asyncio
    async def test_error_returns_apierror_not_stale_cache(self):
        """On error, fail honestly instead of serving stale cache (no stale fallback)."""
        cache = TTLCache(default_ttl=3600)
        rate_limiter = HostRateLimiter()
        api_client = CachedAPIClient(cache=cache, rate_limiter=rate_limiter)

        # Mock that always fails
        async def mock_get_error(url, params=None):
            response = httpx.Response(
                500,
                request=httpx.Request("GET", url)
            )
            raise httpx.HTTPStatusError(
                "Server error",
                request=response.request,
                response=response
            )

        api_client.client.get = AsyncMock(side_effect=mock_get_error)

        steam_store = SteamStoreClient(api_client)

        # Call with error - should return APIError, not try to serve stale cache
        result = await steam_store.get_app_details(646570)
        from src.schemas import APIError
        assert isinstance(result, APIError)
        # After retries exhaust, it's wrapped in RetryError and caught as generic exception (502)
        assert result.error_code == 502
        assert "RetryError" in result.message

        # Verify the error wasn't cached (call count = 3 retries)
        assert api_client.client.get.call_count == 3

        await api_client.close()
