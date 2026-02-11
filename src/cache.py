"""TTL-based in-memory cache with stampede prevention."""

import asyncio
import hashlib
import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Awaitable


@dataclass
class CacheEntry:
    """Single cache entry with TTL and freshness tracking."""
    value: Any
    created_at: float  # time.monotonic() when stored
    fetched_at: datetime  # datetime.now(timezone.utc) when API response was fetched
    ttl: float  # TTL in seconds


class TTLCache:
    """In-memory TTL cache with per-key stampede prevention.

    Uses time.monotonic() for TTL checks (immune to system clock changes).
    Uses asyncio.Lock per key to prevent cache stampede.

    Example:
        cache = TTLCache(default_ttl=3600)

        async def fetch_game(appid):
            return await api.get_app_details(appid)

        # First call fetches, second call returns cached
        entry = await cache.get_or_fetch(key, fetch_game, ttl=86400)
        cached_entry = await cache.get_or_fetch(key, fetch_game)
    """

    def __init__(self, default_ttl: float = 3600):
        """Initialize cache with default TTL.

        Args:
            default_ttl: Default TTL in seconds (default: 1 hour)
        """
        self._cache: dict[str, CacheEntry] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._locks_lock = asyncio.Lock()
        self._default_ttl = default_ttl

    async def get_or_fetch(
        self,
        key: str,
        fetch_func: Callable[[], Awaitable[Any]],
        ttl: float | None = None
    ) -> CacheEntry:
        """Get cached value or fetch if missing/expired.

        Stampede prevention: Concurrent calls for same key result in single fetch.

        Args:
            key: Cache key
            fetch_func: Async function to call on cache miss
            ttl: TTL override in seconds (uses default_ttl if None)

        Returns:
            CacheEntry with value, fetched_at, and cache age
        """
        # Get or create per-key lock
        async with self._locks_lock:
            if key not in self._locks:
                self._locks[key] = asyncio.Lock()
            lock = self._locks[key]

        # Per-key lock prevents stampede
        async with lock:
            # Check cache
            if key in self._cache:
                entry = self._cache[key]
                age = time.monotonic() - entry.created_at
                if age < entry.ttl:
                    # Cache hit - update cache_age_seconds if value supports it
                    if hasattr(entry.value, 'cache_age_seconds'):
                        entry.value.cache_age_seconds = round(age)
                    return entry

            # Cache miss or expired - fetch
            value = await fetch_func()
            use_ttl = ttl if ttl is not None else self._default_ttl
            entry = CacheEntry(
                value=value,
                created_at=time.monotonic(),
                fetched_at=datetime.now(timezone.utc),
                ttl=use_ttl
            )
            self._cache[key] = entry
            return entry

    def get(self, key: str) -> CacheEntry | None:
        """Get cached entry without fetching.

        Returns None if key missing or expired.

        Args:
            key: Cache key

        Returns:
            CacheEntry if exists and not expired, None otherwise
        """
        if key not in self._cache:
            return None

        entry = self._cache[key]
        age = time.monotonic() - entry.created_at
        if age >= entry.ttl:
            return None

        # Update cache_age_seconds if value supports it
        if hasattr(entry.value, 'cache_age_seconds'):
            entry.value.cache_age_seconds = round(age)
        return entry

    def invalidate(self, key: str) -> None:
        """Remove a cache entry.

        Args:
            key: Cache key to remove
        """
        self._cache.pop(key, None)

    def clear(self) -> None:
        """Remove all cache entries."""
        self._cache.clear()

    @property
    def size(self) -> int:
        """Return number of entries (including expired)."""
        return len(self._cache)


def make_cache_key(host: str, endpoint: str, params: dict | None = None) -> str:
    """Generate deterministic cache key from host, endpoint, and params.

    Args:
        host: API host (e.g., "steamspy.com")
        endpoint: API endpoint (e.g., "/api.php")
        params: Query parameters (optional)

    Returns:
        Cache key string

    Examples:
        >>> make_cache_key("steamspy.com", "/api.php")
        'steamspy.com:/api.php'
        >>> make_cache_key("steamspy.com", "/api.php", {"request": "tag", "tag": "rpg"})
        'steamspy.com:/api.php:a1b2c3d4'
    """
    if params is None or not params:
        return f"{host}:{endpoint}"

    # Deterministic hash: sort_keys ensures same params = same hash
    param_str = json.dumps(params, sort_keys=True)
    param_hash = hashlib.md5(param_str.encode()).hexdigest()[:8]
    return f"{host}:{endpoint}:{param_hash}"
