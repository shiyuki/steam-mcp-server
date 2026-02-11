"""Rate-limited async HTTP client for Steam API requests."""

import asyncio
import httpx
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
)


class RateLimitedClient:
    """Async HTTP client with rate limiting and exponential backoff retry.

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

    async def _enforce_rate_limit(self) -> None:
        """Enforce minimum delay between requests."""
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
