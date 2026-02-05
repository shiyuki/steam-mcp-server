# Project Research Summary

**Project:** Games Market Research MCP Server
**Domain:** MCP data-fetching tool -- Steam API + SteamDB/Gamalytic game market research
**Researched:** 2026-02-05
**Confidence:** MEDIUM-HIGH
**Language:** Python (updated from original TypeScript research)

## Executive Summary

This project is a Model Context Protocol (MCP) server written in Python that gives an LLM the ability to research Steam game markets by tag/genre. It pulls data from three independent layers -- Metadata (Steam Web API), Commercial (SteamDB scraping + Gamalytic API), and Engagement (SteamSpy + Steam reviews) -- and surfaces it as structured, LLM-consumable responses. The pattern is well-established: a small number of intent-shaped tools (3-5 max), each internally fanning out to whichever data sources it needs, all routed through shared retry and rate-limit infrastructure. The MCP SDK v1.x with stdio transport is the correct foundation. This is not a novel architecture problem -- it is a disciplined execution of known MCP + scraping patterns against a specific domain.

The single highest-risk element in this project is the Commercial Layer. SteamDB is Cloudflare-protected, actively bans scrapers, and is the richest source of revenue and pricing history. The correct posture is to treat SteamDB scraping as explicitly degradable from day one: design every tool that touches commercial data to function (with reduced confidence) when the scraper is blocked. Gamalytic's free, unauthenticated REST API is the real fallback -- not a secondary source to bolt on later. The Steam Web API and SteamSpy are stable, well-behaved REST APIs and should form the backbone of Metadata and Engagement respectively. Rate limits across all Steam-adjacent services are undocumented and inconsistently enforced; a conservative 1.5-second inter-request delay with exponential backoff is non-negotiable from the first API call.

Revenue triangulation -- cross-referencing SteamDB and Gamalytic estimates -- sounds like it adds confidence, but both sources derive their estimates from the same underlying Steam data (review counts, top-seller rankings). Their errors are correlated. Triangulation is useful for detecting outliers (disagreement > 50% flags a data point for review), not for producing a more accurate single number. All revenue figures must be presented as ranges with explicit confidence intervals. This is a presentation and data-contract decision that must be made in Phase 1, even though it is exercised in Phase 3.

## Key Findings

### Recommended Stack (Python)

**Core technologies:**
- `mcp` -- Official Python MCP SDK; stdio transport for local desktop clients
- `pydantic` -- Schema validation for tool inputs/outputs; Python's equivalent to zod
- `httpx` -- Async HTTP client; cleaner API than requests, better async support
- `playwright` (Python) -- Browser automation for SteamDB; same Cloudflare bypass capability
- `beautifulsoup4` -- HTML parsing; Python's equivalent to cheerio
- Python 3.11+ -- Async support, type hints

**Key insight from research:** Most patterns are language-agnostic. The MCP protocol, tool design principles, rate limiting strategies, and data source behaviors apply regardless of TypeScript vs Python.

### Expected Features

The tool surface must stay small. MCP best practices and documented LLM behavior both converge on the same conclusion: more than 5-10 tools degrades selection accuracy. The natural grouping is three core tools (`search_genre`, `fetch_metadata`, `fetch_commercial_data`), with `fetch_engagement_data` and `generate_report` added in later phases. Each tool answers a research question, not a data-source question. Internally, each tool may fan out to 2-3 clients concurrently.

**Must have (table stakes):**
- `search_genre` -- fetch AppIDs by Steam tag; the entry point for every research workflow
- `fetch_metadata` -- game names, tags, developer info via Steam Web API; context for everything downstream
- `fetch_commercial_data` -- pricing, revenue estimates via SteamDB/Gamalytic; the "is it commercially viable" signal
- Typed zod schemas on all tool inputs/outputs -- protocol-level requirement
- Structured error responses (`isError`, classified error codes) on all tools -- LLMs cannot recover from raw stack traces
- Input validation at tool boundaries -- reject bad AppIDs and malformed queries before hitting external APIs
- All logging routed to stderr only -- stdout is reserved for JSON-RPC; mixing kills the connection silently

