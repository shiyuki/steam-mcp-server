"""Pydantic schemas for Steam API responses."""

from datetime import datetime
from pydantic import BaseModel, ConfigDict, Field, field_serializer


class CachedResponse(BaseModel):
    """Base class for API responses with data freshness tracking.

    All API responses that pass through the cache system inherit this
    to provide fetched_at timestamp and cache_age_seconds.
    """
    fetched_at: datetime
    cache_age_seconds: int = 0

    @field_serializer('fetched_at')
    def serialize_datetime(self, dt: datetime, _info) -> str:
        """Serialize datetime to ISO format string for JSON compatibility."""
        return dt.isoformat()


class GameMetadata(CachedResponse):
    """Metadata for a single Steam game with comprehensive Steam Store data."""
    model_config = ConfigDict(extra="ignore")

    # Core identification (existing fields - unchanged)
    appid: int
    name: str
    tags: list[str] = Field(default_factory=list)
    developer: str = ""
    description: str = ""  # Kept for backward compat (same as short_description)

    # Publishing
    publisher: str = ""

    # Descriptions
    short_description: str = ""  # Explicit short description
    detailed_description: str = ""  # HTML-stripped plain text

    # Visual assets
    header_image: str = ""  # Main capsule art URL

    # Pricing
    price: float | None = None  # In dollars; None if unavailable (not F2P)
    is_free_to_play: bool = False

    # Platform support
    platforms: dict[str, bool] = Field(default_factory=dict)  # {"windows": True, "mac": False, "linux": True}

    # Release information
    release_date: str | None = None  # ISO format "2019-01-23" or None
    release_date_raw: str = ""  # Original string "23 Jan, 2019"

    # Review aggregation
    metacritic_score: int | None = None
    metacritic_url: str = ""

    # Classification
    categories: list[str] = Field(default_factory=list)  # Steam feature tags ("Single-player", "Steam Achievements")
    genres: list[str] = Field(default_factory=list)  # Steam genre classification ("Indie", "Strategy")

    # Community metrics
    recommendations: int = 0  # Total recommendations count

    # Media assets
    screenshots: list[str] = Field(default_factory=list)  # Screenshot URLs (full size)
    screenshots_count: int = 0
    movies: list[dict] = Field(default_factory=list)  # Trailer dicts with {name, thumbnail, webm_url, mp4_url}
    movies_count: int = 0

    # DLC
    dlc: list[int] = Field(default_factory=list)  # DLC AppIDs
    dlc_count: int = 0

    # Content information
    content_descriptors: list[str] = Field(default_factory=list)  # Content descriptor notes
    supported_languages_count: int = 0
    supported_languages_raw: str = ""  # Raw languages string from API


class GameSummary(BaseModel):
    """Per-game summary data from SteamSpy bulk endpoint.

    All fields available in SteamSpy bulk response (request=tag).
    For detailed per-game tag weights, use fetch_engagement(appid) instead.
    """
    appid: int
    name: str = ""
    developer: str = ""
    publisher: str = ""
    owners_min: int = 0
    owners_max: int = 0
    owners_midpoint: float = 0
    ccu: int = 0
    price: float = 0  # In dollars, converted from API cents
    review_score: float | None = None  # Percentage (positive / total * 100)
    positive: int = 0
    negative: int = 0
    average_forever: float = 0  # Hours, converted from API minutes
    median_forever: float = 0  # Hours, converted from API minutes
    average_2weeks: float = 0  # Hours, converted from API minutes
    median_2weeks: float = 0  # Hours, converted from API minutes
    score_rank: str = ""
    userscore: float = 0
    # Tag cross-validation (Steam Store paths only)
    tag_matches: bool | None = None  # True=all searched tags in top 20, False=missing some, None=no data
    tags_missing: list[str] = Field(default_factory=list)  # Searched tags not found in game's top 20


