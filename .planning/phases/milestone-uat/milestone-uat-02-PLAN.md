---
phase: milestone-uat
plan: 02
type: execute
wave: 2
depends_on: ["milestone-uat-01"]
files_modified:
  - src/steam_api.py
  - src/schemas.py
  - tests/test_engagement.py
autonomous: true
gap_closure: true

must_haves:
  truths:
    - "aggregate_engagement only returns games where the searched tag appears in the game's top 20 tags"
    - "PUBG does NOT appear in Indie aggregation results"
    - "API calls are bounded -- validates top N games, not all 52k"
  artifacts:
    - path: "src/schemas.py"
      provides: "GameEngagementSummary with tags field for cross-validation"
      contains: "tags"
    - path: "src/steam_api.py"
      provides: "Per-game tag cross-validation in aggregate_engagement"
  key_links:
    - from: "src/steam_api.py aggregate_engagement"
      to: "SteamSpy appdetails endpoint"
      via: "per-game tag validation for top N candidates"
      pattern: "get_with_metadata.*appdetails"
    - from: "src/steam_api.py aggregate_engagement"
      to: "tag filtering logic"
      via: "check searched tag in game's tags dict"
      pattern: "tag.*in.*tags"
---

<objective>
Add per-game tag cross-validation to aggregate_engagement so that only games where the searched tag actually appears in the game's tag list are included in results.

Purpose: SteamSpy's bulk tag endpoint (request=tag) returns ALL games with ANY association to a tag, regardless of weight. This causes irrelevant games like PUBG to appear under "Indie". The fix: after the bulk fetch, validate the top N candidate games by fetching their individual appdetails (which includes per-game tag dicts), and filter to only games where the searched tag appears in their tags.

Output: aggregate_engagement returns only tag-validated games, bounded to top N candidates for API efficiency.
</objective>

<execution_context>
@C:\Users\shiyu\.claude/get-shit-done/workflows/execute-plan.md
@C:\Users\shiyu\.claude/get-shit-done/templates/summary.md
</execution_context>

<context>
@C:\Users\shiyu\steam-mcp-server/.planning/phases/milestone-uat/v1.1-UAT.md

Key context:
- `src/steam_api.py` lines 355-586: `aggregate_engagement` method
- Lines 408-411: blindly includes every game from bulk endpoint without cross-validation
- `src/schemas.py` lines 118-129: `GameEngagementSummary` has no `tags` field
- SteamSpy appdetails endpoint returns `"tags": {"TagName": weight_int, ...}` per game
- `get_engagement_data()` already fetches and parses tags from appdetails (lines 213-216, 269)
- Bulk endpoint returns ~52k games for popular tags like "Indie" -- MUST bound validation
- The existing `_parse_game_data()` helper (lines 297-353) parses bulk endpoint data but bulk data does NOT contain per-game tags
- The existing `get_engagement_data()` method (lines 161-295) fetches individual appdetails with tags
- Rate limiter already handles concurrent requests via `HostRateLimiter`
</context>

<tasks>

<task type="auto">
  <name>Task 1: Add tags field to GameEngagementSummary schema</name>
  <files>src/schemas.py</files>
  <action>
  In `src/schemas.py`, add a `tags` field to `GameEngagementSummary` (around line 129):

  ```python
  class GameEngagementSummary(BaseModel):
      """Lightweight per-game entry for aggregation breakdown lists."""
      appid: int
      name: str = ""
      ccu: int = 0
      owners_midpoint: float = 0
      average_forever: float = 0  # hours
      average_2weeks: float = 0  # hours
      positive: int = 0
      negative: int = 0
      review_score: float | None = None
      price: float = 0
      tags: dict[str, int] = Field(default_factory=dict)  # Tag name -> weight (from appdetails validation)
  ```

  This field will be populated during tag cross-validation. It defaults to empty dict so the appid-list branch (which already has tags from get_engagement_data) can populate it, and existing code that creates GameEngagementSummary without tags continues to work.
  </action>
  <verify>
  Run: `cd "C:/Users/shiyu/steam-mcp-server" && uv run python -c "from src.schemas import GameEngagementSummary; g = GameEngagementSummary(appid=1); print(g.tags); g2 = GameEngagementSummary(appid=2, tags={'Action': 100}); print(g2.tags); print('OK')"`
  Output shows `{}` then `{'Action': 100}` then `OK`.
  </verify>
  <done>GameEngagementSummary has a tags field with default empty dict.</done>