**Should have (competitive):**
- `fetch_engagement_data` -- CCU, review counts/sentiment via SteamSpy + Steam reviews
- TTL-based caching per data type -- metadata 24-72h, commercial 6-12h, engagement 1-4h
- Concurrent fetching for independent sources -- Promise.all for Metadata + Commercial + Engagement
- Scrape health signaling -- confidence/freshness fields on scraped data so the LLM knows when to trust it
- Gamalytic triangulation -- cross-reference commercial estimates to detect outliers

**Defer (v2+):**
- `generate_report` tool -- report skeleton generation; let Claude assemble reports from structured data until the report shape is proven by real use
- Social sentiment layer (Reddit, Twitch, YouTube) -- three fragile scraping surfaces; core market data must be stable first
- Single orchestration mega-tool ("research this genre" in one call) -- premature without knowing the stable shape of each underlying tool

### Architecture Approach

The server is a thin MCP layer over a client-per-data-source design. Three tool files (`lookup`, `market`, `engagement`) map to LLM-facing intents. Each imports from isolated client modules (`steamApi`, `steamStore`, `steamSpy`) that own their own base URL, auth, and response parsing. All outbound HTTP flows through a single `fetchWithRetry` utility with exponential backoff, per-host rate tracking, and timeout. This separation is the key architectural decision: stable API clients (Steam Web API, SteamSpy) and fragile scraping clients (SteamDB store pages) share retry infrastructure but tune their own retry counts and timeouts independently.

**Major components:**
1. MCP Server (`server.ts`) -- declares tools, handles JSON-RPC lifecycle, routes calls; uses stdio transport
2. Tool handlers (`tools/lookup.ts`, `tools/market.ts`, `tools/engagement.ts`) -- one per research intent; orchestrate across clients, merge results, format for LLM consumption
3. Data source clients (`clients/steamApi.ts`, `clients/steamStore.ts`, `clients/steamSpy.ts`) -- isolated HTTP wrappers per external service; own parsing and auth
4. HTTP transport layer (`http/fetch.ts`, `http/rateLimiter.ts`) -- shared `fetchWithRetry` with exponential backoff; per-host token-bucket rate limiting
5. Type definitions (`types/steam.ts`, `types/tools.ts`) -- centralized zod schemas and Steam response shapes

### Critical Pitfalls

1. **stdout pollution kills the MCP server silently** -- any `console.log()` in stdio-mode corrupts the JSON-RPC stream with no useful error. All logging must go to stderr from the first line of code. Add an ESLint rule; run `grep -r "console.log" src/` before shipping.
2. **SteamDB scraping will get blocked, not just rate-limited** -- SteamDB FAQ explicitly warns of automatic bans. Design commercial data as degradable from day one. Gamalytic API is the production fallback, not a stretch goal. Cache aggressively; re-scrape only on demand with 5-10 second delays.
3. **Steam API rate limits are undocumented and silently enforced** -- 429 errors arrive at rates well below the commonly cited 200 req/5 min. Some endpoints return HTTP 200 with an empty body instead of 429. Default to 1.5s between requests; implement exponential backoff on every call.
4. **Revenue triangulation does not halve the error** -- SteamDB and Gamalytic both derive estimates from the same Steam data. Use triangulation to flag outliers (disagreement > 50%), not to average into a single number. All revenue must be presented as ranges.
5. **Tool interface explosion confuses the LLM** -- 15+ tools with overlapping semantics cause wrong selections and hallucinated parameters. Keep tool count under 10; name tools after research questions, not API endpoints.

## Implications for Roadmap

Based on research, suggested phase structure:

### Phase 1: MCP Skeleton + Metadata Foundation
**Rationale:** The MCP server + stdio transport + tool design decisions are the lowest-level architectural choices. Everything built after this sits on top of it. Metadata (Steam Web API) is the stable, well-documented foundation -- it produces AppIDs that every other tool depends on. This phase also establishes the shared HTTP transport layer (fetchWithRetry, rate limiter) that all subsequent phases reuse.
**Delivers:** Working MCP server connected to Claude Desktop/Cursor. `search_genre` and `fetch_metadata` tools functional. AppID resolution end-to-end. Shared retry/rate-limit infrastructure.
**Addresses:** search_genre (P1), fetch_metadata (P1), input validation (P1), structured error responses (P1), zod schemas (P1)
**Avoids:** stdout pollution (ESLint rule + grep check), tool interface explosion (3-tool design locked in), deprecated SSE transport (use stdio, not SSE)

