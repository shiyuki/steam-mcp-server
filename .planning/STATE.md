# Project State

## Project Reference

See: .planning/PROJECT.md (updated 2026-02-11)

**Core value:** Enable rapid, data-backed answers to "Is this genre a gold mine or a graveyard?" — unified data access that powers the full research workflow from market sizing to opportunity identification.

**Current focus:** v1.1 — Engagement + Commercial Data (Phase 5: Data Enrichment next)

## Current Position

Phase: milestone-uat of 5 (UAT Gap Closure) — COMPLETE
Plan: milestone-uat-01 of 1 complete (bowling fallback fingerprint corrected)
Status: Ready for Phase 5
Last activity: 2026-02-13 — Milestone UAT gap closure complete, bowling fallback detection fixed

Progress: [█████████░] 94% (15/16 estimated plans)

## Performance Metrics

**Velocity:**
- Total plans completed: 15
- Average duration: 3.1 min
- Total execution time: 0.84 hours

**By Phase:**

| Phase | Plans | Total | Avg/Plan |
|-------|-------|-------|----------|
| 01-mcp-foundation | 5 | 18 min | 3.6 min |
| 02-infrastructure-layer | 3 | 18 min | 6.0 min |
| 03-commercial-data | 2 | 6 min | 3.0 min |
| 04-engagement-data | 4 | 13 min | 3.3 min |
| milestone-uat | 1 | 1 min | 1.0 min |

**Recent Trend:**
- Last 5 plans: 04-01 (2min), 04-02 (3min), 04-03 (4min), 04-04 (4min), milestone-uat-01 (1min)
- Trend: Very fast gap closure, consistent execution speed overall

*Updated after each plan completion*

## Accumulated Context

### Decisions

Decisions are logged in PROJECT.md Key Decisions table.
Recent decisions affecting current work:

- Python for MCP (preferred language, good SDK support via `mcp` package)
- Steam API before SteamDB (stable foundation, need AppIDs first)
- Personal tool, no multi-user (faster iteration, complexity reduction)
- Skip Reddit/Twitch/YouTube initially (focus on core market data)

**From Phase 01 Plan 01:**
- Use uv for package management (10-100x faster than pip) — 01-01
- Rate limit 1.5s between Steam API requests (undocumented limits) — 01-01
- Exponential backoff with 1.5x multiplier, 3 retry attempts — 01-01
- Config.validate() called explicitly at startup, not at import time — 01-01

**From Phase 01 Plan 03:**
- Return APIError objects instead of raising exceptions for MCP compliance — 01-03
- Use Pydantic Config.extra='ignore' to handle extra API fields gracefully — 01-03
- Extract genres as tags from Steam Store API (no direct tag endpoint) — 01-03

**From Phase 01 Plan 04:**
- Tool inputs use primitive parameters (str, int) to avoid MCP SDK serialization issues — 01-04
- API responses use Pydantic models from schemas.py for parsing Steam API JSON — 01-04
- Config.validate() called at server startup for fail-fast without valid API key — 01-04
- Tools registered at module load time before server accepts requests — 01-04

**From Phase 02 Plan 01:**
- Use time.monotonic() for TTL checks instead of time.time() (immune to system clock changes) — 02-01
- Use datetime.now(timezone.utc) for API timestamps instead of deprecated datetime.utcnow() — 02-01
- Check hasattr before updating cache_age_seconds to support any cached value type — 02-01
- Use round() instead of int() for cache_age_seconds to handle sub-second ages — 02-01

**From Phase 02 Plan 02:**
- Use aiolimiter AsyncLimiter for leaky bucket per-host rate limiting — 02-02
- Store rate limits in module-level RATE_LIMITS dict for one-line-per-host reconfiguration — 02-02
- Match hosts via substring (e.g., "steamspy.com" in URL) for flexibility — 02-02
- Lazy-initialize limiters on first access to avoid creating unused instances — 02-02
- Provide DEFAULT_RATE_LIMIT (1 req/2s) for unknown hosts as safe fallback — 02-02

