"""MCP tool definitions for Steam API."""

import asyncio
import json
from datetime import datetime, timezone
from mcp.server.fastmcp import FastMCP, Image

from src.http_client import CachedAPIClient
from src.cache import TTLCache
from src.rate_limiter import HostRateLimiter
from src.steam_api import SteamSpyClient, SteamStoreClient, GamalyticClient, SteamReviewClient
from src.schemas import APIError, SearchResult, GameSummary, EngagementData
from src.logging_config import get_logger
from src.market_tools import (
    analyze_market_single,
    evaluate_game_analysis,
    compare_markets_analysis,
    _fetch_game_list_steamspy,
    increment_gamalytic_counter,
)

logger = get_logger(__name__)

# Module-level clients (initialized in register_tools)
_http_client: CachedAPIClient | None = None
_steamspy: SteamSpyClient | None = None
_steam_store: SteamStoreClient | None = None
_gamalytic: GamalyticClient | None = None
_review_client: SteamReviewClient | None = None

# Semaphore for review fetching concurrency control
_review_semaphore = asyncio.Semaphore(5)


def _validate_game_tags(
    searched_tags: list[str], game_tags: dict[str, int], top_n: int = 20
) -> tuple[bool | None, list[str]]:
    """Cross-validate searched tags against a game's SteamSpy tag weights.

    Args:
        searched_tags: Tags the user searched for
        game_tags: Tag name -> vote weight from SteamSpy per-game data
        top_n: Number of top tags to consider (by weight descending)

    Returns:
        (tag_matches, tags_missing):
        - (None, []) if no tag data available
        - (True, []) if all searched tags found in top N
        - (False, [missing_tags]) if some searched tags not in top N
    """
    if not game_tags:
        return (None, [])

    # Sort tags by weight descending, take top N
    sorted_tags = sorted(game_tags.keys(), key=lambda t: game_tags[t], reverse=True)
    top_tags_lower = {t.lower() for t in sorted_tags[:top_n]}

    missing = [tag for tag in searched_tags if tag.lower() not in top_tags_lower]

    if missing:
        return (False, missing)
    return (True, [])


