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


class APIError(BaseModel):
    """Structured error response for MCP."""
    error_code: int
    error_type: str  # validation, not_found, rate_limit, api_error
    message: str
