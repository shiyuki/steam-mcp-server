"""Market analysis orchestration layer for Phase 9 tools.

Pure functions where possible — no HTTP calls, no async.
Testable independently without MCP wiring.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import statistics
import time
from collections import Counter, defaultdict
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.steam_api import GamalyticClient, SteamSpyClient, SteamStoreClient

from src.timeout_utils import (
    gather_with_timeout,
    compute_gather_timeout,
    STEAMSPY_RATE,
    GAMALYTIC_RATE,
    OVERALL_TIMEOUT,
)

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
from src.steam_api import _ms_to_iso, normalize_tag
from src.schemas import (
    APIError,
    AnalyzeMarketResult,
    CompareMarketsResult,
    CompetitorComparison,
    ComputeTimingInfo,
    EvaluateGameResult,
    GamePercentiles,
    GameProfile,
    MarketComparisonEntry,
    MarketHealthScores,
    MetricDelta,
    OverlapInfo,
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
GAMALYTIC_SAMPLE_SIZE = 200            # max games for per-game fallback enrichment
BULK_MATCH_THRESHOLD = 0.6             # minimum AppID overlap ratio for bulk enrichment trust
GAMALYTIC_REVENUE_THRESHOLD_PCT = 0.1  # games with >0.1% of total genre revenue get Tier 2 deep enrichment
GAMALYTIC_TAG_NORMALIZATIONS = {       # Steam tag -> Gamalytic tag when names diverge
    "rogue-like": "roguelike",
    "rogue-lite": "roguelite",
    "hack-and-slash": "hack and slash",
    "beat-em-up": "beat em up",
    "4x": "4X",
}

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
    if "steam_store" in data_source and len(tags) > 1:
        source_label = f"Steam Store multi-tag search for '{', '.join(tags)}'"
    elif "steam_store" in data_source:
        source_label = f"Steam Store search for '{', '.join(tags)}'"
    elif tags:
        source_label = f"SteamSpy tag search for '{', '.join(tags)}'"
    else:
        source_label = f"Custom AppID list ({n_games} games)"
    return {
        "data_source": data_source,
        "game_list_source": source_label,
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
    skip_min_check: bool = False,
) -> dict:
    """Core pure-function market analysis. No I/O, no async.

    Takes an already-fetched flat game dict list and computes all requested
    analytics metrics, health scores, and top games.

    Returns a flat result dict that can be serialized directly to AnalyzeMarketResult.

    Args:
        skip_min_check: If True, bypass MIN_GAMES_THRESHOLD check.
            Used by evaluate_game with custom genre_appids baselines.
    """
    total_start = time.monotonic()

    # Step 1: Check minimum threshold
    if not skip_min_check and len(games) < MIN_GAMES_THRESHOLD:
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
        "positive": game_summary.positive,
        "negative": game_summary.negative,
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


async def _gamalytic_fallback(
    gamalytic: "GamalyticClient",
    appids: list[int],
) -> list[dict]:
    """Recover failed SteamSpy per-game appids using Gamalytic commercial data.

    Builds partial game dicts — no CCU, playtime, or tag data, but provides
    name, price, review_score, release_date, revenue, followers, copies_sold.
    """
    import asyncio
    from src.schemas import APIError as APIErrorSchema

    async def _fetch_one_gamalytic(appid: int) -> dict | None:
        try:
            commercial = await gamalytic.get_commercial_data(appid)
            if isinstance(commercial, APIErrorSchema):
                logger.debug(
                    f"Gamalytic fallback failed for appid {appid}: {commercial.message}"
                )
                return None
            return {
                "appid": appid,
                "name": getattr(commercial, "name", "") or "",
                "developer": "",
                "publisher": "",
                "owners": getattr(commercial, "gamalytic_owners", None),
                "ccu": None,
                "price": getattr(commercial, "price", None),
                "review_score": getattr(commercial, "review_score", None),
                "median_forever": None,
                "average_forever": None,
                "revenue": _commercial_data_to_revenue(commercial),
                "followers": getattr(commercial, "followers", None),
                "copies_sold": getattr(commercial, "copies_sold", None),
                "tags": {},
                "release_date": getattr(commercial, "release_date", None),
            }
        except Exception as exc:
            logger.debug(f"Gamalytic fallback exception for appid {appid}: {exc}")
            return None

    if not appids:
        return []

    tasks = [_fetch_one_gamalytic(appid) for appid in appids]
    gather_result = await gather_with_timeout(
        tasks,
        timeout=compute_gather_timeout(len(appids), GAMALYTIC_RATE),
        label=f"gamalytic_fallback({len(appids)})",
    )
    recovered = [r for r in gather_result.results if isinstance(r, dict)]
    if recovered:
        logger.info(
            f"Gamalytic fallback: recovered {len(recovered)}/{len(appids)} appids "
            "that failed SteamSpy per-game fetch"
        )
    return recovered


async def _fetch_game_list_steamspy(
    steamspy: "SteamSpyClient",
    steam_store: "SteamStoreClient",
    gamalytic: "GamalyticClient",
    tags: list[str],
    appids: list[int] | None = None,
) -> tuple[list[dict], str]:
    """Fetch game list from SteamSpy (single tag) or Steam Store (multi-tag), or per-appid.

    Returns (game_dicts, data_source_str).

    When appids provided and tags empty: fetches per-appid engagement data concurrently.
    For single-tag: calls steamspy.search_by_tag. If result >= STEAMSPY_SUPPLEMENT_THRESHOLD,
    also supplements with Steam Store data.
    For multi-tag: uses Steam Store search.
    Tags take priority when both tags and appids are provided.
    """
    import asyncio
    from src.schemas import APIError as APIErrorSchema, SearchResult

    # Shared inner function: fetch SteamSpy engagement data for a single appid.
    # Used by appids-only path, supplement path, and multi-tag path.
    async def _fetch_one(appid: int) -> dict | None:
        try:
            result = await steamspy.get_engagement_data(appid)
            if isinstance(result, APIErrorSchema):
                logger.debug(f"SteamSpy engagement fetch failed for appid {appid}: {result.message}")
                return None
            return {
                "appid": result.appid,
                "name": result.name,
                "developer": result.developer,
                "publisher": result.publisher,
                "owners": result.owners_midpoint,
                "ccu": result.ccu,
                "price": result.price,
                "review_score": result.review_score,
                "median_forever": result.median_forever,
                "average_forever": result.average_forever,
                "positive": result.positive,
                "negative": result.negative,
                "revenue": None,
                "followers": None,
                "copies_sold": None,
                "tags": result.tags,
                "release_date": None,
            }
        except Exception as exc:
            logger.debug(f"Appid fetch exception for {appid}: {exc}")
            return None

    # Appids-only path: fetch per-appid engagement data concurrently
    if appids and not tags:
        logger.info(f"Appids-only fetch: fetching engagement data for {len(appids)} appids")
        tasks = [_fetch_one(appid) for appid in appids]
        gather_result = await gather_with_timeout(
            tasks,
            timeout=compute_gather_timeout(len(appids), STEAMSPY_RATE),
            label=f"appids_only({len(appids)})",
        )
        results = gather_result.results
        games = [r for r in results if isinstance(r, dict)]
        failed_appids = [appids[i] for i, r in enumerate(results) if not isinstance(r, dict)]
        if failed_appids and gamalytic is not None:
            logger.debug(
                f"Appids-only: {len(failed_appids)}/{len(appids)} appids failed SteamSpy, "
                "attempting Gamalytic fallback"
            )
            fallback_games = await _gamalytic_fallback(gamalytic, failed_appids)
            games.extend(fallback_games)
        return games, "steamspy_appids"

    if len(tags) == 1:
        tag = tags[0]
        result = await steamspy.search_by_tag(tag, limit=None, sort_by="owners")

        needs_fallback = False
        if isinstance(result, APIErrorSchema):
            logger.warning(f"SteamSpy tag search failed for '{tag}': {result.message}")
            needs_fallback = True
        elif isinstance(result, SearchResult) and result.total_found == 0 and len(result.games) == 0:
            logger.warning(f"SteamSpy returned empty result (possible bowling fallback) for tag '{tag}'")
            needs_fallback = True

        if not needs_fallback:
            games = [_game_summary_to_dict(g) for g in result.games]
            data_source = result.data_source or "steamspy"

            if len(games) >= STEAMSPY_SUPPLEMENT_THRESHOLD:
                logger.info(f"Large result set ({len(games)} games >= {STEAMSPY_SUPPLEMENT_THRESHOLD}): supplementing with Steam Store")
                try:
                    # Resolve tag name to integer tag ID via get_tag_map
                    tag_map = await steam_store.get_tag_map()
                    if isinstance(tag_map, APIErrorSchema):
                        logger.warning(f"Steam Store tag map fetch failed: {tag_map.message} — skipping supplement")
                    else:
                        tag_id = tag_map.get(tag.lower())
                        if tag_id is None:
                            logger.warning(f"Tag '{tag}' not found in Steam tag map ({len(tag_map)} tags) — skipping supplement")
                        else:
                            # Fetch appids from Steam Store using integer tag ID
                            store_result = await steam_store.search_by_tag_ids([tag_id], count=50)
                            if isinstance(store_result, APIErrorSchema):
                                logger.warning(f"Steam Store supplement search failed: {store_result.message}")
                            else:
                                # search_by_tag_ids returns tuple[int, list[int]]
                                total_count, store_appids = store_result
                                existing_appids = {g["appid"] for g in games}
                                new_appids = [a for a in store_appids if a not in existing_appids]
                                if new_appids:
                                    # Enrich new appids with SteamSpy engagement data concurrently
                                    enrich_tasks = [_fetch_one(appid) for appid in new_appids]
                                    enrich_gr = await gather_with_timeout(
                                        enrich_tasks,
                                        timeout=compute_gather_timeout(len(new_appids), STEAMSPY_RATE),
                                        label=f"supplement_enrich({len(new_appids)})",
                                    )
                                    new_games = [r for r in enrich_gr.results if isinstance(r, dict)]
                                    games.extend(new_games)
                                    data_source = "steamspy+steam_store"
                                    logger.info(f"Supplement added {len(new_games)} additional games (of {len(new_appids)} new appids)")
                except Exception as exc:
                    logger.warning(f"Steam Store supplement failed: {exc}")

            return games, data_source

        # Steam Store fallback: SteamSpy single-tag path failed
        logger.info(f"Single-tag SteamSpy failed for '{tag}', falling back to Steam Store")
        try:
            tag_map = await steam_store.get_tag_map()
            if isinstance(tag_map, APIErrorSchema):
                logger.warning(f"Steam Store tag map fetch failed: {tag_map.message}")
                return [], "steam_store_fallback"

            tag_id = tag_map.get(tag.lower())
            if tag_id is None:
                logger.warning(
                    f"Tag '{tag}' not found in Steam tag map ({len(tag_map)} tags) — "
                    "cannot fall back to Steam Store"
                )
                return [], "steam_store_fallback"

            store_result = await steam_store.search_by_tag_ids_paginated([tag_id], max_results=2000)
            if isinstance(store_result, APIErrorSchema):
                logger.warning(f"Steam Store fallback search failed: {store_result.message}")
                return [], "steam_store_fallback"

            total_count, store_appids = store_result
            if not store_appids:
                logger.warning(f"Steam Store fallback returned 0 appids for tag '{tag}'")
                return [], "steam_store_fallback"

            logger.info(f"Steam Store fallback: enriching {len(store_appids)} appids for tag '{tag}'")
            enrich_tasks = [_fetch_one(appid) for appid in store_appids]
            enrich_gr = await gather_with_timeout(
                enrich_tasks,
                timeout=compute_gather_timeout(len(store_appids), STEAMSPY_RATE),
                label=f"store_fallback_enrich({len(store_appids)})",
            )
            games = [r for r in enrich_gr.results if isinstance(r, dict)]
            return games, "steam_store_fallback"

        except Exception as exc:
            logger.warning(f"Steam Store fallback failed with exception: {exc}")
            return [], "steam_store_fallback"

    else:
        # Multi-tag: use Steam Store search with integer tag IDs
        logger.info(f"Multi-tag query ({len(tags)} tags): using Steam Store search")
        # Resolve all tag names to integer tag IDs via get_tag_map
        tag_map = await steam_store.get_tag_map()
        if isinstance(tag_map, APIErrorSchema):
            logger.warning(f"Steam Store tag map fetch failed for multi-tag query: {tag_map.message}")
            return [], "steam_store"

        tag_ids = []
        missing_tags = []
        for t in tags:
            tid = tag_map.get(t.lower())
            if tid is None:
                missing_tags.append(t)
            else:
                tag_ids.append(tid)

        if missing_tags:
            logger.warning(f"Tag(s) not found in Steam tag map: {missing_tags}")
            return [], f"steam_store:tag_error:{','.join(missing_tags)}"

        # Fetch appids from Steam Store using all integer tag IDs (intersection)
        store_result = await steam_store.search_by_tag_ids_paginated(tag_ids, max_results=1000)
        if isinstance(store_result, APIErrorSchema):
            logger.warning(f"Steam Store multi-tag search failed: {store_result.message}")
            return [], "steam_store"

        # search_by_tag_ids_paginated returns tuple[int, list[int]]
        total_count, store_appids = store_result
        if not store_appids:
            return [], "steam_store"

        # Enrich appids with SteamSpy engagement data concurrently
        enrich_tasks = [_fetch_one(appid) for appid in store_appids]
        enrich_gr = await gather_with_timeout(
            enrich_tasks,
            timeout=compute_gather_timeout(len(store_appids), STEAMSPY_RATE),
            label=f"multi_tag_enrich({len(store_appids)})",
        )
        games = [r for r in enrich_gr.results if isinstance(r, dict)]
        return games, "steam_store"


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
            # Merge release_date from CommercialData
            if commercial.release_date and not enriched.get("release_date"):
                enriched["release_date"] = commercial.release_date
            return enriched
        except Exception as exc:
            logger.debug(f"Gamalytic enrichment failed for appid {appid}: {exc}")
            return game

    tasks = [_enrich_one(g) for g in games]
    gather_result = await gather_with_timeout(
        tasks,
        timeout=compute_gather_timeout(len(games), GAMALYTIC_RATE),
        label=f"enrich_gamalytic({len(games)})",
        return_exceptions=True,
    )
    # _enrich_one always returns a dict (falls back to original game on error),
    # but with return_exceptions=True, timeouts appear as exceptions
    enriched_games = [r if isinstance(r, dict) else games[i]
                      for i, r in enumerate(gather_result.results)]

    # Quota tracking
    quota_warning = increment_gamalytic_counter(len(games))
    if quota_warning:
        warnings.append(quota_warning)

    return list(enriched_games), warnings


async def _enrich_with_gamalytic_bulk(
    games: list[dict],
    gamalytic: "GamalyticClient",
    steamspy: "SteamSpyClient | None" = None,
    primary_tag: str | None = None,
) -> tuple[list[dict], list[str], dict]:
    """Enrich game dicts with Gamalytic revenue data using two-tier bulk enrichment.

    Strategy:
    1. If primary_tag is provided, try bulk fetch via gamalytic.list_games_by_tag(tag) [Tier 1]
    2. Cross-validate: check top-10 SteamSpy games (by owners) against Gamalytic results
    3. If match_rate >= BULK_MATCH_THRESHOLD (0.6): merge by AppID (bulk path)
    4. If match_rate < 0.6 OR no tag: fall back to per-game enrichment for top GAMALYTIC_SAMPLE_SIZE
    5. After Tier 1 merge, identify games with >0.1% of total genre revenue [Tier 2]
    6. For Tier 2 games: fetch full CommercialData (40+ fields) + SteamSpy tag weights,
       deep-merge over Tier 1 data

    The steamspy parameter is needed for Tier 2 SteamSpy tag weight fetches. If None,
    Tier 2 skips tag weight enrichment (but still does Gamalytic deep enrichment).

    Returns:
        (enriched_games, warnings, enrichment_meta) where enrichment_meta is a dict with:
        - method: "bulk" | "per_game_fallback" | "per_game" (no tag)
        - matched: int (games with Gamalytic revenue merged in Tier 1)
        - tier2_count: int (games that got deep Tier 2 enrichment)
        - tier2_threshold: float (revenue threshold used, e.g., 0.1)
        - total_revenue: float (sum of all Tier 1 revenue for threshold calculation)
        - total_gamalytic: int (games in Gamalytic response)
        - match_rate: float (AppID overlap ratio for top-10 check)
        - tag_used: str | None (Gamalytic tag that worked, may differ from input)
    """
    import asyncio
    from src.schemas import APIError as APIErrorSchema

    warnings: list[str] = []
    enrichment_meta = {
        "method": "per_game",
        "matched": 0,
        "tier2_count": 0,
        "tier2_threshold": GAMALYTIC_REVENUE_THRESHOLD_PCT,
        "total_revenue": 0.0,
        "total_gamalytic": 0,
        "match_rate": 0.0,
        "tag_used": None,
    }

    # --- Path 1: No tag provided (custom AppID list) -> per-game fallback ---
    if not primary_tag:
        sorted_by_owners = sorted(games, key=lambda g: g.get("owners") or 0, reverse=True)
        top_games = sorted_by_owners[:GAMALYTIC_SAMPLE_SIZE]
        remaining = sorted_by_owners[GAMALYTIC_SAMPLE_SIZE:]
        enriched, enrich_warnings = await _enrich_with_gamalytic(top_games, gamalytic)
        matched = sum(1 for g in enriched if g.get("revenue") is not None)
        enrichment_meta.update({
            "method": "per_game",
            "matched": matched,
            "total_gamalytic": len(top_games),
        })
        return enriched + remaining, enrich_warnings, enrichment_meta

    # --- Path 2: Tag provided -> try bulk endpoint ---
    tag_to_try = primary_tag
    gamalytic_games = None
    pagination_meta = None  # Track pagination health for warnings

    # Try exact tag name first
    result = await gamalytic.list_games_by_tag(tag_to_try)
    if isinstance(result, APIErrorSchema):
        logger.warning(f"Gamalytic bulk list failed for tag '{tag_to_try}': {result.message}")
    else:
        games_list, pagination_meta = result
        if len(games_list) == 0:
            # Tag returned 0 games — try normalized form
            normalized = GAMALYTIC_TAG_NORMALIZATIONS.get(tag_to_try.lower())
            if normalized and normalized.lower() != tag_to_try.lower():
                logger.info(f"Gamalytic 0 results for '{tag_to_try}', trying normalized '{normalized}'")
                result2 = await gamalytic.list_games_by_tag(normalized)
                if not isinstance(result2, APIErrorSchema):
                    games_list2, pagination_meta2 = result2
                    if len(games_list2) > 0:
                        gamalytic_games = games_list2
                        pagination_meta = pagination_meta2
                        tag_to_try = normalized
                        logger.info(f"Normalized tag '{normalized}' returned {len(gamalytic_games)} games")
            if gamalytic_games is None:
                # Also try stripping hyphens as a generic fallback
                stripped = tag_to_try.replace("-", "").replace("_", " ")
                if stripped.lower() != tag_to_try.lower():
                    logger.info(f"Gamalytic 0 results, trying stripped '{stripped}'")
                    result3 = await gamalytic.list_games_by_tag(stripped)
                    if not isinstance(result3, APIErrorSchema):
                        games_list3, pagination_meta3 = result3
                        if len(games_list3) > 0:
                            gamalytic_games = games_list3
                            pagination_meta = pagination_meta3
                            tag_to_try = stripped
        else:
            gamalytic_games = games_list

    # Surface pagination warnings if Gamalytic returned partial data
    if pagination_meta and pagination_meta.get("is_partial"):
        pg_warn = (
            f"Gamalytic pagination: partial data — fetched {pagination_meta['pages_fetched']}/"
            f"{pagination_meta.get('total_pages', '?')} pages "
            f"({len(gamalytic_games or [])} of {pagination_meta.get('total_expected', '?')} expected games). "
            f"Error: {pagination_meta.get('error', 'unknown')}"
        )
        warnings.append(pg_warn)
        logger.warning(pg_warn)

    # If bulk fetch failed entirely, fall back to per-game
    if gamalytic_games is None or len(gamalytic_games) == 0:
        logger.info(f"Gamalytic bulk unavailable for tag '{primary_tag}', falling back to per-game enrichment")
        warnings.append(f"Gamalytic bulk list returned no results for tag '{primary_tag}'. Used per-game fallback.")
        sorted_by_owners = sorted(games, key=lambda g: g.get("owners") or 0, reverse=True)
        top_games = sorted_by_owners[:GAMALYTIC_SAMPLE_SIZE]
        remaining = sorted_by_owners[GAMALYTIC_SAMPLE_SIZE:]
        enriched, enrich_warnings = await _enrich_with_gamalytic(top_games, gamalytic)
        matched = sum(1 for g in enriched if g.get("revenue") is not None)
        enrichment_meta.update({
            "method": "per_game_fallback",
            "matched": matched,
            "total_gamalytic": len(top_games),
            "tag_used": None,
        })
        return enriched + remaining, warnings + enrich_warnings, enrichment_meta

    # --- Cross-validation: check AppID overlap ---
    gamalytic_lookup = {g.get("steamId"): g for g in gamalytic_games if g.get("steamId")}
    enrichment_meta["total_gamalytic"] = len(gamalytic_lookup)
    enrichment_meta["tag_used"] = tag_to_try

    # Cross-validation: check AppID overlap across the full dataset (not top-10 sample)
    all_appids = [g.get("appid") for g in games if g.get("appid")]
    if all_appids:
        matches = sum(1 for appid in all_appids if appid in gamalytic_lookup)
        match_rate = matches / len(all_appids)
    else:
        match_rate = 0.0
    enrichment_meta["match_rate"] = round(match_rate, 2)

    if match_rate < BULK_MATCH_THRESHOLD:
        logger.warning(
            f"Gamalytic bulk cross-validation failed: {match_rate:.0%} AppID overlap "
            f"(threshold: {BULK_MATCH_THRESHOLD:.0%}). Tag naming may differ between "
            f"SteamSpy '{primary_tag}' and Gamalytic '{tag_to_try}'. Falling back to per-game."
        )
        warnings.append(
            f"Gamalytic tag mismatch: only {match_rate:.0%} of full dataset found in "
            f"Gamalytic '{tag_to_try}' results. Used per-game fallback for top {GAMALYTIC_SAMPLE_SIZE}."
        )
        sorted_by_owners = sorted(games, key=lambda g: g.get("owners") or 0, reverse=True)
        top_games = sorted_by_owners[:GAMALYTIC_SAMPLE_SIZE]
        remaining = sorted_by_owners[GAMALYTIC_SAMPLE_SIZE:]
        enriched, enrich_warnings = await _enrich_with_gamalytic(top_games, gamalytic)
        matched = sum(1 for g in enriched if g.get("revenue") is not None)
        enrichment_meta.update({
            "method": "per_game_fallback",
            "matched": matched,
        })
        return enriched + remaining, warnings + enrich_warnings, enrichment_meta

    # --- TIER 1: Bulk merge (cross-validation passed) ---
    logger.info(
        f"Gamalytic bulk cross-validation passed: {match_rate:.0%} overlap. "
        f"Merging {len(gamalytic_lookup)} Gamalytic records into {len(games)} SteamSpy games."
    )

    enriched_games = []
    matched_count = 0
    for game in games:
        appid = game.get("appid")
        gam_data = gamalytic_lookup.get(appid) if appid else None
        if gam_data:
            enriched = dict(game)
            # Merge revenue (Gamalytic has single revenue int from bulk endpoint)
            raw_revenue = gam_data.get("revenue")
            if raw_revenue is not None and raw_revenue > 0:
                enriched["revenue"] = raw_revenue  # Direct revenue value from bulk endpoint
                matched_count += 1
            # Merge copiesSold
            copies = gam_data.get("copiesSold")
            if copies is not None:
                enriched["copies_sold"] = int(copies)
            # Merge reviewScore (only if SteamSpy doesn't have it)
            gam_score = gam_data.get("reviewScore")
            if gam_score is not None and not enriched.get("review_score"):
                enriched["review_score"] = float(gam_score)
            # Merge price from Gamalytic if missing
            gam_price = gam_data.get("price")
            if gam_price is not None and not enriched.get("price"):
                enriched["price"] = float(gam_price)
            # Merge releaseDate if missing (convert ms epoch to ISO YYYY-MM-DD)
            gam_release = gam_data.get("releaseDate")
            if gam_release is not None and not enriched.get("release_date"):
                enriched["release_date"] = _ms_to_iso(gam_release)
            enriched_games.append(enriched)
        else:
            enriched_games.append(game)

    enrichment_meta["matched"] = matched_count

    # Quota tracking (bulk uses 1 quota unit per page, not per game)
    pages_used = (len(gamalytic_games) + 199) // 200
    quota_warning = increment_gamalytic_counter(pages_used)
    if quota_warning:
        warnings.append(quota_warning)

    # --- TIER 1.5: Per-game enrichment for unenriched games ---
    tier1_5_appids = [
        g["appid"] for g in enriched_games
        if g.get("appid") and g.get("revenue") is None
    ]

    if tier1_5_appids:
        if len(tier1_5_appids) >= 1000:
            eta_seconds = len(tier1_5_appids)
            logger.warning(
                f"Tier 1.5: {len(tier1_5_appids)} unenriched games — "
                f"SteamSpy bottleneck ETA ~{eta_seconds}s ({eta_seconds // 60}m{eta_seconds % 60}s)"
            )
        else:
            logger.info(f"Tier 1.5: {len(tier1_5_appids)} unenriched games, fetching per-game data")

        tier1_5_commercial_results = await gamalytic.get_commercial_batch(
            tier1_5_appids, detail_level="summary"
        )

        tier1_5_tag_results = []
        tier1_5_steamspy_timed_out = 0
        tier1_5_steamspy_completed = 0
        if steamspy is not None:
            chunk_size = 50
            for chunk_start in range(0, len(tier1_5_appids), chunk_size):
                chunk = tier1_5_appids[chunk_start:chunk_start + chunk_size]
                chunk_tasks = [steamspy.get_engagement_data(appid) for appid in chunk]
                chunk_gr = await gather_with_timeout(
                    chunk_tasks,
                    timeout=compute_gather_timeout(len(chunk), STEAMSPY_RATE),
                    label=f"tier1_5_steamspy_chunk_{chunk_start}",
                )
                tier1_5_tag_results.extend(chunk_gr.results)
                tier1_5_steamspy_completed += chunk_gr.completed
                tier1_5_steamspy_timed_out += chunk_gr.timed_out
                if chunk_gr.hit_timeout:
                    warnings.append(
                        f"Tier 1.5 SteamSpy chunk {chunk_start}: {chunk_gr.timed_out}/{len(chunk)} "
                        f"requests timed out after {chunk_gr.timeout_seconds:.0f}s"
                    )
                if chunk_start + chunk_size < len(tier1_5_appids):
                    logger.info(
                        f"Tier 1.5 SteamSpy progress: {chunk_start + len(chunk)}/{len(tier1_5_appids)} games fetched"
                    )
        else:
            tier1_5_tag_results = [None] * len(tier1_5_appids)

        tier1_5_commercial = {}
        tier1_5_tags = {}
        for appid, comm_result in zip(tier1_5_appids, tier1_5_commercial_results):
            if not isinstance(comm_result, APIErrorSchema) and not isinstance(comm_result, Exception):
                tier1_5_commercial[appid] = comm_result
        for appid, tag_result in zip(tier1_5_appids, tier1_5_tag_results):
            if tag_result is not None and not isinstance(tag_result, Exception) and not isinstance(tag_result, APIErrorSchema):
                tier1_5_tags[appid] = tag_result

        tier1_5_count = 0
        for i, game in enumerate(enriched_games):
            appid = game.get("appid")
            if appid not in tier1_5_commercial and appid not in tier1_5_tags:
                continue
            enriched = dict(game)
            tier1_5_count += 1
            comm = tier1_5_commercial.get(appid)
            if comm:
                rev = _commercial_data_to_revenue(comm)
                if rev is not None:
                    enriched["revenue"] = rev
                if getattr(comm, "copies_sold", None) is not None:
                    enriched["copies_sold"] = comm.copies_sold
                if getattr(comm, "review_score", None) is not None:
                    enriched["review_score"] = comm.review_score
                if getattr(comm, "followers", None) is not None:
                    enriched["followers"] = comm.followers
                if getattr(comm, "release_date", None) is not None:
                    enriched["release_date"] = comm.release_date
            engagement = tier1_5_tags.get(appid)
            if engagement and hasattr(engagement, "tags") and engagement.tags:
                enriched["tags"] = dict(engagement.tags)
            enriched_games[i] = enriched

        enrichment_meta["tier1_5_count"] = len(tier1_5_appids)
        enrichment_meta["tier1_5_commercial_found"] = len(tier1_5_commercial)
        enrichment_meta["tier1_5_tags_found"] = len(tier1_5_tags)
        enrichment_meta["tier1_5_total"] = tier1_5_count
        if tier1_5_steamspy_timed_out > 0:
            enrichment_meta["tier1_5_steamspy_timed_out"] = tier1_5_steamspy_timed_out
            enrichment_meta["tier1_5_steamspy_completed"] = tier1_5_steamspy_completed
        tier1_5_quota = increment_gamalytic_counter(len(tier1_5_appids))
        if tier1_5_quota:
            warnings.append(tier1_5_quota)
        logger.info(
            f"Tier 1.5 complete: {tier1_5_count} games enriched "
            f"({len(tier1_5_commercial)} Gamalytic, {len(tier1_5_tags)} SteamSpy tags)"
        )

    # --- TIER 2: Deep per-game enrichment for revenue-significant games ---
    # Calculate total revenue from Tier 1 merged data
    total_revenue = sum(g.get("revenue") or 0 for g in enriched_games)
    enrichment_meta["total_revenue"] = total_revenue

    if total_revenue > 0:
        revenue_threshold = total_revenue * (GAMALYTIC_REVENUE_THRESHOLD_PCT / 100.0)
        tier2_appids = [
            g["appid"] for g in enriched_games
            if g.get("appid") and (g.get("revenue") or 0) > revenue_threshold
        ]

        if tier2_appids:
            logger.info(
                f"Tier 2: {len(tier2_appids)} games exceed {GAMALYTIC_REVENUE_THRESHOLD_PCT}% "
                f"of total revenue ({total_revenue:,.0f}). "
                f"Threshold: {revenue_threshold:,.0f}. Fetching full CommercialData + SteamSpy tags."
            )

            # Fetch full Gamalytic CommercialData for Tier 2 games
            commercial_results = await gamalytic.get_commercial_batch(tier2_appids, detail_level="full")

            # Fetch SteamSpy tag weights for Tier 2 games (provides tags dict for genre validation)
            tag_results = []
            if steamspy is not None:
                tag_tasks = [steamspy.get_engagement_data(appid) for appid in tier2_appids]
                tier2_gr = await gather_with_timeout(
                    tag_tasks,
                    timeout=compute_gather_timeout(len(tier2_appids), STEAMSPY_RATE),
                    label=f"tier2_steamspy({len(tier2_appids)})",
                )
                tag_results = tier2_gr.results
                if tier2_gr.hit_timeout:
                    warnings.append(
                        f"Tier 2 SteamSpy: {tier2_gr.timed_out}/{len(tier2_appids)} "
                        f"requests timed out after {tier2_gr.timeout_seconds:.0f}s"
                    )
            else:
                tag_results = [None] * len(tier2_appids)

            # Build lookup: appid -> (CommercialData, EngagementData)
            tier2_commercial = {}
            tier2_tags = {}
            for appid, comm_result in zip(tier2_appids, commercial_results):
                if not isinstance(comm_result, APIErrorSchema) and not isinstance(comm_result, Exception):
                    tier2_commercial[appid] = comm_result
            for appid, tag_result in zip(tier2_appids, tag_results):
                if tag_result is not None and not isinstance(tag_result, Exception) and not isinstance(tag_result, APIErrorSchema):
                    tier2_tags[appid] = tag_result

            # Deep-merge Tier 2 data over Tier 1 for each qualifying game
            tier2_count = 0
            for i, game in enumerate(enriched_games):
                appid = game.get("appid")
                if appid not in tier2_commercial and appid not in tier2_tags:
                    continue

                enriched = dict(game)
                tier2_count += 1

                # Deep-merge CommercialData fields (overwrites Tier 1 bulk fields with richer per-game data)
                comm = tier2_commercial.get(appid)
                if comm:
                    # Revenue: use per-game midpoint (more precise than bulk single int)
                    rev = _commercial_data_to_revenue(comm)
                    if rev is not None:
                        enriched["revenue"] = rev
                    # Extended fields only available from per-game endpoint
                    if getattr(comm, "copies_sold", None) is not None:
                        enriched["copies_sold"] = comm.copies_sold
                    if getattr(comm, "followers", None) is not None:
                        enriched["followers"] = comm.followers
                    if getattr(comm, "review_score", None) is not None:
                        enriched["review_score"] = comm.review_score
                    if getattr(comm, "gamalytic_owners", None) is not None:
                        enriched["gamalytic_owners"] = comm.gamalytic_owners
                    if getattr(comm, "gamalytic_players", None) is not None:
                        enriched["gamalytic_players"] = comm.gamalytic_players
                    if getattr(comm, "gamalytic_reviews", None) is not None:
                        enriched["gamalytic_reviews"] = comm.gamalytic_reviews
                    if getattr(comm, "accuracy", None) is not None:
                        enriched["gamalytic_accuracy"] = comm.accuracy
                    if getattr(comm, "gamalytic_is_early_access", None) is not None:
                        enriched["is_early_access"] = comm.gamalytic_is_early_access
                    # Heavy arrays: history, country_data, audience_overlap, also_played, estimate_details, dlc
                    if getattr(comm, "history", None):
                        enriched["gamalytic_history"] = [
                            {"timestamp": e.timestamp, "revenue": e.revenue, "reviews": e.reviews,
                             "price": e.price, "score": e.score, "followers": e.followers}
                            for e in comm.history if e.timestamp
                        ]
                    if getattr(comm, "audience_overlap", None):
                        enriched["audience_overlap"] = [
                            {"appid": e.appid, "name": e.name, "overlap_pct": e.overlap_pct}
                            for e in comm.audience_overlap
                        ]
                    if getattr(comm, "also_played", None):
                        enriched["also_played"] = [
                            {"appid": e.appid, "name": e.name, "overlap_pct": e.overlap_pct}
                            for e in comm.also_played
                        ]
                    if getattr(comm, "country_data", None):
                        enriched["country_data"] = [
                            {"country": e.country_code, "share_pct": e.share_pct}
                            for e in comm.country_data
                        ]

                # Deep-merge SteamSpy tag weights (critical for genre membership validation in plan 09-11)
                engagement = tier2_tags.get(appid)
                if engagement and hasattr(engagement, "tags") and engagement.tags:
                    enriched["tags"] = dict(engagement.tags)  # {"TagName": weight_int, ...}

                enriched_games[i] = enriched

            enrichment_meta["tier2_count"] = tier2_count

            # Tier 2 quota tracking
            tier2_quota = increment_gamalytic_counter(len(tier2_appids))
            if tier2_quota:
                warnings.append(tier2_quota)

            logger.info(
                f"Tier 2 complete: {tier2_count} games deep-enriched "
                f"({len(tier2_commercial)} Gamalytic, {len(tier2_tags)} SteamSpy tags)"
            )

    enrichment_meta["method"] = "bulk"
    if pagination_meta:
        enrichment_meta["gamalytic_pagination"] = pagination_meta
    return enriched_games, warnings, enrichment_meta


def _validate_genre_membership(
    games: list[dict],
    genre_tag: str,
    revenue_threshold_pct: float = GAMALYTIC_REVENUE_THRESHOLD_PCT,
) -> list[dict]:
    """Validate genre membership for revenue-significant games using SteamSpy tag weights.

    For each game that has SteamSpy tag weights (populated by Tier 2 enrichment in
    _enrich_with_gamalytic_bulk), checks if the searched genre tag appears in the
    game's top-20 tags. Games without tag data (tags={}) are skipped (not flagged).

    This is a pure in-memory check — ZERO API calls. Tag weights come from
    SteamSpy get_engagement_data fetched during Tier 2 enrichment.

    Args:
        games: List of enriched game dicts (with "tags" and "revenue" fields)
        genre_tag: The genre tag to validate membership for (should be normalized
            via normalize_tag() to match SteamSpy's format, e.g., "Rogue-like")
        revenue_threshold_pct: Revenue threshold percentage (default: 0.1%)

    Returns:
        List of validation flag dicts, one per validated game:
        [
            {
                "appid": int,
                "name": str,
                "revenue": float,
                "revenue_pct": float,     # game's % of total genre revenue
                "genre_valid": bool,       # True if genre tag found in top-20 tags
                "tag_rank": int | None,    # position of genre tag in tag weights (1-based), None if not found
                "top_tags": list[str],     # top 5 tags by weight for context
            },
            ...
        ]
    """
    total_revenue = sum(g.get("revenue") or 0 for g in games)
    if total_revenue <= 0:
        return []

    threshold = total_revenue * (revenue_threshold_pct / 100.0)
    genre_lower = genre_tag.lower()

    validation_flags = []
    for game in games:
        revenue = game.get("revenue") or 0
        if revenue <= threshold:
            continue

        tags = game.get("tags")
        if not tags or not isinstance(tags, dict):
            # No tag data available (not Tier 2 enriched) — skip, don't flag
            continue

        # Sort tags by weight descending (SteamSpy returns {"TagName": weight_int})
        sorted_tags = sorted(tags.items(), key=lambda t: t[1], reverse=True)
        top_20_tag_names = [name for name, _ in sorted_tags[:20]]
        top_20_lower = [name.lower() for name in top_20_tag_names]

        # Check if genre tag appears in top-20 tags
        genre_valid = genre_lower in top_20_lower
        tag_rank = None
        if genre_valid:
            tag_rank = top_20_lower.index(genre_lower) + 1  # 1-based rank

        top_5 = [name for name, _ in sorted_tags[:5]]

        validation_flags.append({
            "appid": game.get("appid"),
            "name": game.get("name", "Unknown"),
            "revenue": revenue,
            "revenue_pct": round(revenue / total_revenue * 100, 2),
            "genre_valid": genre_valid,
            "tag_rank": tag_rank,
            "top_tags": top_5,
        })

    return validation_flags


# ---------------------------------------------------------------------------
# Single-Phase Orchestration
# ---------------------------------------------------------------------------

async def analyze_market_single(
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
    skip_min_check: bool = False,
    return_games: bool = False,
) -> dict | tuple[dict, list]:
    """Single-phase market analysis: fetch games, enrich with two-tier Gamalytic, compute analytics.

    Enrichment strategy (two-tier):
    - Tier 1: Bulk fetch via Gamalytic /steam-games/list?tags=X for ALL games.
      Provides: revenue, copiesSold, reviewScore, price, releaseDate.
      Cross-validated by AppID overlap (>=60% match required).
    - Tier 2: For games with >0.1% of total genre revenue, fetch full CommercialData
      from /game/{appid} (40+ fields including followers, history, audience_overlap)
      AND SteamSpy tag weights from get_engagement_data (for genre membership validation).
    - Fallback: Per-game enrichment for top 200 when tag names don't align or no tag provided.

    Args:
        skip_min_check: If True, bypass MIN_GAMES_THRESHOLD in _run_market_analysis.
            Used by evaluate_game with custom genre_appids baselines.
        return_games: If True, return (result_dict, enriched_games_list) tuple instead of
            just result_dict. Allows callers to reuse enriched games without re-fetching.
            Default False preserves backward compatibility.

    Overall timeout: OVERALL_TIMEOUT (3600s / 1 hour). If exceeded, returns an error
    with elapsed time so the caller knows the operation timed out.
    """
    overall_start = time.monotonic()
    try:
        return await asyncio.wait_for(
            _analyze_market_single_impl(
                steamspy, steam_store, gamalytic, tags, appids=appids,
                metrics=metrics, exclude=exclude, top_n=top_n,
                include_raw=include_raw, market_label=market_label,
                skip_min_check=skip_min_check, return_games=return_games,
            ),
            timeout=OVERALL_TIMEOUT,
        )
    except asyncio.TimeoutError:
        elapsed = round(time.monotonic() - overall_start)
        label = market_label or ", ".join(tags) or f"Custom ({len(appids or [])} games)"
        logger.error(f"analyze_market_single OVERALL TIMEOUT ({OVERALL_TIMEOUT}s) for '{label}' after {elapsed}s")
        error_result = {
            "error": f"Analysis timed out after {elapsed}s (limit: {OVERALL_TIMEOUT}s). "
                     "This may indicate API connectivity issues with SteamSpy or Gamalytic.",
            "total_games": 0,
            "market_label": label,
            "warnings": [f"Overall timeout: {elapsed}s elapsed, {OVERALL_TIMEOUT}s limit"],
        }
        if return_games:
            return error_result, []
        return error_result


async def _analyze_market_single_impl(
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
    skip_min_check: bool = False,
    return_games: bool = False,
) -> dict | tuple[dict, list]:
    """Inner implementation of analyze_market_single, wrapped with overall timeout."""
    from src.schemas import APIError as APIErrorSchema

    fetch_start = time.monotonic()
    games, data_source = await _fetch_game_list_steamspy(steamspy, steam_store, gamalytic, tags, appids=appids)
    fetch_ms = round((time.monotonic() - fetch_start) * 1000)

    if not games:
        error_msg = "No games found for specified tags."
        if data_source.startswith("steam_store:tag_error:"):
            bad_tags = data_source.split(":", 2)[2]
            error_msg = f"Tag(s) not recognized: {bad_tags}. Check tag names against Steam's tag list (e.g., 'Roguelike Deckbuilder' not 'Deckbuilder')."
        error_result = {
            "error": error_msg,
            "total_games": 0,
            "market_label": market_label or ", ".join(tags) or f"Custom ({len(appids or [])} games)",
        }
        if return_games:
            return error_result, []
        return error_result

    # Fire lightweight Released_DESC count in parallel with enrichment (tag-based only).
    # Our analysis uses Reviews_DESC which only includes review-ranked games (~50-65% of
    # released titles). This parallel call gets the true Steam Store total for reporting.
    total_released_count: int | None = None
    _released_count_task = None
    if tags and not appids:
        async def _get_released_count():
            tag_map = await steam_store.get_tag_map()
            if isinstance(tag_map, APIErrorSchema):
                return None
            tag_ids = [tag_map[t.lower()] for t in tags if t.lower() in tag_map]
            if not tag_ids:
                return None
            return await steam_store.get_total_released_count(tag_ids)
        _released_count_task = asyncio.create_task(_get_released_count())

    # Two-tier Gamalytic enrichment: bulk for tag-based, per-game for appid-based
    enrich_start = time.monotonic()
    # Use first tag as primary for bulk lookup (multi-tag uses Steam Store path, not SteamSpy tag)
    primary_tag = tags[0] if tags and len(tags) == 1 else None
    enriched_games, enrich_warnings, enrichment_meta = await _enrich_with_gamalytic_bulk(
        games, gamalytic, steamspy=steamspy, primary_tag=primary_tag
    )
    enrich_ms = round((time.monotonic() - enrich_start) * 1000)

    # Update data_source to reflect enrichment
    matched = enrichment_meta.get("matched", 0)
    method = enrichment_meta.get("method", "none")
    tier2 = enrichment_meta.get("tier2_count", 0)
    if matched > 0:
        data_source = f"{data_source}+gamalytic_{method}({matched}/{len(enriched_games)})"
        if tier2 > 0:
            data_source += f"+tier2({tier2})"

    # Independent tag weight fetch for genre validation
    # Fetch tags for top-N revenue/owner-significant games that lack tag data
    if tags and len(tags) == 1 and steamspy is not None:
        games_needing_tags = [
            g for g in enriched_games
            if g.get("appid") and (not g.get("tags") or not isinstance(g.get("tags"), dict) or not g["tags"])
        ]
        games_needing_tags.sort(key=lambda g: (g.get("revenue") or 0, g.get("owners") or 0), reverse=True)
        TAG_FETCH_LIMIT = 50
        tag_fetch_targets = games_needing_tags[:TAG_FETCH_LIMIT]
        if tag_fetch_targets:
            tag_tasks = [steamspy.get_engagement_data(g["appid"]) for g in tag_fetch_targets]
            indep_gr = await gather_with_timeout(
                tag_tasks,
                timeout=compute_gather_timeout(len(tag_fetch_targets), STEAMSPY_RATE),
                label=f"independent_tags({len(tag_fetch_targets)})",
            )
            tag_results = indep_gr.results
            if indep_gr.hit_timeout:
                enrich_warnings.append(
                    f"Independent tag fetch: {indep_gr.timed_out}/{len(tag_fetch_targets)} "
                    f"requests timed out after {indep_gr.timeout_seconds:.0f}s"
                )
            tag_lookup = {}
            for g, result in zip(tag_fetch_targets, tag_results):
                if (result is not None
                    and not isinstance(result, Exception)
                    and not isinstance(result, APIError)
                    and hasattr(result, "tags") and result.tags):
                    tag_lookup[g["appid"]] = dict(result.tags)
            if tag_lookup:
                for i, game in enumerate(enriched_games):
                    appid = game.get("appid")
                    if appid in tag_lookup:
                        updated = dict(game)
                        updated["tags"] = tag_lookup[appid]
                        enriched_games[i] = updated
                logger.info(f"Independent tag fetch: populated tags for {len(tag_lookup)}/{len(tag_fetch_targets)} games")

    # Genre membership validation for revenue-significant games (in-memory, zero API calls)
    # Uses SteamSpy tag weights populated by Tier 2 enrichment or independent tag fetch
    validation_flags = []
    if tags and len(tags) == 1:
        validation_flags = _validate_genre_membership(enriched_games, normalize_tag(tags[0]))
        if validation_flags:
            flagged = [f for f in validation_flags if not f["genre_valid"]]
            valid = [f for f in validation_flags if f["genre_valid"]]
            logger.info(
                f"Genre validation: {len(valid)} pass, {len(flagged)} flagged "
                f"out of {len(validation_flags)} checked for tag '{tags[0]}'"
            )
            if flagged:
                flagged_names = ", ".join(f["name"] for f in flagged[:5])
                logger.warning(
                    f"Genre membership flagged: {len(flagged)} revenue-significant games "
                    f"don't have '{tags[0]}' in top-20 SteamSpy tags: {flagged_names}"
                )

    # Await the parallel released count task if it was started
    if _released_count_task is not None:
        try:
            total_released_count = await _released_count_task
        except Exception:
            total_released_count = None

    # Run analysis on all games
    result = _run_market_analysis(
        games=enriched_games,
        tags=tags,
        metrics=metrics,
        exclude=exclude,
        top_n=top_n,
        include_raw=include_raw,
        market_label=market_label,
        data_source=data_source,
        skip_min_check=skip_min_check,
    )

    if "error" in result:
        if return_games:
            return result, enriched_games
        return result

    # Patch released count into methodology
    if total_released_count is not None and result.get("methodology"):
        result["methodology"]["total_released_on_steam"] = total_released_count
        result["methodology"]["notes"].append(
            f"Analysis covers {len(enriched_games)} review-ranked games out of "
            f"{total_released_count} total released titles on Steam Store for this tag. "
            f"Games without sufficient reviews are excluded from analysis to ensure "
            f"data quality (revenue estimates, engagement metrics)."
        )

    # Add validation flags to result (games flagged but NOT removed)
    if validation_flags:
        result["validation_flags"] = validation_flags

    # Add timing info
    if result.get("compute_time"):
        result["compute_time"]["data_fetch_ms"] = fetch_ms
        result["compute_time"]["gamalytic_enrich_ms"] = enrich_ms

    # Add enrichment metadata
    enrichment_dict = {
        "method": method,
        "gamalytic_matched": matched,
        "gamalytic_total": enrichment_meta.get("total_gamalytic", 0),
        "match_rate": enrichment_meta.get("match_rate", 0.0),
        "tag_used": enrichment_meta.get("tag_used"),
        "total_games": len(enriched_games),
        "tier2_count": tier2,
        "tier2_threshold": enrichment_meta.get("tier2_threshold", GAMALYTIC_REVENUE_THRESHOLD_PCT),
        "total_revenue": enrichment_meta.get("total_revenue", 0.0),
    }
    # Add timeout stats if any timeouts occurred
    timeout_stats = {}
    if enrichment_meta.get("tier1_5_steamspy_timed_out"):
        timeout_stats["tier1_5_steamspy"] = {
            "completed": enrichment_meta["tier1_5_steamspy_completed"],
            "timed_out": enrichment_meta["tier1_5_steamspy_timed_out"],
        }
    if enrichment_meta.get("gamalytic_pagination", {}).get("is_partial"):
        pg = enrichment_meta["gamalytic_pagination"]
        timeout_stats["gamalytic_pagination"] = {
            "pages_fetched": pg["pages_fetched"],
            "total_pages": pg.get("total_pages"),
            "is_partial": True,
        }
    if timeout_stats:
        enrichment_dict["timeout_stats"] = timeout_stats
    result["enrichment"] = enrichment_dict

    # Surface enrichment warnings
    if enrich_warnings:
        existing_warnings = result.get("warnings", [])
        result["warnings"] = existing_warnings + enrich_warnings

    if return_games:
        return result, enriched_games
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
    Potential score: Market-level opportunity (growth, accessibility, competition).
    Opportunity score: Composite heavily weighted toward position.

    Calibration targets:
    - Top performer in growing market: 85-95 (exceptional)
    - Top performer in stagnant market: 75-85 (strong)
    - Mid-pack in growing market: 55-65 (moderate)
    - Low performer in declining market: 20-35 (weak)

    Returns (position_score, potential_score, opportunity_score).
    """
    # Position score: average of available percentiles (unchanged)
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
        position_score = 50.0

    # Potential score: market-level signals (NOT inverse of position)
    # This measures "how good is this MARKET for games" not "room for THIS game to grow"
    if genre_health:
        # Growth (0-100): is the market growing?
        growth_signal = genre_health.growth / 100
        # Accessibility (0-100): can new players easily enter?
        accessibility_signal = genre_health.accessibility / 100 if genre_health.accessibility else 0.5
        # Competition (0-100): HIGH = distributed (good for entrants)
        competition_signal = genre_health.competition / 100 if genre_health.competition else 0.5
        potential_raw = (growth_signal * 0.5 + accessibility_signal * 0.25 + competition_signal * 0.25) * 100
    else:
        potential_raw = 50.0
    potential_score = round(max(0.0, min(100.0, potential_raw)), 1)

    # Opportunity score: position-dominant composite
    # Position 0.65 + Potential 0.25 + strength_bonus 0.10
    # strength_bonus rewards games with many strengths (max 1.0 at 4+ strengths)
    strength_bonus = min(len(strengths) / 4, 1.0) * 100
    opportunity_raw = position_score * 0.65 + potential_score * 0.25 + strength_bonus * 0.10
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


# ---------------------------------------------------------------------------
# Compare Markets — Helper Functions
# ---------------------------------------------------------------------------

# Metric polarity: True = higher is better, False = lower is better
_METRIC_HIGHER_IS_BETTER: dict[str, bool] = {
    "total_games": True,
    "avg_revenue": True,
    "median_revenue": True,
    "gini": False,
    "success_rate_100k": True,
    "top_10_share": False,
    "growth_score": True,
    "competition_score": True,
    "accessibility_score": True,
}


def _extract_comparison_metrics(label: str, analysis: dict) -> dict[str, float | None]:
    """Extract key numeric metrics from a market analysis dict for delta comparison.

    Returns a flat dict of metric_name -> value.
    """
    values: dict[str, float | None] = {}

    # Basic counts
    values["total_games"] = analysis.get("total_games")

    # Revenue from descriptive_stats
    ds = analysis.get("descriptive_stats") or {}
    rev_stats = ds.get("revenue") or {}
    values["avg_revenue"] = rev_stats.get("mean")
    values["median_revenue"] = rev_stats.get("median")

    # Concentration
    concentration = analysis.get("concentration") or {}
    values["gini"] = concentration.get("gini")
    top_shares = concentration.get("top_shares") or {}
    top_10 = top_shares.get("top_10") or {}
    values["top_10_share"] = top_10.get("share_pct")

    # Success rates
    success_rates = analysis.get("success_rates") or {}
    thresholds = success_rates.get("thresholds") or {}
    values["success_rate_100k"] = thresholds.get("100k")

    # Health scores
    health = analysis.get("health_scores") or {}
    values["growth_score"] = health.get("growth")
    values["competition_score"] = health.get("competition")
    values["accessibility_score"] = health.get("accessibility")

    return values


def _compute_deltas(market_analyses: list[tuple[str, dict]]) -> list[MetricDelta]:
    """Compare key numeric metrics across markets.

    Args:
        market_analyses: list of (label, analysis_dict) tuples

    Returns:
        list of MetricDelta objects, one per metric.
    """
    if len(market_analyses) < 2:
        return []

    # Extract per-market values
    per_market: dict[str, dict[str, float | None]] = {}
    for label, analysis in market_analyses:
        per_market[label] = _extract_comparison_metrics(label, analysis)

    deltas: list[MetricDelta] = []

    for metric, higher_is_better in _METRIC_HIGHER_IS_BETTER.items():
        values_dict: dict[str, float | None] = {
            label: per_market[label].get(metric)
            for label, _ in market_analyses
        }

        # Find winner: label with best value (non-None)
        non_null = [(label, v) for label, v in values_dict.items() if v is not None]
        if len(non_null) < 2:
            # Not enough data to compare — still include metric, no winner/delta
            deltas.append(MetricDelta(
                metric=metric,
                values=values_dict,
                winner="",
                delta_pct=None,
            ))
            continue

        if higher_is_better:
            winner_label, winner_val = max(non_null, key=lambda x: x[1])
            loser_label, loser_val = min(non_null, key=lambda x: x[1])
        else:
            # Lower is better (gini, top_10_share)
            winner_label, winner_val = min(non_null, key=lambda x: x[1])
            loser_label, loser_val = max(non_null, key=lambda x: x[1])

        # Delta pct: how much better is winner vs. worst
        delta_pct: float | None = None
        if loser_val is not None and loser_val != 0:
            delta_pct = round(abs((winner_val - loser_val) / loser_val) * 100, 1)
        elif loser_val == 0 and winner_val != 0:
            delta_pct = None  # division by zero case

        deltas.append(MetricDelta(
            metric=metric,
            values=values_dict,
            winner=winner_label,
            delta_pct=delta_pct,
        ))

    return deltas


def _compute_overlap(
    market_game_sets: list[tuple[str, list[dict]]],
) -> tuple[list[OverlapInfo], OverlapInfo | None]:
    """Compute pairwise and global game overlap between markets.

    Args:
        market_game_sets: list of (label, game_dicts) tuples

    Returns:
        (pairwise_overlaps, global_overlap)
        - pairwise_overlaps: one OverlapInfo per unique pair
        - global_overlap: games appearing in ALL markets (None if < 3 markets)
    """
    # Build appid sets per market
    market_appids: list[tuple[str, set[int], list[dict]]] = []
    for label, games in market_game_sets:
        appid_set = {g.get("appid") for g in games if g.get("appid") is not None}
        market_appids.append((label, appid_set, games))

    pairwise: list[OverlapInfo] = []

    # Pairwise comparisons
    for i in range(len(market_appids)):
        label_a, ids_a, games_a = market_appids[i]
        for j in range(i + 1, len(market_appids)):
            label_b, ids_b, games_b = market_appids[j]

            shared_ids = ids_a & ids_b
            if not shared_ids:
                pairwise.append(OverlapInfo(
                    pair=f"{label_a} vs {label_b}",
                    overlap_count=0,
                    overlap_games=[],
                    pct_of_smaller_market=0.0,
                ))
                continue

            smaller_market_size = min(len(ids_a), len(ids_b))
            pct = round(len(shared_ids) / smaller_market_size * 100, 1) if smaller_market_size > 0 else 0.0

            # Build GameProfile list for shared games (use market_a data as source)
            shared_game_dicts = [g for g in games_a if g.get("appid") in shared_ids]
            overlap_profiles = [_build_game_profile(g) for g in shared_game_dicts[:10]]

            pairwise.append(OverlapInfo(
                pair=f"{label_a} vs {label_b}",
                overlap_count=len(shared_ids),
                overlap_games=overlap_profiles,
                pct_of_smaller_market=pct,
            ))

    # Global overlap (games in ALL markets) — only meaningful for 3+ markets
    global_overlap: OverlapInfo | None = None
    if len(market_appids) >= 3:
        all_ids_sets = [ids for _, ids, _ in market_appids]
        global_ids = all_ids_sets[0].copy()
        for s in all_ids_sets[1:]:
            global_ids &= s

        smallest_market_size = min(len(ids) for _, ids, _ in market_appids)
        global_pct = round(len(global_ids) / smallest_market_size * 100, 1) if smallest_market_size > 0 else 0.0

        # Build profiles for global overlap games
        first_label, first_ids, first_games = market_appids[0]
        global_game_dicts = [g for g in first_games if g.get("appid") in global_ids]
        global_profiles = [_build_game_profile(g) for g in global_game_dicts[:10]]

        global_overlap = OverlapInfo(
            pair=" vs ".join(label for label, _, _ in market_appids),
            overlap_count=len(global_ids),
            overlap_games=global_profiles,
            pct_of_smaller_market=global_pct,
        )

    return pairwise, global_overlap


def _compute_confidence_notes(market_analyses: list[tuple[str, dict]]) -> list[str]:
    """Warn about potentially misleading comparisons.

    Warns when:
    - Size ratio > 5:1 between any two markets
    - Any market has < 50 games
    """
    notes: list[str] = []

    sizes = [(label, analysis.get("total_games", 0)) for label, analysis in market_analyses]

    # Small market warning
    for label, size in sizes:
        if size < 50:
            notes.append(
                f"Market '{label}' has only {size} games — analysis may not be statistically reliable."
            )

    # Size ratio warning (pairwise)
    for i in range(len(sizes)):
        label_a, size_a = sizes[i]
        for j in range(i + 1, len(sizes)):
            label_b, size_b = sizes[j]
            if size_a == 0 or size_b == 0:
                continue
            ratio = max(size_a, size_b) / min(size_a, size_b)
            if ratio > 5.0:
                larger = label_a if size_a > size_b else label_b
                smaller = label_b if size_a > size_b else label_a
                notes.append(
                    f"Market size ratio between '{larger}' ({max(size_a, size_b)} games) "
                    f"and '{smaller}' ({min(size_a, size_b)} games) is {ratio:.1f}x — "
                    "comparisons may be skewed by sample size differences."
                )

    return notes


def _rank_markets_by_health(market_analyses: list[tuple[str, dict]]) -> list[dict]:
    """Rank markets by average health score (growth + competition + accessibility) / 3.

    Returns list of dicts with label, avg_health, growth, competition, accessibility, rank.
    Sorted by avg_health descending.
    """
    ranked: list[dict] = []
    for label, analysis in market_analyses:
        health = analysis.get("health_scores") or {}
        growth = health.get("growth", 50)
        competition = health.get("competition", 50)
        accessibility = health.get("accessibility", 50)
        avg_health = round((growth + competition + accessibility) / 3, 1)
        ranked.append({
            "label": label,
            "avg_health": avg_health,
            "growth": growth,
            "competition": competition,
            "accessibility": accessibility,
        })

    ranked.sort(key=lambda x: x["avg_health"], reverse=True)

    # Add rank field (1-based)
    for i, entry in enumerate(ranked):
        entry["rank"] = i + 1

    return ranked


# ---------------------------------------------------------------------------
# Compare Markets — Main Orchestration
# ---------------------------------------------------------------------------

async def compare_markets_analysis(
    market_inputs: list[dict],
    steamspy,
    steam_store,
    gamalytic,
) -> dict:
    """Compare 2-4 markets side-by-side with deltas, overlap detection, and ranking.

    Each market_input dict must have:
      - tags: list[str] — required, market tag query
      - label: str — optional, human label for the market
      - metrics: list[str] | None — optional metric filter (default: all)
      - exclude: list[str] | None — optional metrics to exclude
      - top_n: int — optional, number of top games (default: 10)

    Returns a flat dict matching CompareMarketsResult schema.
    """
    total_start = time.monotonic()
    warnings: list[str] = []

    # Validate market count
    if not (2 <= len(market_inputs) <= 4):
        return {
            "error": f"compare_markets requires 2-4 markets, got {len(market_inputs)}.",
            "market_count": len(market_inputs),
        }

    # Fetch game lists and run analysis for each market
    market_analyses: list[tuple[str, dict]] = []  # (label, analysis_dict)
    market_game_sets: list[tuple[str, list[dict]]] = []  # (label, raw_games)

    for idx, market_input in enumerate(market_inputs):
        tags = market_input.get("tags") or []
        appids = market_input.get("appids") or []
        if not tags and not appids:
            warnings.append(f"Market {idx + 1} has no tags or appids — skipping.")
            continue

        label = market_input.get("label") or ", ".join(tags) or f"Custom ({len(appids)} games)"
        metrics = market_input.get("metrics")
        exclude = market_input.get("exclude")
        top_n = market_input.get("top_n", 10)

        # Fetch game list
        fetch_start = time.monotonic()
        games, data_source = await _fetch_game_list_steamspy(steamspy, steam_store, gamalytic, tags, appids=appids if not tags else None)
        fetch_ms = round((time.monotonic() - fetch_start) * 1000)

        if not games:
            if data_source.startswith("steam_store:tag_error:"):
                bad_tags = data_source.split(":", 2)[2]
                warning_msg = f"Market '{label}': tag(s) not recognized: {bad_tags}. Check tag names against Steam's tag list."
            else:
                warning_msg = f"Market '{label}': no games found."
            warnings.append(warning_msg)
            analysis = {
                "market_label": label,
                "total_games": 0,
                "error": warning_msg,
            }
            market_analyses.append((label, analysis))
            market_game_sets.append((label, []))
            continue

        # Enrich with Gamalytic (consistent with analyze_market_single pipeline)
        enrich_start = time.monotonic()
        primary_tag = tags[0] if tags and len(tags) == 1 else None
        enriched_games, enrich_warnings, enrichment_meta = await _enrich_with_gamalytic_bulk(
            games, gamalytic, steamspy=steamspy, primary_tag=primary_tag
        )
        enrich_ms = round((time.monotonic() - enrich_start) * 1000)

        # Update data_source to reflect enrichment
        matched = enrichment_meta.get("matched", 0)
        method = enrichment_meta.get("method", "none")
        tier2 = enrichment_meta.get("tier2_count", 0)
        if matched > 0:
            data_source = f"{data_source}+gamalytic_{method}({matched}/{len(enriched_games)})"
            if tier2 > 0:
                data_source += f"+tier2({tier2})"

        if enrich_warnings:
            warnings.extend(enrich_warnings)

        # Run pure analysis on enriched games
        analysis = _run_market_analysis(
            games=enriched_games,
            tags=tags,
            metrics=metrics,
            exclude=exclude,
            top_n=top_n,
            include_raw=False,
            market_label=label,
            data_source=data_source,
        )

        # Attach fetch and enrichment timing
        if analysis.get("compute_time"):
            analysis["compute_time"]["data_fetch_ms"] = fetch_ms
            analysis["compute_time"]["gamalytic_enrich_ms"] = enrich_ms

        # Add enrichment metadata to market analysis
        analysis["enrichment"] = {
            "method": method,
            "gamalytic_matched": matched,
            "gamalytic_total": enrichment_meta.get("total_gamalytic", 0),
            "match_rate": enrichment_meta.get("match_rate", 0.0),
            "tag_used": enrichment_meta.get("tag_used"),
            "total_games": len(enriched_games),
            "tier2_count": tier2,
        }

        if "error" in analysis:
            warnings.append(f"Market '{label}': {analysis['error']}")

        market_analyses.append((label, analysis))
        market_game_sets.append((label, enriched_games))

    if not market_analyses:
        return {
            "error": "No markets could be analyzed.",
            "market_count": 0,
            "warnings": warnings,
        }

    # Compute deltas
    valid_analyses = [(label, a) for label, a in market_analyses if "error" not in a]
    deltas = _compute_deltas(valid_analyses) if len(valid_analyses) >= 2 else []

    # Compute overlap
    valid_game_sets = [(label, games) for label, a in market_analyses for gl, games in [(label, next(
        (g for ml, g in market_game_sets if ml == label), []
    ))]]
    pairwise_overlap, global_overlap = _compute_overlap(market_game_sets)

    # Confidence notes
    confidence_notes = _compute_confidence_notes(market_analyses)

    # Ranking by health score
    overall_ranking = _rank_markets_by_health(valid_analyses) if valid_analyses else []

    # Build health score comparison dict: {label -> health_dict}
    health_score_comparison: dict[str, dict] = {}
    for label, analysis in market_analyses:
        health = analysis.get("health_scores") or {}
        health_score_comparison[label] = health

    # Build MarketComparisonEntry list (embed full analysis)
    rank_lookup = {r["label"]: r["rank"] for r in overall_ranking}
    market_entries: list[MarketComparisonEntry] = []
    for label, analysis in market_analyses:
        market_entries.append(MarketComparisonEntry(
            market_label=label,
            analysis=analysis,
            rank=rank_lookup.get(label, 0),
        ))

    # Build timing
    total_ms = round((time.monotonic() - total_start) * 1000)
    compute_time = ComputeTimingInfo(
        total_ms=total_ms,
        per_metric={},
        data_fetch_ms=0,
    )

    # Build methodology
    methodology = {
        "market_count": len(market_analyses),
        "markets_analyzed": [label for label, _ in market_analyses],
        "data_source": "steamspy+gamalytic_bulk",
        "biases": list(STANDARD_BIASES),
        "notes": [
            "Each market runs analyze_market independently.",
            "Each market enriched with Gamalytic bulk revenue data before analysis.",
            "Overlap detection by Steam AppID (exact match) using enriched game lists.",
            "Health scores computed independently per market.",
        ],
    }

    result_obj = CompareMarketsResult(
        market_count=len(market_analyses),
        markets=market_entries,
        deltas=deltas,
        health_score_comparison=health_score_comparison,
        overall_ranking=overall_ranking,
        overlap=pairwise_overlap,
        global_overlap=global_overlap,
        confidence_notes=confidence_notes,
        compute_time=compute_time,
        warnings=warnings,
        methodology=methodology,
    )

    return result_obj.model_dump()
