"""Market analysis orchestration layer for Phase 9 tools.

Pure functions where possible — no HTTP calls, no async.
Testable independently without MCP wiring.
"""
from __future__ import annotations

import hashlib
import json
import statistics
import time
from collections import Counter, defaultdict

from src.analytics import (
    STANDARD_BIASES,
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
