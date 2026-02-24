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

    # Phase 8: Steam Store extended fields
    achievement_count: int | None = None
    ratings: dict = Field(default_factory=dict)  # {authority: {rating, descriptors}}
    supported_languages: list["LanguageEntry"] = Field(default_factory=list)  # structured parsed list
    developer_website: str | None = None
    pc_requirements_min: str = ""  # HTML-stripped plain text
    pc_requirements_rec: str = ""  # HTML-stripped plain text
    controller_support: str | None = None  # "full", "partial", or None


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

    # Phase 8: Gamalytic extended fields
    copies_sold: int | None = None
    followers: int | None = None
    accuracy: float | None = None
    total_revenue: bool | None = None  # data quality flag: True=lifetime, False=partial
    review_score: float | None = None  # Gamalytic's review score (0-100)
    gamalytic_owners: int | None = None
    gamalytic_players: int | None = None  # daily avg CCU
    gamalytic_reviews: int | None = None
    gamalytic_is_early_access: bool | None = None
    gamalytic_ea_date: str | None = None  # ISO date
    release_date: str | None = None  # ISO date from Gamalytic releaseDate field
    history: list["GamalyticHistoryEntry"] = Field(default_factory=list)
    country_data: list["CountryDataEntry"] = Field(default_factory=list)
    audience_overlap: list["CompetitorEntry"] = Field(default_factory=list)
    also_played: list["CompetitorEntry"] = Field(default_factory=list)
    estimate_details: list["EstimateModelEntry"] = Field(default_factory=list)
    gamalytic_dlc: list["DLCEntry"] = Field(default_factory=list)

    # Phase 8: Player count fallback
    current_player_count: int | None = None
    player_count_source: str | None = None  # "gamalytic_history" / "steam_web_api" / "steamspy"
    player_count_note: str | None = None  # Warning when fallback used


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


# ---------------------------------------------------------------------------
# Phase 7: Review Intelligence schemas (additive — existing schemas unchanged)
# ---------------------------------------------------------------------------

class ReviewItem(BaseModel):
    """Per-review data for a single Steam user review."""
    review_id: str
    text: str  # BBCode-stripped, truncated review text
    truncated: bool = False  # whether text was truncated at 1000 chars
    full_length: int = 0  # original text length before truncation
    voted_up: bool
    votes_up: int = 0  # helpfulness upvotes
    votes_funny: int = 0
    playtime_at_review: float = 0  # hours (converted from API minutes)
    playtime_total: float = 0  # hours (author's current total)
    timestamp_created: int = 0  # Unix timestamp
    timestamp_updated: int = 0  # Unix timestamp
    written_during_early_access: bool = False
    received_for_free: bool = False
    language: str = "english"
    author_steamid: str = ""
    author_num_games_owned: int = 0
    author_num_reviews: int = 0
    developer_response: str | None = None  # only present when developer responded


class ReviewSnippet(BaseModel):
    """Compact review entry for summary/compact detail level."""
    review_id: str
    text: str  # first 200 chars
    voted_up: bool
    votes_up: int = 0
    perspective: str = ""  # "top_positive", "top_negative", or "most_helpful"


class ReviewAggregates(BaseModel):
    """Steam query_summary aggregates — covers ALL reviews, not just fetched pages."""
    total_positive: int = 0
    total_negative: int = 0
    total_reviews: int = 0
    review_score: int = 0  # Steam's 1-9 score
    review_score_desc: str = ""  # e.g. "Overwhelmingly Positive"
    positive_ratio: float | None = None  # total_positive / total_reviews * 100
    weighted_positive_ratio: float | None = None  # helpfulness-weighted ratio


class PlaytimeBucket(BaseModel):
    """Single playtime histogram bucket."""
    label: str  # e.g. "0_2hr", "2_5hr", "50hr_plus"
    count: int = 0
    positive_count: int = 0
    negative_count: int = 0


class SentimentPeriod(BaseModel):
    """Sentiment metrics for a single time period."""
    period: str  # e.g. "first_30d", "last_90d", "2024"
    positive_count: int = 0
    negative_count: int = 0
    total_count: int = 0
    positive_ratio: float | None = None
    weighted_ratio: float | None = None  # helpfulness-weighted ratio
    avg_playtime_at_review: float | None = None  # hours
    avg_helpfulness: float | None = None
    avg_reviews_per_day: float | None = None
    review_count: int = 0  # same as total_count (for confidence assessment readability)


class EADateInfo(BaseModel):
    """Early Access date metadata with source provenance."""
    ea_release_date: str | None = None  # ISO date
    full_release_date: str | None = None  # ISO date
    currently_ea: bool = False
    date_source: str = ""  # "gamalytic", "steam_api", or "review_derived"