def register_tools(mcp: FastMCP):
    """Register all MCP tools with the server."""
    global _http_client, _steamspy, _steam_store, _gamalytic, _review_client

    # Initialize shared infrastructure with per-host rate limiting and TTL cache
    cache = TTLCache(default_ttl=3600)  # 1 hour default
    rate_limiter = HostRateLimiter()
    _http_client = CachedAPIClient(cache=cache, rate_limiter=rate_limiter)
    _steamspy = SteamSpyClient(_http_client)
    _steam_store = SteamStoreClient(_http_client)
    _gamalytic = GamalyticClient(_http_client)
    _review_client = SteamReviewClient(_http_client)

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
                data_source="steam_store",
            )

        # Step 3: Enrich with SteamSpy per-game data (concurrent, rate-limited)
        logger.info(f"Steam Store fallback: enriching {len(appids)} games for tag '{genre}'")
        tasks = [_steamspy.get_engagement_data(appid) for appid in appids]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Step 4: Build GameSummary from EngagementData with tag cross-validation
        searched_tags = [genre]
        games = []
        enriched_appids = []
        for result in results:
            if isinstance(result, EngagementData):
                tag_matches, tags_missing = _validate_game_tags(searched_tags, result.tags)
                games.append(GameSummary(
                    appid=result.appid,
                    name=result.name,
                    developer=result.developer,
                    publisher=result.publisher,
                    owners_min=result.owners_min,
                    owners_max=result.owners_max,
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
                    tag_matches=tag_matches,
                    tags_missing=tags_missing,
                ))
                enriched_appids.append(result.appid)

        # Step 5: Sort and limit
        sort_key = _sort_keys.get(sort_by, _sort_keys["owners"])
        games.sort(key=sort_key, reverse=True)
        if limit is not None:
            games = games[:limit]
            enriched_appids = [g.appid for g in games]

        # Step 6: Build cross-validation summary
        validated = sum(1 for g in games if g.tag_matches is not None)
        matched = sum(1 for g in games if g.tag_matches is True)
        flagged = sum(1 for g in games if g.tag_matches is False)
        no_data = sum(1 for g in games if g.tag_matches is None)
        cross_validation = {
            "validated": validated,
            "matched": matched,
            "flagged": flagged,
            "no_data": no_data,
            "searched_tags": searched_tags,
        }

        return SearchResult(
            fetched_at=datetime.now(timezone.utc),
            cache_age_seconds=0,
            appids=enriched_appids,
            tag=genre,
            total_found=total_count,
            games=games,
            sort_by=sort_by,
            normalized_tag=genre,
            data_source="steam_store",
            cross_validation=cross_validation,
        )

    async def _multi_tag_search(
        tags: list[str], limit: int | None, sort_by: str
    ) -> SearchResult | APIError:
        """Search for games matching ALL specified tags (intersection).

        Resolves each tag name to a Steam tag ID, then uses Steam Store's
        multi-tag search endpoint. Enriches results with SteamSpy per-game data.

        Args:
            tags: List of tag names (at least 2)
            limit: Max results (None for all)
            sort_by: Sort field

        Returns:
            SearchResult with data_source="steam_store", or APIError
        """
        # Step 1: Resolve all tag names to IDs
        tag_map = await _steam_store.get_tag_map()
        if isinstance(tag_map, APIError):
            return tag_map

        tag_ids = []
        missing_tags = []
        for tag in tags:
            tag_id = tag_map.get(tag.lower())
            if tag_id is None:
                missing_tags.append(tag)
            else:
                tag_ids.append(tag_id)

        if missing_tags:
            return APIError(
                error_code=404,
                error_type="tag_not_found",
                message=f"Tag(s) not found: {', '.join(missing_tags)} ({len(tag_map)} tags available)"
            )

        # Step 2: Search Steam Store with all tag IDs (intersection)
        request_count = min((limit or 50) * 2, 100)
        search_result = await _steam_store.search_by_tag_ids(tag_ids, count=request_count)
        if isinstance(search_result, APIError):
            return search_result

        total_count, appids = search_result
        combined_tag = ", ".join(tags)

        if not appids:
            return SearchResult(
                fetched_at=datetime.now(timezone.utc),
                cache_age_seconds=0,
                appids=[],
                tag=combined_tag,
                total_found=0,
                games=[],
                sort_by=sort_by,
                normalized_tag=combined_tag,
                data_source="steam_store",
            )

        # Step 3: Enrich with SteamSpy per-game data
        logger.info(f"Multi-tag search: enriching {len(appids)} games for tags {tags}")
        tasks = [_steamspy.get_engagement_data(appid) for appid in appids]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Step 4: Build GameSummary from EngagementData with tag cross-validation
        games = []
        enriched_appids = []
        for result in results:
            if isinstance(result, EngagementData):
                tag_matches, tags_missing = _validate_game_tags(tags, result.tags)
                games.append(GameSummary(
                    appid=result.appid,
                    name=result.name,
                    developer=result.developer,
                    publisher=result.publisher,
                    owners_min=result.owners_min,
                    owners_max=result.owners_max,
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
                    tag_matches=tag_matches,
                    tags_missing=tags_missing,
                ))
                enriched_appids.append(result.appid)

        # Step 5: Sort and limit
        sort_key = _sort_keys.get(sort_by, _sort_keys["owners"])
        games.sort(key=sort_key, reverse=True)
        if limit is not None:
            games = games[:limit]
            enriched_appids = [g.appid for g in games]

        # Step 6: Build cross-validation summary
        validated = sum(1 for g in games if g.tag_matches is not None)
        matched = sum(1 for g in games if g.tag_matches is True)
        flagged = sum(1 for g in games if g.tag_matches is False)
        no_data = sum(1 for g in games if g.tag_matches is None)
        cross_validation = {
            "validated": validated,
            "matched": matched,
            "flagged": flagged,
            "no_data": no_data,
            "searched_tags": tags,
        }

        return SearchResult(
            fetched_at=datetime.now(timezone.utc),
            cache_age_seconds=0,
            appids=enriched_appids,
            tag=combined_tag,
            total_found=total_count,
            games=games,
            sort_by=sort_by,
            normalized_tag=combined_tag,
            data_source="steam_store",
            cross_validation=cross_validation,
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
                   Supports comma-separated tags for intersection search (e.g., "Visual Novel, Choices Matter")
                   Multi-tag searches use Steam Store and return games matching ALL specified tags.
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

        # Parse tags — comma-separated for multi-tag intersection
        tags = [t.strip() for t in genre.split(",") if t.strip()]
        if not tags:
            return json.dumps({"error": "genre is required", "error_type": "validation"})

        # Multi-tag: always use Steam Store (SteamSpy doesn't support intersection)
        if len(tags) > 1:
            logger.info("Multi-tag search: %s, limit: %s, sort_by: %s", tags, limit_value or "all", sort_by)
            result = await _multi_tag_search(tags, limit_value, sort_by)
            if isinstance(result, APIError):
                return json.dumps(result.model_dump())
            return json.dumps(result.model_dump())

        # Single tag: try SteamSpy first, then fallback
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

        # Set data_source if not already set (fallback sets it, SteamSpy path doesn't)
        if result.data_source is None:
            result.data_source = "steamspy"

        return json.dumps(result.model_dump())

    @mcp.tool()
    async def fetch_metadata(appid: int) -> str:
        """Fetch metadata for a Steam game by AppID.

        Returns comprehensive game details from the Steam Store including identification,
        pricing, platform support, release info, reviews, categories, media assets, DLC,
        and content information.

        Extended fields (Phase 8): achievement_count, ratings (regional: ESRB, PEGI, USK, etc.),
        supported_languages (structured list with full_audio indicator), developer_website,
        pc_requirements_min/rec (HTML-stripped plain text), controller_support ("full"/"partial"/None).

        Args:
            appid: Steam AppID (e.g., 646570 for Slay the Spire)

        Returns:
            JSON string with game metadata including name, tags, developer, description,
            and all extended Steam Store fields
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
    async def fetch_commercial(appid: int, detail_level: str = "full") -> str:
        """Fetch commercial data (pricing, revenue estimates, and extended analytics) for a Steam game.

        Returns revenue as a range (min-max) with confidence level and data source.
        Uses Gamalytic API as primary source, falls back to review-based estimation.
        Includes triangulation warning when data sources disagree significantly.

        Extended fields (Phase 8): copies_sold, followers, accuracy, history array
        (daily snapshots with revenue/reviews/players trends), country_data (top 10 markets),
        audience_overlap and also_played (10 competitor entries each), estimate_details
        (3 independent revenue models), gamalytic_dlc, early access info, and player count.

        Overlapping fields prefixed with gamalytic_ (gamalytic_owners, gamalytic_players,
        gamalytic_reviews) for cross-validation with SteamSpy data.

        Args:
            appid: Steam AppID (e.g., 646570 for Slay the Spire)
            detail_level: "full" (default) returns all fields; "summary" returns core fields only
                         (revenue, copies_sold, price, accuracy, review_score, followers, top-3 countries).
                         Summary mode excludes: history array, competitor arrays, DLC, estimation breakdown.

        Returns:
            JSON string with revenue range, confidence, source, extended Gamalytic fields,
            and optional triangulation warning
        """
        if appid <= 0:
            return json.dumps({"error": "appid must be positive", "error_type": "validation"})

        if detail_level not in ("full", "summary"):
            return json.dumps({"error": "detail_level must be 'full' or 'summary'", "error_type": "validation"})

        logger.info("Fetching commercial data for AppID: %d, detail_level: %s", appid, detail_level)
        result = await _gamalytic.get_commercial_data(appid, detail_level=detail_level)

        if isinstance(result, APIError):
            return json.dumps(result.model_dump())

        return json.dumps(result.model_dump())

    @mcp.tool()
    async def fetch_commercial_batch(appids: str, detail_level: str = "summary") -> str:
        """Fetch commercial data for multiple Steam games in a single call.

        Accepts comma-separated AppIDs and returns an array of CommercialData results.
        Uses concurrent fetching with rate limiting (5 req/s for Gamalytic).
        Default detail_level is "summary" for batch to reduce response size.

        Args:
            appids: Comma-separated Steam AppIDs (e.g., "646570,2379780,247080")
            detail_level: "full" or "summary" (default "summary" for batch efficiency)

        Returns:
            JSON string with array of commercial data objects (one per AppID, same order as input).
            Failed AppIDs return error objects in the array instead of commercial data.
        """
        appids_str = appids.strip()
        if not appids_str:
            return json.dumps({"error": "appids is required", "error_type": "validation"})

        if detail_level not in ("full", "summary"):
            return json.dumps({"error": "detail_level must be 'full' or 'summary'", "error_type": "validation"})

        try:
            appid_list = [int(a.strip()) for a in appids_str.split(",") if a.strip()]
            if not appid_list:
                return json.dumps({"error": "appids list is empty", "error_type": "validation"})
            if any(a <= 0 for a in appid_list):
                return json.dumps({"error": "All appids must be positive", "error_type": "validation"})
        except ValueError:
            return json.dumps({"error": "appids must be comma-separated integers", "error_type": "validation"})

        logger.info("Batch fetching commercial data for %d AppIDs, detail_level: %s", len(appid_list), detail_level)
        results = await _gamalytic.get_commercial_batch(appid_list, detail_level=detail_level)

        return json.dumps([r.model_dump() for r in results])

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
        used_fallback = False
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
                            used_fallback = True

        if isinstance(result, APIError):
            return json.dumps(result.model_dump())

        result.data_source = "steam_store" if used_fallback else "steamspy"

        return json.dumps(result.model_dump())

    @mcp.tool()
    async def fetch_reviews(
        appid: int,
        limit: int = 1000,
        language: str = "english",
        review_type: str = "all",
        purchase_type: str = "all",
        detail_level: str = "full",
        filter_offtopic: bool = True,
        min_playtime: float = 0,
        date_range: str = "",
        platform: str = "all",
        sort: str = "helpful",
        cursor: str = "",
        include_dimensions: bool = True,
    ) -> str:
        """Fetch Steam review text with metadata and playtime distribution for any game.

        Returns individual reviews with sentiment, playtime, helpfulness scores, plus
        aggregate stats, playtime histogram, and sentiment timeline. Two detail levels:
        full (1000 reviews + all stats) or compact (3 snippets + stats only).

        Reviews are sorted by helpfulness (most helpful first) by default.
        Stats (histogram, sentiment) are computed from a separate recent-sort scan
        for statistical accuracy regardless of the review text sort/filter.

        Args:
            appid: Steam AppID (e.g., 646570 for Slay the Spire)
            limit: Number of reviews to fetch (default 1000, max 10000). Only affects review text, not stats.
            language: Language filter (default "english"). Use "all" for all languages.
                      Codes: english, schinese, tchinese, japanese, koreana, russian, german, french, spanish, etc.
            review_type: Filter by sentiment: "all" (default), "positive", "negative"
            purchase_type: Filter by purchase: "all" (default), "steam", "non_steam"
            detail_level: "full" (default) = reviews + all stats; "compact" = 3 snippets + stats only
            filter_offtopic: Filter review bombs (default True). Set False to include off-topic review activity.
            min_playtime: Minimum playtime at review in hours (default 0 = no filter)
            date_range: Date filter. Relative: "30d", "6m", "1y". Absolute: "2024-01-01,2024-06-30". Empty = no filter.
            platform: Platform filter: "all" (default), "windows", "mac", "linux", "steam_deck"
            sort: Sort order: "helpful" (default) or "recent"
            cursor: Pagination cursor from previous call. Pass cursor from meta.cursor to get next batch.
            include_dimensions: Compute dimension profile showing what reviewers focus on —
                art, music, story, gameplay, difficulty, UI/UX, performance, content value.
                Returns keyword mention rates per dimension. Default True.

        Returns:
            JSON with sections: reviews/snippets, aggregates, histogram, sentiment, yearly, meta, query_params,
            dimension_profile (keyword mention rates across 8 player-concern dimensions).
            On first call: all sections populated. On cursor continuation: reviews only (no aggregates/stats).
        """
        # Input validation
        if appid <= 0:
            return json.dumps({"error": "appid must be positive", "error_type": "validation"})

        if limit < 1 or limit > 10000:
            return json.dumps({"error": "limit must be between 1 and 10000", "error_type": "validation"})

        if detail_level not in ("full", "compact"):
            return json.dumps({"error": "detail_level must be 'full' or 'compact'", "error_type": "validation"})

        if review_type not in ("all", "positive", "negative"):
            return json.dumps({"error": "review_type must be 'all', 'positive', or 'negative'", "error_type": "validation"})

        if purchase_type not in ("all", "steam", "non_steam"):
            return json.dumps({"error": "purchase_type must be 'all', 'steam', or 'non_steam'", "error_type": "validation"})

        if sort not in ("helpful", "recent"):
            return json.dumps({"error": "sort must be 'helpful' or 'recent'", "error_type": "validation"})

        valid_platforms = ("all", "windows", "mac", "linux", "steam_deck")
        if platform not in valid_platforms:
            return json.dumps({"error": f"platform must be one of: {', '.join(valid_platforms)}", "error_type": "validation"})

        logger.info("Fetching reviews for AppID: %d, limit: %d, language: %s, detail: %s", appid, limit, language, detail_level)

        async with _review_semaphore:
            result = await _review_client.fetch_reviews(
                appid=appid,
                limit=limit,
                language=language,
                review_type=review_type,
                purchase_type=purchase_type,
                detail_level=detail_level,
                filter_offtopic=filter_offtopic,
                min_playtime=min_playtime if min_playtime > 0 else None,
                date_range=date_range if date_range else None,
                platform=platform,
                sort=sort,
                cursor=cursor if cursor else "",
                include_dimensions=include_dimensions,
            )

        return json.dumps(result.model_dump(), default=str)

    # --- Phase 9: Market Analysis Tools ---

    @mcp.tool()
    async def analyze_market(
        genre: str = "",
        appids: str = "",
        metrics: str = "",
        exclude: str = "",
        top_n: int = 10,
        include_raw: bool = False,
        market_label: str = "",
    ) -> str:
        """Compute full market analytics for a Steam genre or custom game list.

        Returns concentration, temporal trends, price brackets, tag multipliers,
        score-revenue correlation, publisher analysis, release timing, sub-genres,
        competitive density, health scores, and top games in a single response.

        Enriches with Gamalytic revenue data automatically via two-tier enrichment
        (bulk for tag-based, per-game for appid-based). Revenue-significant games get
        deep enrichment with full commercial data and SteamSpy tag weights.

        Args:
            genre: Steam tag (e.g., "Roguelike") or comma-separated tags for intersection
                   (e.g., "Roguelike, Deckbuilder"). Mutually exclusive with appids.
            appids: Comma-separated Steam AppIDs for custom game list. Mutually exclusive with genre.
            metrics: Comma-separated metric names to compute (allowlist).
                     Valid: concentration, temporal_trends, price_brackets, success_rates,
                     tag_multipliers, score_revenue, publisher_analysis, release_timing,
                     sub_genres, competitive_density. Empty = compute all available.
            exclude: Comma-separated metric names to skip (blocklist). Applied after metrics.
            top_n: Number of top games to include (default 10). Set higher for full leaderboard.
            include_raw: If true, embed full game-level data array in response.
            market_label: Custom label for this market (auto-generated if empty).

        Returns:
            JSON with flat analytics structure: total_games, coverage, each metric result at
            top level, health_scores, top_games, skipped_metrics, methodology, compute_time,
            enrichment (Gamalytic enrichment stats).
        """
        # Input validation
        genre = genre.strip()
        appids_str = appids.strip()

        if not genre and not appids_str:
            return json.dumps({"error": "Either genre or appids must be provided", "error_type": "validation"})

        if genre and appids_str:
            return json.dumps({"error": "Provide either genre or appids, not both", "error_type": "validation"})

        # Parse metrics/exclude
        metrics_list = [m.strip() for m in metrics.split(",") if m.strip()] if metrics.strip() else None
        exclude_list = [m.strip() for m in exclude.split(",") if m.strip()] if exclude.strip() else None

        # Parse tags
        tags = [t.strip() for t in genre.split(",") if t.strip()] if genre else []

        # Parse appid list
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

        logger.info(
            "analyze_market: genre=%s, appids=%d, top_n=%d",
            genre or "N/A",
            len(appid_list or []),
            top_n,
        )

        try:
            result = await analyze_market_single(
                steamspy=_steamspy,
                steam_store=_steam_store,
                gamalytic=_gamalytic,
                tags=tags,
                appids=appid_list,
                metrics=metrics_list,
                exclude=exclude_list,
                top_n=top_n,
                include_raw=include_raw,
                market_label=market_label,
            )
            return json.dumps(result, default=str)
        except Exception as e:
            logger.error("analyze_market failed: %s", e)
            return json.dumps({"error": str(e), "error_type": "analysis_error"})

    @mcp.tool()
    async def evaluate_game(
        appid: int = 0,
        genre: str = "",
        genre_appids: str = "",
        price_tier: str = "",
    ) -> str:
        """Evaluate a Steam game against genre benchmarks with percentile rankings.

        Returns percentile rankings for revenue, review score, playtime, CCU,
        followers, copies sold, and review count. Identifies strengths and weaknesses
        (metrics >1 standard deviation from genre mean). Computes opportunity score
        (position + potential) calibrated to tiers: 90-100 exceptional, 75-89 strong,
        60-74 moderate, 40-59 neutral, 20-39 competitive risk, 0-19 significant disadvantage.

        Also provides: competitor identification (3-5 similar games), tag insights
        (revenue multiplier per tag), genre fit analysis, pricing context (where game
        sits on genre's price-revenue curve), review sentiment summary, and historical
        performance trends.

        Args:
            appid: Steam AppID of the game to evaluate (required, must be positive).
            genre: Genre to evaluate against. Single tag or comma-separated for intersection
                   (e.g., "Roguelike, Deckbuilder"). Mutually exclusive with genre_appids.
            genre_appids: Comma-separated Steam AppIDs to use as the baseline game list.
                          Alternative to genre for custom baseline lists.
                          Mutually exclusive with genre.
            price_tier: Optional price tier filter for percentile comparison.
                        Values: "f2p", "0.01_4.99", "5_9.99", "10_14.99", "15_19.99",
                        "20_29.99", "30_plus". Empty = full genre (default).

        Returns:
            JSON with percentiles, strengths, weaknesses, opportunity_score (with position
            and potential breakdown), competitors, tag_insights, genre_fit, pricing_context,
            review_sentiment, historical_performance, genre_baseline.
        """
        if appid <= 0:
            return json.dumps({"error": "appid must be positive", "error_type": "validation"})

        genre = genre.strip()
        genre_appids_str = genre_appids.strip()

        if not genre and not genre_appids_str:
            return json.dumps({"error": "Either genre or genre_appids must be provided", "error_type": "validation"})

        if genre and genre_appids_str:
            return json.dumps({"error": "Provide either genre or genre_appids, not both", "error_type": "validation"})

        genre_tags = [t.strip() for t in genre.split(",") if t.strip()] if genre else []

        # Parse genre_appids if provided
        genre_appid_list: list[int] | None = None
        if genre_appids_str:
            try:
                genre_appid_list = [int(a.strip()) for a in genre_appids_str.split(",") if a.strip()]
                if not genre_appid_list:
                    return json.dumps({"error": "genre_appids list is empty", "error_type": "validation"})
                if any(a <= 0 for a in genre_appid_list):
                    return json.dumps({"error": "All genre_appids must be positive", "error_type": "validation"})
            except ValueError:
                return json.dumps({"error": "genre_appids must be comma-separated integers", "error_type": "validation"})

        logger.info("evaluate_game: appid=%d, genre=%s, genre_appids=%d, price_tier=%s",
                    appid, genre or "N/A", len(genre_appid_list or []), price_tier or "all")

        try:
            # Step 1: Get genre baseline via analyze_market_single, capturing enriched games list
            genre_result, genre_games = await analyze_market_single(
                steamspy=_steamspy,
                steam_store=_steam_store,
                gamalytic=_gamalytic,
                tags=genre_tags,
                appids=genre_appid_list,
                metrics=None,
                exclude=None,
                top_n=10,
                include_raw=False,
                market_label="",
                skip_min_check=bool(genre_appid_list),
                return_games=True,
            )

            if "error" in genre_result:
                return json.dumps(
                    {"error": f"Genre analysis failed: {genre_result['error']}", "error_type": "genre_error"}
                )

            # Step 2: Get game data from all sources
            game_commercial = await _gamalytic.get_commercial_data(appid, detail_level="full")
            game_engagement = await _steamspy.get_engagement_data(appid)
            game_metadata = await _steam_store.get_app_details(appid)

            # Build unified game dict
            game_data: dict = {"appid": appid}
            if not isinstance(game_engagement, APIError):
                game_data.update({
                    "name": game_engagement.name,
                    "review_score": game_engagement.review_score,
                    "owners": game_engagement.owners_midpoint,
                    "ccu": game_engagement.ccu,
                    "median_forever": game_engagement.median_forever,
                    "price": game_engagement.price,
                    "developer": game_engagement.developer,
                    "publisher": game_engagement.publisher,
                    "positive": game_engagement.positive,
                    "negative": game_engagement.negative,
                    "tags": game_engagement.tags,
                })
            if not isinstance(game_commercial, APIError):
                game_data.update({
                    "revenue": (
                        game_commercial.gamalytic_revenue
                        or (
                            (game_commercial.revenue_min + game_commercial.revenue_max) / 2
                            if (game_commercial.revenue_min is not None and game_commercial.revenue_max is not None)
                            else None
                        )
                    ),
                    "followers": game_commercial.followers,
                    "copies_sold": game_commercial.copies_sold,
                })
                if not game_data.get("name"):
                    game_data["name"] = game_commercial.name
                if not game_data.get("review_score") and game_commercial.review_score is not None:
                    game_data["review_score"] = game_commercial.review_score
                if not game_data.get("owners") and game_commercial.gamalytic_owners is not None:
                    game_data["owners"] = game_commercial.gamalytic_owners
            if not isinstance(game_metadata, APIError):
                game_data.setdefault("name", game_metadata.name)
                game_data.setdefault("price", game_metadata.price)
                game_data["release_date"] = game_metadata.release_date

            if not game_data.get("name"):
                return json.dumps(
                    {"error": f"Game AppID {appid} not found or has no data", "error_type": "not_found"}
                )

            # genre_games is populated from Step 1's enriched game list (Gamalytic-enriched)
            if not genre_games:
                genre_games = []

            # Step 2b: Find target game in enriched genre data (avoids cache pollution from enrichment phase)
            # During Step 1, analyze_market_single runs Tier 1 bulk + Tier 1.5 + Tier 2 enrichment.
            # The Tier 1 bulk fetch caches a low bulk revenue for the target game (e.g., $7.3M for StS).
            # Step 2's get_commercial_data call then hits that stale cache instead of fetching per-game data.
            # Solution: use the post-enrichment revenue from the genre_games list, which reflects Tier 2 data.
            target_enriched = None
            if genre_games:
                target_enriched = next((g for g in genre_games if g.get("appid") == appid), None)

            if target_enriched:
                enriched_rev = target_enriched.get("revenue")
                current_rev = game_data.get("revenue")
                if enriched_rev is not None and (current_rev is None or enriched_rev > current_rev):
                    game_data["revenue"] = enriched_rev
                    if current_rev is not None:
                        logger.info(
                            "evaluate_game: using enriched genre revenue for appid %d: "
                            "$%.0f (was $%.0f from API cache)",
                            appid, enriched_rev, current_rev,
                        )
                    else:
                        logger.info(
                            "evaluate_game: using enriched genre revenue for appid %d: $%.0f",
                            appid, enriched_rev,
                        )
                # Also fill gaps from enriched data
                for field in ("copies_sold", "followers", "name", "review_score", "release_date"):
                    if target_enriched.get(field) is not None and not game_data.get(field):
                        game_data[field] = target_enriched[field]

            # Step 4: Optionally get review data (compact, non-blocking)
            reviews_data = None
            try:
                async with _review_semaphore:
                    reviews_result = await _review_client.fetch_reviews(
                        appid=appid,
                        limit=50,
                        language="english",
                        detail_level="compact",
                        filter_offtopic=True,
                    )
                reviews_data = reviews_result.model_dump()
            except Exception as e:
                logger.warning("Failed to fetch reviews for evaluate_game: %s", e)

            # Step 5: Run evaluation
            result = await evaluate_game_analysis(
                appid=appid,
                genre_tags=genre_tags,
                genre_games=genre_games,
                genre_analysis=genre_result,
                game_data=game_data,
                commercial_data=game_commercial if not isinstance(game_commercial, APIError) else None,
                reviews_data=reviews_data,
                price_tier=price_tier.strip() if price_tier.strip() else None,
            )
            # Phase 10: Propagate commercial data quality (PROV-03)
            if not isinstance(game_commercial, APIError):
                if game_commercial.api_warnings:
                    result.setdefault("warnings", []).extend(game_commercial.api_warnings)
                result["commercial_data_quality"] = game_commercial.data_quality
            else:
                result["commercial_data_quality"] = "unavailable"
            return json.dumps(result, default=str)
        except Exception as e:
            logger.error("evaluate_game failed: %s", e)
            return json.dumps({"error": str(e), "error_type": "evaluation_error"})

    @mcp.tool()
    async def compare_markets(
        markets: str = "",
    ) -> str:
        """Compare 2-4 Steam game markets side-by-side with deltas and rankings.

        Each market entry is a JSON object with: tag (string), appids (comma-separated), and optional label.
        Runs analyze_market internally for each market, then computes side-by-side
        deltas, overlap detection, health score comparison, and overall ranking.

        Always compares all metrics (no selection parameter). Each market embeds its
        complete analyze_market output. Warns when comparing very different-sized markets.

        Args:
            markets: JSON array of market definitions. Each object has:
                     - "tag": Steam tag string (e.g., "Roguelike") — supports comma-separated for intersection
                     - "appids": comma-separated AppIDs (alternative to tag)
                     - "label": optional custom label
                     Example: '[{"tag": "Roguelike"}, {"tag": "Deckbuilder"}]'
                     Example: '[{"tag": "Roguelike, Deckbuilder", "label": "Hybrid"}, {"appids": "646570,247080"}]'

        Returns:
            JSON with markets (each embedding full analyze_market output), deltas
            (side-by-side metric comparisons), overlap (shared games), health_score_comparison,
            overall_ranking (by avg health score), confidence_notes.
        """
        if not markets.strip():
            return json.dumps({"error": "markets parameter is required (JSON array)", "error_type": "validation"})

        try:
            market_defs = json.loads(markets)
        except json.JSONDecodeError as e:
            return json.dumps({"error": f"Invalid JSON in markets parameter: {e}", "error_type": "validation"})

        if not isinstance(market_defs, list):
            return json.dumps({"error": "markets must be a JSON array", "error_type": "validation"})

        if len(market_defs) < 2 or len(market_defs) > 4:
            return json.dumps(
                {"error": f"2-4 markets required, got {len(market_defs)}", "error_type": "validation"}
            )

        # Parse and validate each market definition
        market_inputs = []
        for i, m in enumerate(market_defs):
            if not isinstance(m, dict):
                return json.dumps(
                    {"error": f"Market {i + 1} must be a JSON object", "error_type": "validation"}
                )

            tag = m.get("tag", "").strip()
            appids_str = m.get("appids", "").strip()
            label = m.get("label", "").strip()

            if not tag and not appids_str:
                return json.dumps(
                    {"error": f"Market {i + 1}: either 'tag' or 'appids' required", "error_type": "validation"}
                )

            tags = [t.strip() for t in tag.split(",") if t.strip()] if tag else []
            appid_list = None
            if appids_str:
                try:
                    appid_list = [int(a.strip()) for a in appids_str.split(",") if a.strip()]
                except ValueError:
                    return json.dumps(
                        {
                            "error": f"Market {i + 1}: appids must be comma-separated integers",
                            "error_type": "validation",
                        }
                    )

            market_inputs.append({
                "label": label or tag or f"Custom ({len(appid_list or [])} games)",
                "tags": tags,
                "appids": appid_list or [],
            })

        logger.info("compare_markets: %d markets", len(market_inputs))

        try:
            result = await compare_markets_analysis(
                market_inputs=market_inputs,
                steamspy=_steamspy,
                steam_store=_steam_store,
                gamalytic=_gamalytic,
            )
            return json.dumps(result, default=str)
        except Exception as e:
            logger.error("compare_markets failed: %s", e)
            return json.dumps({"error": str(e), "error_type": "comparison_error"})

    # Image cache TTL: 24 hours (Steam CDN images rarely change)
    _IMAGE_CACHE_TTL = 86400

    @mcp.tool()
    async def fetch_game_art(appid: int, include_screenshots: bool = True, max_screenshots: int = 4) -> list:
        """Fetch header art and screenshots for a Steam game as viewable images.

        Downloads the header capsule image and optionally up to 10 screenshots
        from Steam's CDN. Returns them as image content blocks that Claude can
        see directly, alongside a JSON context block with game metadata.

        Args:
            appid: Steam AppID (e.g., 646570 for Slay the Spire)
            include_screenshots: Whether to include screenshots (default True)
            max_screenshots: Maximum screenshots to download (1-10, default 4)

        Returns:
            List of [json_context, header_image, screenshot_1, ...] content blocks
        """
        if appid <= 0:
            return [json.dumps({"error": "appid must be positive", "error_type": "validation"})]

        if not 1 <= max_screenshots <= 10:
            return [json.dumps({"error": "max_screenshots must be between 1 and 10", "error_type": "validation"})]

        logger.info("Fetching game art for AppID: %d", appid)
        result = await _steam_store.get_app_details(appid)

        if isinstance(result, APIError):
            return [json.dumps(result.model_dump())]

        # Build context metadata
        context = {
            "appid": result.appid,
            "name": result.name,
            "developer": result.developer,
            "publisher": result.publisher,
            "tags": result.tags[:10],
            "genres": result.genres,
            "release_date": result.release_date,
            "screenshots_available": result.screenshots_count,
        }

        content_blocks: list = []
        images_failed: list[str] = []

        # Download header image
        header_url = result.header_image
        if header_url:
            try:
                header_bytes = await _http_client.get_bytes(header_url, cache_ttl=_IMAGE_CACHE_TTL)
                content_blocks.append(Image(data=header_bytes, format="jpeg"))
            except Exception as e:
                logger.warning("Failed to download header image for %d: %s", appid, e)
                images_failed.append(f"header: {e}")

        # Download screenshots
        if include_screenshots and result.screenshots:
            screenshot_urls = result.screenshots[:max_screenshots]
            # Use thumbnail size (600x338) instead of full 1920x1080
            thumbnail_urls = [
                url.replace(".1920x1080.", ".600x338.") for url in screenshot_urls
            ]

            async def _download_screenshot(url: str, idx: int) -> tuple[int, Image | None, str | None]:
                try:
                    data = await _http_client.get_bytes(url, cache_ttl=_IMAGE_CACHE_TTL)
                    return (idx, Image(data=data, format="jpeg"), None)
                except Exception as e:
                    return (idx, None, f"screenshot_{idx}: {e}")

            tasks = [_download_screenshot(url, i) for i, url in enumerate(thumbnail_urls)]
            results = await asyncio.gather(*tasks)

            # Maintain order
            for idx, img, err in sorted(results, key=lambda r: r[0]):
                if img is not None:
                    content_blocks.append(img)
                if err is not None:
                    images_failed.append(err)

        context["images_returned"] = len(content_blocks)
        if images_failed:
            context["images_failed"] = images_failed

        return [json.dumps(context)] + content_blocks
