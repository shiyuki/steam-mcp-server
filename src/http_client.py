"""Rate-limited async HTTP client for Steam API requests."""

import asyncio
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

import httpx
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
)

from src.cache import TTLCache, make_cache_key
from src.rate_limiter import HostRateLimiter


class CachedAPIClient:
    """Async HTTP client with per-host rate limiting, caching, and retry.

    Integrates TTLCache for response caching and HostRateLimiter for
    per-host rate limiting, replacing the Phase 1 global rate limiter.
    """

    def __init__(self, cache: TTLCache, rate_limiter: HostRateLimiter, timeout: float = 30.0):
        """Initialize the cached API client.

        Args:
            cache: TTLCache instance for response caching
            rate_limiter: HostRateLimiter for per-host rate limiting
            timeout: Request timeout in seconds (default: 30.0)
        """
        self.cache = cache
        self.rate_limiter = rate_limiter
        self.client = httpx.AsyncClient(timeout=timeout)

    def _extract_host(self, url: str) -> str:
        """Extract hostname from URL for rate limiter lookup.

        Args:
            url: Full URL to extract hostname from

        Returns:
            Hostname (e.g., "steamspy.com" from "https://steamspy.com/api.php")
        """
        return urlparse(url).netloc

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1.5, min=1, max=10),
        retry=retry_if_exception_type((httpx.HTTPError, asyncio.TimeoutError)),
    )
    async def _fetch(self, url: str, params: dict = None) -> dict:
        """Make a rate-limited GET request with retry. No caching.

        Args:
            url: The URL to request
            params: Optional query parameters

        Returns:
            dict: Parsed JSON response

        Raises:
            httpx.HTTPError: If request fails after retries
            asyncio.TimeoutError: If request times out after retries
        """
        host = self._extract_host(url)
        await self.rate_limiter.acquire(host)
        response = await self.client.get(url, params=params)
        response.raise_for_status()
        return response.json()

    async def get(self, url: str, params: dict = None, cache_ttl: float = None) -> dict:
        """Make a cached, rate-limited GET request.

        Args:
            url: The URL to request
            params: Optional query parameters
            cache_ttl: Cache TTL in seconds. None uses cache default. 0 bypasses cache.

        Returns:
            dict: Parsed JSON response
        """
        if cache_ttl == 0:
            # Bypass cache, just fetch with rate limiting
            return await self._fetch(url, params)

        host = self._extract_host(url)
        endpoint = url.split(host)[-1] if host else url
        cache_key = make_cache_key(host, endpoint, params)

        entry = await self.cache.get_or_fetch(
            key=cache_key,
            fetch_func=lambda: self._fetch(url, params),
            ttl=cache_ttl,
        )
        return entry.value

    async def get_with_metadata(self, url: str, params: dict = None, cache_ttl: float = None) -> tuple[dict, datetime, int]:
        """Like get(), but also returns (data, fetched_at, cache_age_seconds).

        Use this when the caller needs freshness metadata for the response.

        Args:
            url: The URL to request
            params: Optional query parameters
            cache_ttl: Cache TTL in seconds. None uses cache default. 0 bypasses cache.

        Returns:
            Tuple of (data dict, fetched_at datetime, cache_age_seconds int)
        """
        if cache_ttl == 0:
            data = await self._fetch(url, params)
            return data, datetime.now(timezone.utc), 0

        host = self._extract_host(url)
        endpoint = url.split(host)[-1] if host else url
        cache_key = make_cache_key(host, endpoint, params)

        entry = await self.cache.get_or_fetch(
            key=cache_key,
            fetch_func=lambda: self._fetch(url, params),
            ttl=cache_ttl,
        )
        cache_age = int(time.monotonic() - entry.created_at)
        return entry.value, entry.fetched_at, cache_age

    async def close(self):
        """Close the HTTP client."""
        await self.client.aclose()

    async def __aenter__(self):
        """Context manager entry."""
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit."""
        await self.close()


# DEPRECATED: Phase 1 client, kept for backward compatibility during transition
# Will be removed in future cleanup. Use CachedAPIClient instead.
class RateLimitedClient:
    """Async HTTP client with rate limiting and exponential backoff retry.

    DEPRECATED: Use CachedAPIClient with TTLCache and HostRateLimiter instead.

    Enforces minimum delay between requests and retries failed requests
    with exponential backoff strategy.
    """

    def __init__(self, rate_limit_delay: float = 1.5, timeout: float = 30.0):
        """Initialize the rate-limited HTTP client.

        Args:
            rate_limit_delay: Minimum seconds between requests (default: 1.5)
            timeout: Request timeout in seconds (default: 30.0)
        """
        self.rate_limit_delay = rate_limit_delay
        self.last_request_time = 0.0
        self.client = httpx.AsyncClient(timeout=timeout)
        self._rate_limit_lock = asyncio.Lock()

    async def _enforce_rate_limit(self) -> None:
        """Enforce minimum delay between requests. Thread-safe for concurrent requests."""
        async with self._rate_limit_lock:
            now = asyncio.get_event_loop().time()
            elapsed = now - self.last_request_time
            if elapsed < self.rate_limit_delay:
                await asyncio.sleep(self.rate_limit_delay - elapsed)
            self.last_request_time = asyncio.get_event_loop().time()

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1.5, min=1, max=10),
        retry=retry_if_exception_type((httpx.HTTPError, asyncio.TimeoutError)),
    )
    async def get(self, url: str, params: dict = None) -> dict:
        """Make a rate-limited GET request with automatic retry.

        Args:
            url: The URL to request
            params: Optional query parameters

        Returns:
            dict: Parsed JSON response

        Raises:
            httpx.HTTPError: If request fails after retries
            asyncio.TimeoutError: If request times out after retries
        """
        await self._enforce_rate_limit()
        response = await self.client.get(url, params=params)
        response.raise_for_status()
        return response.json()

    async def close(self) -> None:
        """Close the HTTP client."""
        await self.client.aclose()

    async def __aenter__(self):
        """Context manager entry."""
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit."""
        await self.close()
