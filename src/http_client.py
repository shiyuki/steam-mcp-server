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
    retry_if_exception,
)

from src.cache import TTLCache, make_cache_key
from src.rate_limiter import HostRateLimiter
from src.timeout_utils import PER_REQUEST_TIMEOUT


def _is_retryable(exc: BaseException) -> bool:
    """Return True if the exception should trigger a retry.

    429 (Too Many Requests) is NOT retried — it indicates a quota limit
    that won't clear within retry windows, wasting ~11s per game.
    503 and other HTTP errors are still retried as transient failures.
    """
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code != 429
    return isinstance(exc, (httpx.HTTPError, asyncio.TimeoutError))


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
        retry=retry_if_exception(_is_retryable),
    )
    async def _fetch(self, url: str, params: dict = None, headers: dict = None) -> dict:
        """Make a rate-limited GET request with retry. No caching.

        Args:
            url: The URL to request
            params: Optional query parameters
            headers: Optional extra headers for the request

        Returns:
            dict: Parsed JSON response

        Raises:
            httpx.HTTPError: If request fails after retries
            asyncio.TimeoutError: If request times out after retries
        """
        host = self._extract_host(url)
        await self.rate_limiter.acquire(host)
        response = await self.client.get(url, params=params, headers=headers)
        response.raise_for_status()
        return response.json()

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1.5, min=1, max=10),
        retry=retry_if_exception(_is_retryable),
    )
    async def _fetch_bytes(self, url: str, headers: dict = None) -> bytes:
        """Make a rate-limited GET request returning raw bytes. No caching.

        Args:
            url: The URL to request
            headers: Optional extra headers for the request

        Returns:
            bytes: Raw response content

        Raises:
            httpx.HTTPError: If request fails after retries
            asyncio.TimeoutError: If request times out after retries
        """
        host = self._extract_host(url)
        await self.rate_limiter.acquire(host)
        response = await self.client.get(url, headers=headers)
        response.raise_for_status()
        return response.content

    async def _fetch_bytes_with_timeout(self, url: str, headers: dict = None) -> bytes:
        """Wrap _fetch_bytes() in a per-request timeout."""
        return await asyncio.wait_for(
            self._fetch_bytes(url, headers),
            timeout=PER_REQUEST_TIMEOUT,
        )

    async def _fetch_with_timeout(self, url: str, params: dict = None, headers: dict = None) -> dict:
        """Wrap _fetch() (including all retries) in a per-request timeout.

        Prevents a single request from blocking indefinitely when retries
        combine with rate limiter contention.

        Raises:
            asyncio.TimeoutError: If the entire retry cycle exceeds PER_REQUEST_TIMEOUT.
        """
        return await asyncio.wait_for(
            self._fetch(url, params, headers),
            timeout=PER_REQUEST_TIMEOUT,
        )

    async def get_bytes(self, url: str, cache_ttl: float = None, headers: dict = None) -> bytes:
        """Make a cached, rate-limited GET request returning raw bytes.

        Args:
            url: The URL to request
            cache_ttl: Cache TTL in seconds. None uses cache default. 0 bypasses cache.
            headers: Optional extra headers for the request

        Returns:
            bytes: Raw response content
        """
        if cache_ttl == 0:
            return await self._fetch_bytes_with_timeout(url, headers)

        host = self._extract_host(url)
        endpoint = url.split(host)[-1] if host else url
        cache_key = make_cache_key(f"bytes:{host}", endpoint)

        entry = await self.cache.get_or_fetch(
            key=cache_key,
            fetch_func=lambda: self._fetch_bytes_with_timeout(url, headers),
            ttl=cache_ttl,
        )
        return entry.value

    async def get(self, url: str, params: dict = None, cache_ttl: float = None, headers: dict = None) -> dict:
        """Make a cached, rate-limited GET request.

        Args:
            url: The URL to request
            params: Optional query parameters
            cache_ttl: Cache TTL in seconds. None uses cache default. 0 bypasses cache.
            headers: Optional extra headers for the request

        Returns:
            dict: Parsed JSON response
        """
        if cache_ttl == 0:
            # Bypass cache, just fetch with rate limiting
            return await self._fetch_with_timeout(url, params, headers)

        host = self._extract_host(url)
        endpoint = url.split(host)[-1] if host else url
        auth_state = None
        if headers:
            for k in headers:
                if k.lower() in ("authorization", "x-api-key", "api-key"):
                    auth_state = "authed"
                    break
        cache_key = make_cache_key(host, endpoint, params, auth_state=auth_state)

        entry = await self.cache.get_or_fetch(
            key=cache_key,
            fetch_func=lambda: self._fetch_with_timeout(url, params, headers),
            ttl=cache_ttl,
        )
        return entry.value

    async def get_with_metadata(self, url: str, params: dict = None, cache_ttl: float = None, headers: dict = None) -> tuple[dict, datetime, int]:
        """Like get(), but also returns (data, fetched_at, cache_age_seconds).

        Use this when the caller needs freshness metadata for the response.

        Args:
            url: The URL to request
            params: Optional query parameters
            cache_ttl: Cache TTL in seconds. None uses cache default. 0 bypasses cache.
            headers: Optional extra headers for the request

        Returns:
            Tuple of (data dict, fetched_at datetime, cache_age_seconds int)
        """
        if cache_ttl == 0:
            data = await self._fetch_with_timeout(url, params, headers)
            return data, datetime.now(timezone.utc), 0

        host = self._extract_host(url)
        endpoint = url.split(host)[-1] if host else url
        auth_state = None
        if headers:
            for k in headers:
                if k.lower() in ("authorization", "x-api-key", "api-key"):
                    auth_state = "authed"
                    break
        cache_key = make_cache_key(host, endpoint, params, auth_state=auth_state)

        entry = await self.cache.get_or_fetch(
            key=cache_key,
            fetch_func=lambda: self._fetch_with_timeout(url, params, headers),
            ttl=cache_ttl,
        )
        cache_age = int(time.monotonic() - entry.created_at)
        return entry.value, entry.fetched_at, cache_age

    async def get_with_metadata_stale(
        self,
        url: str,
        params: dict = None,
        cache_ttl: float = None,
        stale_ttl: float = None,
        headers: dict = None,
    ) -> tuple[dict, datetime, int, bool]:
        """Like get_with_metadata(), but adds stale-while-revalidate support.

        When the cache entry is within the stale window (cache_ttl < age < stale_ttl),
        attempts to revalidate. If revalidation fails, returns stale data with
        is_stale=True so callers can surface a warning.

        Args:
            url: The URL to request
            params: Optional query parameters
            cache_ttl: Fresh TTL in seconds. None uses cache default. 0 bypasses cache.
            stale_ttl: Stale window TTL in seconds. None means no stale window (identical
                to get_with_metadata). Must be >= cache_ttl if provided.
            headers: Optional extra headers for the request

        Returns:
            Tuple of (data dict, fetched_at datetime, cache_age_seconds int, is_stale bool).
            is_stale is True only when serving stale cached data after a failed revalidation.
        """
        if cache_ttl == 0:
            data = await self._fetch_with_timeout(url, params, headers)
            return data, datetime.now(timezone.utc), 0, False

        host = self._extract_host(url)
        endpoint = url.split(host)[-1] if host else url
        auth_state = None
        if headers:
            for k in headers:
                if k.lower() in ("authorization", "x-api-key", "api-key"):
                    auth_state = "authed"
                    break
        cache_key = make_cache_key(host, endpoint, params, auth_state=auth_state)

        entry, is_stale = await self.cache.get_or_fetch_stale(
            key=cache_key,
            fetch_func=lambda: self._fetch_with_timeout(url, params, headers),
            ttl=cache_ttl,
            stale_ttl=stale_ttl,
        )
        cache_age = int(time.monotonic() - entry.created_at)
        return entry.value, entry.fetched_at, cache_age, is_stale

    async def close(self):
        """Close the HTTP client."""
        await self.client.aclose()

    async def __aenter__(self):
        """Context manager entry."""
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit."""
        await self.close()
