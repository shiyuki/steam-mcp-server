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
    """Metadata for a single Steam game."""
    model_config = ConfigDict(extra="ignore")

    appid: int
    name: str
    tags: list[str] = Field(default_factory=list)
    developer: str = ""
    description: str = ""


class SearchResult(CachedResponse):
    """Result from tag-based game search."""
    appids: list[int]
    tag: str
    total_found: int


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
