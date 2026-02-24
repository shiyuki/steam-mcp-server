"""Market analysis orchestration layer for Phase 9 tools.

Pure functions where possible — no HTTP calls, no async.
Testable independently without MCP wiring.
"""
from __future__ import annotations

import base64
import hashlib
import json
import statistics
import time
from collections import Counter, defaultdict
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.steam_api import GamalyticClient, SteamSpyClient, SteamStoreClient

from src.analytics import (
    PRICE_BRACKETS,
    STANDARD_BIASES,
    _assign_price_bracket,
    _assign_revenue_tier,
    _gini_coefficient,
    _get_top_tags,
    _safe_mean,
    _safe_median,
    compute_competitive_density,
    compute_concentration,
    compute_price_brackets,
    compute_publisher_analysis,
    compute_release_timing,
    compute_score_revenue,
    compute_sub_genres,
    compute_success_rates,
    compute_tag_multipliers,
    compute_temporal_trends,
    get_computable_metrics,
)
from src.logging_config import get_logger
from src.schemas import (
    AnalyzeMarketResult,
    CompetitorComparison,
    ComputeTimingInfo,
    EvaluateGameResult,
    GamePercentiles,
    GameProfile,
    MarketHealthScores,
    SkippedMetric,
    TagInsight,
)

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ANALYSIS_CACHE_TTL = 4 * 3600          # 4 hours in seconds
GAMALYTIC_QUOTA_WARN_THRESHOLD = 500   # soft warning threshold
GAMALYTIC_QUOTA_HARD_WARN = 800        # hard warning threshold
MIN_GAMES_THRESHOLD = 20               # minimum games for meaningful analysis

# ---------------------------------------------------------------------------
# Analysis-Level Cache
# ---------------------------------------------------------------------------

_analysis_cache: dict[str, tuple[dict, float]] = {}  # key -> (result_dict, created_at_monotonic)


def _get_analysis_cache_key(tag: str, params: dict) -> str:
    """Produce a stable cache key from tag and analysis parameters."""
    param_str = json.dumps(params, sort_keys=True)
    param_hash = hashlib.md5(param_str.encode()).hexdigest()[:8]
    return f"analysis:{tag.lower().strip()}:{param_hash}"


def _cache_get(key: str) -> dict | None:
    """Return cached result if present and not expired; delete stale entries."""
    entry = _analysis_cache.get(key)
    if entry is None:
        return None
    result_dict, created_at = entry
    if time.monotonic() - created_at > ANALYSIS_CACHE_TTL:
        del _analysis_cache[key]
        return None
    return result_dict


def _cache_set(key: str, result: dict) -> None:
    """Store result in the analysis cache with current monotonic timestamp."""
    _analysis_cache[key] = (result, time.monotonic())


def clear_analysis_cache() -> None:
    """Clear all entries from the analysis cache (useful for testing)."""
    _analysis_cache.clear()


# ---------------------------------------------------------------------------
# Gamalytic Quota Tracking
# ---------------------------------------------------------------------------

_gamalytic_call_count: int = 0


def increment_gamalytic_counter(n: int = 1) -> str | None:
    """Increment Gamalytic API call counter and return a warning string if threshold reached.

    Returns None when below warning threshold, a soft warning string at GAMALYTIC_QUOTA_WARN_THRESHOLD,
    and a hard warning string at GAMALYTIC_QUOTA_HARD_WARN.
    """
    global _gamalytic_call_count
    _gamalytic_call_count += n
    if _gamalytic_call_count >= GAMALYTIC_QUOTA_HARD_WARN:
        return (
            f"WARNING: Gamalytic API calls this session: {_gamalytic_call_count}. "
            "Likely exceeding free tier limit."
        )
    if _gamalytic_call_count >= GAMALYTIC_QUOTA_WARN_THRESHOLD:
        return (
            f"Gamalytic API calls this session: {_gamalytic_call_count}. "
            "Approaching estimated free tier limit."
        )
    return None


def get_gamalytic_call_count() -> int:
    """Return the current Gamalytic API call count for this session."""
    return _gamalytic_call_count


def reset_gamalytic_counter() -> None:
    """Reset the Gamalytic API call counter (useful for testing)."""
    global _gamalytic_call_count
    _gamalytic_call_count = 0


# ---------------------------------------------------------------------------
# Percentile Ranking Helpers
# ---------------------------------------------------------------------------

def _percentile_rank(value: float | None, population: list[float]) -> float | None:
    """Return percentile rank of value within population (0-100).

    Uses "less than" definition: percentage of population values strictly below value.
    Returns None if value is None or population is empty.
    """
    if value is None or not population:
        return None
    below = sum(1 for v in population if v < value)
    return round(below / len(population) * 100, 1)


def _std_deviations_from_mean(value: float | None, population: list[float]) -> float | None:
    """Return how many standard deviations value is from the population mean.

    Positive = above mean, negative = below mean.
    Returns None if value is None or population has fewer than 2 elements.
    Returns 0.0 if population standard deviation is zero.
    """
    if value is None or len(population) < 2:
        return None
    try:
        mean_val = statistics.mean(population)
        stdev_val = statistics.stdev(population)
        if stdev_val == 0:
            return 0.0
        return round((value - mean_val) / stdev_val, 2)
    except statistics.StatisticsError:
        return None


# ---------------------------------------------------------------------------
# Descriptive Statistics
# ---------------------------------------------------------------------------

def _compute_descriptive_stats(games: list[dict]) -> dict:
    """Compute mean/median/stdev/min/max for revenue, price, review_score, and ccu.

    Returns a dict keyed by field name. Each value is a stats dict with:
    mean, median, stdev, min, max, count, coverage_pct.
    All values rounded to 2 decimal places; stdev is None when count < 2.
    """
    fields = ["revenue", "price", "review_score", "ccu"]
    result: dict = {}
    for field in fields:
        values = [g[field] for g in games if g.get(field) is not None]
        if not values:
            result[field] = {
                "mean": None,
                "median": None,
                "stdev": None,
                "min": None,
                "max": None,
                "count": 0,
                "coverage_pct": 0.0,
            }
            continue
        coverage = round(len(values) / len(games) * 100, 1) if games else 0.0
        result[field] = {
            "mean": round(statistics.mean(values), 2),
            "median": round(statistics.median(values), 2),
            "stdev": round(statistics.stdev(values), 2) if len(values) >= 2 else None,
            "min": round(min(values), 2),
            "max": round(max(values), 2),
            "count": len(values),
            "coverage_pct": coverage,
        }
    return result


# ---------------------------------------------------------------------------
# Tag Frequency
# ---------------------------------------------------------------------------

