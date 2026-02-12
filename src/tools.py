"""MCP tool definitions for Steam API."""

import json
from mcp.server.fastmcp import FastMCP

from src.http_client import CachedAPIClient
from src.cache import TTLCache
from src.rate_limiter import HostRateLimiter
from src.steam_api import SteamSpyClient, SteamStoreClient, GamalyticClient
from src.schemas import APIError
from src.logging_config import get_logger

logger = get_logger(__name__)

# Module-level clients (initialized in register_tools)
_http_client: CachedAPIClient | None = None
_steamspy: SteamSpyClient | None = None
_steam_store: SteamStoreClient | None = None
_gamalytic: GamalyticClient | None = None


def register_tools(mcp: FastMCP):
    """Register all MCP tools with the server."""
    global _http_client, _steamspy, _steam_store, _gamalytic

    # Initialize shared infrastructure with per-host rate limiting and TTL cache
    cache = TTLCache(default_ttl=3600)  # 1 hour default
    rate_limiter = HostRateLimiter()
    _http_client = CachedAPIClient(cache=cache, rate_limiter=rate_limiter)
    _steamspy = SteamSpyClient(_http_client)
    _steam_store = SteamStoreClient(_http_client)
    _gamalytic = GamalyticClient(_http_client)

    logger.info("Initializing MCP tools with per-host rate limiting and TTL cache")

    @mcp.tool()
    async def search_genre(genre: str, limit: int = 10) -> str:
        """Search Steam games by genre/tag and return AppIDs.

        Args:
            genre: Steam tag to search (e.g., "Roguelike", "Action", "RPG")
            limit: Maximum number of results (1-10000, default 10). Use 0 to return all matching AppIDs.

        Returns:
            JSON string with list of Steam AppIDs matching the genre
        """
        # Input validation
        if not genre or not genre.strip():
            return json.dumps({"error": "genre is required", "error_type": "validation"})

        # Handle 0 as "return all"
        return_all = (limit == 0)
        
        # Validate limit if not returning all
        if not return_all and not 1 <= limit <= 10000:
            return json.dumps({"error": "limit must be between 1 and 10000, or 0 for all results", "error_type": "validation"})
        
        limit_value = None if return_all else limit

        limit_str = "all" if return_all else str(limit)
        logger.info("Searching for genre: %s, limit: %s", genre, limit_str)
        result = await _steamspy.search_by_tag(genre.strip(), limit_value)

        if isinstance(result, APIError):
            return json.dumps(result.model_dump())

        return json.dumps(result.model_dump())

    @mcp.tool()
    async def fetch_metadata(appid: int) -> str:
        """Fetch metadata for a Steam game by AppID.

        Args:
            appid: Steam AppID (e.g., 646570 for Slay the Spire)

        Returns:
            JSON string with game metadata including name, tags, developer, and description
        """
        # Input validation
        if appid <= 0:
            return json.dumps({"error": "appid must be positive", "error_type": "validation"})

        logger.info("Fetching metadata for AppID: %d", appid)
        result = await _steam_store.get_app_details(appid)

        if isinstance(result, APIError):
            return json.dumps(result.model_dump())

        return json.dumps(result.model_dump())

    @mcp.tool()
    async def fetch_commercial(appid: int) -> str:
        """Fetch commercial data (pricing, revenue estimates) for a Steam game.

        Returns revenue as a range (min-max) with confidence level and data source.
        Uses Gamalytic API as primary source, falls back to review-based estimation.
        Includes triangulation warning when data sources disagree significantly.

        Args:
            appid: Steam AppID (e.g., 646570 for Slay the Spire)

        Returns:
            JSON string with revenue range, confidence, source, and optional triangulation warning
        """
        if appid <= 0:
            return json.dumps({"error": "appid must be positive", "error_type": "validation"})

        logger.info("Fetching commercial data for AppID: %d", appid)
        result = await _gamalytic.get_revenue_estimate(appid)

        if isinstance(result, APIError):
            return json.dumps(result.model_dump())

        return json.dumps(result.model_dump())

    @mcp.tool()
    async def fetch_engagement(appid: int) -> str:
        """Fetch player engagement metrics for a Steam game from SteamSpy.

        Returns CCU, owner estimates (min/max/midpoint), playtime (avg/median, forever/2weeks in hours),
        review counts and score, price, tags with weights, and derived health metrics.

        Derived metrics (numeric ratios, Claude interprets significance):
        - ccu_ratio: CCU / owners_midpoint * 100 (% of owners currently playing; <1% low engagement, >5% cult following)
        - review_score: positive / total_reviews * 100 (% positive reviews)
        - playtime_engagement: median_playtime / average_playtime (distribution skew; <1 means long-tail outliers inflate average)
        - activity_ratio: average_2weeks / average_forever * 100 (% of lifetime playtime happening recently; high = still active)

        Quality flags per ambiguous numeric field: "available" = real measurement, "zero_uncertain" = likely no data.

        Args:
            appid: Steam AppID (e.g., 646570 for Slay the Spire)

        Returns:
            JSON string with engagement metrics, derived health metrics, and quality flags
        """
        if appid <= 0:
            return json.dumps({"error": "appid must be positive", "error_type": "validation"})

        logger.info("Fetching engagement data for AppID: %d", appid)
        result = await _steamspy.get_engagement_data(appid)

        if isinstance(result, APIError):
            return json.dumps(result.model_dump())

        return json.dumps(result.model_dump())
