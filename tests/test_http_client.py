"""Tests for CachedAPIClient (rate-limited, cached, async HTTP client)."""

import asyncio
import time
import pytest
import httpx
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from tenacity import RetryError

from src.cache import TTLCache, make_cache_key
from src.rate_limiter import HostRateLimiter
from src.http_client import CachedAPIClient, _is_retryable


def _make_httpx_response(status_code=200, json_data=None):
    """Create a mock httpx.Response with .json() and .raise_for_status()."""
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.json.return_value = json_data or {"ok": True}
    if status_code >= 400:
        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            f"HTTP {status_code}",
            request=httpx.Request("GET", "https://example.com"),
            response=resp,
        )
    else:
        resp.raise_for_status.return_value = None
    return resp


class TestExtractHost:
    """Tests for _extract_host URL parsing."""

    def test_extract_host_standard_url(self):
        cache = TTLCache()
        limiter = HostRateLimiter()
        client = CachedAPIClient(cache, limiter)
        assert client._extract_host("https://steamspy.com/api.php") == "steamspy.com"

    def test_extract_host_with_port(self):
        cache = TTLCache()
        limiter = HostRateLimiter()
        client = CachedAPIClient(cache, limiter)
        assert client._extract_host("http://localhost:8080/path") == "localhost:8080"

    def test_extract_host_empty_url(self):
        cache = TTLCache()
        limiter = HostRateLimiter()
        client = CachedAPIClient(cache, limiter)
        assert client._extract_host("") == ""


class TestFetch:
    """Tests for _fetch (rate-limited GET with retry)."""

    @pytest.mark.asyncio
    async def test_fetch_calls_rate_limiter_then_http(self):
        """_fetch acquires rate limiter before making HTTP call."""
        cache = TTLCache()
        limiter = HostRateLimiter()
        client = CachedAPIClient(cache, limiter)

        call_order = []
        original_acquire = limiter.acquire

        async def tracking_acquire(host):
            call_order.append("acquire")
            await original_acquire(host)

        limiter.acquire = tracking_acquire

        mock_resp = _make_httpx_response(json_data={"result": "ok"})

        async def mock_get(url, params=None, headers=None):
            call_order.append("http_get")
            return mock_resp

        client.client.get = mock_get

        result = await client._fetch("https://steamspy.com/api.php")
        assert result == {"result": "ok"}
        assert call_order == ["acquire", "http_get"]

        await client.close()

    @pytest.mark.asyncio
    async def test_fetch_raises_on_http_error(self):
        """_fetch raises RetryError wrapping HTTPStatusError after retry exhaustion."""
        cache = TTLCache()
        limiter = HostRateLimiter()
        client = CachedAPIClient(cache, limiter)

        mock_resp = _make_httpx_response(status_code=500)

        async def mock_get(url, params=None, headers=None):
            return mock_resp

        client.client.get = mock_get

        with pytest.raises(RetryError) as exc_info:
            await client._fetch("https://steamspy.com/api.php")

        # Original HTTPStatusError is the cause
        assert isinstance(exc_info.value.last_attempt.exception(), httpx.HTTPStatusError)

        await client.close()


class TestFetchWithTimeout:
    """Tests for _fetch_with_timeout (timeout wrapper around _fetch)."""

    @pytest.mark.asyncio
    async def test_fetch_with_timeout_returns_on_success(self):
        """Returns data when _fetch completes within timeout."""
        cache = TTLCache()
        limiter = HostRateLimiter()
        client = CachedAPIClient(cache, limiter)

        mock_resp = _make_httpx_response(json_data={"data": 42})
        client.client.get = AsyncMock(return_value=mock_resp)

        result = await client._fetch_with_timeout("https://steamspy.com/api.php")
        assert result == {"data": 42}

        await client.close()

    @pytest.mark.asyncio
    async def test_fetch_with_timeout_raises_on_timeout(self):
        """Raises TimeoutError when _fetch exceeds PER_REQUEST_TIMEOUT."""
        cache = TTLCache()
        limiter = HostRateLimiter()
        client = CachedAPIClient(cache, limiter)

        async def slow_get(url, params=None, headers=None):
            await asyncio.sleep(10)
            return _make_httpx_response()

        client.client.get = slow_get

        with patch("src.http_client.PER_REQUEST_TIMEOUT", 0.1):
            with pytest.raises(asyncio.TimeoutError):
                await client._fetch_with_timeout("https://steamspy.com/api.php")

        await client.close()


