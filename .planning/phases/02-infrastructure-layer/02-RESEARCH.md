# Phase 2: Infrastructure Layer - Research

**Researched:** 2026-02-11
**Domain:** Python async caching and per-host rate limiting infrastructure
**Confidence:** HIGH

## Summary

This phase builds shared infrastructure for caching, per-host rate limiting, and data freshness tracking to support multiple API hosts (SteamSpy, Gamalytic, Steam Store). The user has made clear decisions: memory-only cache (no disk persistence), always include `fetched_at`/`cache_age_seconds` in responses, fail honestly on errors (no stale cache serving), and hardcoded per-host rate limits with easy reconfigurability.

The standard Python async stack uses **simple dict-based TTL cache with asyncio.Lock** for cache concurrency protection (aiocache adds unnecessary complexity for memory-only caching), **aiolimiter** for per-host rate limiting (mature leaky bucket implementation), and **time.monotonic()** for cache age tracking (better than time.time() for duration measurement). The critical architectural pattern is **per-host clients** with independent rate limiters and a shared cache keyed by `(host, endpoint, params)`.

Research confirmed SteamSpy rate limits (1 req/sec standard, 1 req/60s for "all" endpoint), Steam Store appdetails (200 req/5min = ~2.5 req/sec), and Gamalytic limits are TBD. Cache stampede prevention is essential: use **asyncio.Lock per cache key** to ensure only one coroutine fetches when cache misses, preventing thundering herd where 100 concurrent requests all hit the API simultaneously.

**Primary recommendation:** Build a simple dict-based TTL cache with per-key locks and per-host rate limiters. Don't use aiocache (overkill for memory-only), don't hand-roll rate limiting (use aiolimiter), always protect cache checks with locks to prevent stampede.

## Standard Stack

The established libraries/tools for async caching and rate limiting in Python:

### Core
| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| aiolimiter | 1.2+ | Per-host rate limiting | Mature leaky bucket implementation, precise asyncio integration, widely used in async Python ecosystem |
| Python dict | stdlib | In-memory TTL cache | Simplest solution for memory-only caching, no dependencies, full control over eviction |
| asyncio.Lock | stdlib | Cache stampede prevention | Official Python async primitive, prevents thundering herd on cache miss |
| time.monotonic() | stdlib | Cache age tracking | Better than time.time() for duration measurement (not affected by system clock changes) |
| datetime.now(timezone.utc) | stdlib | Timestamp generation | Modern UTC timestamp approach (utcnow() deprecated in Python 3.12+) |

### Supporting
| Library | Version | Purpose | When to Use |
|---------|---------|---------|-------------|
| cachetools | 7.0+ | TTLCache reference implementation | If dict-based approach becomes too complex (provides maxsize eviction) |
| async-lru | 1.0+ | Decorator-based caching | For simple function memoization (not needed here - we need cache control) |

### Alternatives Considered
| Instead of | Could Use | Tradeoff |
|------------|-----------|----------|
| dict-based cache | aiocache SimpleMemoryCache | aiocache adds Redis/Memcached abstraction we don't need; overkill for memory-only |
| dict-based cache | cachetools TTLCache | Less control over stampede prevention; adds dependency when dict + cleanup works |
| aiolimiter | Custom rate limiter with asyncio.Semaphore + sleep | Harder to get right (leaky bucket math, precision), reinventing wheel |

**Installation:**
```bash
uv add aiolimiter
```

No other dependencies needed - cache and timestamps use stdlib only.

## Architecture Patterns

### Recommended Project Structure
```
src/
├── cache.py            # TTL cache with stampede prevention
├── rate_limiters.py    # Per-host AsyncLimiter instances
├── http_client.py      # REFACTOR: PerHostRateLimitedClient (was RateLimitedClient)
├── steam_api.py        # REFACTOR: Inject cache, use per-host clients
└── schemas.py          # ADD: fetched_at, cache_age_seconds to response models
```

### Pattern 1: Per-Host Rate Limiter Architecture
**What:** Each API host (SteamSpy, Gamalytic, Steam Store) gets its own AsyncLimiter instance, allowing concurrent requests to different hosts while respecting per-host limits.

**When to use:** Always for multi-host API infrastructure where different hosts have different rate limits.

