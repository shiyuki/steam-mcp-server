"""Pydantic schemas for Steam API responses."""

from datetime import datetime
from pydantic import BaseModel, ConfigDict, Field


class CachedResponse(BaseModel):
    """Base class for API responses with data freshness tracking.

    All API responses that pass through the cache system inherit this
    to provide fetched_at timestamp and cache_age_seconds.
    """
    model_config = ConfigDict(json_encoders={datetime: lambda v: v.isoformat()})

    fetched_at: datetime
    cache_age_seconds: int = 0


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


class APIError(BaseModel):
    """Structured error response for MCP."""
    error_code: int
    error_type: str  # validation, not_found, rate_limit, api_error
    message: str