</task>

<task type="auto">
  <name>Task 2: Add tag cross-validation to aggregate_engagement</name>
  <files>src/steam_api.py, tests/test_engagement.py</files>
  <action>
  **Implementation in `src/steam_api.py`:**

  Modify the tag-based branch of `aggregate_engagement` (lines 408-411 area). After the bulk endpoint fetch and bowling fallback check, instead of blindly adding all games:

  1. Sort games from bulk endpoint by owners (descending) to prioritize high-value games.
  2. Take the top N candidates (use N=100 as the validation cap -- configurable via a constant `TAG_VALIDATION_LIMIT = 100`).
  3. For each candidate, fetch appdetails via `get_engagement_data()` to get per-game tags.
  4. Filter: only include games where the searched tag (case-insensitive comparison) appears in the game's tags dict keys.
  5. Build `GameEngagementSummary` from the validated `EngagementData` objects (NOT from bulk endpoint data), including the tags field.
  6. Use `asyncio.gather` for concurrent fetching, same pattern as the appid-list branch.
  7. Track failures (games that errored during appdetails fetch).
  8. Update `games_total` to reflect the validation cap, not the full bulk count.

  Specifically, replace the block at lines 408-411:
  ```python
  # Parse each game from tag endpoint
  for game_data in data.values():
      parsed = self._parse_game_data(game_data)
      games_list.append(GameEngagementSummary(**parsed))
  ```

  With something like:
  ```python
  # Sort bulk results by owners (descending) to validate highest-value games first
  bulk_games = []
  for game_data in data.values():
      parsed = self._parse_game_data(game_data)
      bulk_games.append(parsed)
  bulk_games.sort(key=lambda g: g["owners_midpoint"], reverse=True)

  # Validate top N candidates via appdetails (which includes per-game tags)
  TAG_VALIDATION_LIMIT = 100
  candidates = bulk_games[:TAG_VALIDATION_LIMIT]
  candidate_appids = [g["appid"] for g in candidates]

  # Fetch appdetails concurrently for tag validation
  tasks = [self.get_engagement_data(appid) for appid in candidate_appids]
  results = await asyncio.gather(*tasks, return_exceptions=True)

  # Filter to games where searched tag is in their tag dict
  tag_lower = tag.lower()
  for appid, result in zip(candidate_appids, results):
      if isinstance(result, Exception):
          failures.append({"appid": appid, "reason": "exception"})
      elif isinstance(result, APIError):
          failures.append({"appid": appid, "reason": result.error_type})
      elif isinstance(result, EngagementData):
          # Check if searched tag appears in this game's tags (case-insensitive)
          game_tag_names_lower = {t.lower() for t in result.tags.keys()}
          if tag_lower in game_tag_names_lower:
              games_list.append(GameEngagementSummary(
                  appid=result.appid,
                  name=result.name,
                  ccu=result.ccu,
                  owners_midpoint=result.owners_midpoint,
                  average_forever=result.average_forever,
                  average_2weeks=result.average_2weeks,
                  positive=result.positive,
                  negative=result.negative,
                  review_score=result.review_score,
                  price=result.price,
                  tags=result.tags
              ))

  games_total = len(candidate_appids)  # Override: validated scope, not full bulk count
  ```

  Also update the `games_total` assignment near line 469 -- move it after the if/elif/else branches and use `len(candidates)` for the tag branch. Make sure `games_total` is set correctly for both branches:
  - Tag branch: `games_total = len(candidates)` (the validation scope)
  - AppID branch: `games_total = len(games_list) + len(failures)` (unchanged)

  Also update the appid-list branch (lines 440-452) to populate the `tags` field on GameEngagementSummary:
  ```python
  tags=result.tags,  # Add this line
  ```

  Add a log line noting how many games were filtered out:
  ```python
  logger.info(f"Tag validation: {len(games_list)}/{len(candidates)} games confirmed with tag '{tag}'")
  ```

  **Add a module-level constant** near the top of the file (after BOWLING_FALLBACK_APPIDS):
  ```python
  # Maximum number of games to validate via appdetails for tag cross-validation
  TAG_VALIDATION_LIMIT = 100
  ```

  **Tests in `tests/test_engagement.py`:**

  Add a new test class `TestTagCrossValidation` with these tests:

  1. `test_aggregate_filters_games_without_searched_tag`: Mock get_with_metadata for the bulk call to return 5 games. Mock get_with_metadata to return appdetails for each game, where 3 have the searched tag in their tags dict and 2 do not. Assert only 3 games appear in results. Use side_effect to return different data for bulk vs appdetails calls.

  2. `test_aggregate_tag_validation_bounded`: Mock bulk endpoint returning 200 games. Assert that get_with_metadata is called at most TAG_VALIDATION_LIMIT+1 times (1 bulk + N appdetails). This validates the API call bound.

  3. `test_aggregate_includes_tags_in_summary`: Mock a successful tag query with 2 validated games. Assert each GameEngagementSummary in results has a non-empty tags dict.

  For mocking approach: Since both the bulk endpoint and appdetails calls go through `self.http.get_with_metadata`, use `side_effect` to return different responses based on the `params` argument. When `params` contains `"request": "tag"`, return bulk data. When `params` contains `"request": "appdetails"`, return per-game data with tags.

  Example side_effect pattern:
  ```python
  async def mock_responses(url, params, cache_ttl):
      if params.get("request") == "tag":
          return (bulk_data, datetime.now(timezone.utc), 0)
      elif params.get("request") == "appdetails":
          appid = params["appid"]
          return (appdetails_data[appid], datetime.now(timezone.utc), 0)

  mock_http.get_with_metadata.side_effect = mock_responses
  ```
  </action>
  <verify>
  Run: `cd "C:/Users/shiyu/steam-mcp-server" && uv run pytest tests/test_engagement.py -v -k "cross_validation or tag_validation or tags_in_summary"`
  All new tests pass.
  Run: `cd "C:/Users/shiyu/steam-mcp-server" && uv run pytest tests/ -v`
  Full suite passes (135+ tests).
  </verify>
  <done>
  aggregate_engagement with tag input validates top 100 candidate games via appdetails, filters to only games where searched tag appears in their tags dict, and includes tags in GameEngagementSummary. PUBG would NOT appear in "Indie" results because "Indie" is not in PUBG's top tags. Full test suite green.
  </done>
</task>

</tasks>

<verification>
1. `cd "C:/Users/shiyu/steam-mcp-server" && uv run pytest tests/ -v` -- all tests pass
2. `GameEngagementSummary` has `tags` field (dict[str, int])
3. `aggregate_engagement` tag branch fetches appdetails for top 100 candidates
4. Only games where searched tag appears in their tags dict are included
5. AppID-list branch also populates tags field
6. No more than TAG_VALIDATION_LIMIT (100) appdetails calls per tag query
</verification>

<success_criteria>
- aggregate_engagement with tag input only includes games where the searched tag is in the game's top tags
- API calls bounded to TAG_VALIDATION_LIMIT (100) appdetails fetches
- GameEngagementSummary includes tags field
- Full test suite green (135+ tests plus new cross-validation tests)
</success_criteria>

<output>
After completion, create `.planning/phases/milestone-uat/milestone-uat-02-SUMMARY.md`
</output>