**Example:**
```python
# Source: aiolimiter official docs + research findings
from aiolimiter import AsyncLimiter

class RateLimiters:
    """Container for per-host rate limiters."""

    def __init__(self):
        # SteamSpy: 1 req/sec (source: https://steamspy.com/api.php)
        self.steamspy = AsyncLimiter(max_rate=1, time_period=1.0)

        # Steam Store appdetails: 200 req/5min = 40 req/60s
        # Source: Medium article on Steam Store API scraping
        self.steam_store = AsyncLimiter(max_rate=40, time_period=60.0)

        # Gamalytic: TBD - use conservative default until verified
        self.gamalytic = AsyncLimiter(max_rate=10, time_period=60.0)

    def get_limiter(self, host: str) -> AsyncLimiter:
        """Get rate limiter for specific host."""
        if "steamspy.com" in host:
            return self.steamspy
        elif "steampowered.com" in host:
            return self.steam_store
        elif "gamalytic.com" in host:
            return self.gamalytic
        else:
            # Default conservative rate limit
            return AsyncLimiter(max_rate=10, time_period=60.0)

# Usage in HTTP client
async def get(self, url: str, params: dict = None) -> dict:
    limiter = self.rate_limiters.get_limiter(url)
    async with limiter:
        response = await self.client.get(url, params=params)
        response.raise_for_status()
        return response.json()
```

**Why this works:** Different hosts can't block each other (SteamSpy slowness doesn't delay Steam Store), easy to reconfigure per-host limits (one-line change in `__init__`), AsyncLimiter handles concurrency correctly with leaky bucket algorithm.

### Pattern 2: TTL Cache with Stampede Prevention
**What:** Dict-based cache with per-key locks to prevent thundering herd. When cache misses, only the first coroutine fetches; others wait for the lock and then find cache populated.

**When to use:** Always for shared cache accessed by concurrent requests. Essential to prevent cache stampede where 100 concurrent cache misses trigger 100 API calls.

**Example:**
```python
# Source: Research on async cache patterns + asyncio.Lock docs
import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import time

@dataclass
class CacheEntry:
    """Cache entry with data and metadata."""
    data: dict
    fetched_at: datetime  # UTC timestamp when fetched
    expires_at: float     # monotonic time when entry expires

class TTLCache:
    """Thread-safe TTL cache with stampede prevention."""

    def __init__(self, default_ttl: float = 3600):
        """Initialize cache.

        Args:
            default_ttl: Default time-to-live in seconds
        """
        self._cache: dict[str, CacheEntry] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._locks_lock = asyncio.Lock()  # Protects _locks dict itself
        self.default_ttl = default_ttl

    async def _get_lock(self, key: str) -> asyncio.Lock:
        """Get or create lock for specific cache key."""
        async with self._locks_lock:
            if key not in self._locks:
                self._locks[key] = asyncio.Lock()
            return self._locks[key]

    async def get_or_fetch(
        self,
        key: str,
        fetch_func,
        ttl: float | None = None
    ) -> tuple[dict, datetime, int]:
        """Get from cache or fetch if missing/expired.

        Args:
            key: Cache key
            fetch_func: Async callable to fetch data if cache miss
            ttl: Time-to-live in seconds (uses default if None)

        Returns:
            tuple of (data, fetched_at, cache_age_seconds)
        """
        ttl = ttl if ttl is not None else self.default_ttl
        lock = await self._get_lock(key)

        async with lock:  # Only one coroutine fetches per key
            now_monotonic = time.monotonic()

            # Check if cached and not expired
            if key in self._cache:
                entry = self._cache[key]
                if now_monotonic < entry.expires_at:
                    # Cache hit
                    age_seconds = int((datetime.now(timezone.utc) - entry.fetched_at).total_seconds())
                    return entry.data, entry.fetched_at, age_seconds

            # Cache miss or expired - fetch
            fetched_at = datetime.now(timezone.utc)
            data = await fetch_func()

            # Store in cache
            self._cache[key] = CacheEntry(
                data=data,
                fetched_at=fetched_at,
                expires_at=now_monotonic + ttl
            )

            return data, fetched_at, 0  # Just fetched, age is 0

    def evict_expired(self):
        """Remove expired entries (call periodically to prevent memory leak)."""
        now = time.monotonic()
        expired_keys = [k for k, v in self._cache.items() if now >= v.expires_at]
        for key in expired_keys:
            del self._cache[key]
            # Clean up lock if no longer needed
            if key in self._locks:
                del self._locks[key]
```