class SearchResult(CachedResponse):
    """Result from tag-based game search with enriched per-game summaries."""
    # DEPRECATED: Use games list instead; kept for backward compatibility
    appids: list[int]
    tag: str
    total_found: int
    # NEW FIELDS: Phase 5 enrichment
    games: list[GameSummary] = Field(default_factory=list)
    sort_by: str = "owners"  # Sort field applied to games list
    result_size_warning: str | None = None  # Warning for large result sets (>5000 games)
    normalized_tag: str | None = None  # Tag after normalization (shows what was actually queried)
    data_source: str | None = None  # "steamspy" or "steam_store" — indicates which API provided the results
    cross_validation: dict | None = None  # Tag cross-validation summary (Steam Store paths only)


class CommercialData(CachedResponse):
    """Commercial data for a single Steam game including revenue estimates."""
    model_config = ConfigDict(extra="ignore")

    appid: int
    name: str = ""
    price: float | None = None
    revenue_min: float
    revenue_max: float
    currency: str = "USD"
    confidence: str  # "high" or "low"
    source: str  # "gamalytic" or "review_estimate"
    method: str | None = None
    triangulation_warning: str | None = None
    steamspy_implied_revenue: float | None = None
    gamalytic_revenue: float | None = None


class EngagementData(CachedResponse):
    """Player engagement metrics for a single Steam game from SteamSpy."""
    model_config = ConfigDict(extra="ignore")

    # Basic identification
    appid: int
    name: str
    developer: str = ""
    publisher: str = ""

    # Player counts
    ccu: int  # Current concurrent users
    owners_min: int
    owners_max: int
    owners_midpoint: float

    # Playtime metrics (all in hours, converted from API minutes)
    average_forever: float
    average_2weeks: float
    median_forever: float
    median_2weeks: float

    # Review metrics
    positive: int
    negative: int
    total_reviews: int

    # Commercial
    price: float  # In dollars, converted from API cents; 0.0 for F2P

    # Metadata
    score_rank: str = ""
    genre: str = ""
    languages: str = ""
    tags: dict[str, int] = Field(default_factory=dict)  # Tag name -> weight

    # Derived health metrics (None when data insufficient)
    ccu_ratio: float | None = None  # CCU / owners_midpoint * 100
    review_score: float | None = None  # positive / total_reviews * 100
    playtime_engagement: float | None = None  # median_forever / average_forever (ratio, not %)
    activity_ratio: float | None = None  # average_2weeks / average_forever * 100

    # Quality flags (distinguish real zeros from missing data)
    ccu_quality: str | None = None  # "available" or "zero_uncertain"
    owners_quality: str | None = None
    playtime_quality: str | None = None
    reviews_quality: str | None = None


class MetricStats(BaseModel):
    """Statistical distribution for a single metric across multiple games."""
    mean: float
    median: float
    p25: float  # 25th percentile
    p75: float  # 75th percentile
    min: float
    max: float
    games_with_data: int  # Number of games with valid data for this metric


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


class AggregateEngagementData(CachedResponse):
    """Genre-level engagement aggregation with statistical distribution data."""
    # Input identification
    tag: str | None = None  # If tag-based query
    appids_requested: list[int] | None = None  # If AppID-list query
    data_source: str | None = None  # "steamspy" or "steam_store" — indicates which API provided the game list

    # Coverage metrics
    games_total: int  # Total games found/requested
    games_analyzed: int  # Games with sufficient data for stats

    # Per-metric statistical distributions (None when insufficient data)
    ccu_stats: MetricStats | None = None
    owners_stats: MetricStats | None = None
    playtime_stats: MetricStats | None = None  # Based on average_forever in hours
    review_score_stats: MetricStats | None = None

    # Dual metric approaches for different analytical perspectives
    market_weighted: dict[str, float | None] = Field(default_factory=dict)  # Aggregate raw then compute
    per_game_average: dict[str, float | None] = Field(default_factory=dict)  # Compute per game then average

    # Per-game breakdown
    games: list[GameEngagementSummary] = Field(default_factory=list)
    failures: list[dict] = Field(default_factory=list)  # Failed AppIDs with reasons
    sort_by: str = "owners"  # Field used for sorting