### Phase 2: Engagement Layer
**Rationale:** SteamSpy is a stable, free, unauthenticated API. It is the cheapest layer to add once fetchWithRetry exists. It is also independent of the Commercial Layer -- adding it here gives the system two working data dimensions before touching the fragile scraping surface. Engagement data (CCU, playtime, ownership) is high-value and low-risk.
**Delivers:** `fetch_engagement_data` tool. CCU, review counts, sentiment from SteamSpy + Steam store JSON API. Concurrent fetching (Promise.all) for independent sources.
**Uses:** `steamSpy.ts` client, `fetchWithRetry` from Phase 1
**Implements:** Tool-per-query-intent pattern (engagement tool fans out to SteamSpy + Steam reviews concurrently)
**Avoids:** Steam API rate limits (shared rate limiter from Phase 1 applies)

### Phase 3: Commercial Layer + Caching
**Rationale:** This is the most fragile and highest-value layer. It must be built after the retry and rate-limit infrastructure is battle-tested (Phase 1+2). The JSON tier of the Steam store API (`/api/appdetails`) comes first -- it is stable, not Cloudflare-gated, and covers base price and review summary. SteamDB HTML scraping is added only after the JSON tier proves insufficient. Gamalytic API integration happens in parallel as the production-grade commercial data source. TTL caching is introduced here because the Commercial Layer is where redundant API calls become expensive (rate limits + scraping fragility).
**Delivers:** `fetch_commercial_data` tool. Pricing, revenue estimates. Gamalytic integration. TTL caching (metadata 24-72h, commercial 6-12h, engagement 1-4h). Scrape health signaling on SteamDB data.
**Uses:** Playwright + cheerio (SteamDB scraping path), native fetch (Gamalytic API path), `steamStore.ts` client
**Implements:** Stable-API-First, Scrape-as-Fallback pattern. Degradable commercial data (returns cached/estimated values when SteamDB is blocked).
**Avoids:** SteamDB scraping fragility (degradable by design, Gamalytic as real fallback), revenue triangulation misuse (outlier detection only, ranges not point estimates)

### Phase 4: Polish + Report Generation
**Rationale:** Report generation is the terminal node -- nothing depends on it, and its shape should be informed by real usage of the data tools from Phases 1-3. This phase also covers the cross-cutting concerns that improve reliability without changing the core data flow: per-host rate limiter tuning, response size limits (top-N pagination), data freshness timestamps, and the "looks done but isn't" verification checklist.
**Delivers:** `generate_report` tool (structured report skeleton). Pagination on large genre queries. Data freshness timestamps on all responses. Per-host rate limiter tuning based on observed behavior.
**Avoids:** Returning full datasets that overflow LLM context (paginate at ~20 games), stale data served without timestamps

### Phase Ordering Rationale

- Phases 1-3 follow a strict dependency chain: AppIDs (Phase 1) must exist before Commercial or Engagement can run. Engagement (Phase 2) is added before Commercial (Phase 3) because it is stable and low-risk, giving the system a second working dimension before the fragile scraping surface is introduced.
- The shared HTTP transport layer (fetchWithRetry, rate limiter) is built in Phase 1 and reused in every subsequent phase. Building it early means Phase 2 and 3 clients inherit retry and rate-limit behavior for free.
- SteamDB scraping is intentionally last within the Commercial Layer. The JSON tier of the Steam store API covers most pricing needs without Cloudflare risk. HTML scraping is an opt-in addition, not a foundation.
- Report generation is Phase 4 because it depends on knowing what the data actually looks like in practice. Building it before the data tools are stable would lock in a report shape that needs rework.

### Research Flags

Phases likely needing deeper research during planning:
- **Phase 3 (Commercial Layer):** SteamDB scraping resilience, Cloudflare bypass specifics, Playwright stealth configuration -- these have MEDIUM confidence sources and the failure modes are high-impact. Verify Gamalytic API stability and endpoint coverage before implementation begins.
- **Phase 4 (Report Generation):** Report structure and format are entirely inferred from PROJECT.md. No user-validated report shape exists yet. Structure this phase as discovery-first.

Phases with standard patterns (skip research-phase):
- **Phase 1 (MCP Skeleton):** MCP SDK usage, stdio transport, tool registration -- all HIGH confidence from official docs. Well-documented, no novel patterns.
- **Phase 2 (Engagement Layer):** SteamSpy API is simple REST with no auth. Steam review summary fields are in the standard `/api/appdetails` response. Straightforward integration.

