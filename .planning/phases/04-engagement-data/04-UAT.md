---
status: diagnosed
phase: 04-engagement-data
source: ROADMAP.md Phase 4 success criteria, milestone-uat-01-SUMMARY.md, milestone-uat-02-SUMMARY.md
started: 2026-02-13T05:50:00Z
updated: 2026-02-13T09:00:00Z
---

## Current Test

number: 8
name: Aggregate filters irrelevant games via tag cross-validation
expected: |
  Call `aggregate_engagement` with a niche tag. Games that don't actually have the searched tag in their top tags should NOT appear. All qualifying games from the bulk endpoint should be validated, not just top 100.
awaiting: user response

## Tests

### 1. Fetch engagement data for a known game
expected: Call `fetch_engagement` with AppID 646570 (Slay the Spire). Response includes CCU, owner estimate (midpoint), average playtime (in hours), and review counts (positive/negative) with a review_score percentage.
result: pass

### 2. Owner range parsed into structured values
expected: The engagement response includes `owners_min`, `owners_max`, and `owners_midpoint` as numeric values (not raw "20,000 .. 50,000" strings).
result: pass

### 3. Derived health metrics present
expected: Response includes `ccu_ratio` (CCU / owners_midpoint * 100), `review_score`, `playtime_engagement` (median/average), and `activity_ratio` (2weeks/forever * 100). Each has a `quality_flag` ("available" or "zero_uncertain").
result: pass

### 4. Aggregate engagement for a tag
expected: Call `aggregate_engagement` with tag "Roguelike". Response includes per-metric stats (mean, median, p25, p75, min, max) for CCU, owners, playtime, and review scores across all games in that tag.
result: pass

### 5. Aggregate includes market-weighted and per-game metrics
expected: The aggregate response includes both `market_weighted` metrics (aggregates raw values, emphasizes popular games) and `per_game_average` metrics (computes per-game ratios then averages).
result: pass

### 6. Tag normalization works
expected: Call `search_genre` or `aggregate_engagement` with "roguelike" (lowercase). The tag is normalized to "Rogue-like" (SteamSpy's format) and returns valid results, not an error.
result: pass

### 7. Unrecognized tag returns error
expected: Call `search_genre` with a nonsense tag (e.g., "xyznotarealtag123"). Returns an error with `error_type: "tag_not_found"`, not bowling game data.
result: pass

### 8. Aggregate filters irrelevant games via tag cross-validation
expected: Call `aggregate_engagement` with a niche tag. Games that don't actually have the searched tag in their top tags should NOT appear. All qualifying games from the bulk endpoint should be validated.
result: issue
reported: "Cross-validation only checks top 100 games by owners. The remaining 364 games (for Roguelike Deckbuilder: 464 bulk - 100 validated = 364 dropped) are excluded without checking. Want all bulk results to be cross-validated so all qualifying games are included."
severity: major

## Summary

total: 8
passed: 7
issues: 1
pending: 0
skipped: 0

## Gaps

- truth: "All games from bulk endpoint should be cross-validated against their top tags, returning every qualifying game"
  status: failed
  reason: "User reported: Cross-validation only checks top 100 games by owners. The remaining 364 games (for Roguelike Deckbuilder: 464 bulk - 100 validated = 364 dropped) are excluded without checking. Want all bulk results to be cross-validated so all qualifying games are included."
  severity: major
  test: 8
  root_cause: "TAG_VALIDATION_LIMIT=100 caps validation at line 430: candidates = bulk_games[:TAG_VALIDATION_LIMIT]. Games beyond rank 100 by owners are never checked."
  artifacts:
    - path: "src/steam_api.py"
      issue: "Line 68: TAG_VALIDATION_LIMIT = 100; Line 430: candidates = bulk_games[:TAG_VALIDATION_LIMIT]"
    - path: "tests/test_engagement.py"
      issue: "Line 1559: test assertion enforces the cap (assert appdetails_call_count <= TAG_VALIDATION_LIMIT)"
  missing:
    - "Remove TAG_VALIDATION_LIMIT cap — validate all bulk results"
    - "Update test assertion that enforces the 100-game cap"
    - "Keep TAG_CROSSVAL_THRESHOLD (5000) broad-tag bypass — validating 50k+ games is impractical"
  debug_session: ""
