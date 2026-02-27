"""Tests for CachedAPIClient get_bytes method."""

import asyncio
import pytest
import httpx
from unittest.mock import AsyncMock, MagicMock

from src.cache import TTLCache
from src.rate_limiter import HostRateLimiter
from src.http_client import CachedAPIClient


def _make_httpx_response(status_code=200, json_data=None, content=None):
    """Create a mock httpx.Response with .json(), .content, and .raise_for_status()."""
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.json.return_value = json_data or {"ok": True}
    resp.content = content or b""
    if status_code >= 400:
        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            f"HTTP {status_code}",
            request=httpx.Request("GET", "https://example.com"),
            response=resp,
        )
    else:
        resp.raise_for_status.return_value = None
    return resp


class TestGetBytes:
    """Tests for get_bytes() (cached, rate-limited binary GET)."""

    @pytest.mark.asyncio
    async def test_get_bytes_returns_raw_bytes(self):
        """get_bytes returns raw response.content bytes."""
        cache = TTLCache(default_ttl=3600)
        limiter = HostRateLimiter()
        client = CachedAPIClient(cache, limiter)

        fake_image = b"\xff\xd8\xff\xe0fake-jpeg-data"
        mock_resp = _make_httpx_response(content=fake_image)
        client.client.get = AsyncMock(return_value=mock_resp)

        result = await client.get_bytes("https://shared.akamai.steamstatic.com/store/apps/646570/header.jpg")
        assert result == fake_image
        assert isinstance(result, bytes)

        await client.close()

    @pytest.mark.asyncio
    async def test_get_bytes_caches_second_call(self):
        """Second get_bytes call returns cached value, HTTP called once."""
        cache = TTLCache(default_ttl=3600)
        limiter = HostRateLimiter()
        client = CachedAPIClient(cache, limiter)

        call_count = 0

        async def counting_get(url, headers=None):
            nonlocal call_count
            call_count += 1
            return _make_httpx_response(content=b"image-data")

        client.client.get = counting_get

        url = "https://shared.akamai.steamstatic.com/store/apps/646570/header.jpg"
        result1 = await client.get_bytes(url)
        result2 = await client.get_bytes(url)

        assert result1 == b"image-data"
        assert result2 == b"image-data"
        assert call_count == 1  # Only one HTTP call

        await client.close()

    @pytest.mark.asyncio
    async def test_get_bytes_bypasses_cache_when_ttl_zero(self):
        """cache_ttl=0 fetches directly, never touches cache."""
        cache = TTLCache()
        limiter = HostRateLimiter()
        client = CachedAPIClient(cache, limiter)

        mock_resp = _make_httpx_response(content=b"fresh-bytes")
        client.client.get = AsyncMock(return_value=mock_resp)

        result = await client.get_bytes(
            "https://shared.akamai.steamstatic.com/store/apps/646570/header.jpg",
            cache_ttl=0,
        )
        assert result == b"fresh-bytes"
        assert cache.size == 0

        await client.close()

    @pytest.mark.asyncio
    async def test_get_bytes_does_not_collide_with_json_cache(self):
        """get_bytes and get() for same URL use different cache keys."""
        cache = TTLCache(default_ttl=3600)
        limiter = HostRateLimiter()
        client = CachedAPIClient(cache, limiter)

        call_count = 0

        async def mock_get(url, params=None, headers=None):
            nonlocal call_count
            call_count += 1
            resp = _make_httpx_response(json_data={"data": "json"}, content=b"binary")
            return resp

        client.client.get = mock_get

        url = "https://shared.akamai.steamstatic.com/store/apps/646570/header.jpg"
        json_result = await client.get(url)
        bytes_result = await client.get_bytes(url)

        assert json_result == {"data": "json"}
        assert bytes_result == b"binary"
        assert call_count == 2  # Separate cache entries

        await client.close()