**Why this works:** Lock per cache key prevents stampede (100 concurrent requests for same AppID = 1 API call), time.monotonic() prevents issues from system clock changes, datetime.now(timezone.utc) provides honest timestamps for users, maxsize not needed initially (user accepted memory-only with clean slate on restart).

### Pattern 3: Cache Key Generation
**What:** Generate unique cache keys that combine host, endpoint, and normalized parameters.

**When to use:** Always for multi-host caching to prevent key collisions.

**Example:**
```python
# Source: General caching best practices
import hashlib
import json

def make_cache_key(host: str, endpoint: str, params: dict | None = None) -> str:
    """Generate cache key from request components.

    Args:
        host: API host (e.g., "steamspy.com")
        endpoint: API endpoint or request type
        params: Query parameters (will be sorted for consistency)

    Returns:
        Cache key string
    """
    # Normalize params for consistent keys
    if params:
        # Sort dict for deterministic ordering
        normalized = json.dumps(params, sort_keys=True)
        param_hash = hashlib.md5(normalized.encode()).hexdigest()[:8]
        return f"{host}:{endpoint}:{param_hash}"
    else:
        return f"{host}:{endpoint}"

# Examples:
# make_cache_key("steamspy.com", "tag", {"tag": "roguelike"})
# -> "steamspy.com:tag:a3f2c1b4"
#
# make_cache_key("steampowered.com", "appdetails", {"appids": "12345"})
# -> "steampowered.com:appdetails:8b4e2f3a"
```

### Pattern 4: Adding Cache Metadata to Responses
**What:** Enrich API response models with `fetched_at` and `cache_age_seconds` for transparency.

**When to use:** Always for cached API responses (user decision: always include, let LLM decide when to surface).

**Example:**
```python
# Source: User requirements + datetime best practices
from pydantic import BaseModel, Field
from datetime import datetime

class CachedResponse(BaseModel):
    """Base class for cached API responses."""
    fetched_at: datetime = Field(..., description="UTC timestamp when data was fetched")
    cache_age_seconds: int = Field(..., description="Seconds since data was fetched")

class GameMetadata(CachedResponse):
    """Metadata for a single Steam game with cache info."""
    appid: int
    name: str
    tags: list[str] = Field(default_factory=list)
    developer: str = ""
    description: str = ""
    # fetched_at and cache_age_seconds inherited from CachedResponse

# In API client:
async def get_app_details(self, appid: int) -> GameMetadata | APIError:
    cache_key = make_cache_key("steampowered.com", "appdetails", {"appids": str(appid)})

    async def fetch():
        # ... actual API call ...
        return data

    data, fetched_at, cache_age = await self.cache.get_or_fetch(
        cache_key,
        fetch,
        ttl=6 * 3600  # 6 hours for commercial data
    )

    return GameMetadata(
        appid=appid,
        name=data.get("name", "Unknown"),
        # ... other fields ...
        fetched_at=fetched_at,
        cache_age_seconds=cache_age
    )
```

### Anti-Patterns to Avoid
- **Global rate limiter for all hosts:** Defeats the purpose of per-host architecture. SteamSpy slowness would block Steam Store requests.
- **No stampede prevention:** 100 concurrent requests for same AppID = 100 API calls, wasting rate limit budget and risking 429 errors.
- **Serving stale cache on error:** User explicitly rejected this. Fail honestly with APIError when outside TTL or API down.
- **Using time.time() for cache age:** System clock changes break cache age calculation. Use time.monotonic() for durations.
- **Checking cache without lock:** Race condition where multiple coroutines see cache miss before any populates it (stampede).
- **datetime.utcnow():** Deprecated in Python 3.12+. Use datetime.now(timezone.utc) instead.

## Don't Hand-Roll

Problems that look simple but have existing solutions:

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Rate limiting algorithm | Custom Semaphore + sleep loop | aiolimiter.AsyncLimiter | Leaky bucket is subtle (burst handling, precision), battle-tested implementation |
| TTL cache eviction | Manual LRU + expiry tracking | cachetools.TTLCache (if needed) | Edge cases: maxsize + TTL interaction, thread safety, eviction timing |
| Concurrent request coalescing | Custom event coordination | async-lru or pattern 2 above | Easy to get wrong (deadlocks, race conditions in cleanup) |
| Timestamp generation | time.time() or datetime.utcnow() | datetime.now(timezone.utc) | utcnow() deprecated, time.time() doesn't have timezone awareness |