**From Phase 02 Plan 03:**
- SteamSpy search results cached for 1 hour (3600s) - reasonable freshness for tag searches — 02-03
- Steam Store metadata cached for 24 hours (86400s) - game details change rarely — 02-03
- Errors fail honestly without stale cache fallback - data quality over availability — 02-03
- Kept RateLimitedClient as deprecated for backward compatibility during transition — 02-03

**From Phase 03 Plan 01:**
- Gamalytic API requires no authentication (free public API) — 03-01
- Convert single Gamalytic revenue to +/-20% range for uncertainty representation — 03-01
- Fallback multiplier: reviews x 30 (min) to 50 (max) for review-based estimates — 03-01
- Triangulation triggers when SteamSpy diverges >20% from Gamalytic — 03-01
- SteamSpy price in cents, Gamalytic price in dollars (different unit conventions) — 03-01

**From Phase 03 Plan 02:**
- fetch_commercial validates appid (>0) at tool boundary, not in client — 03-02
- Test triangulation logic with side_effect list for sequential API calls — 03-02
- Include comma-parsing test even though implementation handles it (regression prevention) — 03-02

**From Phase 04 Plan 01:**
- Playtime converted from minutes to hours for user-friendly display — 04-01
- Price in dollars (0.0 for F2P) differs from Phase 3 CommercialData (None for F2P) — 04-01
- playtime_engagement is ratio not percentage (median/average) for distribution skew analysis — 04-01
- Quality flags based on presence of other data - zero with data = "available", all zeros = "zero_uncertain" — 04-01

**From Phase 04 Plan 02:**
- Per-metric stats require minimum 2 data points (quantiles constraint) — 04-02
- Market-weighted metrics aggregate raw values then compute ratios (emphasizes popular games) — 04-02
- Per-game average metrics compute ratios per game then average (emphasizes typical game) — 04-02
- Tag-based queries use bulk endpoint (1 API call), AppID lists use concurrent individual calls — 04-02

**From Phase 04 Plan 04:**
- TAG_ALIASES dict with 27 common tag variants for SteamSpy normalization — 04-04
- normalize_tag() is module-level (not class method) for reusability across clients — 04-04
- Bowling fallback detection uses >=2 known AppIDs in result set (<100 games) — 04-04
- Normalized tag returned in SearchResult/AggregateEngagementData (caller sees what was queried) — 04-04
- Unrecognized tags return APIError with error_type="tag_not_found" instead of silent fallback — 04-04

**From Milestone UAT Plan 01:**
- Real bowling fallback AppIDs: {901583, 12210, 2990, 891040, 22230} empirically verified from SteamSpy — milestone-uat-01
- Tests use real AppIDs for non-circular validation instead of invented fingerprint — milestone-uat-01

### Pending Todos

None yet.

### Blockers/Concerns

**v1.1 milestone focus:**
- Gamalytic API availability unconfirmed (MEDIUM confidence) — verify with test request in Phase 3
- SteamSpy field names may vary (average_forever vs average_playtime_forever) — verify with real API response in Phase 4

**Research-flagged risks:**
- ~~Cache race conditions from concurrent access~~ — RESOLVED: asyncio.Lock per cache key in 02-01
- ~~Concurrent fetching may exceed per-host rate limits~~ — RESOLVED: Per-host rate limiter registry in 02-02
- ~~TTL cache memory leak without eviction~~ — DECISION: Memory-only cache, restarts fresh (simple, acceptable for personal tool)
- ~~Per-host rate limiting is critical foundation for concurrent fetching~~ — RESOLVED: Phase 2 complete with unified HTTP client in 02-03

## Session Continuity

Last session: 2026-02-13T02:33:53Z
Stopped at: Completed milestone-uat-01-PLAN.md — bowling fallback detection corrected
Resume file: None

Config (if exists):
{
  "mode": "yolo",
  "depth": "standard",
  "parallelization": true,
  "commit_docs": true,
  "model_profile": "balanced",
  "workflow": {
    "research": true,
    "plan_check": true,
    "verifier": true
  }
}