class APIError(BaseModel):
    """Structured error response for MCP."""
    error_code: int
    error_type: str  # validation, not_found, rate_limit, api_error
    message: str


# ---------------------------------------------------------------------------
# Phase 6: Analytics Engine schemas (additive — existing schemas unchanged)
# ---------------------------------------------------------------------------

class ConfidenceMeta(BaseModel):
    """Reusable confidence metadata for analytics results."""
    confidence: str  # high / medium / low
    sample_size: int
    methodology: str = ""


class TopNShare(BaseModel):
    """Single top-N revenue share entry."""
    count: int
    share_pct: float
    no_data: bool = False


class RevenueTier(BaseModel):
    """Single revenue tier bucket."""
    label: str  # e.g. "titan", "breakout"
    min_revenue: float
    max_revenue: float | None = None  # None = unbounded
    count: int = 0
    total_revenue: float = 0.0
    no_data: bool = True


class ConcentrationResult(BaseModel):
    """ANALYTICS-01: Revenue concentration metrics."""
    gini: float | None = None
    top_shares: dict[str, TopNShare] = Field(default_factory=dict)  # keys: top_10, top_25, top_50, top_100
    revenue_tiers: list[RevenueTier] = Field(default_factory=list)  # 7 tiers
    meta: ConfidenceMeta


class TemporalPeriod(BaseModel):
    """Single year or quarter entry for temporal analysis."""
    year: int
    quarter: int | None = None  # None = yearly aggregate
    game_count: int = 0
    avg_revenue: float | None = None
    median_revenue: float | None = None
    avg_score: float | None = None
    total_revenue: float = 0.0
    success_rate: float | None = None  # % above $100K
    partial: bool = False
    no_data: bool = False
    events: list[str] = Field(default_factory=list)  # Steam events in period


class TemporalTrendsResult(BaseModel):
    """ANALYTICS-02: Temporal trends over time."""
    yearly: list[TemporalPeriod] = Field(default_factory=list)
    quarterly: list[TemporalPeriod] = Field(default_factory=list)
    meta: ConfidenceMeta


class BracketStats(BaseModel):
    """Single price/score/timing bracket stats."""
    label: str
    count: int = 0
    avg_revenue: float | None = None
    median_revenue: float | None = None
    success_rate: float | None = None
    avg_score: float | None = None
    no_data: bool = True


class PriceBracketResult(BaseModel):
    """ANALYTICS-03: Revenue by price bracket."""
    brackets: list[BracketStats] = Field(default_factory=list)
    meta: ConfidenceMeta


class SuccessRateResult(BaseModel):
    """ANALYTICS-04: Success rates at various revenue thresholds."""
    thresholds: dict[str, float] = Field(default_factory=dict)  # e.g. {"100k": 12.5, "1m": 3.2}
    total_with_revenue: int = 0
    meta: ConfidenceMeta


class TagMultiplierEntry(BaseModel):
    """Single tag revenue multiplier entry."""
    tag: str
    mean_multiplier: float
    median_multiplier: float
    game_count: int


class TagMultiplierResult(BaseModel):
    """ANALYTICS-05: Tag revenue multipliers vs genre baseline."""
    boosters: list[TagMultiplierEntry] = Field(default_factory=list)
    penalties: list[TagMultiplierEntry] = Field(default_factory=list)
    genre_avg_revenue: float | None = None
    genre_median_revenue: float | None = None
    min_games_threshold: int = 5
    meta: ConfidenceMeta


class ScoreRevenueBucket(BaseModel):
    """ANALYTICS-06: Single score range revenue bucket."""
    label: str
    count: int = 0
    avg_revenue: float | None = None
    median_revenue: float | None = None
    no_data: bool = True


class ScoreRevenueResult(BaseModel):
    """ANALYTICS-06 container: Score vs revenue correlation."""
    buckets: list[ScoreRevenueBucket] = Field(default_factory=list)
    correlation: float | None = None
    meta: ConfidenceMeta


