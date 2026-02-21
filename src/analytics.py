"""Pure-Python analytics computation for Steam market data. No I/O, no HTTP, no async."""

from __future__ import annotations

import statistics
from collections import defaultdict
from datetime import date, datetime
from math import floor

from src.schemas import (
    BracketStats,
    ConfidenceMeta,
    ConcentrationResult,
    PriceBracketResult,
    RevenueTier,
    SuccessRateResult,
    TagMultiplierEntry,
    TagMultiplierResult,
    TemporalPeriod,
    TemporalTrendsResult,
    TopNShare,
    MethodologyNote,
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# (label, min_price_inclusive, max_price_exclusive_or_none)
PRICE_BRACKETS: list[tuple[str, float, float | None]] = [
    ("f2p", 0.0, 0.001),           # Exactly 0
    ("0.01_4.99", 0.01, 5.0),
    ("5_9.99", 5.0, 10.0),
    ("10_14.99", 10.0, 15.0),
    ("15_19.99", 15.0, 20.0),
    ("20_29.99", 20.0, 30.0),
    ("30_plus", 30.0, None),        # None = unbounded
]

# (label, min_score_inclusive, max_score_exclusive_or_none)
SCORE_RANGES: list[tuple[str, int, int | None]] = [
    ("below_70", 0, 70),
    ("70_74", 70, 75),
    ("75_79", 75, 80),
    ("80_84", 80, 85),
    ("85_89", 85, 90),
    ("90_94", 90, 95),
    ("95_100", 95, 101),  # 101 to include exactly 100
]

# (label, min_revenue_inclusive, max_revenue_exclusive_or_none)
REVENUE_TIERS: list[tuple[str, float, float | None]] = [
    ("failed", 0, 1_000),
    ("minimal", 1_000, 10_000),
    ("small", 10_000, 100_000),
    ("moderate", 100_000, 1_000_000),
    ("classic", 1_000_000, 5_000_000),
    ("breakout", 5_000_000, 10_000_000),
    ("titan", 10_000_000, None),
]

DEFAULT_SUCCESS_THRESHOLDS: list[int] = [100_000, 1_000_000, 10_000_000]

KNOWN_PUBLISHERS: frozenset[str] = frozenset([
    # Major indie publishers
    "Team17", "Devolver Digital", "Paradox Interactive",
    "THQ Nordic", "Raw Fury", "Humble Games",
    "Curve Games", "tinyBuild", "Thunderful",
    # Boutique indie publishers
    "505 Games", "Assemble Entertainment", "Whitethorn Games",
    "Fellow Traveller", "Hooded Horse", "Yogscast Games",
    "Armor Games Studios", "Kasedo Games", "META Publishing",
    # Others commonly seen in indie market reports
    "Astragon Entertainment", "Freedom Games", "Graffiti Games",
    "Versus Evil", "The Iterative Collective", "Playstack",
    "No More Robots",
])

# Month -> list of Steam events that fall in that month
STEAM_EVENTS_BY_MONTH: dict[int, list[str]] = {
    1: ["winter_sale"],               # Late Dec bleeds into Jan
    2: ["next_fest"],                 # Feb Next Fest
    6: ["next_fest", "summer_sale"],  # Jun Next Fest + Summer Sale
    10: ["next_fest"],                # Oct Next Fest
    12: ["winter_sale"],              # Winter Sale
}

STANDARD_BIASES: list[str] = [
    "Survivorship bias: delisted games are invisible to all data sources.",
    "Age bias: lifetime revenue favors older games; partially corrected by revenue velocity.",
    "Bundle/key sale blindspot: revenue from bundles and key resellers is not captured.",
    "Tag application bias: popular games have more accurate tags; partially corrected by tag confidence.",
    "Review bombing: anomalous negative review spikes may skew score metrics; partially corrected by anomaly detection.",
    "Geographic/language bias: global revenue treated uniformly regardless of regional market share.",
    "F2P revenue opacity: free-to-play games rarely disclose microtransaction revenue; Gamalytic estimates may be unreliable.",
]


# ---------------------------------------------------------------------------
# Helper functions (all private except get_computable_metrics)
# ---------------------------------------------------------------------------

def _deduplicate(games: list[dict]) -> list[dict]:
    """Deduplicate games by appid. First occurrence wins. O(N)."""
    seen: set[int] = set()
    result: list[dict] = []
    for g in games:
        appid = g.get("appid")
        if appid is not None and appid not in seen:
            seen.add(appid)
            result.append(g)
    return result


def _coverage(games: list[dict], field: str) -> float:
    """Returns percentage (0-100) of games with non-None value for field."""
    if not games:
        return 0.0
    has_data = sum(1 for g in games if g.get(field) is not None)
    return round(has_data / len(games) * 100, 1)


def _gini_coefficient(values: list[float]) -> float | None:
    """
    Standard Gini coefficient using Brown formula. Returns [0, 1].
    Returns None if empty list, 0.0 if total is 0 (perfect equality).
    """
    if not values:
        return None
    n = len(values)
    total = sum(values)
    if total == 0:
        return 0.0
    sorted_vals = sorted(values)
    # G = (2 * sum_i(i * x_i) / (n * sum_x)) - (n+1)/n  [1-indexed i]
    weighted_sum = sum((i + 1) * v for i, v in enumerate(sorted_vals))
    return round((2 * weighted_sum) / (n * total) - (n + 1) / n, 4)


def _assign_price_bracket(price: float | None) -> str | None:
    """Returns price bracket label for the given price, or None if price is None."""
    if price is None:
        return None
    for label, min_p, max_p in PRICE_BRACKETS:
        if max_p is None:
            if price >= min_p:
                return label
        else:
            if min_p <= price < max_p:
                return label
    return None


def _assign_score_range(score: float | None) -> str | None:
    """Returns score range label for the given review score, or None if score is None."""
    if score is None:
        return None
    for label, min_s, max_s in SCORE_RANGES:
        if max_s is None:
            if score >= min_s:
                return label
        else:
            if min_s <= score < max_s:
                return label
    return None


def _assign_revenue_tier(revenue: float | None) -> str | None:
    """Returns revenue tier label for the given revenue, or None if revenue is None."""
    if revenue is None:
        return None
    for label, min_r, max_r in REVENUE_TIERS:
        if max_r is None:
            if revenue >= min_r:
                return label
        else:
            if min_r <= revenue < max_r:
                return label
    return None


def _extract_year_quarter(release_date: str | None) -> tuple[int, int] | None:
    """Parse 'YYYY-MM-DD' format, return (year, quarter) or None."""
    if not release_date:
        return None
    try:
        dt = datetime.strptime(release_date, "%Y-%m-%d")
        quarter = (dt.month - 1) // 3 + 1
        return dt.year, quarter
    except (ValueError, TypeError):
        return None


def _extract_month(release_date: str | None) -> int | None:
    """Parse 'YYYY-MM-DD', return month (1-12) or None."""
    if not release_date:
        return None
    try:
        dt = datetime.strptime(release_date, "%Y-%m-%d")
        return dt.month
    except (ValueError, TypeError):
        return None


def _is_partial_period(year: int, quarter: int | None = None, today: date | None = None) -> bool:
    """Returns True if the period is current or future (i.e., not yet complete)."""
    if today is None:
        today = date.today()
    cur_year = today.year
    cur_quarter = (today.month - 1) // 3 + 1

    if year > cur_year:
        return True
    if year == cur_year:
        if quarter is None:
            return True  # Current year is always partial
        return quarter >= cur_quarter  # Current quarter is always partial
    return False


def _revenue_velocity(
    revenue: float | None,
    release_date: str | None,
    today: date | None = None,
) -> float | None:
    """
    revenue / months_since_release. Minimum 1 month to avoid division by zero.
    Returns None if either input is None or release_date is unparseable.
    """
    if revenue is None or release_date is None:
        return None
    try:
        release = datetime.strptime(release_date, "%Y-%m-%d").date()
        if today is None:
            today = date.today()
        months = max((today - release).days / 30.44, 1.0)
        return round(revenue / months, 2)
    except (ValueError, TypeError):
        return None


def _classify_publisher(developer: str, publisher: str) -> str:
    """
    Returns 'self_published', 'third_party', or 'unknown'.
    Self-published = developer field matches publisher field exactly (stripped).
    Third-party = field mismatch OR publisher is on KNOWN_PUBLISHERS list.
    Unknown = either field is empty/missing.
    """
    if not developer or not publisher:
        return "unknown"
    dev = developer.strip()
    pub = publisher.strip()
    if not dev or not pub:
        return "unknown"
    if dev == pub:
        return "self_published"
    return "third_party"


def _get_top_tags(tags, top_n: int = 20) -> list[str]:
    """
    Returns up to top_n tag names.
    Handles dict[str, int] (sorted by weight desc) and list[str] formats.
    """
    if isinstance(tags, dict):
        return sorted(tags.keys(), key=lambda t: tags[t], reverse=True)[:top_n]
    elif isinstance(tags, list):
        return list(tags)[:top_n]
    return []


def _tag_confidence(tags) -> str:
    """
    Returns 'high' (>=500 total votes), 'medium' (100-499), 'low' (<100).
    Handles both dict[str, int] and list[str] formats.
    For list format, confidence is always 'low' (no vote counts available).
    """
    if isinstance(tags, dict):
        total_votes = sum(tags.values())
        if total_votes >= 500:
            return "high"
        elif total_votes >= 100:
            return "medium"
        else:
            return "low"
    # list format — no vote weight data
    return "low"


def _safe_mean(values: list[float]) -> float | None:
    """Returns mean rounded to 2 decimal places, or None if list is empty."""
    if not values:
        return None
    return round(statistics.mean(values), 2)


def _safe_median(values: list[float]) -> float | None:
    """Returns median rounded to 2 decimal places, or None if list is empty."""
    if not values:
        return None
    return round(statistics.median(values), 2)


def _get_quarter_events(quarter: int) -> list[str]:
    """Returns sorted list of Steam events for a calendar quarter."""
    months_map = {1: [1, 2, 3], 2: [4, 5, 6], 3: [7, 8, 9], 4: [10, 11, 12]}
    months = months_map.get(quarter, [])
    events: set[str] = set()
    for m in months:
        events.update(STEAM_EVENTS_BY_MONTH.get(m, []))
    return sorted(events)


def get_computable_metrics(n_games: int, requested: list[str] | None = None) -> set[str]:
    """
    Return the set of metric names computable given the sample size.

    Always available (any N):
        descriptive_stats, tag_frequency

    N >= 20 unlocks:
        concentration, success_rates, price_brackets, publisher_analysis

    N >= 50 unlocks all remaining:
        temporal_trends, tag_multipliers, score_revenue, competitive_density,
        release_timing, sub_genres

    If `requested` is provided, returns the intersection with what is available.
    """
    available: set[str] = {"descriptive_stats", "tag_frequency"}

    if n_games >= 20:
        available |= {"concentration", "success_rates", "price_brackets", "publisher_analysis"}

    if n_games >= 50:
        available |= {
            "temporal_trends",
            "tag_multipliers",
            "score_revenue",
            "competitive_density",
            "release_timing",
            "sub_genres",
        }

    if requested is not None:
        return available & set(requested)
    return available


def _build_methodology(data_sources: list[str], skipped: list[str]) -> MethodologyNote:
    """Creates a MethodologyNote with STANDARD_BIASES and provided sources/skipped metrics."""
    return MethodologyNote(
        biases=list(STANDARD_BIASES),
        data_sources=list(data_sources),
        skipped_metrics=list(skipped),
    )


def _confidence_level(sample_size: int, high_threshold: int = 100, medium_threshold: int = 20) -> str:
    """Returns 'high', 'medium', or 'low' based on sample size."""
    if sample_size >= high_threshold:
        return "high"
    if sample_size >= medium_threshold:
        return "medium"
    return "low"


# ---------------------------------------------------------------------------
# Public compute functions (ANALYTICS-01 through ANALYTICS-05)
# ---------------------------------------------------------------------------


def compute_concentration(games: list[dict]) -> ConcentrationResult | None:
    """
    ANALYTICS-01: Revenue concentration metrics.

    Returns ConcentrationResult with Gini coefficient, top-N revenue shares,
    and revenue tier distribution. Returns None if no games have revenue data.
    """
    revenues = [g["revenue"] for g in games if g.get("revenue") is not None]
    if not revenues:
        return None

    sorted_rev = sorted(revenues, reverse=True)
    total = sum(sorted_rev)

    # Build top-N shares
    top_shares: dict[str, TopNShare] = {}
    for n, key in [(10, "top_10"), (25, "top_25"), (50, "top_50"), (100, "top_100")]:
        actual_count = min(n, len(sorted_rev))
        top_sum = sum(sorted_rev[:actual_count])
        share_pct = round(top_sum / total * 100, 2) if total > 0 else 0.0
        top_shares[key] = TopNShare(count=actual_count, share_pct=share_pct)

    # Compute Gini
    gini = _gini_coefficient(revenues)

    # Build revenue tier distribution
    tier_buckets: dict[str, list[float]] = {label: [] for label, _, _ in REVENUE_TIERS}
    for rev in revenues:
        tier_label = _assign_revenue_tier(rev)
        if tier_label is not None:
            tier_buckets[tier_label].append(rev)

    tiers: list[RevenueTier] = []
    for label, min_r, max_r in REVENUE_TIERS:
        bucket = tier_buckets[label]
        tiers.append(RevenueTier(
            label=label,
            min_revenue=float(min_r),
            max_revenue=float(max_r) if max_r is not None else None,
            count=len(bucket),
            total_revenue=sum(bucket),
            no_data=(len(bucket) == 0),
        ))

    confidence = _confidence_level(len(revenues))
    meta = ConfidenceMeta(
        confidence=confidence,
        sample_size=len(revenues),
        methodology=(
            "Revenue-based concentration. Revenue estimates from Gamalytic "
            "with +/-20-50% variance."
        ),
    )
    return ConcentrationResult(gini=gini, top_shares=top_shares, revenue_tiers=tiers, meta=meta)


def compute_temporal_trends(games: list[dict]) -> TemporalTrendsResult | None:
    """
    ANALYTICS-02: Trends over time (yearly and quarterly).

    Returns TemporalTrendsResult with per-year and per-quarter breakdowns.
    Returns None if no games have valid release dates.
    """
    # Group by year and (year, quarter)
    yearly_games: dict[int, list[dict]] = defaultdict(list)
    quarterly_games: dict[tuple[int, int], list[dict]] = defaultdict(list)

    for g in games:
        yq = _extract_year_quarter(g.get("release_date"))
        if yq is None:
            continue
        year, quarter = yq
        yearly_games[year].append(g)
        quarterly_games[(year, quarter)].append(g)

    if not yearly_games:
        return None

    SUCCESS_THRESHOLD = 100_000

    def _build_period_stats(period_games: list[dict], year: int, quarter: int | None) -> TemporalPeriod:
        revenues = [g["revenue"] for g in period_games if g.get("revenue") is not None]
        scores = [g["review_score"] for g in period_games if g.get("review_score") is not None]
        total_rev = sum(revenues)
        success_count = sum(1 for r in revenues if r >= SUCCESS_THRESHOLD)
        success_rate = round(success_count / len(revenues) * 100, 2) if revenues else None
        events = _get_quarter_events(quarter) if quarter is not None else []
        return TemporalPeriod(
            year=year,
            quarter=quarter,
            game_count=len(period_games),
            avg_revenue=_safe_mean(revenues),
            median_revenue=_safe_median(revenues),
            avg_score=_safe_mean(scores),
            total_revenue=total_rev,
            success_rate=success_rate,
            partial=_is_partial_period(year, quarter),
            no_data=(len(period_games) == 0),
            events=events,
        )

    yearly_list = sorted(
        [_build_period_stats(gs, yr, None) for yr, gs in yearly_games.items()],
        key=lambda p: p.year,
    )
    quarterly_list = sorted(
        [_build_period_stats(gs, yr, q) for (yr, q), gs in quarterly_games.items()],
        key=lambda p: (p.year, p.quarter),
    )

    total_games = sum(len(gs) for gs in yearly_games.values())
    confidence = _confidence_level(total_games, high_threshold=100, medium_threshold=50)
    meta = ConfidenceMeta(
        confidence=confidence,
        sample_size=total_games,
        methodology="Year/quarter aggregations of release dates. Partial-period flag set for current and future periods.",
    )
    return TemporalTrendsResult(yearly=yearly_list, quarterly=quarterly_list, meta=meta)


def compute_price_brackets(games: list[dict]) -> PriceBracketResult | None:
    """
    ANALYTICS-03: Revenue analysis by price bracket.

    Returns PriceBracketResult with all 7 brackets. Brackets with no games
    have no_data=True. Returns None if no games have price data.
    """
    SUCCESS_THRESHOLD = 100_000

    # Collect per-bracket data
    bracket_revenues: dict[str, list[float]] = {label: [] for label, _, _ in PRICE_BRACKETS}
    bracket_scores: dict[str, list[float]] = {label: [] for label, _, _ in PRICE_BRACKETS}
    bracket_counts: dict[str, int] = {label: 0 for label, _, _ in PRICE_BRACKETS}

    total_with_price = 0
    for g in games:
        price = g.get("price")
        if price is None:
            continue
        bracket_label = _assign_price_bracket(price)
        if bracket_label is None:
            continue
        total_with_price += 1
        bracket_counts[bracket_label] += 1
        revenue = g.get("revenue")
        if revenue is not None:
            bracket_revenues[bracket_label].append(revenue)
        score = g.get("review_score")
        if score is not None:
            bracket_scores[bracket_label].append(score)

    if total_with_price == 0:
        return None

    brackets: list[BracketStats] = []
    for label, _, _ in PRICE_BRACKETS:
        count = bracket_counts[label]
        revenues = bracket_revenues[label]
        scores = bracket_scores[label]
        success_count = sum(1 for r in revenues if r >= SUCCESS_THRESHOLD)
        success_rate = round(success_count / len(revenues) * 100, 2) if revenues else None
        brackets.append(BracketStats(
            label=label,
            count=count,
            avg_revenue=_safe_mean(revenues),
            median_revenue=_safe_median(revenues),
            success_rate=success_rate,
            avg_score=_safe_mean(scores),
            no_data=(count == 0),
        ))

    confidence = _confidence_level(total_with_price, high_threshold=50, medium_threshold=20)
    meta = ConfidenceMeta(
        confidence=confidence,
        sample_size=total_with_price,
        methodology="Revenue grouped by price tier. Success = revenue >= $100K.",
    )
    return PriceBracketResult(brackets=brackets, meta=meta)