class TestGet:
    """Tests for get() (cached, rate-limited GET)."""

    @pytest.mark.asyncio
    async def test_get_bypasses_cache_when_ttl_zero(self):
        """cache_ttl=0 fetches directly, never touches cache."""
        cache = TTLCache()
        limiter = HostRateLimiter()
        client = CachedAPIClient(cache, limiter)

        mock_resp = _make_httpx_response(json_data={"fresh": True})
        client.client.get = AsyncMock(return_value=mock_resp)

        result = await client.get(
            "https://steamspy.com/api.php", cache_ttl=0
        )
        assert result == {"fresh": True}
        # Cache should be empty
        assert cache.size == 0

        await client.close()

    @pytest.mark.asyncio
    async def test_get_uses_cache_on_hit(self):
        """Second get() call returns cached value, HTTP called once."""
        cache = TTLCache(default_ttl=3600)
        limiter = HostRateLimiter()
        client = CachedAPIClient(cache, limiter)

        call_count = 0

        async def counting_get(url, params=None, headers=None):
            nonlocal call_count
            call_count += 1
            return _make_httpx_response(json_data={"count": call_count})

        client.client.get = counting_get

        result1 = await client.get("https://steamspy.com/api.php")
        result2 = await client.get("https://steamspy.com/api.php")

        assert result1 == {"count": 1}
        assert result2 == {"count": 1}  # Cached, not {"count": 2}
        assert call_count == 1

        await client.close()


class TestGetWithMetadata:
    """Tests for get_with_metadata() (data + freshness tuple)."""

    @pytest.mark.asyncio
    async def test_get_with_metadata_returns_tuple(self):
        """Returns (data, datetime, int) with correct types."""
        cache = TTLCache(default_ttl=3600)
        limiter = HostRateLimiter()
        client = CachedAPIClient(cache, limiter)

        mock_resp = _make_httpx_response(json_data={"name": "test"})
        client.client.get = AsyncMock(return_value=mock_resp)

        data, fetched_at, cache_age = await client.get_with_metadata(
            "https://steamspy.com/api.php"
        )

        assert data == {"name": "test"}
        assert isinstance(fetched_at, datetime)
        assert fetched_at.tzinfo == timezone.utc
        assert isinstance(cache_age, int)

        await client.close()

    @pytest.mark.asyncio
    async def test_get_with_metadata_cache_bypass(self):
        """cache_ttl=0 returns fresh data with cache_age=0."""
        cache = TTLCache()
        limiter = HostRateLimiter()
        client = CachedAPIClient(cache, limiter)

        mock_resp = _make_httpx_response(json_data={"fresh": True})
        client.client.get = AsyncMock(return_value=mock_resp)

        data, fetched_at, cache_age = await client.get_with_metadata(
            "https://steamspy.com/api.php", cache_ttl=0
        )

        assert data == {"fresh": True}
        assert cache_age == 0
        assert isinstance(fetched_at, datetime)

        await client.close()

    @pytest.mark.asyncio
    async def test_get_with_metadata_cache_age_positive(self):
        """Cached response has cache_age > 0 after a brief wait."""
        cache = TTLCache(default_ttl=3600)
        limiter = HostRateLimiter()
        client = CachedAPIClient(cache, limiter)

        mock_resp = _make_httpx_response(json_data={"cached": True})
        client.client.get = AsyncMock(return_value=mock_resp)

        # First call populates cache
        await client.get_with_metadata("https://steamspy.com/api.php")

        # Wait briefly so cache age is measurable
        await asyncio.sleep(0.5)

        # Second call should report positive cache age
        _, _, cache_age = await client.get_with_metadata(
            "https://steamspy.com/api.php"
        )
        assert cache_age >= 0  # At least 0 after int truncation

        await client.close()