class ReviewsMeta(BaseModel):
    """Response metadata for review fetch operations."""
    appid: int
    name: str = ""
    schema_version: str = "1.0"
    total_fetched: int = 0
    total_available: int = 0
    pages_fetched: int = 0
    partial: bool = False  # True if pagination failed mid-way
    cursor: str | None = None  # for multi-call pagination
    ea_dates: EADateInfo | None = None
    detail_level: str = "full"  # "full" or "compact"
    status: str = "ok"  # "ok", "no_reviews", "reviews_disabled", or error message
    delisted: bool = False
    fetched_at: datetime
    cache_age_seconds: int = 0

    @field_serializer('fetched_at')
    def serialize_datetime(self, dt: datetime, _info) -> str:
        """Serialize datetime to ISO format string for JSON compatibility."""
        return dt.isoformat()


class QueryParams(BaseModel):
    """Echo of request parameters for audit/reproducibility."""
    appid: int
    language: str = "english"
    review_type: str = "all"
    purchase_type: str = "all"
    limit: int = 200
    detail_level: str = "full"
    filter_offtopic: bool = True
    min_playtime: float | None = None  # hours
    date_range: str | None = None
    platform: str = "all"
    sort: str = "helpful"  # "helpful" or "recent"


class ReviewsData(BaseModel):
    """Top-level response for fetch_reviews — full review intelligence output."""
    reviews: list[ReviewItem] = Field(default_factory=list)  # full detail level only
    snippets: list[ReviewSnippet] = Field(default_factory=list)  # compact detail level only
    aggregates: ReviewAggregates | None = None
    histogram: list[PlaytimeBucket] = Field(default_factory=list)
    sentiment: list[SentimentPeriod] = Field(default_factory=list)  # fixed + rolling periods
    yearly: list[SentimentPeriod] = Field(default_factory=list)  # calendar year breakdown
    ratio_timeline: list[list] = Field(default_factory=list)  # [[period, ratio], ...]
    language_distribution: dict[str, int] = Field(default_factory=dict)  # language -> count
    meta: ReviewsMeta | None = None
    query_params: QueryParams | None = None


# ---------------------------------------------------------------------------
# Phase 8: Extended Data Sources schemas (additive — existing schemas unchanged)
# ---------------------------------------------------------------------------

class GamalyticHistoryEntry(BaseModel):
    """Single history snapshot from Gamalytic game history data."""
    entry_type: str  # "launch" or "daily"
    timestamp: str | None = None  # ISO 8601 date string
    reviews: int | None = None
    price: float | None = None
    score: float | None = None
    rank: int | None = None
    followers: int | None = None
    gamalytic_players: int | None = None  # daily avg CCU (gamalytic_ prefix)
    avg_playtime: float | None = None
    sales: int | None = None
    revenue: float | None = None


class CountryDataEntry(BaseModel):
    """Single country revenue share entry."""
    country_code: str  # 2-letter ISO code, passed through as-is
    share_pct: float | None = None


class CompetitorEntry(BaseModel):
    """Competitor game entry from audienceOverlap or alsoPlayed."""
    appid: int | None = None
    name: str = ""
    overlap_pct: float | None = None  # for audienceOverlap (may be None for alsoPlayed)
    revenue: float | None = None
    followers: int | None = None
    score: float | None = None


class EstimateModelEntry(BaseModel):
    """Single revenue estimate model entry."""
    model_name: str = ""
    estimate: float | None = None


class DLCEntry(BaseModel):
    """Single Gamalytic DLC entry."""
    name: str = ""
    price: float | None = None
    release_date: str | None = None  # ISO date
    revenue: float | None = None


class LanguageEntry(BaseModel):
    """Structured language support entry from Steam Store data."""
    language: str
    full_audio: bool = False


# ---------------------------------------------------------------------------
# Phase 9: Market Analysis Tools schemas (additive — existing schemas unchanged)
# ---------------------------------------------------------------------------

class GameProfile(BaseModel):
    """Full profile for a top game in market analysis results."""
    appid: int
    name: str = ""
    revenue: float | None = None
    review_score: float | None = None
    release_date: str | None = None
    owners: float | None = None          # owners_midpoint
    ccu: int | None = None
    median_playtime: float | None = None  # hours
    followers: int | None = None          # from Gamalytic
    copies_sold: int | None = None        # from Gamalytic
    price: float | None = None
    tags: dict[str, int] = Field(default_factory=dict)  # tag -> weight
    developer: str = ""
    publisher: str = ""


class SkippedMetric(BaseModel):
    """A metric that was skipped with reason for the skip."""
    metric: str
    reason: str


class MarketHealthScores(BaseModel):
    """Three 0-100 health scores for a market segment."""
    growth: int = 50
    competition: int = 50
    accessibility: int = 50


class ComputeTimingInfo(BaseModel):
    """Per-metric and total execution timing information."""
    total_ms: int = 0
    per_metric: dict[str, int] = Field(default_factory=dict)
    data_fetch_ms: int = 0


