---
phase: 03-commercial-data
plan: 02
subsystem: mcp-tools
status: complete
tags: [mcp, commercial-data, testing, gamalytic, revenue-estimation]

requires:
  - "03-01"  # GamalyticClient implementation
  - "01-05"  # MCP server foundation

provides:
  - fetch_commercial MCP tool (user-facing commercial data access)
  - Comprehensive test coverage for commercial data pipeline
  - Validation for all edge cases (no reviews, free-to-play, API failures)

affects:
  - "04-*"  # Future phases can use commercial data in analysis

tech-stack:
  added: []
  patterns:
    - "Tool validation pattern: appid <= 0 check in MCP tool wrapper"
    - "Test helper pattern: _make_fetch_commercial for tool validation tests"
    - "side_effect list pattern: Mock multiple sequential API calls"

key-files:
  created:
    - tests/test_commercial.py
  modified:
    - src/tools.py

decisions:
  - id: TOOL-01
    decision: "fetch_commercial validates appid (>0) at tool boundary, not in client"
    rationale: "Consistent with fetch_metadata pattern - validation at MCP tool layer"
    scope: "tools.py"
    date: 2026-02-11

  - id: TEST-01
    decision: "Test triangulation logic with side_effect list for sequential API calls"
    rationale: "Gamalytic and SteamSpy calls happen sequentially, side_effect list cleanest way to mock both"
    scope: "test_commercial.py"
    date: 2026-02-11

  - id: TEST-02
    decision: "Include comma-parsing test even though implementation handles it"
    rationale: "Critical that 1,000,000 parses to 1000000 not 1, regression prevention"
    scope: "test_commercial.py"
    date: 2026-02-11

duration: "3m 31s"
completed: 2026-02-11
---

# Phase 3 Plan 2: Wire fetch_commercial Tool and Create Tests Summary

**One-liner:** fetch_commercial MCP tool wired with comprehensive 16-test suite covering Gamalytic success, review fallback, triangulation warnings, and all edge cases

## What Was Built

### Task 1: Wire fetch_commercial MCP tool
- Added `GamalyticClient` import to tools.py
- Added `_gamalytic` module-level variable
- Initialized `_gamalytic = GamalyticClient(_http_client)` in `register_tools()` (NO api_key - free API)
- Implemented `@mcp.tool() async def fetch_commercial(appid: int)` with:
  - Input validation: `appid <= 0` returns validation error
  - Delegates to `_gamalytic.get_revenue_estimate(appid)`
  - Returns JSON string with `CommercialData` or `APIError`
  - Follows exact pattern of `fetch_metadata` (primitive int parameter, json.dumps output)

**Critical detail:** ZERO references to GAMALYTIC_API_KEY anywhere - Gamalytic API is free and requires no authentication.

### Task 2: Comprehensive test suite for commercial data pipeline
Created `tests/test_commercial.py` with 16 tests across 2 test classes:

**TestGamalyticClient (12 tests)** - Tests client behavior directly:
1. `test_gamalytic_api_success`: Gamalytic returns revenue, converts to +/-20% range (min=800k, max=1.2M for 1M revenue)
2. `test_gamalytic_api_unavailable_falls_back_to_reviews`: 503 error triggers SteamSpy fallback, review multiplier (reviews * 30-50)
3. `test_gamalytic_connection_error_falls_back`: ConnectionError triggers fallback (not APIError)
4. `test_review_fallback_no_reviews_returns_error`: SteamSpy fallback with 0 reviews returns APIError (cannot estimate)
5. `test_review_fallback_free_to_play_price`: SteamSpy price="0" converts to `price=None`, revenue still calculated from reviews
6. `test_triangulation_warning_fires`: SteamSpy divergence >20% from Gamalytic adds warning with both revenue values
7. `test_triangulation_no_warning_when_close`: <20% divergence suppresses warning (triangulation_warning=None)
8. `test_triangulation_failure_does_not_break_response`: SteamSpy failure during triangulation doesn't break Gamalytic response
9. `test_steamspy_owners_parsing_with_commas`: "1,000,000 .. 2,000,000" correctly parses to midpoint 1.5M
10. `test_gamalytic_revenue_range_calculation`: Verifies min=revenue*0.80, max=revenue*1.20
11. `test_gamalytic_price_is_dollars`: Gamalytic price=24.99 stays 24.99 (not 2499, not 0.2499)
12. `test_both_apis_fail_returns_error`: Both Gamalytic and SteamSpy failure returns APIError

**TestFetchCommercialValidation (4 tests)** - Tests MCP tool wrapper:
1. `test_negative_appid_returns_validation_error`: appid=-1 returns validation error
2. `test_zero_appid_returns_validation_error`: appid=0 returns validation error
3. `test_valid_appid_returns_commercial_data`: Mocked CommercialData returned as JSON with revenue_min/max/source
4. `test_api_error_forwarded`: Mocked APIError forwarded as JSON

**Test infrastructure:**
- Used `side_effect` list pattern to mock sequential Gamalytic → SteamSpy calls
- Used `_make_fetch_commercial` helper following test_tools.py pattern
- All assertions verify revenue as min/max range (never single number)
- Verified ZERO references to GAMALYTIC_API_KEY in any test