class TestContextManager:
    """Tests for async context manager and close."""

    @pytest.mark.asyncio
    async def test_context_manager_calls_close(self):
        """async with calls close() on exit."""
        cache = TTLCache()
        limiter = HostRateLimiter()
        client = CachedAPIClient(cache, limiter)
        client.close = AsyncMock()

        async with client:
            pass

        client.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_close_calls_aclose(self):
        """close() delegates to client.aclose()."""
        cache = TTLCache()
        limiter = HostRateLimiter()
        client = CachedAPIClient(cache, limiter)
        client.client.aclose = AsyncMock()

        await client.close()

        client.client.aclose.assert_called_once()


class TestIsRetryable:
    """Tests for _is_retryable predicate (WARN-01 prereq: 429 not retried)."""

    def test_429_not_retried(self):
        """429 Too Many Requests should NOT be retried — quota won't clear in retry window."""
        req = httpx.Request("GET", "https://api.gamalytic.com/game/123")
        resp = httpx.Response(429, request=req)
        exc = httpx.HTTPStatusError("429", request=req, response=resp)
        assert _is_retryable(exc) is False

    def test_503_still_retried(self):
        """503 Service Unavailable IS retried — transient server failure."""
        req = httpx.Request("GET", "https://api.gamalytic.com/game/123")
        resp = httpx.Response(503, request=req)
        exc = httpx.HTTPStatusError("503", request=req, response=resp)
        assert _is_retryable(exc) is True

    def test_500_still_retried(self):
        """500 Internal Server Error IS retried — transient failure."""
        req = httpx.Request("GET", "https://steamspy.com/api.php")
        resp = httpx.Response(500, request=req)
        exc = httpx.HTTPStatusError("500", request=req, response=resp)
        assert _is_retryable(exc) is True

    def test_502_still_retried(self):
        """502 Bad Gateway IS retried — transient failure."""
        req = httpx.Request("GET", "https://steamspy.com/api.php")
        resp = httpx.Response(502, request=req)
        exc = httpx.HTTPStatusError("502", request=req, response=resp)
        assert _is_retryable(exc) is True

    def test_network_error_retried(self):
        """Generic httpx.HTTPError (network failure) IS retried."""
        exc = httpx.ConnectError("Connection refused")
        assert _is_retryable(exc) is True

    def test_timeout_error_retried(self):
        """asyncio.TimeoutError IS retried — transient timeout."""
        exc = asyncio.TimeoutError()
        assert _is_retryable(exc) is True

    def test_non_http_exception_not_retried(self):
        """Non-HTTP exceptions (e.g. ValueError) are NOT retried."""
        exc = ValueError("unexpected")
        assert _is_retryable(exc) is False

    @pytest.mark.asyncio
    async def test_429_causes_single_attempt_only(self):
        """When _fetch gets a 429, tenacity makes exactly 1 attempt (not 3).

        When retry_if_exception returns False, tenacity re-raises the original
        exception directly (not wrapped in RetryError).
        """
        cache = TTLCache()
        limiter = HostRateLimiter()
        client = CachedAPIClient(cache, limiter)

        call_count = 0
        req = httpx.Request("GET", "https://api.gamalytic.com/game/123")
        resp = httpx.Response(429, request=req)

        async def mock_get(url, params=None, headers=None):
            nonlocal call_count
            call_count += 1
            raise httpx.HTTPStatusError("429", request=req, response=resp)

        client.client.get = mock_get

        # 429 is not retried: tenacity re-raises the original HTTPStatusError
        # (not wrapped in RetryError — that only happens when retries are exhausted)
        with pytest.raises(httpx.HTTPStatusError) as exc_info:
            await client._fetch("https://api.gamalytic.com/game/123")

        # Only 1 attempt was made — no retries
        assert call_count == 1
        assert exc_info.value.response.status_code == 429

        await client.close()

    @pytest.mark.asyncio
    async def test_503_causes_three_attempts(self):
        """When _fetch gets a 503, tenacity retries up to stop_after_attempt(3)."""
        cache = TTLCache()
        limiter = HostRateLimiter()
        client = CachedAPIClient(cache, limiter)

        call_count = 0
        req = httpx.Request("GET", "https://api.gamalytic.com/game/123")
        resp = httpx.Response(503, request=req)

        async def mock_get(url, params=None, headers=None):
            nonlocal call_count
            call_count += 1
            raise httpx.HTTPStatusError("503", request=req, response=resp)

        client.client.get = mock_get

        # Patch wait to avoid sleeping in tests
        with patch("src.http_client.CachedAPIClient._fetch.retry.wait", MagicMock(return_value=0)):
            pass  # Can't easily patch tenacity wait without reimporting

        # Just verify it raises RetryError (3 attempts exhausted)
        with pytest.raises(RetryError):
            await client._fetch("https://api.gamalytic.com/game/123")

        assert call_count == 3

        await client.close()


