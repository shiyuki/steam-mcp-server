# Requirements: Games Research Tool

**Defined:** 2026-02-05
**Core Value:** Enable rapid, data-backed answers to "Is this genre a gold mine or a graveyard?"

## v1 Requirements

Requirements for Step 1 — MCP foundation + Steam API integration.

### MCP Server Core

- [ ] **MCP-01**: MCP server skeleton connects to Claude/Cursor via stdio transport
- [ ] **MCP-02**: Tools registered with typed pydantic schemas for inputs/outputs
- [ ] **MCP-03**: Structured error responses with isError flag and error classification (400/404/429/502)
- [ ] **MCP-04**: All logging routed to stderr only (stdout reserved for JSON-RPC)
- [ ] **MCP-05**: Input validation rejects malformed requests before hitting external APIs

### Data Tools

- [ ] **TOOL-01**: `search_genre` tool fetches games by Steam tag and returns AppID list
- [ ] **TOOL-02**: `fetch_metadata` tool returns game details (name, tags, developer, description) from Steam API

### Infrastructure

- [ ] **INFRA-01**: Shared HTTP transport layer using httpx with retry logic
- [ ] **INFRA-02**: Rate limiting with exponential backoff (1.5s default between requests)
- [ ] **INFRA-03**: Steam API key loaded from environment variable (never hardcoded)

### Validation

- [ ] **VAL-01**: Each API request confirms successful data retrieval
- [ ] **VAL-02**: Failed requests return structured error with clear message

## v1.1 Requirements

Deferred to Step 2 — API refinement and commercial data.

### Data Tools

- **TOOL-03**: `fetch_commercial` tool returns pricing and revenue estimates via Gamalytic API
- **TOOL-04**: `fetch_engagement` tool returns CCU, reviews, playtime via SteamSpy API

### Infrastructure

- **INFRA-04**: TTL-based caching (metadata 24h, commercial 6h, engagement 1h)
- **INFRA-05**: Concurrent fetching for independent data sources

### Validation

- **VAL-03**: Revenue presented as ranges with confidence intervals
- **VAL-04**: Data freshness timestamps on all responses

## v2 Requirements

Deferred to Step 3 — Testing and advanced features.

### Data Tools

- **TOOL-05**: SteamDB scraping for historical data (with degradable fallback)
- **TOOL-06**: Revenue triangulation between Gamalytic and SteamDB for outlier detection

### Reports

- **RPT-01**: `generate_report` tool produces structured report skeleton
- **RPT-02**: Report includes market structure, opportunity matrix sections

### Testing

- **TEST-01**: Unit tests for all data parsing/transformation logic
- **TEST-02**: E2E tests for full MCP request/response flow
- **TEST-03**: Test report generation with sample data

## Out of Scope

| Feature | Reason |
|---------|--------|
| Web UI | Claude/Cursor is the interface |
| Multi-user support | Personal research tool |
| Real-time dashboards | Batch research, not monitoring |
| Reddit/Twitch/YouTube | Defer until core market data stable |
| OAuth / social login | No user accounts needed |

## Traceability

| Requirement | Phase | Status |
|-------------|-------|--------|
| MCP-01 | Phase 1 | Pending |
| MCP-02 | Phase 1 | Pending |
| MCP-03 | Phase 1 | Pending |
| MCP-04 | Phase 1 | Pending |
| MCP-05 | Phase 1 | Pending |
| TOOL-01 | Phase 1 | Pending |
| TOOL-02 | Phase 1 | Pending |
| INFRA-01 | Phase 1 | Pending |
| INFRA-02 | Phase 1 | Pending |
| INFRA-03 | Phase 1 | Pending |
| VAL-01 | Phase 1 | Pending |
| VAL-02 | Phase 1 | Pending |

**Coverage:**
- v1 requirements: 12 total
- Mapped to phases: 12
- Unmapped: 0 ✓

---
*Requirements defined: 2026-02-05*
*Last updated: 2026-02-05 after roadmap creation*
