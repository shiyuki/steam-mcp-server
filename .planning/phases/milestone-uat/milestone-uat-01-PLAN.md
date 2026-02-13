---
phase: milestone-uat
plan: 01
type: execute
wave: 1
depends_on: []
files_modified:
  - src/steam_api.py
  - tests/test_engagement.py
autonomous: true
gap_closure: true

must_haves:
  truths:
    - "Unrecognized tag returns error with error_type tag_not_found, not silent wrong data"
    - "Bowling fallback detection correctly identifies SteamSpy's real fallback dataset"
  artifacts:
    - path: "src/steam_api.py"
      provides: "Correct bowling fallback fingerprint AppIDs"
      contains: "BOWLING_FALLBACK_APPIDS"
    - path: "tests/test_engagement.py"
      provides: "Non-circular bowling fallback tests using real AppIDs"
      contains: "_create_bowling_fallback_data"
  key_links:
    - from: "src/steam_api.py"
      to: "BOWLING_FALLBACK_APPIDS constant"
      via: "_detect_steamspy_fallback intersection check"
      pattern: "BOWLING_FALLBACK_APPIDS & appids_in_response"
---

<objective>
Fix bowling fallback detection by replacing wrong fingerprint AppIDs with real ones from SteamSpy's actual bowling fallback dataset.

Purpose: The current BOWLING_FALLBACK_APPIDS constant contains {436590, 212370, 340170} which are NOT in SteamSpy's actual bowling fallback. The real fallback contains AppIDs like {901583, 12210, 2990, 891040, 22230}. The detection logic itself is sound -- only the fingerprint data is wrong. Tests are circular (mock data uses the same wrong AppIDs), masking the bug.

Output: Corrected fingerprint constant and non-circular tests that use real bowling fallback AppIDs.
</objective>

<execution_context>
@C:\Users\shiyu\.claude/get-shit-done/workflows/execute-plan.md
@C:\Users\shiyu\.claude/get-shit-done/templates/summary.md
</execution_context>

<context>
@C:\Users\shiyu\steam-mcp-server/.planning/phases/milestone-uat/v1.1-UAT.md

Key context:
- `src/steam_api.py` line 64: `BOWLING_FALLBACK_APPIDS = {436590, 212370, 340170}` -- WRONG
- Real bowling fallback AppIDs from SteamSpy: {901583, 12210, 2990, 891040, 22230}
- Detection logic at lines 80-102 is correct (check small result set < 100 games, intersection >= 2)
- `tests/test_engagement.py` lines 1170-1202: `_create_bowling_fallback_data()` uses same wrong AppIDs
- Also line 1261-1262: `test_large_result_set_not_flagged` hardcodes the wrong AppIDs
- 135 tests currently passing
</context>

<tasks>

<task type="auto">
  <name>Task 1: Fix bowling fingerprint constant and update all tests</name>
  <files>src/steam_api.py, tests/test_engagement.py</files>
  <action>
  1. In `src/steam_api.py` line 64, replace:
     ```python
     BOWLING_FALLBACK_APPIDS = {436590, 212370, 340170}
     ```
     with:
     ```python
     BOWLING_FALLBACK_APPIDS = {901583, 12210, 2990, 891040, 22230}
     ```
     Update the comment above to clarify these are empirically verified AppIDs from SteamSpy's actual bowling fallback response.

  2. In `tests/test_engagement.py`, update `_create_bowling_fallback_data()` (around line 1170):
     - Change `bowling_appids = [436590, 212370, 340170]` to `bowling_appids = [901583, 12210, 2990, 891040, 22230]`
     - Adjust the filler game count so total remains ~70 (e.g., `range(100000, 100065)` instead of `100067`)
     - Add a comment explaining these are real AppIDs from SteamSpy's bowling fallback

  3. In `tests/test_engagement.py`, update `test_large_result_set_not_flagged` (around line 1257-1262):
     - Change `bowling_appids = [436590, 212370, 340170]` to `bowling_appids = [901583, 12210, 2990, 891040, 22230]`
     - Adjust filler range count so total remains 500

  4. Search the entire test file for any other hardcoded references to the old AppIDs {436590, 212370, 340170} and update them.
  </action>
  <verify>
  Run: `cd "C:/Users/shiyu/steam-mcp-server" && uv run pytest tests/test_engagement.py -v -k "bowling or fallback"`
  All bowling/fallback tests pass. No references to old AppIDs remain:
  Run: `cd "C:/Users/shiyu/steam-mcp-server" && uv run python -c "from src.steam_api import BOWLING_FALLBACK_APPIDS; print(BOWLING_FALLBACK_APPIDS); assert 901583 in BOWLING_FALLBACK_APPIDS; assert 436590 not in BOWLING_FALLBACK_APPIDS; print('OK')"`
  </verify>
  <done>
  BOWLING_FALLBACK_APPIDS contains {901583, 12210, 2990, 891040, 22230}. All bowling-related tests use these real AppIDs. Full test suite passes: `cd "C:/Users/shiyu/steam-mcp-server" && uv run pytest tests/ -v`
  </done>
</task>

</tasks>

<verification>
1. `cd "C:/Users/shiyu/steam-mcp-server" && uv run pytest tests/ -v` -- all 135+ tests pass
2. `cd "C:/Users/shiyu/steam-mcp-server" && uv run python -c "from src.steam_api import BOWLING_FALLBACK_APPIDS; print(BOWLING_FALLBACK_APPIDS)"` -- shows real AppIDs
3. Grep for old AppIDs confirms no remnants: no matches for 436590, 212370, or 340170 in any .py file
</verification>

<success_criteria>
- BOWLING_FALLBACK_APPIDS constant contains real SteamSpy bowling fallback AppIDs
- No circular test validation (tests use same real AppIDs, not invented ones)
- Full test suite green (135+ tests)
</success_criteria>

<output>
After completion, create `.planning/phases/milestone-uat/milestone-uat-01-SUMMARY.md`
</output>