**Key insight:** Async concurrency primitives are subtle. asyncio.Lock looks simple but incorrect usage (lock held across await points that never return, nested locks) causes deadlocks. Rate limiting math (leaky bucket, token bucket) is harder than it appears. Use proven libraries for non-trivial algorithms.

## Common Pitfalls

### Pitfall 1: Cache Stampede (Thundering Herd)
**What goes wrong:** When popular cache entry expires, hundreds of concurrent requests all see cache miss and simultaneously call the API, overwhelming rate limits.

**Why it happens:** Multiple coroutines check cache before any completes the fetch. Without lock, all see empty cache and proceed to fetch.

**How to avoid:** Use asyncio.Lock per cache key (Pattern 2). Only first coroutine fetches; others wait for lock and find cache populated.

**Warning signs:** Sudden bursts of 429 rate limit errors, API latency spikes when cache expires, duplicate fetch logs for same key.

### Pitfall 2: Lock Held Across Blocking Await
**What goes wrong:** Deadlock or extreme slowness when lock is held while awaiting a slow operation.

**Why it happens:** asyncio.Lock blocks all other coroutines from acquiring the lock. If fetch takes 5 seconds and lock held during entire fetch, all other requests wait 5 seconds even if they could be doing other work.

**How to avoid:** This is actually CORRECT behavior for stampede prevention - we WANT other requests to wait rather than all fetching. Only problematic if lock held for non-critical work.

**Warning signs:** All requests serialized even for different cache keys (indicates global lock instead of per-key lock).

### Pitfall 3: System Clock Changes Break Cache TTL
**What goes wrong:** Cache entries expire immediately or last forever when system clock changes (NTP sync, daylight saving, manual adjustment).

**Why it happens:** Using time.time() for expiry tracking. time.time() is wall clock time, can jump backwards/forwards.

**How to avoid:** Use time.monotonic() for cache expiry (never goes backwards, unaffected by clock changes). Use datetime.now(timezone.utc) only for user-facing timestamps.

**Warning signs:** Cache entries mysteriously invalid after server time sync, inconsistent cache hit rates.

### Pitfall 4: Rate Limiter Per-Request Instead of Per-Host
**What goes wrong:** Creating new AsyncLimiter for each request means no rate limiting (each limiter allows full burst).

**Why it happens:** Misunderstanding limiter lifecycle - limiter must be shared across requests to track rate.

**How to avoid:** Create limiters once at startup (Pattern 1), pass shared instance to all requests for that host.

**Warning signs:** 429 errors despite "having rate limiting", limiter not reducing throughput.

### Pitfall 5: No Cache Eviction = Memory Leak
**What goes wrong:** Cache grows indefinitely, consuming all memory, server crashes.

**Why it happens:** Dict-based cache with TTL checks expiry on access, but expired entries never removed if not accessed. Old entries accumulate.

**How to avoid:** Either (1) accept risk for short-lived servers (user chose restart = clean slate), (2) add periodic cleanup task, or (3) use maxsize with LRU eviction.

**Warning signs:** Memory usage grows over time, never decreases, RSS grows even when traffic low.

### Pitfall 6: Cache Key Collisions
**What goes wrong:** Different API calls return cached data from wrong endpoint.

**Why it happens:** Cache key doesn't include enough context. E.g., using just AppID as key means SteamSpy tag search and Steam Store appdetails share keys.

**How to avoid:** Include host, endpoint, and params in cache key (Pattern 3). Use hashing for long param strings.

**Warning signs:** Incorrect data returned, cache hit when should be miss, schema validation errors.

## Code Examples

Verified patterns from official sources:

### Using AsyncLimiter for Rate Limiting
```python
# Source: https://aiolimiter.readthedocs.io/ + https://github.com/mjpieters/aiolimiter
from aiolimiter import AsyncLimiter
import asyncio

# SteamSpy: 1 request per second
steamspy_limiter = AsyncLimiter(max_rate=1, time_period=1.0)

async def fetch_from_steamspy(url: str) -> dict:
    async with steamspy_limiter:
        # Only 1 coroutine can be here per second
        # Others wait for their turn
        response = await client.get(url)
        return response.json()

# Concurrent requests are automatically queued
tasks = [fetch_from_steamspy(url) for url in urls]
results = await asyncio.gather(*tasks)  # All respect 1/sec limit
```

