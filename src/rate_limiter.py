"""Per-host rate limiter registry using aiolimiter.

Provides independent rate limiting for each API provider (SteamSpy, Gamalytic, Steam Store)
to allow concurrent requests to different hosts while respecting per-host rate limits.
"""

from dataclasses import dataclass
from aiolimiter import AsyncLimiter


@dataclass(frozen=True)
class RateLimitConfig:
    """Rate limit configuration for a host.

    Args:
        max_rate: Maximum requests allowed in the time period
        time_period: Time period in seconds
    """
    max_rate: float
    time_period: float


# Per-host rate limit configurations
RATE_LIMITS: dict[str, RateLimitConfig] = {
    "steamspy.com": RateLimitConfig(max_rate=1, time_period=1.0),  # 1 req/sec
    "store.steampowered.com": RateLimitConfig(max_rate=1, time_period=1.5),  # ~0.67 req/sec (Phase 1 conservative limit)
    "api.steampowered.com": RateLimitConfig(max_rate=1, time_period=1.5),  # Same as store
    "gamalytic.com": RateLimitConfig(max_rate=5, time_period=1.0),  # 5 req/s (Phase 8 verified)
    "shared.akamai.steamstatic.com": RateLimitConfig(max_rate=10, time_period=1.0),  # CDN: high throughput
}

# Safe fallback for unknown hosts
DEFAULT_RATE_LIMIT = RateLimitConfig(max_rate=1, time_period=2.0)


class HostRateLimiter:
    """Registry of per-host AsyncLimiter instances.

    Provides lazy-initialized rate limiters for each API host, allowing concurrent
    requests to different hosts while respecting per-host rate limits.

    Example:
        limiter = HostRateLimiter()
        await limiter.acquire("steamspy.com")
        # Make request to SteamSpy
    """

    def __init__(self):
        """Initialize the rate limiter registry with empty limiter dict."""
        self._limiters: dict[str, AsyncLimiter] = {}

    def _get_config(self, host: str) -> RateLimitConfig:
        """Get rate limit configuration for a host.

        Args:
            host: The host to look up (can be full URL, will match substring)

        Returns:
            RateLimitConfig for the host, or DEFAULT_RATE_LIMIT if not found
        """
        for known_host, config in RATE_LIMITS.items():
            if known_host in host:
                return config
        return DEFAULT_RATE_LIMIT

    def get_limiter(self, host: str) -> AsyncLimiter:
        """Get or create an AsyncLimiter for the given host.

        Lazy initialization: creates limiter on first access for each host.
        Subsequent calls for the same host return the cached limiter.

        Args:
            host: The host to get a limiter for

        Returns:
            AsyncLimiter instance configured for the host's rate limits
        """
        if host not in self._limiters:
            config = self._get_config(host)
            self._limiters[host] = AsyncLimiter(
                max_rate=config.max_rate,
                time_period=config.time_period
            )
        return self._limiters[host]

    async def acquire(self, host: str) -> None:
        """Acquire rate limit token for the given host.

        Convenience method that wraps get_limiter().acquire().
        Call this before making a request to the host.

        Args:
            host: The host to acquire rate limit for
        """
        await self.get_limiter(host).acquire()

    def get_config(self, host: str) -> RateLimitConfig:
        """Get the rate limit configuration for a host.

        Public accessor for host configuration, useful for logging/debugging.

        Args:
            host: The host to get configuration for

        Returns:
            RateLimitConfig for the host
        """
        return self._get_config(host)