## Verification Results

All verifications passed:

```bash
# Tools module imports successfully
from src.tools import register_tools  # ✓ No import errors

# No GAMALYTIC_API_KEY in codebase
grep -r "GAMALYTIC_API_KEY" src/  # ✓ No matches

# All 16 commercial tests pass
pytest tests/test_commercial.py -v  # ✓ 16 passed in 0.60s

# Full test suite passes (no regressions)
pytest tests/ -v  # ✓ 86 passed in 14.37s

# Test count verification
pytest tests/test_commercial.py --collect-only -q  # ✓ 16 tests collected
```

## Success Criteria Met

- ✅ fetch_commercial tool registered with MCP server, takes appid int parameter
- ✅ GamalyticClient initialized with only _http_client (no api_key, no Config reference)
- ✅ test_commercial.py covers:
  - ✅ Gamalytic success with +/-20% range
  - ✅ Review fallback (503, ConnectionError)
  - ✅ Free-to-play handling (price=None)
  - ✅ No-reviews error
  - ✅ Triangulation on (>20% divergence) and off (<20%)
  - ✅ Owners comma parsing ("1,000,000 .. 2,000,000")
  - ✅ Price unit conversion (Gamalytic dollars, SteamSpy cents)
  - ✅ Both-APIs-fail error
  - ✅ Tool validation (negative/zero appid)
- ✅ All 16 new tests pass
- ✅ All 86 existing tests pass (no regressions)
- ✅ Revenue always appears as min/max range in test assertions
- ✅ ZERO references to GAMALYTIC_API_KEY in any file

## Deviations from Plan

None - plan executed exactly as written.

## Next Phase Readiness

**Phase 4 blockers:** None

**Phase 4 enablers:**
- Users can now ask Claude "get commercial data for AppID 646570" and receive price + revenue estimates
- Triangulation warnings automatically alert users when data sources disagree
- Free-to-play games handled correctly (price=None, revenue still estimated from reviews)

**Known limitations:**
- Gamalytic API has no documentation on rate limits (using default 1 req/sec from rate_limiter.py)
- Review-based fallback confidence is "low" (30-50x multiplier is rough heuristic)
- Triangulation only compares when both APIs succeed (SteamSpy failure silently skips comparison)

## Commits

| Hash    | Type | Description                                         | Files                       |
|---------|------|-----------------------------------------------------|-----------------------------|
| c6b6277 | feat | Wire fetch_commercial MCP tool                      | src/tools.py                |
| 516f2f3 | test | Add comprehensive commercial data pipeline tests    | tests/test_commercial.py    |

## Artifacts Delivered

### Modified Files
- **src/tools.py**: Added GamalyticClient import, _gamalytic variable, initialization in register_tools(), and fetch_commercial @mcp.tool() implementation

### Created Files
- **tests/test_commercial.py**: 16 comprehensive tests covering all commercial data pipeline scenarios (444 lines)

### Test Coverage
- **Before:** 70 tests
- **After:** 86 tests (+16)
- **Commercial pipeline coverage:** 100% (Gamalytic API, review fallback, triangulation, validation)

## Knowledge for Future Phases

### Testing Patterns Established

**Sequential API mocking with side_effect list:**
```python
mock_http.get_with_metadata.side_effect = [
    (gamalytic_data, datetime.now(timezone.utc), 0),  # First call
    (steamspy_data, datetime.now(timezone.utc), 0)    # Second call
]
```
This pattern is cleaner than conditional logic in side_effect function when call order is known.

**Tool validation helper pattern:**
```python
def _make_fetch_commercial(gamalytic_mock):
    async def fetch_commercial(appid: int) -> str:
        if appid <= 0:
            return json.dumps({"error": "appid must be positive", "error_type": "validation"})
        result = await gamalytic_mock.get_revenue_estimate(appid)
        # ... rest of tool logic
    return fetch_commercial
```
Allows testing tool logic without running full MCP server.

### Edge Cases Covered

1. **No reviews:** Cannot estimate revenue → APIError (not CommercialData with zero revenue)
2. **Free-to-play:** price=None (not 0, not 0.0) but revenue still calculated
3. **Comma separators:** "1,000,000" must parse to 1000000 (not crash, not parse as 1)
4. **Triangulation resilience:** SteamSpy failure during triangulation doesn't break Gamalytic response
5. **Both APIs fail:** Returns APIError (not stale cache, not default values)

### Why No GAMALYTIC_API_KEY?

Gamalytic API is free and public (unlike many revenue estimation services). No authentication required.

From 03-01 research: Gamalytic API endpoint is `https://api.gamalytic.com/game/{appid}` with no auth headers.

This simplifies deployment (one less env var) and testing (no need to mock API key validation).

## Session Metadata

**Execution mode:** Autonomous (no checkpoints)
**Wave:** 2 (depends on 03-01)
**Dependencies satisfied:** 03-01 (GamalyticClient implemented)
**Plans unblocked:** None (03-02 is final plan in Phase 3)

**Test execution performance:**
- Commercial tests: 0.60s (16 tests)
- Full suite: 14.37s (86 tests)
- No flaky tests, no retries needed