class AnalyzeMarketResult(BaseModel):
    """Top-level FLAT response for the analyze_market tool."""
    # Query metadata
    market_label: str = ""
    total_games: int = 0
    games_with_revenue: int = 0
    coverage_pct: float = 0.0
    phase: int = 1
    continuation_token: str | None = None

    # Analytics metrics (all at top level, None = insufficient data or not requested)
    concentration: "ConcentrationResult | None" = None
    temporal_trends: "TemporalTrendsResult | None" = None
    price_brackets: "PriceBracketResult | None" = None
    success_rates: "SuccessRateResult | None" = None
    tag_multipliers: "TagMultiplierResult | None" = None
    score_revenue: "ScoreRevenueResult | None" = None
    publisher_analysis: "PublisherAnalysisResult | None" = None
    release_timing: "ReleaseTimingResult | None" = None
    sub_genres: "SubGenreResult | None" = None
    competitive_density: "CompetitiveDensityResult | None" = None

    # Derived analytics
    descriptive_stats: dict | None = None
    tag_frequency: list[dict] | None = None
    health_scores: MarketHealthScores | None = None

    # Game sample and enrichment
    top_games: list[GameProfile] = Field(default_factory=list)
    skipped_metrics: list[SkippedMetric] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    methodology: dict | None = None
    compute_time: ComputeTimingInfo | None = None
    raw_data: list[dict] | None = None
    gamalytic_enrichment: dict | None = None
    quota_warning: str | None = None


class GamePercentiles(BaseModel):
    """Per-metric percentile rankings for a single game vs its market."""
    revenue: float | None = None
    review_score: float | None = None
    median_playtime: float | None = None
    ccu: float | None = None
    followers: float | None = None
    copies_sold: float | None = None
    review_count: float | None = None


class CompetitorComparison(BaseModel):
    """Side-by-side competitor stats for evaluate_game."""
    appid: int
    name: str = ""
    revenue: float | None = None
    review_score: float | None = None
    price: float | None = None
    ccu: int | None = None
    owners: float | None = None
    followers: int | None = None
    similarity_score: float | None = None
    overlap_source: str = ""
    shared_tags: list[str] = Field(default_factory=list)


class TagInsight(BaseModel):
    """Tag multiplier vs genre baseline for evaluate_game."""
    tag: str
    multiplier: float
    game_count: int = 0
    classification: str = ""


class EvaluateGameResult(BaseModel):
    """Top-level response for the evaluate_game tool."""
    appid: int
    name: str = ""
    genre: str = ""
    game_profile: GameProfile | None = None
    percentiles: GamePercentiles | None = None
    strengths: list[str] = Field(default_factory=list)
    weaknesses: list[str] = Field(default_factory=list)
    position_score: float = 50.0
    potential_score: float = 50.0
    opportunity_score: float = 50.0
    competitors: list[CompetitorComparison] = Field(default_factory=list)
    tag_insights: list[TagInsight] = Field(default_factory=list)
    genre_fit: dict | None = None
    review_sentiment: dict | None = None
    pricing_context: dict | None = None
    historical_performance: list[dict] | None = None
    genre_baseline: dict | None = None
    compute_time: ComputeTimingInfo | None = None
    warnings: list[str] = Field(default_factory=list)
    methodology: dict | None = None


# ---------------------------------------------------------------------------
# Phase 9: Compare Markets schemas (additive — existing schemas unchanged)
# ---------------------------------------------------------------------------

class MarketComparisonEntry(BaseModel):
    """Per-market entry in a compare_markets result."""
    market_label: str = ""
    analysis: dict | None = None
    rank: int = 0


class MetricDelta(BaseModel):
    """Single metric comparison across markets."""
    metric: str
    values: dict[str, float | None] = Field(default_factory=dict)
    winner: str = ""
    delta_pct: float | None = None


class OverlapInfo(BaseModel):
    """Games appearing in multiple markets (pairwise or global)."""
    pair: str = ""
    overlap_count: int = 0
    overlap_games: list[GameProfile] = Field(default_factory=list)
    pct_of_smaller_market: float = 0.0


class CompareMarketsResult(BaseModel):
    """Top-level response for the compare_markets tool."""
    market_count: int = 0
    markets: list[MarketComparisonEntry] = Field(default_factory=list)
    deltas: list[MetricDelta] = Field(default_factory=list)
    health_score_comparison: dict[str, dict] = Field(default_factory=dict)
    overall_ranking: list[dict] = Field(default_factory=list)
    overlap: list[OverlapInfo] = Field(default_factory=list)
    global_overlap: OverlapInfo | None = None
    confidence_notes: list[str] = Field(default_factory=list)
    compute_time: ComputeTimingInfo | None = None
    warnings: list[str] = Field(default_factory=list)
    methodology: dict | None = None