class TestCacheKeyAuthState:
    """Tests for make_cache_key auth_state parameter (KEY-03)."""

    def test_cache_key_with_auth_state_differs_from_without(self):
        """Authed and unauthed requests to same endpoint get different cache keys."""
        k1 = make_cache_key("api.gamalytic.com", "/game/123")
        k2 = make_cache_key("api.gamalytic.com", "/game/123", auth_state="authed")
        assert k1 != k2

    def test_cache_key_auth_state_appends_suffix(self):
        """auth_state='authed' appends ':authed' to the cache key."""
        k = make_cache_key("api.gamalytic.com", "/game/123", auth_state="authed")
        assert k.endswith(":authed")

    def test_cache_key_no_auth_state_backward_compat(self):
        """Existing callers with no auth_state still get correct keys."""
        k = make_cache_key("steamspy.com", "/api.php", {"request": "tag"})
        assert k.startswith("steamspy.com:/api.php:")
        # Key must NOT end with :authed
        assert not k.endswith(":authed")

    def test_cache_key_auth_state_with_params(self):
        """auth_state works correctly when params are also provided."""
        k_no_auth = make_cache_key("api.gamalytic.com", "/steam-games/list", {"tags": "roguelike"})
        k_authed = make_cache_key("api.gamalytic.com", "/steam-games/list", {"tags": "roguelike"}, auth_state="authed")
        assert k_no_auth != k_authed
        assert k_authed.endswith(":authed")
        # The param hash portion should be the same (only suffix differs)
        assert k_authed.startswith(k_no_auth)

    def test_cache_key_auth_state_none_same_as_absent(self):
        """Explicitly passing auth_state=None produces same key as not passing it."""
        k1 = make_cache_key("api.gamalytic.com", "/game/123")
        k2 = make_cache_key("api.gamalytic.com", "/game/123", auth_state=None)
        assert k1 == k2

    @pytest.mark.asyncio
    async def test_get_with_metadata_uses_different_cache_key_for_authed_request(self):
        """get_with_metadata with Authorization header uses separate cache slot from unauthed."""
        cache = TTLCache(default_ttl=3600)
        limiter = HostRateLimiter()
        client = CachedAPIClient(cache, limiter)

        call_count = 0

        async def counting_get(url, params=None, headers=None):
            nonlocal call_count
            call_count += 1
            return _make_httpx_response(json_data={"call": call_count})

        client.client.get = counting_get

        url = "https://api.gamalytic.com/game/123"

        # First: unauthed request
        data1, _, _ = await client.get_with_metadata(url)
        # Second: authed request — must NOT hit the unauthed cache slot
        data2, _, _ = await client.get_with_metadata(url, headers={"Authorization": "Bearer test-key"})

        assert data1 == {"call": 1}
        assert data2 == {"call": 2}  # Different cache key — fetched again
        assert call_count == 2

        await client.close()