def _compute_tag_frequency(
    games: list[dict],
    primary_tags: set[str] | None = None,
    top_n: int = 20,
) -> list[dict]:
    """Count how frequently each tag appears across the game list.

    Excludes tags in primary_tags (the search tags) from the results so the
    output shows co-occurring tags rather than the query tags themselves.

    Returns list of {tag, count, pct_of_games} dicts sorted by count descending,
    limited to top_n entries.
    """
    tag_counter: Counter = Counter()
    for g in games:
        tags = g.get("tags", {})
        if isinstance(tags, dict):
            tag_names = list(tags.keys())
        elif isinstance(tags, list):
            tag_names = list(tags)
        else:
            continue
        for tag in tag_names:
            if primary_tags and tag in primary_tags:
                continue
            tag_counter[tag] += 1
    total = len(games)
    result: list[dict] = []
    for tag, count in tag_counter.most_common(top_n):
        result.append({
            "tag": tag,
            "count": count,
            "pct_of_games": round(count / total * 100, 1) if total > 0 else 0.0,
        })
    return result


# ---------------------------------------------------------------------------
# GameProfile Builder
# ---------------------------------------------------------------------------

def _build_game_profile(game: dict) -> GameProfile:
    """Construct a GameProfile from a raw game dict.

    Maps median_forever -> median_playtime (hours).
    Tags passed through as-is (dict[str, int] from SteamSpy engagement data).
    """
    return GameProfile(
        appid=game.get("appid", 0),
        name=game.get("name", ""),
        revenue=game.get("revenue"),
        review_score=game.get("review_score"),
        release_date=game.get("release_date"),
        owners=game.get("owners"),
        ccu=game.get("ccu"),
        median_playtime=game.get("median_forever"),
        followers=game.get("followers"),
        copies_sold=game.get("copies_sold"),
        price=game.get("price"),
        tags=game.get("tags", {}),
        developer=game.get("developer", ""),
        publisher=game.get("publisher", ""),
    )


# ---------------------------------------------------------------------------
# Health Score Computation
# ---------------------------------------------------------------------------

STEAMSPY_SUPPLEMENT_THRESHOLD = 1900  # trigger SteamSpy supplement at >= this many games


