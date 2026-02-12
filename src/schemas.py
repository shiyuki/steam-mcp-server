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


class APIError(BaseModel):
    """Structured error response for MCP."""
    error_code: int
    error_type: str  # validation, not_found, rate_limit, api_error
    message: str