class PublisherGroup(BaseModel):
    """Publisher category stats."""
    label: str  # self_published, third_party, unknown
    count: int = 0
    avg_revenue: float | None = None
    median_revenue: float | None = None
    success_rate: float | None = None
    no_data: bool = True


class PublisherAnalysisResult(BaseModel):
    """ANALYTICS-07: Publisher type analysis."""
    groups: list[PublisherGroup] = Field(default_factory=list)
    meta: ConfidenceMeta


class ReleaseTimingResult(BaseModel):
    """ANALYTICS-08: Revenue by release timing (quarter and month)."""
    quarterly: list[BracketStats] = Field(default_factory=list)  # Q1-Q4
    monthly: list[BracketStats] = Field(default_factory=list)    # 12 months
    meta: ConfidenceMeta


class SubGenreEntry(BaseModel):
    """ANALYTICS-09: Single sub-genre performance entry."""
    tag: str
    game_count: int = 0
    avg_revenue: float | None = None
    median_revenue: float | None = None
    success_rate: float | None = None


class SubGenreResult(BaseModel):
    """ANALYTICS-09 container: Sub-genre breakdown."""
    sub_genres: list[SubGenreEntry] = Field(default_factory=list)
    meta: ConfidenceMeta


class DensityPeriod(BaseModel):
    """ANALYTICS-10: Single period entry for competitive density."""
    year: int
    game_count: int = 0
    avg_revenue: float | None = None
    revenue_per_game_trend: float | None = None
    success_rate: float | None = None
    partial: bool = False


class CompetitiveDensityResult(BaseModel):
    """ANALYTICS-10: Competitive density over time."""
    periods: list[DensityPeriod] = Field(default_factory=list)
    pipeline_count: int = 0  # unreleased games
    meta: ConfidenceMeta


class GameAnalyticsSummary(BaseModel):
    """Lightweight per-game entry for analytics top-games list."""
    appid: int
    name: str = ""
    revenue: float | None = None
    revenue_velocity: float | None = None  # revenue/month
    price: float | None = None
    review_score: float | None = None
    owners: float | None = None
    ccu: int | None = None
    release_date: str | None = None
    developer: str = ""
    publisher: str = ""
    tag_confidence: str | None = None  # high / medium / low
    early_access: bool = False
    review_anomaly: bool = False


class MethodologyNote(BaseModel):
    """Bias and methodology documentation for analytics results."""
    biases: list[str] = Field(default_factory=list)  # always the 7 standard biases
    data_sources: list[str] = Field(default_factory=list)
    skipped_metrics: list[str] = Field(default_factory=list)  # metrics below N threshold
    notes: list[str] = Field(default_factory=list)  # additional methodology notes


class MarketAnalysis(BaseModel):
    """Top-level analytics output for market analysis."""
    schema_version: str = "1.2"

    # Query metadata (always present)
    query_tags: list[str] = Field(default_factory=list)
    total_games_input: int = 0
    games_analyzed: int = 0
    coverage: dict[str, float] = Field(default_factory=dict)  # field -> coverage %
    date_range: dict[str, str | None] = Field(default_factory=dict)
    data_sources: list[str] = Field(default_factory=list)

    # Analytics results (None = insufficient data or not requested)
    concentration: ConcentrationResult | None = None
    success_rates: SuccessRateResult | None = None
    price_brackets: PriceBracketResult | None = None
    temporal_trends: TemporalTrendsResult | None = None
    tag_multipliers: TagMultiplierResult | None = None
    score_revenue: ScoreRevenueResult | None = None
    publisher_analysis: PublisherAnalysisResult | None = None
    release_timing: ReleaseTimingResult | None = None
    sub_genres: SubGenreResult | None = None
    competitive_density: CompetitiveDensityResult | None = None

    # Per-game sample (top 25 by revenue)
    top_games: list[GameAnalyticsSummary] = Field(default_factory=list)

    # Methodology documentation (always present)
    methodology: MethodologyNote | None = None