## Confidence Assessment

| Area | Confidence | Notes |
|------|------------|-------|
| Stack | HIGH | Core MCP SDK, zod, playwright, cheerio all verified via official sources and npm. The typed Steam API wrapper (`steam-webapi-ts`) is MEDIUM due to low adoption (6 stars), but it is replaceable with 10 lines of typed fetch per endpoint. |
| Features | MEDIUM-HIGH | MCP tool design patterns verified via official docs and practitioner guides. Feature prioritization is domain-inference from PROJECT.md -- the three data layers and research workflow are assumed, not user-validated. |
| Architecture | MEDIUM-HIGH | MCP structure is HIGH confidence (official docs). Client isolation and retry patterns are MEDIUM (community consensus across 5+ sources). SteamSpy and Steam store JSON API behavior is MEDIUM (third-party, long-running but undocumented rate limits). |
| Pitfalls | HIGH | stdout pollution and SSE deprecation are HIGH confidence (official spec). SteamDB scraping ban risk is HIGH confidence (SteamDB FAQ is explicit). Steam rate limit behavior is MEDIUM (community-reported, undocumented by Valve). Revenue triangulation correlation is MEDIUM (Gamalytic's own accuracy claims are self-reported). |

**Overall confidence:** MEDIUM-HIGH

### Gaps to Address

- **Gamalytic API stability:** The Gamalytic API reference page did not render fully during research (LOW confidence source). Verify endpoint availability, response shape, and rate limits during Phase 3 planning. If the API is unstable or undocumented, the fallback strategy for commercial data needs revision.
- **Steam rate limit behavior per endpoint:** Valve does not publish per-endpoint rate limits. The 1.5s inter-request delay is a conservative default based on community reports. Monitor 429 and empty-body responses during Phase 1 testing and tune the rate limiter accordingly.
- **SteamDB scraping viability in current environment:** Cloudflare detection evolves. The Playwright stealth configuration that works today may not work in a week. Phase 3 must include a verification step before treating SteamDB as a viable source, and the degradable-design pattern must be confirmed working (blocked scraper returns structured fallback, not a crash).
- **Report format:** No user-validated report structure exists. Phase 4 should be scoped as iterative: ship a minimal report skeleton, use it in real research sessions, then refine. Do not design the report in advance.
- **`ISteamApps/GetAppList` vs `IStoreService/GetAppList`:** The deprecated endpoint (`ISteamApps/GetAppList/v2`) is still referenced in some community code. Confirm `IStoreService/GetAppList` availability and response format during Phase 1 implementation.

## Sources

### Primary (HIGH confidence)
- MCP TypeScript SDK GitHub (modelcontextprotocol/typescript-sdk) -- SDK version, transport options, zod peer dep, tool registration patterns
- MCP Specification 2025-03-26 (modelcontextprotocol.io) -- transport types, SSE deprecation, Streamable HTTP, tool contracts
- npm registry -- package versions confirmed: @modelcontextprotocol/sdk 1.26.0, playwright 1.58.1, zod 4.3.5
- SteamDB FAQ (steamdb.info/faq) -- explicit scraping prohibition, Cloudflare protection
- Steam Web API Terms of Use (steamcommunity.com/dev/apiterms) -- rate limit terms
- Valve Steamworks docs (partner.steamgames.com) -- ISteamApps interface, GetAppList deprecation notice
- Zod v4 release (zod.dev/v4, InfoQ coverage) -- version requirements, TS 5.5+ constraint

### Secondary (MEDIUM confidence)
- Nearform MCP pitfalls guide -- tool design anti-patterns, state management
- SteamSpy API docs (steamspy.com/api.php) -- endpoint behavior, rate behavior
- ScrapingBee Cloudflare bypass analysis -- SteamDB anti-bot specifics
- McKay Development Forums -- Steam rate limit practical observations
- Gamalytic About page -- estimation methodology, 77% within 30% margin of error claim
- Community Steam research projects (GitHub) -- Gamalytic API usage patterns
- Multiple MCP server best practices blogs (cdata.com, gravitee.io, stainless.com) -- caching, error handling, external API integration

### Tertiary (LOW confidence)
- Gamalytic API reference (api.gamalytic.com/reference) -- page did not render fully; endpoint list and behavior need verification during implementation

---
*Research completed: 2026-02-05*
*Ready for roadmap: yes*