def _compute_health_scores(analysis_result: dict) -> MarketHealthScores:
    """Compute three 0-100 market health scores from completed analysis result dict.

    Polarity:
    - growth: HIGH = growing market (recent years outperform prior years)
    - competition: HIGH = distributed/easy-to-compete market (low Gini concentration)
    - accessibility: HIGH = accessible to new entrants (affordable + good success rates)
    """
    # --- Growth Score ---
    growth = 50  # neutral default
    temporal = analysis_result.get("temporal_trends")
    if temporal is not None:
        yearly = temporal.get("yearly", [])
        # Filter partial years
        complete_years = [p for p in yearly if not p.get("partial", False)]
        if len(complete_years) >= 2:
            complete_years_sorted = sorted(complete_years, key=lambda p: p["year"])
            # Split into recent 3 and prior 3
            n = len(complete_years_sorted)
            mid = max(n // 2, 1)
            recent_periods = complete_years_sorted[max(n - 3, mid):]
            prior_periods = complete_years_sorted[:max(n - 3, mid)]
            if not prior_periods:
                prior_periods = complete_years_sorted[:mid]
                recent_periods = complete_years_sorted[mid:]

            recent_revenues = [p["avg_revenue"] for p in recent_periods if p.get("avg_revenue") is not None]
            prior_revenues = [p["avg_revenue"] for p in prior_periods if p.get("avg_revenue") is not None]
            recent_counts = [p["game_count"] for p in recent_periods if p.get("game_count") is not None]
            prior_counts = [p["game_count"] for p in prior_periods if p.get("game_count") is not None]

            revenue_change = 0.0
            count_change = 0.0

            if recent_revenues and prior_revenues:
                recent_avg = statistics.mean(recent_revenues)
                prior_avg = statistics.mean(prior_revenues)
                if prior_avg > 0:
                    revenue_change = (recent_avg - prior_avg) / prior_avg

            if recent_counts and prior_counts:
                recent_count = statistics.mean(recent_counts)
                prior_count = statistics.mean(prior_counts)
                if prior_count > 0:
                    count_change = (recent_count - prior_count) / prior_count

            growth_raw = (revenue_change * 0.6 + count_change * 0.4) * 100
            # 50 = neutral, +growth_raw if growing, -growth_raw if declining
            growth = max(0, min(100, round(50 + growth_raw)))

    # --- Competition Score (HIGH = distributed = good for new entrants) ---
    competition = 50  # neutral default
    concentration = analysis_result.get("concentration")
    if concentration is not None:
        gini = concentration.get("gini")
        if gini is not None:
            competition = max(0, min(100, round((1 - gini) * 100)))

    # --- Accessibility Score ---
    accessibility = 50  # neutral default
    price_brackets = analysis_result.get("price_brackets")
    success_rates = analysis_result.get("success_rates")

    affordable_pct = 0.5  # default: assume 50% affordable
    if price_brackets is not None:
        brackets = price_brackets.get("brackets", [])
        # Affordable: f2p, 0.01-4.99, 5-9.99, 10-14.99
        affordable_labels = {"f2p", "0.01_4.99", "5_9.99", "10_14.99"}
        total_count = sum(b.get("count", 0) for b in brackets)
        affordable_count = sum(b.get("count", 0) for b in brackets if b.get("label") in affordable_labels)
        if total_count > 0:
            affordable_pct = affordable_count / total_count

    success_rate_100k = 0.5  # default: 50% meet $100K threshold
    if success_rates is not None:
        thresholds = success_rates.get("thresholds", {})
        # Key is "100k" (from _format_threshold_label(100_000))
        rate = thresholds.get("100k")
        if rate is not None:
            success_rate_100k = rate / 100.0  # convert pct to fraction

    accessibility = max(0, min(100, round(affordable_pct * 0.6 * 100 + success_rate_100k * 0.4 * 100)))

    return MarketHealthScores(
        growth=growth,
        competition=competition,
        accessibility=accessibility,
    )


# ---------------------------------------------------------------------------
# Top Games Builder
# ---------------------------------------------------------------------------

def _build_top_games(games: list[dict], top_n: int) -> list[GameProfile]:
    """Sort games by revenue descending (None=0), take top_n, build GameProfile list."""
    sorted_games = sorted(games, key=lambda g: g.get("revenue") or 0, reverse=True)
    return [_build_game_profile(g) for g in sorted_games[:top_n]]


# ---------------------------------------------------------------------------
# Methodology Builder
# ---------------------------------------------------------------------------

def _build_methodology_dict(tags: list[str], data_source: str, n_games: int, skipped: list[SkippedMetric]) -> dict:
    """Return flat methodology dict for inclusion in result."""
    return {
        "data_source": data_source,
        "game_list_source": f"SteamSpy tag search for '{', '.join(tags)}'",
        "total_games_analyzed": n_games,
        "skipped_metrics": [s.model_dump() for s in skipped],
        "biases": list(STANDARD_BIASES),
        "notes": [],
    }


# ---------------------------------------------------------------------------
# Core _run_market_analysis — pure sync computation
# ---------------------------------------------------------------------------

def _run_market_analysis(
    games: list[dict],
    tags: list[str],
    metrics: list[str] | None = None,
    exclude: list[str] | None = None,
    top_n: int = 10,
    include_raw: bool = False,
    market_label: str = "",
    data_source: str = "steamspy",
) -> dict:
    """Core pure-function market analysis. No I/O, no async.

    Takes an already-fetched flat game dict list and computes all requested
    analytics metrics, health scores, and top games.

    Returns a flat result dict that can be serialized directly to AnalyzeMarketResult.
    """
    total_start = time.monotonic()

    # Step 1: Check minimum threshold
    if len(games) < MIN_GAMES_THRESHOLD:
        return {
            "error": f"Insufficient data: {len(games)} games found (minimum {MIN_GAMES_THRESHOLD} required for meaningful analysis).",
            "total_games": len(games),
            "market_label": market_label or (", ".join(tags) if tags else ""),
        }

    # Step 2: Determine label
    label = market_label or (", ".join(tags) if tags else "Market Analysis")

    # Step 3: Determine computable metrics
    computable = get_computable_metrics(len(games), requested=metrics)
    if exclude:
        computable -= set(exclude)

    # Step 4: Metric function map
    primary_tags_set = set(tags) if tags else set()

    metric_fn_map: dict[str, tuple] = {
        "concentration": (compute_concentration, {"games": games}),
        "temporal_trends": (compute_temporal_trends, {"games": games}),
        "price_brackets": (compute_price_brackets, {"games": games}),
        "success_rates": (compute_success_rates, {"games": games}),
        "tag_multipliers": (compute_tag_multipliers, {"games": games, "primary_tags": primary_tags_set}),
        "score_revenue": (compute_score_revenue, {"games": games}),
        "publisher_analysis": (compute_publisher_analysis, {"games": games}),
        "release_timing": (compute_release_timing, {"games": games}),
        "sub_genres": (compute_sub_genres, {"games": games, "primary_tags": primary_tags_set}),
        "competitive_density": (compute_competitive_density, {"games": games}),
    }

    all_metrics = set(metric_fn_map.keys())

    # Step 5: Always compute descriptive_stats and tag_frequency
    descriptive_stats = _compute_descriptive_stats(games)
    tag_frequency = _compute_tag_frequency(games, primary_tags=primary_tags_set, top_n=20)

    # Step 6: Compute each metric
    metric_results: dict[str, dict | None] = {}
    skipped: list[SkippedMetric] = []
    per_metric_timing: dict[str, int] = {}

    for metric_name in sorted(all_metrics):
        if metric_name not in computable:
            # Determine why it was skipped
            if metrics is not None and metric_name not in metrics:
                reason = "not requested"
            elif exclude and metric_name in exclude:
                reason = "excluded by caller"
            elif len(games) < 50 and metric_name in {
                "temporal_trends", "tag_multipliers", "score_revenue",
                "competitive_density", "release_timing", "sub_genres",
            }:
                reason = f"insufficient data (need 50 games, have {len(games)})"
            elif len(games) < 20 and metric_name in {
                "concentration", "success_rates", "price_brackets", "publisher_analysis",
            }:
                reason = f"insufficient data (need 20 games, have {len(games)})"
            else:
                reason = "below sample size threshold"
            skipped.append(SkippedMetric(metric=metric_name, reason=reason))
            metric_results[metric_name] = None
            continue

        fn, kwargs = metric_fn_map[metric_name]
        t0 = time.monotonic()
        try:
            result = fn(**kwargs)
        except Exception as exc:
            logger.warning(f"Metric {metric_name} failed: {exc}")
            result = None
            skipped.append(SkippedMetric(metric=metric_name, reason=f"computation error: {exc}"))
        elapsed_ms = round((time.monotonic() - t0) * 1000)
        per_metric_timing[metric_name] = elapsed_ms

        if result is None:
            metric_results[metric_name] = None
        else:
            metric_results[metric_name] = result.model_dump()

    # Step 7: Build preliminary result dict for health scores
    preliminary = {
        "concentration": metric_results.get("concentration"),
        "temporal_trends": metric_results.get("temporal_trends"),
        "price_brackets": metric_results.get("price_brackets"),
        "success_rates": metric_results.get("success_rates"),
    }
    health_scores = _compute_health_scores(preliminary)

    # Step 8: Build top games
    top_games = _build_top_games(games, top_n=top_n)

    # Step 9: Build methodology dict
    methodology = _build_methodology_dict(tags, data_source, len(games), skipped)

    # Step 10: Compute coverage
    games_with_revenue = sum(1 for g in games if g.get("revenue") is not None)
    coverage_pct = round(games_with_revenue / len(games) * 100, 1) if games else 0.0

    # Step 11: Build timing info
    total_ms = round((time.monotonic() - total_start) * 1000)
    compute_time = ComputeTimingInfo(
        total_ms=total_ms,
        per_metric=per_metric_timing,
        data_fetch_ms=0,
    )

    # Step 12: Assemble flat result dict
    result: dict = {
        "market_label": label,
        "total_games": len(games),
        "games_with_revenue": games_with_revenue,
        "coverage_pct": coverage_pct,
        "phase": 1,
        "continuation_token": None,
        # Analytics metrics
        "concentration": metric_results.get("concentration"),
        "temporal_trends": metric_results.get("temporal_trends"),
        "price_brackets": metric_results.get("price_brackets"),
        "success_rates": metric_results.get("success_rates"),
        "tag_multipliers": metric_results.get("tag_multipliers"),
        "score_revenue": metric_results.get("score_revenue"),
        "publisher_analysis": metric_results.get("publisher_analysis"),
        "release_timing": metric_results.get("release_timing"),
        "sub_genres": metric_results.get("sub_genres"),
        "competitive_density": metric_results.get("competitive_density"),
        # Derived analytics
        "descriptive_stats": descriptive_stats,
        "tag_frequency": tag_frequency,
        "health_scores": health_scores.model_dump(),
        # Game sample
        "top_games": [g.model_dump() for g in top_games],
        "skipped_metrics": [s.model_dump() for s in skipped],
        "warnings": [],
        "methodology": methodology,
        "compute_time": compute_time.model_dump(),
        "raw_data": None,
        "gamalytic_enrichment": None,
        "quota_warning": None,
    }

    # Step 13: Include raw data if requested
    if include_raw:
        result["raw_data"] = games

    return result


# ---------------------------------------------------------------------------
# Continuation Token Helpers
# ---------------------------------------------------------------------------

def _make_continuation_token(games: list[dict], tags: list[str], params: dict) -> str:
    """Encode game appids + tags + params into a compact base64 continuation token."""
    payload = {
        "appids": [g.get("appid") for g in games],
        "tags": tags,
        "params": params,
        "v": 1,
    }
    raw = json.dumps(payload, separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode()).decode()


def _decode_continuation_token(token: str) -> dict | None:
    """Decode continuation token. Returns None on any parse error."""
    try:
        raw = base64.urlsafe_b64decode(token.encode()).decode()
        return json.loads(raw)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Game Dict Conversion Helpers
# ---------------------------------------------------------------------------

def _game_summary_to_dict(game_summary) -> dict:
    """Convert a GameSummary schema object to a flat analysis-ready dict."""
    return {
        "appid": game_summary.appid,
        "name": game_summary.name,
        "developer": game_summary.developer,
        "publisher": game_summary.publisher,
        "owners": game_summary.owners_midpoint,
        "ccu": game_summary.ccu,
        "price": game_summary.price,
        "review_score": game_summary.review_score,
        "median_forever": game_summary.median_forever,
        "average_forever": game_summary.average_forever,
        # Revenue not yet in GameSummary (added by Gamalytic enrichment in Phase 2)
        "revenue": None,
        "followers": None,
        "copies_sold": None,
        "tags": {},  # GameSummary bulk data doesn't have per-game tag weights
        "release_date": None,  # not in GameSummary bulk response
    }


def _commercial_data_to_revenue(commercial_data) -> float | None:
    """Extract midpoint revenue estimate from CommercialData."""
    if commercial_data is None:
        return None
    rev_min = getattr(commercial_data, "revenue_min", None)
    rev_max = getattr(commercial_data, "revenue_max", None)
    if rev_min is not None and rev_max is not None:
        return (rev_min + rev_max) / 2
    if rev_min is not None:
        return rev_min
    if rev_max is not None:
        return rev_max
    return None


# ---------------------------------------------------------------------------
# Async Data Fetching Pipeline
# ---------------------------------------------------------------------------

async def _fetch_game_list_steamspy(
    steamspy: "SteamSpyClient",
    steam_store: "SteamStoreClient",
    tags: list[str],
) -> tuple[list[dict], str]:
    """Fetch game list from SteamSpy (single tag) or Steam Store (multi-tag).

    Returns (game_dicts, data_source_str).

    For single-tag: calls steamspy.search_by_tag. If result >= STEAMSPY_SUPPLEMENT_THRESHOLD,
    also supplements with Steam Store data.
    For multi-tag: uses Steam Store search.
    """
    from src.schemas import APIError as APIErrorSchema

    if len(tags) == 1:
        tag = tags[0]
        result = await steamspy.search_by_tag(tag, limit=None, sort_by="owners")

        if isinstance(result, APIErrorSchema):
            logger.warning(f"SteamSpy tag search failed for '{tag}': {result.message}")
            return [], "steamspy"

        games = [_game_summary_to_dict(g) for g in result.games]
        data_source = result.data_source or "steamspy"

        if len(games) >= STEAMSPY_SUPPLEMENT_THRESHOLD:
            logger.info(f"Large result set ({len(games)} games >= {STEAMSPY_SUPPLEMENT_THRESHOLD}): supplementing with Steam Store")
            try:
                store_result = await steam_store.search_by_tag_ids([tag], limit=None)
                if not isinstance(store_result, APIErrorSchema) and hasattr(store_result, "games"):
                    # Merge by appid: prefer SteamSpy data, add any new games from Store
                    existing_appids = {g["appid"] for g in games}
                    store_games = [_game_summary_to_dict(g) for g in store_result.games]
                    new_games = [g for g in store_games if g["appid"] not in existing_appids]
                    games.extend(new_games)
                    data_source = "steamspy+steam_store"
                    logger.info(f"Supplement added {len(new_games)} additional games")
            except Exception as exc:
                logger.warning(f"Steam Store supplement failed: {exc}")

        return games, data_source

    else:
        # Multi-tag: use Steam Store search
        logger.info(f"Multi-tag query ({len(tags)} tags): using Steam Store search")
        try:
            result = await steam_store.search_by_tag_ids(tags, limit=None)
            if isinstance(result, APIErrorSchema):
                logger.warning(f"Steam Store search failed for tags {tags}: {result.message}")
                return [], "steam_store"
            games = [_game_summary_to_dict(g) for g in result.games]
            return games, "steam_store"
        except Exception as exc:
            logger.error(f"Multi-tag fetch failed: {exc}")
            return [], "steam_store"


# ---------------------------------------------------------------------------
# Gamalytic Enrichment (Phase 2)
# ---------------------------------------------------------------------------

async def _enrich_with_gamalytic(
    games: list[dict],
    gamalytic: "GamalyticClient",
) -> tuple[list[dict], list[str]]:
    """Enrich game dicts with Gamalytic revenue and metadata.

    Returns (enriched_games, warnings_list).
    Fetches commercial data for each game concurrently.
    """
    import asyncio
    from src.schemas import APIError as APIErrorSchema

    warnings: list[str] = []

    async def _enrich_one(game: dict) -> dict:
        appid = game.get("appid")
        if not appid:
            return game
        try:
            commercial = await gamalytic.get_commercial_data(appid)
            if isinstance(commercial, APIErrorSchema):
                return game
            enriched = dict(game)
            enriched["revenue"] = _commercial_data_to_revenue(commercial)
            enriched["copies_sold"] = getattr(commercial, "copies_sold", None)
            enriched["followers"] = getattr(commercial, "followers", None)
            if not enriched.get("review_score") and getattr(commercial, "review_score", None):
                enriched["review_score"] = commercial.review_score
            return enriched
        except Exception as exc:
            logger.debug(f"Gamalytic enrichment failed for appid {appid}: {exc}")
            return game

    tasks = [_enrich_one(g) for g in games]
    enriched_games = await asyncio.gather(*tasks)

    # Quota tracking
    quota_warning = increment_gamalytic_counter(len(games))
    if quota_warning:
        warnings.append(quota_warning)

    return list(enriched_games), warnings


# ---------------------------------------------------------------------------
# Two-Phase Orchestration
# ---------------------------------------------------------------------------

async def analyze_market_phase1(
    steamspy: "SteamSpyClient",
    steam_store: "SteamStoreClient",
    gamalytic: "GamalyticClient",
    tags: list[str],
    appids: list[int] | None = None,
    metrics: list[str] | None = None,
    exclude: list[str] | None = None,
    top_n: int = 10,
    include_raw: bool = False,
    market_label: str = "",
) -> dict:
    """Phase 1: Fetch SteamSpy data, run analysis, return result + continuation_token.

    Phase 1 result has phase=1 with continuation_token for Phase 2 (Gamalytic enrichment).
    Games list is returned without revenue data (SteamSpy doesn't have it).
    """
    fetch_start = time.monotonic()
    games, data_source = await _fetch_game_list_steamspy(steamspy, steam_store, tags)
    fetch_ms = round((time.monotonic() - fetch_start) * 1000)

    if not games:
        return {
            "error": "No games found for specified tags.",
            "total_games": 0,
            "market_label": market_label or ", ".join(tags),
            "phase": 1,
        }

    # Build result dict
    result = _run_market_analysis(
        games=games,
        tags=tags,
        metrics=metrics,
        exclude=exclude,
        top_n=top_n,
        include_raw=include_raw,
        market_label=market_label,
        data_source=data_source,
    )

    if "error" in result:
        result["phase"] = 1
        return result

    # Add fetch timing
    if result.get("compute_time"):
        result["compute_time"]["data_fetch_ms"] = fetch_ms

    # Build continuation token (encodes game list + params for Phase 2)
    params = {
        "metrics": metrics,
        "exclude": exclude,
        "top_n": top_n,
        "include_raw": include_raw,
        "market_label": market_label,
    }
    token = _make_continuation_token(games, tags, params)
    result["continuation_token"] = token
    result["phase"] = 1

    return result


async def analyze_market_phase2(
    gamalytic: "GamalyticClient",
    continuation_token: str,
    steamspy: "SteamSpyClient | None" = None,
    steam_store: "SteamStoreClient | None" = None,
) -> dict:
    """Phase 2: Gamalytic enrichment of Phase 1 result.

    Decodes continuation_token to recover game list and params, then enriches
    with Gamalytic revenue data and re-runs analysis.
    """
    payload = _decode_continuation_token(continuation_token)
    if payload is None:
        return {
            "error": "Invalid or expired continuation_token.",
            "phase": 2,
        }

    appids = payload.get("appids", [])
    tags = payload.get("tags", [])
    params = payload.get("params", {})

    if not appids:
        return {
            "error": "continuation_token contains no game data.",
            "phase": 2,
        }

    # Re-construct game dicts from appids (minimal structure for enrichment)
    games = [{"appid": appid, "revenue": None} for appid in appids]

    # Enrich with Gamalytic
    fetch_start = time.monotonic()
    enriched_games, warnings = await _enrich_with_gamalytic(games, gamalytic)
    fetch_ms = round((time.monotonic() - fetch_start) * 1000)

    # Re-run analysis with enriched data
    result = _run_market_analysis(
        games=enriched_games,
        tags=tags,
        metrics=params.get("metrics"),
        exclude=params.get("exclude"),
        top_n=params.get("top_n", 10),
        include_raw=params.get("include_raw", False),
        market_label=params.get("market_label", ""),
        data_source="steamspy+gamalytic",
    )

    if "error" in result:
        result["phase"] = 2
        return result

    result["phase"] = 2
    result["continuation_token"] = None  # Phase 2 is terminal
    result["gamalytic_enrichment"] = {
        "games_enriched": len(enriched_games),
        "fetch_ms": fetch_ms,
    }

    if warnings:
        existing_warnings = result.get("warnings", [])
        result["warnings"] = existing_warnings + warnings

    if result.get("compute_time"):
        result["compute_time"]["data_fetch_ms"] = fetch_ms

    return result


# ---------------------------------------------------------------------------
# Evaluate Game — Sub-computation Functions
# ---------------------------------------------------------------------------

def _compute_percentiles(game: dict, genre_games: list[dict]) -> "GamePercentiles":
    """Compute percentile rank for 7 metrics against the genre population.

    Uses _percentile_rank (less-than definition): percentage of genre games
    strictly below the target game's value. Returns None per metric when the
    population for that metric is empty.
    """
    populations: dict[str, list[float]] = {
        "revenue": [g["revenue"] for g in genre_games if g.get("revenue") is not None],
        "review_score": [g["review_score"] for g in genre_games if g.get("review_score") is not None],
        "median_playtime": [g["median_forever"] for g in genre_games if g.get("median_forever") is not None],
        "ccu": [g["ccu"] for g in genre_games if g.get("ccu") is not None],
        "followers": [g["followers"] for g in genre_games if g.get("followers") is not None],
        "copies_sold": [g["copies_sold"] for g in genre_games if g.get("copies_sold") is not None],
        "review_count": [
            g.get("positive", 0) + g.get("negative", 0)
            for g in genre_games
            if g.get("positive") is not None
        ],
    }

    game_values: dict[str, float | None] = {
        "revenue": game.get("revenue"),
        "review_score": game.get("review_score"),
        "median_playtime": game.get("median_forever"),
        "ccu": game.get("ccu"),
        "followers": game.get("followers"),
        "copies_sold": game.get("copies_sold"),
        "review_count": (
            (game.get("positive", 0) + game.get("negative", 0))
            if game.get("positive") is not None
            else None
        ),
    }

    return GamePercentiles(
        revenue=_percentile_rank(game_values["revenue"], populations["revenue"]),
        review_score=_percentile_rank(game_values["review_score"], populations["review_score"]),
        median_playtime=_percentile_rank(game_values["median_playtime"], populations["median_playtime"]),
        ccu=_percentile_rank(game_values["ccu"], populations["ccu"]),
        followers=_percentile_rank(game_values["followers"], populations["followers"]),
        copies_sold=_percentile_rank(game_values["copies_sold"], populations["copies_sold"]),
        review_count=_percentile_rank(game_values["review_count"], populations["review_count"]),
    )


def _compute_strengths_weaknesses(
    game: dict, genre_games: list[dict]
) -> tuple[list[str], list[str]]:
    """Identify strengths and weaknesses using standard deviation analysis.

    A metric is a strength if the game is > 1.0 stdev above the mean.
    A metric is a weakness if the game is < -1.0 stdev below the mean.
    """
    populations: dict[str, list[float]] = {
        "revenue": [g["revenue"] for g in genre_games if g.get("revenue") is not None],
        "review_score": [g["review_score"] for g in genre_games if g.get("review_score") is not None],
        "median_playtime": [g["median_forever"] for g in genre_games if g.get("median_forever") is not None],
        "ccu": [g["ccu"] for g in genre_games if g.get("ccu") is not None],
        "followers": [g["followers"] for g in genre_games if g.get("followers") is not None],
        "copies_sold": [g["copies_sold"] for g in genre_games if g.get("copies_sold") is not None],
        "review_count": [
            g.get("positive", 0) + g.get("negative", 0)
            for g in genre_games
            if g.get("positive") is not None
        ],
    }

    game_values: dict[str, float | None] = {
        "revenue": game.get("revenue"),
        "review_score": game.get("review_score"),
        "median_playtime": game.get("median_forever"),
        "ccu": game.get("ccu"),
        "followers": game.get("followers"),
        "copies_sold": game.get("copies_sold"),
        "review_count": (
            (game.get("positive", 0) + game.get("negative", 0))
            if game.get("positive") is not None
            else None
        ),
    }

    strengths: list[str] = []
    weaknesses: list[str] = []

    for metric, value in game_values.items():
        pop = populations[metric]
        deviation = _std_deviations_from_mean(value, pop)
        if deviation is None:
            continue
        if deviation > 1.0:
            strengths.append(metric)
        elif deviation < -1.0:
            weaknesses.append(metric)

    return strengths, weaknesses


def _compute_opportunity_score(
    percentiles: "GamePercentiles",
    strengths: list[str],
    weaknesses: list[str],
    genre_health: "MarketHealthScores | None",
) -> tuple[float, float, float]:
    """Compute position, potential, and opportunity scores (all 0-100).

    Position score: How well the game performs NOW vs. genre peers.
    Potential score: Room to grow based on market health and current gaps.
    Opportunity score: Composite = position * 0.5 + potential * 0.5.

    Returns (position_score, potential_score, opportunity_score).
    """
    # Position score: average of available percentiles
    percentile_values = [
        v for v in [
            percentiles.revenue, percentiles.review_score, percentiles.median_playtime,
            percentiles.ccu, percentiles.followers, percentiles.copies_sold,
            percentiles.review_count,
        ]
        if v is not None
    ]
    if percentile_values:
        position_score = round(statistics.mean(percentile_values), 1)
    else:
        position_score = 50.0  # neutral when no data

    # Potential score: headroom + market growth signals.
    # Calibrated so neutral market (growth=50) + mid-pack (position=50) => potential=50.
    growth_factor = genre_health.growth / 100 if genre_health else 0.5
    headroom = (100 - position_score) / 100
    # weakness_factor: many fixable gaps => more growth room
    weakness_factor = min(len(weaknesses) / 3, 1.0)
    potential_raw = (growth_factor * 0.5 + headroom * 0.3 + weakness_factor * 0.2) * 100
    potential_score = round(max(0.0, min(100.0, potential_raw)), 1)

    # Opportunity score (composite):
    # Weights: position 0.5 + potential 0.3 + market_growth 0.2.
    # Growth component ensures top performers in growing markets get > 70.
    # Calibrated tiers: 90-100 exceptional, 75-89 strong, 60-74 moderate, 40-59 neutral.
    growth_component = genre_health.growth if genre_health else 50.0
    opportunity_raw = position_score * 0.5 + potential_score * 0.3 + growth_component * 0.2
    opportunity_score = round(max(0.0, min(100.0, opportunity_raw)), 1)

    return position_score, potential_score, opportunity_score


def _find_competitors(
    game: dict,
    genre_games: list[dict],
    commercial_data=None,
    n: int = 5,
) -> list["CompetitorComparison"]:
    """Identify top competitor games via audience_overlap or tag-based fallback.

    Primary path: audience_overlap from Gamalytic commercial_data.
    Fallback path: Jaccard tag similarity + revenue tier match + price proximity.
    """
    target_appid = game.get("appid")

    # --- Primary: audience_overlap from Gamalytic ---
    audience_overlap = getattr(commercial_data, "audience_overlap", None) or []
    if audience_overlap:
        # Sort by overlap_pct descending, take top n
        sorted_overlap = sorted(
            audience_overlap,
            key=lambda e: (e.overlap_pct or 0),
            reverse=True,
        )[:n]
        competitors = []
        for entry in sorted_overlap:
            if entry.appid is None:
                continue
            # Find matching game in genre_games for extra data
            match = next((g for g in genre_games if g.get("appid") == entry.appid), None)
            competitors.append(CompetitorComparison(
                appid=entry.appid,
                name=entry.name or (match.get("name", "") if match else ""),
                revenue=entry.revenue or (match.get("revenue") if match else None),
                review_score=entry.score or (match.get("review_score") if match else None),
                price=match.get("price") if match else None,
                ccu=match.get("ccu") if match else None,
                owners=match.get("owners") if match else None,
                followers=entry.followers or (match.get("followers") if match else None),
                similarity_score=round(entry.overlap_pct or 0, 2),
                overlap_source="audience_overlap",
                shared_tags=[],
            ))
        return competitors

    # --- Fallback: tag similarity scoring ---
    game_tags = set(_get_top_tags(game.get("tags", {}), top_n=20))
    game_revenue = game.get("revenue")
    game_tier = _assign_revenue_tier(game_revenue)
    game_price = game.get("price") or 0.0

    scored: list[tuple[float, dict, list[str]]] = []

    for g in genre_games:
        g_appid = g.get("appid")
        if g_appid == target_appid:
            continue

        g_tags = set(_get_top_tags(g.get("tags", {}), top_n=20))
        # Jaccard similarity
        union = game_tags | g_tags
        intersection = game_tags & g_tags
        tag_score = len(intersection) / len(union) if union else 0.0

        # Revenue tier match
        g_tier = _assign_revenue_tier(g.get("revenue"))
        tier_match = 1.0 if (game_tier and g_tier and game_tier == g_tier) else 0.5

        # Price proximity
        g_price = g.get("price") or 0.0
        denom = max(game_price, g_price, 0.01)
        price_score = min(game_price, g_price) / denom

        total_score = tag_score * 0.6 + tier_match * 0.3 + price_score * 0.1
        shared = sorted(intersection)
        scored.append((total_score, g, shared))

    # Sort by total score descending, take top n
    scored.sort(key=lambda x: x[0], reverse=True)
    top = scored[:n]

    competitors = []
    for score, g, shared_tags in top:
        competitors.append(CompetitorComparison(
            appid=g.get("appid", 0),
            name=g.get("name", ""),
            revenue=g.get("revenue"),
            review_score=g.get("review_score"),
            price=g.get("price"),
            ccu=g.get("ccu"),
            owners=g.get("owners"),
            followers=g.get("followers"),
            similarity_score=round(score, 3),
            overlap_source="tag_fallback",
            shared_tags=shared_tags,
        ))

    return competitors


def _compute_tag_insights(
    game: dict,
    genre_games: list[dict],
    primary_tags: set[str],
) -> list["TagInsight"]:
    """Classify each non-primary game tag as positive_differentiator or niche_risk.

    Compares tag revenue multiplier vs genre average. Tags with more games than
    the minimum threshold get a computed multiplier; others get "neutral".
    """
    # Compute genre average revenue
    all_revenues = [g["revenue"] for g in genre_games if g.get("revenue") is not None]
    genre_avg = _safe_mean(all_revenues)
    if not genre_avg:
        return []

    # Build tag -> list of revenues across genre
    min_games = max(3, len(all_revenues) // 30)
    tag_revenues: dict[str, list[float]] = defaultdict(list)
    for g in genre_games:
        if g.get("revenue") is None:
            continue
        for tag in _get_top_tags(g.get("tags", {}), top_n=20):
            if tag in primary_tags:
                continue
            tag_revenues[tag].append(g["revenue"])

    # Classify each tag on the target game
    game_tags = _get_top_tags(game.get("tags", {}), top_n=30)
    insights: list[TagInsight] = []

    for tag in game_tags:
        if tag in primary_tags:
            continue
        revs = tag_revenues.get(tag, [])
        game_count = len(revs)

        if game_count >= min_games:
            tag_avg = statistics.mean(revs)
            multiplier = round(tag_avg / genre_avg, 3) if genre_avg else 1.0
        else:
            multiplier = 1.0  # insufficient data -> neutral

        if multiplier > 1.0:
            classification = "positive_differentiator"
        elif multiplier < 1.0:
            classification = "niche_risk"
        else:
            classification = "neutral"

        insights.append(TagInsight(
            tag=tag,
            multiplier=multiplier,
            game_count=game_count,
            classification=classification,
        ))

    # Sort by multiplier descending (best differentiators first)
    insights.sort(key=lambda t: t.multiplier, reverse=True)
    return insights


def _build_genre_fit(
    game_tags: list[str],
    genre_top_tags: list[dict],
    tag_insights: list["TagInsight"],
) -> dict:
    """Assess how well the game's tag profile matches genre norms.

    Returns dict with unique_tags, missing_common_tags, differentiators, niche_risks.
    """
    game_tag_set = set(game_tags)
    # Genre top tags (common = appears in >25% of genre games)
    common_genre_tags = {
        t["tag"]
        for t in genre_top_tags
        if t.get("pct_of_games", 0) >= 25.0
    }

    unique_tags = sorted(game_tag_set - {t["tag"] for t in genre_top_tags[:20]})
    missing_common_tags = sorted(common_genre_tags - game_tag_set)

    differentiators = [ti.model_dump() for ti in tag_insights if ti.classification == "positive_differentiator"]
    niche_risks = [ti.model_dump() for ti in tag_insights if ti.classification == "niche_risk"]

    return {
        "unique_tags": unique_tags,
        "missing_common_tags": missing_common_tags,
        "differentiators": differentiators,
        "niche_risks": niche_risks,
    }


def _build_pricing_context(game: dict, genre_games: list[dict]) -> dict:
    """Show where the game sits on the genre's price-revenue curve.

    Returns dict with game_price, genre_price_stats, price_bracket,
    bracket_avg_revenue, bracket_success_rate, and sweet_spot.
    """
    _SUCCESS_THRESHOLD = 100_000

    game_price = game.get("price")
    game_bracket = _assign_price_bracket(game_price)

    # Genre price stats
    all_prices = [g["price"] for g in genre_games if g.get("price") is not None]
    price_stats = {
        "mean": round(statistics.mean(all_prices), 2) if all_prices else None,
        "median": round(statistics.median(all_prices), 2) if all_prices else None,
        "min": round(min(all_prices), 2) if all_prices else None,
        "max": round(max(all_prices), 2) if all_prices else None,
    }

    # Per-bracket revenue stats
    bracket_data: dict[str, dict] = {}
    for label, min_p, max_p in PRICE_BRACKETS:
        bracket_games = [
            g for g in genre_games
            if g.get("price") is not None and _assign_price_bracket(g["price"]) == label
        ]
        revenues = [g["revenue"] for g in bracket_games if g.get("revenue") is not None]
        count = len(bracket_games)
        avg_rev = round(statistics.mean(revenues), 2) if revenues else None
        if revenues:
            success_count = sum(1 for r in revenues if r >= _SUCCESS_THRESHOLD)
            success_rate = round(success_count / len(revenues) * 100, 1)
        else:
            success_rate = None
        bracket_data[label] = {
            "count": count,
            "avg_revenue": avg_rev,
            "success_rate": success_rate,
        }

    # Current game bracket stats
    game_bracket_info = bracket_data.get(game_bracket, {}) if game_bracket else {}
    bracket_avg_revenue = game_bracket_info.get("avg_revenue")
    bracket_success_rate = game_bracket_info.get("success_rate")

    # Sweet spot: bracket with highest avg_revenue (that has enough games)
    valid_brackets = [
        (label, data)
        for label, data in bracket_data.items()
        if data["count"] >= 3 and data["avg_revenue"] is not None
    ]
    sweet_spot = None
    if valid_brackets:
        best_label, best_data = max(valid_brackets, key=lambda x: x[1]["avg_revenue"])
        sweet_spot = {
            "bracket": best_label,
            "avg_revenue": best_data["avg_revenue"],
        }

    return {
        "game_price": game_price,
        "genre_price_stats": price_stats,
        "price_bracket": game_bracket,
        "bracket_avg_revenue": bracket_avg_revenue,
        "bracket_success_rate": bracket_success_rate,
        "sweet_spot": sweet_spot,
    }


def _build_review_sentiment(reviews_data: dict | None) -> dict | None:
    """Extract a medium-depth review sentiment summary from ReviewsData dict.

    Returns None if reviews_data is None.
    """
    if reviews_data is None:
        return None

    # Handle both dict and ReviewsData schema objects
    if hasattr(reviews_data, "aggregates"):
        # ReviewsData schema object
        aggregates = reviews_data.aggregates
        sentiment_periods = reviews_data.sentiment or []
        snippets = reviews_data.snippets or []
        reviews = reviews_data.reviews or []
    elif isinstance(reviews_data, dict):
        aggregates = reviews_data.get("aggregates") or {}
        sentiment_periods = reviews_data.get("sentiment") or []
        snippets = reviews_data.get("snippets") or []
        reviews = reviews_data.get("reviews") or []
    else:
        return None

    # Extract aggregates
    if isinstance(aggregates, dict):
        overall_score = aggregates.get("review_score_desc", "")
        positive_pct = aggregates.get("positive_ratio")
        total_reviews = aggregates.get("total_reviews", 0)
    else:
        overall_score = getattr(aggregates, "review_score_desc", "")
        positive_pct = getattr(aggregates, "positive_ratio", None)
        total_reviews = getattr(aggregates, "total_reviews", 0)

    # Take first 3 sentiment periods
    top_sentiment = []
    for p in sentiment_periods[:3]:
        if isinstance(p, dict):
            top_sentiment.append(p)
        else:
            top_sentiment.append(p.model_dump())

    # Top snippets or reviews
    top_snippets = []
    if snippets:
        for s in snippets[:3]:
            if isinstance(s, dict):
                top_snippets.append(s)
            else:
                top_snippets.append(s.model_dump())
    elif reviews:
        for r in reviews[:3]:
            if isinstance(r, dict):
                top_snippets.append({"text": r.get("text", ""), "voted_up": r.get("voted_up", True)})
            else:
                top_snippets.append({"text": getattr(r, "text", ""), "voted_up": getattr(r, "voted_up", True)})

    return {
        "overall_score": overall_score,
        "positive_pct": positive_pct,
        "total_reviews": total_reviews,
        "sentiment_periods": top_sentiment,
        "top_snippets": top_snippets,
    }


def _build_historical_performance(commercial_data) -> list[dict] | None:
    """Extract monthly performance data points from Gamalytic history.

    Returns None if commercial_data is None or history is empty.
    """
    if commercial_data is None:
        return None
    history = getattr(commercial_data, "history", None) or []
    if not history:
        return None

    result = []
    for entry in history:
        result.append({
            "timestamp": getattr(entry, "timestamp", None),
            "revenue": getattr(entry, "revenue", None),
            "players": getattr(entry, "gamalytic_players", None),
            "reviews": getattr(entry, "reviews", None),
            "followers": getattr(entry, "followers", None),
        })
    return result if result else None


# ---------------------------------------------------------------------------
# Evaluate Game — Main Orchestration
# ---------------------------------------------------------------------------

async def evaluate_game_analysis(
    appid: int,
    genre_tags: list[str],
    genre_games: list[dict],
    genre_analysis: dict | None,
    game_data: dict,
    commercial_data=None,
    reviews_data: dict | None = None,
    price_tier: str | None = None,
) -> dict:
    """Evaluate a specific game against its genre benchmarks.

    Computes percentile rankings, strengths/weaknesses, opportunity score,
    competitor identification, tag insights, genre fit, pricing context,
    review sentiment, and historical performance.

    Returns a flat dict matching EvaluateGameResult schema.
    """
    total_start = time.monotonic()
    warnings: list[str] = []

    # Step 1: Build game profile
    game_profile = _build_game_profile(game_data)

    # Step 2: Filter genre games by price tier if requested
    working_games = genre_games
    if price_tier:
        tier_games = [g for g in genre_games if _assign_price_bracket(g.get("price")) == price_tier]
        if len(tier_games) >= MIN_GAMES_THRESHOLD:
            working_games = tier_games
        else:
            warnings.append(
                f"Price tier '{price_tier}' has only {len(tier_games)} games "
                f"(minimum {MIN_GAMES_THRESHOLD}). Using full genre pool."
            )

    # Step 3: Compute percentiles
    percentiles = _compute_percentiles(game_data, working_games)

    # Step 4: Compute strengths and weaknesses
    strengths, weaknesses = _compute_strengths_weaknesses(game_data, working_games)

    # Step 5: Get health scores from genre_analysis
    genre_health: MarketHealthScores | None = None
    if genre_analysis is not None:
        health_dict = genre_analysis.get("health_scores")
        if health_dict:
            if isinstance(health_dict, dict):
                genre_health = MarketHealthScores(**health_dict)
            elif isinstance(health_dict, MarketHealthScores):
                genre_health = health_dict

    # Step 6: Compute opportunity scores
    position_score, potential_score, opportunity_score = _compute_opportunity_score(
        percentiles, strengths, weaknesses, genre_health
    )

    # Step 7: Find competitors
    competitors = _find_competitors(game_data, working_games, commercial_data=commercial_data, n=5)

    # Step 8: Compute tag insights
    primary_tags_set = set(genre_tags)
    tag_insights = _compute_tag_insights(game_data, working_games, primary_tags_set)

    # Step 9: Build genre fit analysis
    game_tags_list = _get_top_tags(game_data.get("tags", {}), top_n=30)
    genre_top_tags = _compute_tag_frequency(working_games, primary_tags=primary_tags_set, top_n=20)
    genre_fit = _build_genre_fit(game_tags_list, genre_top_tags, tag_insights)

    # Step 10: Build pricing context
    pricing_context = _build_pricing_context(game_data, working_games)

    # Step 11: Build review sentiment
    review_sentiment = _build_review_sentiment(reviews_data)

    # Step 12: Build historical performance
    historical_performance = _build_historical_performance(commercial_data)

    # Step 13: Build genre baseline summary
    revenues = [g["revenue"] for g in working_games if g.get("revenue") is not None]
    games_with_revenue = len(revenues)
    coverage_pct = round(games_with_revenue / len(working_games) * 100, 1) if working_games else 0.0
    genre_baseline = {
        "total_games": len(working_games),
        "games_with_revenue": games_with_revenue,
        "avg_revenue": round(statistics.mean(revenues), 2) if revenues else None,
        "median_revenue": round(statistics.median(revenues), 2) if revenues else None,
        "coverage_pct": coverage_pct,
    }

    # Step 14: Build methodology
    total_ms = round((time.monotonic() - total_start) * 1000)
    compute_time = ComputeTimingInfo(total_ms=total_ms)
    methodology = {
        "data_source": "steamspy+gamalytic" if commercial_data else "steamspy",
        "genre_tags": genre_tags,
        "genre_games_analyzed": len(working_games),
        "competitor_method": "audience_overlap" if getattr(commercial_data, "audience_overlap", None) else "tag_fallback",
        "biases": list(STANDARD_BIASES),
        "notes": [],
    }

    # Step 15: Assemble EvaluateGameResult and return as dict
    result_obj = EvaluateGameResult(
        appid=appid,
        name=game_data.get("name", ""),
        genre=", ".join(genre_tags),
        game_profile=game_profile,
        percentiles=percentiles,
        strengths=strengths,
        weaknesses=weaknesses,
        position_score=position_score,
        potential_score=potential_score,
        opportunity_score=opportunity_score,
        competitors=competitors,
        tag_insights=tag_insights,
        genre_fit=genre_fit,
        review_sentiment=review_sentiment,
        pricing_context=pricing_context,
        historical_performance=historical_performance,
        genre_baseline=genre_baseline,
        compute_time=compute_time,
        warnings=warnings,
        methodology=methodology,
    )

    return result_obj.model_dump()