### Preventing Cache Stampede with asyncio.Lock
```python
# Source: https://docs.python.org/3/library/asyncio-sync.html + research findings
import asyncio

cache = {}
cache_locks = {}
locks_lock = asyncio.Lock()

async def get_or_fetch(key: str, fetch_func):
    # Get or create lock for this key
    async with locks_lock:
        if key not in cache_locks:
            cache_locks[key] = asyncio.Lock()
        lock = cache_locks[key]

    # Only one coroutine fetches per key
    async with lock:
        if key in cache:
            print(f"Cache hit: {key}")
            return cache[key]

        print(f"Cache miss, fetching: {key}")
        result = await fetch_func()
        cache[key] = result
        return result

# Simulate 100 concurrent requests for same key
async def fetch():
    await asyncio.sleep(1)  # Simulate slow API
    return {"data": "expensive fetch"}

# Only ONE "Cache miss, fetching" printed, 99 wait and get cached result
tasks = [get_or_fetch("same-key", fetch) for _ in range(100)]
results = await asyncio.gather(*tasks)
```

### Computing Cache Age Correctly
```python
# Source: https://docs.python.org/3/library/time.html + datetime best practices research
import time
from datetime import datetime, timezone

class CacheEntry:
    def __init__(self, data: dict):
        self.data = data
        self.fetched_at = datetime.now(timezone.utc)  # User-facing timestamp
        self.expires_at_mono = time.monotonic() + 3600  # Internal expiry tracking

    def is_expired(self) -> bool:
        """Check if entry expired (using monotonic time)."""
        return time.monotonic() >= self.expires_at_mono

    def age_seconds(self) -> int:
        """Calculate age in seconds (using UTC datetime)."""
        delta = datetime.now(timezone.utc) - self.fetched_at
        return int(delta.total_seconds())

# Usage:
entry = CacheEntry({"some": "data"})
await asyncio.sleep(5)

print(entry.is_expired())    # False (not expired yet)
print(entry.age_seconds())   # 5 (seconds since creation)
```

### Per-Host HTTP Client Architecture
```python
# Source: Combining aiolimiter pattern with existing http_client.py
from aiolimiter import AsyncLimiter
import httpx

class PerHostRateLimitedClient:
    """HTTP client with per-host rate limiting."""

    def __init__(self, rate_limiters: dict[str, AsyncLimiter], timeout: float = 30.0):
        """Initialize client.

        Args:
            rate_limiters: Dict mapping host to AsyncLimiter
            timeout: Request timeout
        """
        self.rate_limiters = rate_limiters
        self.client = httpx.AsyncClient(timeout=timeout)

    def _get_limiter(self, url: str) -> AsyncLimiter:
        """Get rate limiter for URL's host."""
        for host, limiter in self.rate_limiters.items():
            if host in url:
                return limiter
        # Default limiter for unknown hosts
        return AsyncLimiter(max_rate=10, time_period=60.0)

    async def get(self, url: str, params: dict = None) -> dict:
        """Rate-limited GET request."""
        limiter = self._get_limiter(url)
        async with limiter:
            response = await self.client.get(url, params=params)
            response.raise_for_status()
            return response.json()

    async def close(self):
        await self.client.aclose()

# Usage:
limiters = {
    "steamspy.com": AsyncLimiter(1, 1.0),
    "steampowered.com": AsyncLimiter(40, 60.0),
}
client = PerHostRateLimitedClient(limiters)

# These can run concurrently without blocking each other
steamspy_data = await client.get("https://steamspy.com/api.php", {...})
steam_store_data = await client.get("https://store.steampowered.com/api/appdetails", {...})
```

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| datetime.utcnow() | datetime.now(timezone.utc) | Python 3.12 (2023) | utcnow() returns naive datetime, now deprecated; use timezone-aware |
| aiocache for simple cache | dict + asyncio.Lock | Ongoing trend | aiocache designed for Redis/Memcached backends; overkill for memory-only |
| time.time() for cache TTL | time.monotonic() | Best practice since Python 3.3 | time.time() affected by system clock changes; monotonic() for durations |
| Global rate limiter | Per-host rate limiters | Always best practice | Allows concurrent requests to different hosts, better resource utilization |
| @lru_cache for async | async-lru, aiocache decorators | asyncio maturity (~2020+) | functools.lru_cache not async-aware, causes issues with coroutines |

**Deprecated/outdated:**
- **datetime.utcnow():** Returns naive datetime without timezone. Use datetime.now(timezone.utc) for timezone-aware UTC.
- **RateLimitedClient with single global limit:** Doesn't scale to multi-host architecture. Refactor to per-host.
- **aiocache for memory-only caching:** Adds Redis/Memcached abstraction overhead. Use simple dict for memory-only.

