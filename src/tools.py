"""MCP tool definitions for Steam API."""

import asyncio
import json
from datetime import datetime, timezone
from mcp.server.fastmcp import FastMCP

from src.http_client import CachedAPIClient
from src.cache import TTLCache
from src.rate_limiter import HostRateLimiter
from src.steam_api import SteamSpyClient, SteamStoreClient, GamalyticClient
from src.schemas import APIError, SearchResult, GameSummary, EngagementData
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

    # Sort key functions for GameSummary objects
    _sort_keys = {
        "owners": lambda g: g.owners_midpoint,
        "ccu": lambda g: g.ccu,
        "price": lambda g: g.price,
        "review_score": lambda g: (g.review_score if g.review_score is not None else 0),
    }

    async def _steam_store_fallback_search(
        genre: str, limit: int | None, sort_by: str
    ) -> SearchResult | APIError:
        """Fall back to Steam Store tag search when SteamSpy tag search fails.

        Resolves tag name to Steam tag ID, searches Steam Store for AppIDs,
        then enriches each with SteamSpy per-game data.

        Args:
            genre: Tag name to search
            limit: Max results (None for all)
            sort_by: Sort field

        Returns:
            SearchResult with enriched games, or APIError
        """
        # Step 1: Resolve tag name to tag ID
        tag_map = await _steam_store.get_tag_map()
        if isinstance(tag_map, APIError):
            return tag_map

        tag_id = tag_map.get(genre.lower())
        if tag_id is None:
            return APIError(
                error_code=404,
                error_type="tag_not_found",
                message=f"Tag '{genre}' not found in Steam's tag registry ({len(tag_map)} tags available)"
            )

        # Step 2: Search Steam Store for AppIDs
        # Request more than limit to account for enrichment failures
        request_count = min((limit or 50) * 2, 100)
        search_result = await _steam_store.search_by_tag_ids([tag_id], count=request_count)
        if isinstance(search_result, APIError):
            return search_result

        total_count, appids = search_result
        if not appids:
            return SearchResult(
                fetched_at=datetime.now(timezone.utc),
                cache_age_seconds=0,
                appids=[],
                tag=genre,
                total_found=0,
                games=[],
                sort_by=sort_by,
                normalized_tag=genre,
            )

        # Step 3: Enrich with SteamSpy per-game data (concurrent, rate-limited)
        logger.info(f"Steam Store fallback: enriching {len(appids)} games for tag '{genre}'")
        tasks = [_steamspy.get_engagement_data(appid) for appid in appids]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Step 4: Build GameSummary from EngagementData
        games = []
        enriched_appids = []
        for result in results:
            if isinstance(result, EngagementData):
                games.append(GameSummary(
                    appid=result.appid,
                    name=result.name,
                    developer=result.developer,
                    publisher=result.publisher,
                    owners_midpoint=result.owners_midpoint,
                    ccu=result.ccu,
                    price=result.price,
                    review_score=result.review_score,
                    positive=result.positive,
                    negative=result.negative,
                    average_forever=result.average_forever,
                    median_forever=result.median_forever,
                    average_2weeks=result.average_2weeks,
                    median_2weeks=result.median_2weeks,
                    score_rank=result.score_rank,
                ))
                enriched_appids.append(result.appid)

        # Step 5: Sort and limit
        sort_key = _sort_keys.get(sort_by, _sort_keys["owners"])
        games.sort(key=sort_key, reverse=True)
        if limit is not None:
            games = games[:limit]
            enriched_appids = [g.appid for g in games]

        return SearchResult(
            fetched_at=datetime.now(timezone.utc),
            cache_age_seconds=0,
            appids=enriched_appids,
            tag=genre,
            total_found=total_count,
            games=games,
            sort_by=sort_by,
            normalized_tag=genre,
        )

    @mcp.tool()
    async def search_genre(genre: str, limit: int = 10, sort_by: str = "owners") -> str:
        """Search Steam games by genre/tag and return enriched per-game summaries.

        Returns per-game data (name, owners, CCU, price, review score, playtime) alongside AppIDs.
        Fields come from SteamSpy bulk endpoint - for per-game tag weights, use fetch_engagement(appid).

        For niche tags (<5000 games), applies cross-validation to filter false positives.
        For broad tags (>=5000 games), uses bulk data as-is for coverage.

        Args:
            genre: Steam tag to search (e.g., "Roguelike", "Action", "RPG")
            limit: Maximum number of results (1-10000, default 10). Use 0 to return all matching games.
            sort_by: Sort field - "owners" (default), "ccu", "price", "review_score"

        Returns:
            JSON string with enriched game summaries (games list) and AppIDs (for backward compatibility)
        """
        # Input validation
        if not genre or not genre.strip():
            return json.dumps({"error": "genre is required", "error_type": "validation"})

        # Handle 0 as "return all"
        return_all = (limit == 0)

        # Validate limit if not returning all
        if not return_all and not 1 <= limit <= 10000:
            return json.dumps({"error": "limit must be between 1 and 10000, or 0 for all results", "error_type": "validation"})

        # Validate sort_by
        valid_sort_fields = ["owners", "ccu", "price", "review_score"]
        if sort_by not in valid_sort_fields:
            return json.dumps({"error": f"sort_by must be one of: {', '.join(valid_sort_fields)}", "error_type": "validation"})

        limit_value = None if return_all else limit

        limit_str = "all" if return_all else str(limit)
        logger.info("Searching for genre: %s, limit: %s, sort_by: %s", genre, limit_str, sort_by)
        result = await _steamspy.search_by_tag(genre.strip(), limit_value, sort_by)

        # Fallback to Steam Store if SteamSpy fails or returns empty
        needs_fallback = False
        if isinstance(result, APIError):
            needs_fallback = True
        elif isinstance(result, SearchResult) and result.total_found == 0 and len(result.games) == 0:
            needs_fallback = True

        if needs_fallback:
            logger.info(f"SteamSpy tag search failed for '{genre}', falling back to Steam Store")
            result = await _steam_store_fallback_search(genre.strip(), limit_value, sort_by)

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
        - playtime_engagement: median_forever / average_forever (distribution skew; <1 means long-tail outliers inflate average)
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

    @mcp.tool()
    async def aggregate_engagement(tag: str = "", appids: str = "", sort_by: str = "owners") -> str:
        """Aggregate engagement metrics across all games in a genre/tag or custom AppID list.

        Provides statistical distribution (mean, median, p25, p75, min, max) for CCU, owners,
        playtime, and review scores across the genre. Includes both market-weighted and per-game
        average metric calculations for different analytical perspectives.

        Use tag for quick genre overview (1 API call, all games in tag).
        Use appids for custom game selections (concurrent calls, rate-limited).

        Args:
            tag: Steam tag name (e.g., "Roguelike", "Indie"). Mutually exclusive with appids.
            appids: Comma-separated AppIDs (e.g., "646570,2379780,247080"). Mutually exclusive with tag.
            sort_by: Sort per-game list by: "owners" (default), "ccu", "reviews", "playtime"

        Returns:
            JSON string with per-metric stats, market-weighted and per-game ratios, per-game breakdown, coverage info
        """
        # Validate: exactly one of tag or appids must be provided
        tag = tag.strip()
        appids_str = appids.strip()

        if not tag and not appids_str:
            return json.dumps({"error": "Either tag or appids must be provided", "error_type": "validation"})

        if tag and appids_str:
            return json.dumps({"error": "Provide either tag or appids, not both", "error_type": "validation"})

        # Parse AppID list if provided
        appid_list = None
        if appids_str:
            try:
                appid_list = [int(a.strip()) for a in appids_str.split(",") if a.strip()]
                if not appid_list:
                    return json.dumps({"error": "appids list is empty", "error_type": "validation"})
                if any(a <= 0 for a in appid_list):
                    return json.dumps({"error": "All appids must be positive", "error_type": "validation"})
            except ValueError:
                return json.dumps({"error": "appids must be comma-separated integers", "error_type": "validation"})

        logger.info("Aggregating engagement for tag=%s, appids=%s, sort_by=%s", tag or "N/A", appids_str or "N/A", sort_by)
        result = await _steamspy.aggregate_engagement(
            tag=tag if tag else None,
            appids=appid_list,
            sort_by=sort_by
        )

        # Fallback to Steam Store for tag-based aggregation when SteamSpy fails
        if isinstance(result, APIError) and tag:
            logger.info(f"SteamSpy aggregation failed for tag '{tag}', falling back to Steam Store")
            tag_map = await _steam_store.get_tag_map()
            if not isinstance(tag_map, APIError):
                tag_id = tag_map.get(tag.lower())
                if tag_id is not None:
                    # Get a sample of AppIDs from Steam Store for aggregation
                    search_result = await _steam_store.search_by_tag_ids([tag_id], count=50)
                    if not isinstance(search_result, APIError):
                        total_count, fallback_appids = search_result
                        if fallback_appids:
                            logger.info(f"Steam Store fallback: aggregating {len(fallback_appids)} games for tag '{tag}' (of {total_count} total)")
                            result = await _steamspy.aggregate_engagement(
                                appids=fallback_appids,
                                sort_by=sort_by
                            )

        if isinstance(result, APIError):
            return json.dumps(result.model_dump())

        return json.dumps(result.model_dump())
