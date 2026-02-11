"""Pydantic schemas for Steam API responses."""

from pydantic import BaseModel, Field


class GameMetadata(BaseModel):
    """Metadata for a single Steam game."""
    appid: int
    name: str
    tags: list[str] = Field(default_factory=list)
    developer: str = ""
    description: str = ""

    class Config:
        extra = "ignore"  # Ignore extra fields from Steam API


class SearchResult(BaseModel):
    """Result from tag-based game search."""
    appids: list[int]
    tag: str
    total_found: int


class APIError(BaseModel):
    """Structured error response for MCP."""
    error_code: int
    error_type: str  # validation, not_found, rate_limit, api_error
    message: str