## Open Questions

Things that couldn't be fully resolved:

1. **Gamalytic API rate limits**
   - What we know: Gamalytic API exists at api.gamalytic.com, requires API key
   - What's unclear: Exact rate limits (req/sec or req/min), burst allowance, error codes
   - Recommendation: Use conservative default (10 req/min) until user tests or contacts Gamalytic support. Easy to update in RateLimiters.__init__() once confirmed.

2. **Cache maxsize eviction strategy**
   - What we know: User accepted memory-only with restart = clean slate; STATE.md flagged memory leak risk
   - What's unclear: Whether periodic cleanup task needed, or if server restarts frequent enough
   - Recommendation: Start without maxsize (Phase 2 scope = basic cache). If memory issues in Phase 3+, add periodic cleanup task (call evict_expired() every hour) or use cachetools.TTLCache with maxsize=10000.

3. **Steam Store API "all requests" vs specific endpoints**
   - What we know: appdetails has 200 req/5min limit, but unclear if this is per-endpoint or global
   - What's unclear: If fetching appdetails and other Store endpoints share the same 200/5min budget
   - Recommendation: Assume shared budget (conservative). Single steam_store limiter for all store.steampowered.com endpoints. Monitor for 429s and adjust if needed.

## Sources

### Primary (HIGH confidence)
- **aiolimiter official docs** - https://aiolimiter.readthedocs.io/ (rate limiting patterns, AsyncLimiter usage)
- **asyncio synchronization docs** - https://docs.python.org/3/library/asyncio-sync.html (Lock, Event, Condition primitives)
- **SteamSpy API documentation** - https://steamspy.com/api.php (1 req/sec standard, 1 req/60s "all", 24h refresh)
- **Python time module docs** - https://docs.python.org/3/library/time.html (time.monotonic() vs time.time())
- **aiolimiter GitHub** - https://github.com/mjpieters/aiolimiter (leaky bucket implementation, examples)

### Secondary (MEDIUM confidence)
- **Medium: Scraping Steam with Python** - Community documentation of Steam Store appdetails 200 req/5min limit
- **Medium: Avoiding Race Conditions in Python 2025** - https://medium.com/pythoneers/avoiding-race-conditions-in-python-in-2025-best-practices-for-async-and-threads-4e006579a622 (async cache stampede patterns)
- **OneUpTime: Cache Stampede Prevention** - https://oneuptime.com/blog/post/2026-01-21-redis-cache-stampede/view (thundering herd mitigation strategies)
- **OneUpTime: Request Coalescing** - https://oneuptime.com/blog/post/2026-01-25-request-coalescing/view (preventing duplicate concurrent fetches)
- **Miguel Grinberg: utcnow() deprecated** - https://blog.miguelgrinberg.com/post/it-s-time-for-a-change-datetime-utcnow-is-now-deprecated (datetime best practices)

### Tertiary (LOW confidence - marked for validation)
- **Gamalytic API docs** - https://api.gamalytic.com/reference/ (page exists but rate limits not extracted; requires direct testing)
- **Steam Store API unofficial docs** - Multiple community sources agree on 200 req/5min but no official Steam documentation found

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH - aiolimiter, asyncio.Lock, stdlib datetime are authoritative with official docs
- Architecture: HIGH - Per-host pattern verified in multiple sources, cache stampede prevention well-documented
- Pitfalls: HIGH - Cache stampede, clock changes, rate limiter lifecycle are well-known issues with official guidance
- Rate limits: MEDIUM - SteamSpy confirmed from official API page, Steam Store from community consensus, Gamalytic TBD

**Research date:** 2026-02-11
**Valid until:** 2026-03-15 (30 days - stable domain, rate limits don't change frequently)

**Notes for planner:**
- User decisions in CONTEXT.md are locked (memory-only cache, fail honestly on error, always include cache metadata, hardcoded rate limits with easy reconfiguration)
- Existing code has RateLimitedClient with global limit - Phase 2 must refactor to per-host architecture
- Existing schemas.py uses Pydantic - extend with CachedResponse base class for fetched_at/cache_age_seconds
- Existing tests verify single-host behavior - Phase 2 tests must verify per-host isolation (concurrent requests to different hosts don't block each other)
- Research confirms STATE.md flagged risks are real: cache stampede, concurrent rate limit violations, memory leak all have documented solutions in this research
