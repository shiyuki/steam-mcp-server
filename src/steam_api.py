"""Steam API clients for SteamSpy and Steam Store."""

import asyncio
import httpx
from datetime import datetime, timezone
from statistics import mean, median, quantiles
from src.http_client import CachedAPIClient
from src.schemas import (
    GameMetadata, SearchResult, CommercialData, EngagementData, APIError,
    AggregateEngagementData, MetricStats, GameEngagementSummary
)
from src.logging_config import get_logger

logger = get_logger(__name__)


# Tag normalization: Map common user-friendly tag names to SteamSpy's exact format
TAG_ALIASES: dict[str, str] = {
    "roguelike": "Rogue-like",
    "roguelite": "Rogue-lite",
    "roguevania": "Rogue-like",
    "metroidvania": "Metroidvania",
    "hack and slash": "Hack and Slash",
    "beat em up": "Beat 'em up",
    "shoot em up": "Shoot 'Em Up",
    "4x": "4X",
    "turn based": "Turn-Based",
    "turn based strategy": "Turn-Based Strategy",
    "turn based tactics": "Turn-Based Tactics",
    "real time strategy": "Real-Time Strategy",
    "real time tactics": "Real-Time Tactics",
    "co op": "Co-op",
    "local co op": "Local Co-Op",
    "sci fi": "Sci-fi",
    "choose your own adventure": "Choose Your Own Adventure",
    "city builder": "City Builder",
    "base building": "Base Building",
    "tower defense": "Tower Defense",
    "deck building": "Deckbuilder",
    "deckbuilding": "Deckbuilder",
    "bullet hell": "Bullet Hell",
    "souls like": "Souls-like",
    "soulslike": "Souls-like",
    "rogue like": "Rogue-like",
    "rogue lite": "Rogue-lite",
}


def normalize_tag(tag: str) -> str:
    """Normalize tag name to SteamSpy's expected format.

    Args:
        tag: User-provided tag name

    Returns:
        Normalized tag name (from alias map if found, otherwise original with whitespace stripped)
    """
    tag = tag.strip()
    normalized = TAG_ALIASES.get(tag.lower())
    return normalized if normalized else tag


# Bowling fallback detection: Known AppIDs in SteamSpy's default fallback set
# These are empirically verified AppIDs from SteamSpy's actual bowling fallback response
BOWLING_FALLBACK_APPIDS = {901583, 12210, 2990, 891040, 22230}

# Tags with fewer games than this threshold get cross-validated via appdetails;
# broad tags (>=5000) use bulk data as-is for coverage
TAG_CROSSVAL_THRESHOLD = 5000


class SteamSpyClient:
    """Client for SteamSpy API (tag-based game search)."""

    BASE_URL = "https://steamspy.com/api.php"

    def __init__(self, http_client: CachedAPIClient):
        """Initialize SteamSpy client with cached HTTP client.

        Args:
            http_client: CachedAPIClient instance for API requests
        """
        self.http = http_client

    def _detect_steamspy_fallback(self, data: dict, tag: str) -> bool:
        """Detect if SteamSpy returned its default bowling game fallback instead of actual tag results.

        SteamSpy returns a default ~70 bowling games dataset for unrecognized tags.
        Detection: Small result set (<100) containing known bowling game AppIDs.

        Args:
            data: SteamSpy API response dict (appid keys)
            tag: Tag that was queried

        Returns:
            True if bowling fallback detected, False otherwise
        """
        # Only check for small result sets (fallback is ~70 games)
        if len(data) >= 100:
            return False

        # Convert string AppID keys to integers
        appids_in_response = {int(appid) for appid in data.keys()}

        # Check if at least 2 known bowling game AppIDs are present
        bowling_matches = len(BOWLING_FALLBACK_APPIDS & appids_in_response)
        return bowling_matches >= 2

    async def search_by_tag(self, tag: str, limit: int | None = 10) -> SearchResult | APIError:
        """Search for games by tag using SteamSpy API.

        Args:
            tag: Tag to search for (e.g., "roguelike", "indie")
            limit: Maximum number of AppIDs to return (default: 10). Use None to return all results.

        Returns:
            SearchResult with AppID list, or APIError on failure
        """
        # Normalize tag before API call
        tag = normalize_tag(tag)

        try:
            # Use get_with_metadata for freshness tracking (1 hour cache TTL)
            data, fetched_at, cache_age = await self.http.get_with_metadata(
                self.BASE_URL,
                params={"request": "tag", "tag": tag},
                cache_ttl=3600
            )

            # Check for bowling fallback before processing
            if self._detect_steamspy_fallback(data, tag):
                logger.warning(f"SteamSpy returned bowling fallback for tag '{tag}' - tag may not exist")
                return APIError(
                    error_code=404,
                    error_type="tag_not_found",
                    message=f"Tag '{tag}' not recognized by SteamSpy. The API returned default results instead of matching games. Try a different tag name or check Steam's tag list."
                )

            # SteamSpy returns {appid: {game_data}, ...}
            all_appids = [int(appid) for appid in data.keys()]
            # If limit is None, return all AppIDs; otherwise slice to limit
            appids = all_appids if limit is None else all_appids[:limit]
            return SearchResult(
                appids=appids,
                tag=tag,
                total_found=len(data),
                fetched_at=fetched_at,
                cache_age_seconds=cache_age
            )
        except httpx.HTTPStatusError as e:
            status = e.response.status_code
            error_type = "rate_limit" if status == 429 else "api_error"
            return APIError(
                error_code=status,
                error_type=error_type,
                message=f"SteamSpy API error: {status}"
            )
        except Exception as e:
            logger.error(f"SteamSpy error: {e}")
            return APIError(
                error_code=502,
                error_type="api_error",
                message=str(e)
            )

    async def get_engagement_data(self, appid: int) -> EngagementData | APIError:
        """Fetch player engagement metrics for a specific game from SteamSpy.

        Args:
            appid: Steam AppID to fetch engagement data for

        Returns:
            EngagementData with CCU, owners, playtime, reviews, tags, and derived metrics, or APIError on failure
        """
        try:
            # Use get_with_metadata for freshness tracking (1 hour cache TTL)
            data, fetched_at, cache_age = await self.http.get_with_metadata(
                self.BASE_URL,
                params={"request": "appdetails", "appid": str(appid)},
                cache_ttl=3600
            )

            # Parse owner range: "20,000 .. 50,000" -> owners_min, owners_max, owners_midpoint
            owners_str = data.get("owners", "0 .. 0")
            owners_str = owners_str.replace(",", "")
            parts = owners_str.split(" .. ")
            if len(parts) == 2:
                try:
                    owners_min = int(parts[0])
                    owners_max = int(parts[1])
                except ValueError:
                    owners_min = 0
                    owners_max = 0
            else:
                owners_min = 0
                owners_max = 0
            owners_midpoint = (owners_min + owners_max) / 2

            # Get raw values
            ccu = data.get("ccu", 0)
            positive = data.get("positive", 0)
            negative = data.get("negative", 0)
            total_reviews = positive + negative

            # Convert playtime from minutes to hours
            average_forever = data.get("average_forever", 0) / 60.0
            average_2weeks = data.get("average_2weeks", 0) / 60.0
            median_forever = data.get("median_forever", 0) / 60.0
            median_2weeks = data.get("median_2weeks", 0) / 60.0

            # Convert price from cents to dollars
            price_str = data.get("price", "0")
            try:
                price = int(price_str) / 100.0
            except (ValueError, TypeError):
                price = 0.0

            # Parse tags dict (SteamSpy returns {"TagName": weight_int, ...})
            tags = data.get("tags", {})
            if not isinstance(tags, dict):
                tags = {}

            # Compute derived metrics with division-by-zero guards
            ccu_ratio = round(ccu / owners_midpoint * 100, 2) if owners_midpoint > 0 else None
            review_score = round(positive / total_reviews * 100, 2) if total_reviews > 0 else None
            playtime_engagement = round(median_forever / average_forever, 2) if average_forever > 0 else None
            activity_ratio = round(average_2weeks / average_forever * 100, 2) if average_forever > 0 else None

            # Apply context-aware quality flags
            # Pattern: if zero AND other data exists = "available" (real zero)
            #          if zero AND no other data = "zero_uncertain" (likely missing)
            has_other_data = owners_midpoint > 0 or total_reviews > 0

            if ccu == 0:
                ccu_quality = "available" if has_other_data else "zero_uncertain"
            else:
                ccu_quality = "available"

            if owners_midpoint == 0:
                owners_quality = "available" if (ccu > 0 or total_reviews > 0) else "zero_uncertain"
            else:
                owners_quality = "available"

            if average_forever == 0 and median_forever == 0:
                playtime_quality = "available" if has_other_data else "zero_uncertain"
            else:
                playtime_quality = "available"

            if total_reviews == 0:
                reviews_quality = "available" if (ccu > 0 or owners_midpoint > 0) else "zero_uncertain"
            else:
                reviews_quality = "available"

            return EngagementData(
                appid=appid,
                name=data.get("name", ""),
                developer=data.get("developer", ""),
                publisher=data.get("publisher", ""),
                ccu=ccu,
                owners_min=owners_min,
                owners_max=owners_max,
                owners_midpoint=owners_midpoint,
                average_forever=average_forever,
                average_2weeks=average_2weeks,
                median_forever=median_forever,
                median_2weeks=median_2weeks,
                positive=positive,
                negative=negative,
                total_reviews=total_reviews,
                price=price,
                score_rank=data.get("score_rank", ""),
                genre=data.get("genre", ""),
                languages=data.get("languages", ""),
                tags=tags,
                ccu_ratio=ccu_ratio,
                review_score=review_score,
                playtime_engagement=playtime_engagement,
                activity_ratio=activity_ratio,
                ccu_quality=ccu_quality,
                owners_quality=owners_quality,
                playtime_quality=playtime_quality,
                reviews_quality=reviews_quality,
                fetched_at=fetched_at,
                cache_age_seconds=cache_age
            )
        except httpx.HTTPStatusError as e:
            status = e.response.status_code
            error_type = "rate_limit" if status == 429 else "api_error"
            return APIError(
                error_code=status,
                error_type=error_type,
                message=f"SteamSpy API error: {status}"
            )
        except Exception as e:
            logger.error(f"SteamSpy engagement data error for AppID {appid}: {e}")
            return APIError(
                error_code=502,
                error_type="api_error",
                message=str(e)
            )

    def _parse_game_data(self, data: dict) -> dict:
        """Parse common fields from SteamSpy game data into standardized format.

        Args:
            data: Raw SteamSpy game data dict

        Returns:
            Parsed dict with standardized field names and types
        """
        # Parse owner range
        owners_str = data.get("owners", "0 .. 0")
        owners_str = owners_str.replace(",", "")
        parts = owners_str.split(" .. ")
        if len(parts) == 2:
            try:
                owners_min = int(parts[0])
                owners_max = int(parts[1])
            except ValueError:
                owners_min = 0
                owners_max = 0
        else:
            owners_min = 0
            owners_max = 0
        owners_midpoint = (owners_min + owners_max) / 2

        # Get raw values
        ccu = data.get("ccu", 0)
        positive = data.get("positive", 0)
        negative = data.get("negative", 0)

        # Convert playtime from minutes to hours
        average_forever = data.get("average_forever", 0) / 60.0
        average_2weeks = data.get("average_2weeks", 0) / 60.0

        # Convert price from cents to dollars
        price_str = data.get("price", "0")
        try:
            price = int(price_str) / 100.0
        except (ValueError, TypeError):
            price = 0.0

        # Compute review score if reviews exist
        total_reviews = positive + negative
        review_score = round(positive / total_reviews * 100, 2) if total_reviews > 0 else None

        return {
            "appid": int(data.get("appid", 0)),
            "name": data.get("name", ""),
            "ccu": ccu,
            "owners_midpoint": owners_midpoint,
            "average_forever": average_forever,
            "average_2weeks": average_2weeks,
            "positive": positive,
            "negative": negative,
            "review_score": review_score,
            "price": price
        }

    async def aggregate_engagement(
        self,
        tag: str | None = None,
        appids: list[int] | None = None,
        sort_by: str = "owners"
    ) -> AggregateEngagementData | APIError:
        """Aggregate engagement metrics across multiple games.

        Supports two input modes:
        1. Tag-based: Fetch all games in a tag (bulk endpoint, 1 API call)
        2. AppID-list: Fetch specific games (concurrent individual calls)

        Args:
            tag: Steam tag name for bulk query
            appids: List of AppIDs for custom selection
            sort_by: Sort field for per-game list ("owners", "ccu", "reviews", "playtime")

        Returns:
            AggregateEngagementData with per-metric stats, dual metric approaches, per-game breakdown, or APIError
        """
        games_list = []
        failures = []
        fetched_at = datetime.now(timezone.utc)
        cache_age = 0

        # Branch 1: Tag-based (bulk endpoint)
        if tag:
            # Normalize tag before API call
            tag = normalize_tag(tag)

            try:
                data, fetched_at, cache_age = await self.http.get_with_metadata(
                    self.BASE_URL,
                    params={"request": "tag", "tag": tag},
                    cache_ttl=3600
                )

                if not data:
                    return APIError(
                        error_code=404,
                        error_type="not_found",
                        message=f"No games found for tag: {tag}"
                    )

                # Check for bowling fallback before processing
                if self._detect_steamspy_fallback(data, tag):
                    logger.warning(f"SteamSpy returned bowling fallback for tag '{tag}' in aggregation")
                    return APIError(
                        error_code=404,
                        error_type="tag_not_found",
                        message=f"Tag '{tag}' not recognized by SteamSpy. The API returned default results instead of matching games. Try a different tag name or check Steam's tag list."
                    )

                if len(data) >= TAG_CROSSVAL_THRESHOLD:
                    # Broad tag: use bulk data as-is (cross-validation too aggressive for large tags)
                    for game_data in data.values():
                        parsed = self._parse_game_data(game_data)
                        games_list.append(GameEngagementSummary(**parsed))
                    logger.info(f"Broad tag '{tag}': {len(games_list)} games from bulk endpoint (>={TAG_CROSSVAL_THRESHOLD}, skipping cross-validation)")
                else:
                    # Niche tag: cross-validate via appdetails to filter irrelevant games
                    bulk_games = []
                    for game_data in data.values():
                        parsed = self._parse_game_data(game_data)
                        bulk_games.append(parsed)
                    bulk_games.sort(key=lambda g: g["owners_midpoint"], reverse=True)

                    # Validate all candidates via appdetails (which includes per-game tags)
                    candidates = bulk_games
                    candidate_appids = [g["appid"] for g in candidates]

                    # Fetch appdetails concurrently for tag validation
                    detail_tasks = [self.get_engagement_data(appid) for appid in candidate_appids]
                    results = await asyncio.gather(*detail_tasks, return_exceptions=True)

                    # Filter to games where searched tag is in their tag dict
                    tag_lower = tag.lower()
                    for appid, result in zip(candidate_appids, results):
                        if isinstance(result, Exception):
                            failures.append({"appid": appid, "reason": "exception"})
                        elif isinstance(result, APIError):
                            failures.append({"appid": appid, "reason": result.error_type})
                        elif isinstance(result, EngagementData):
                            game_tag_names_lower = {t.lower() for t in result.tags.keys()}
                            if tag_lower in game_tag_names_lower:
                                games_list.append(GameEngagementSummary(
                                    appid=result.appid,
                                    name=result.name,
                                    ccu=result.ccu,
                                    owners_midpoint=result.owners_midpoint,
                                    average_forever=result.average_forever,
                                    average_2weeks=result.average_2weeks,
                                    positive=result.positive,
                                    negative=result.negative,
                                    review_score=result.review_score,
                                    price=result.price,
                                    tags=result.tags
                                ))

                    logger.info(f"Tag validation: {len(games_list)}/{len(candidates)} games confirmed with tag '{tag}'")

            except httpx.HTTPStatusError as e:
                status = e.response.status_code
                error_type = "rate_limit" if status == 429 else "api_error"
                return APIError(
                    error_code=status,
                    error_type=error_type,
                    message=f"SteamSpy API error: {status}"
                )
            except Exception as e:
                logger.error(f"Tag aggregation error: {e}")
                return APIError(
                    error_code=502,
                    error_type="api_error",
                    message=str(e)
                )

        # Branch 2: AppID-list (concurrent individual calls)
        elif appids:
            tasks = [self.get_engagement_data(appid) for appid in appids]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            for appid, result in zip(appids, results):
                if isinstance(result, Exception):
                    failures.append({"appid": appid, "reason": "exception"})
                elif isinstance(result, APIError):
                    failures.append({"appid": appid, "reason": result.error_type})
                elif isinstance(result, EngagementData):
                    # Convert EngagementData to GameEngagementSummary
                    games_list.append(GameEngagementSummary(
                        appid=result.appid,
                        name=result.name,
                        ccu=result.ccu,
                        owners_midpoint=result.owners_midpoint,
                        average_forever=result.average_forever,
                        average_2weeks=result.average_2weeks,
                        positive=result.positive,
                        negative=result.negative,
                        review_score=result.review_score,
                        price=result.price,
                        tags=result.tags
                    ))

            if not games_list:
                return APIError(
                    error_code=404,
                    error_type="not_found",
                    message="No valid games retrieved from AppID list"
                )

        else:
            return APIError(
                error_code=400,
                error_type="validation",
                message="Either tag or appids must be provided"
            )

        # Compute statistics for each metric
        # For tag branch: games_total = validation scope (candidates checked)
        # For appid branch: games_total = all requested games (successes + failures)
        if tag:
            games_total = len(games_list) + len(failures)  # Total candidates validated
        else:
            games_total = len(games_list) + len(failures)
        games_analyzed = len(games_list)

        # Helper to compute stats for a metric
        def compute_metric_stats(values: list[float]) -> MetricStats | None:
            if len(values) < 2:
                return None
            return MetricStats(
                mean=round(mean(values), 2),
                median=round(median(values), 2),
                p25=round(quantiles(values, n=4)[0], 2),
                p75=round(quantiles(values, n=4)[2], 2),
                min=round(min(values), 2),
                max=round(max(values), 2),
                games_with_data=len(values)
            )

        # CCU stats (include zeros - real measurement)
        ccu_values = [g.ccu for g in games_list]
        ccu_stats = compute_metric_stats(ccu_values)

        # Owners stats (exclude zeros)
        owners_values = [g.owners_midpoint for g in games_list if g.owners_midpoint > 0]
        owners_stats = compute_metric_stats(owners_values)

        # Playtime stats (exclude zeros)
        playtime_values = [g.average_forever for g in games_list if g.average_forever > 0]
        playtime_stats = compute_metric_stats(playtime_values)

        # Review score stats (exclude None)
        review_score_values = [g.review_score for g in games_list if g.review_score is not None]
        review_score_stats = compute_metric_stats(review_score_values)

        # Compute dual metrics: market-weighted vs per-game average
        market_weighted = {}
        per_game_average = {}

        # CCU ratio
        total_ccu = sum(g.ccu for g in games_list)
        total_owners = sum(g.owners_midpoint for g in games_list)
        if total_owners > 0:
            market_weighted["ccu_ratio"] = round(total_ccu / total_owners * 100, 2)
        else:
            market_weighted["ccu_ratio"] = None

        # Per-game CCU ratio
        ccu_ratios = [
            g.ccu / g.owners_midpoint * 100
            for g in games_list
            if g.owners_midpoint > 0
        ]
        if ccu_ratios:
            per_game_average["ccu_ratio"] = round(mean(ccu_ratios), 2)
        else:
            per_game_average["ccu_ratio"] = None

        # Review score (market-weighted)
        total_positive = sum(g.positive for g in games_list)
        total_negative = sum(g.negative for g in games_list)
        total_reviews = total_positive + total_negative
        if total_reviews > 0:
            market_weighted["review_score"] = round(total_positive / total_reviews * 100, 2)
        else:
            market_weighted["review_score"] = None

        # Review score (per-game average)
        review_scores = [g.review_score for g in games_list if g.review_score is not None]
        if review_scores:
            per_game_average["review_score"] = round(mean(review_scores), 2)
        else:
            per_game_average["review_score"] = None

        # Activity ratio (market-weighted)
        total_avg_2weeks = sum(g.average_2weeks for g in games_list)
        total_avg_forever = sum(g.average_forever for g in games_list)
        if total_avg_forever > 0:
            market_weighted["activity_ratio"] = round(total_avg_2weeks / total_avg_forever * 100, 2)
        else:
            market_weighted["activity_ratio"] = None

        # Activity ratio (per-game average)
        activity_ratios = [
            g.average_2weeks / g.average_forever * 100
            for g in games_list
            if g.average_forever > 0
        ]
        if activity_ratios:
            per_game_average["activity_ratio"] = round(mean(activity_ratios), 2)
        else:
            per_game_average["activity_ratio"] = None

        # Sort per-game list
        sort_key_map = {
            "owners": lambda g: g.owners_midpoint,
            "ccu": lambda g: g.ccu,
            "reviews": lambda g: g.positive + g.negative,
            "playtime": lambda g: g.average_forever
        }
        sort_key = sort_key_map.get(sort_by, sort_key_map["owners"])
        games_list.sort(key=sort_key, reverse=True)

        return AggregateEngagementData(
            tag=tag,
            appids_requested=appids,
            games_total=games_total,
            games_analyzed=games_analyzed,
            ccu_stats=ccu_stats,
            owners_stats=owners_stats,
            playtime_stats=playtime_stats,
            review_score_stats=review_score_stats,
            market_weighted=market_weighted,
            per_game_average=per_game_average,
            games=games_list,
            failures=failures,
            sort_by=sort_by,
            fetched_at=fetched_at,
            cache_age_seconds=cache_age
        )


class SteamStoreClient:
    """Client for Steam Store API (game metadata fetching)."""

    BASE_URL = "https://store.steampowered.com/api"

    def __init__(self, http_client: CachedAPIClient):
        """Initialize Steam Store client with cached HTTP client.

        Args:
            http_client: CachedAPIClient instance for API requests
        """
        self.http = http_client

    async def get_app_details(self, appid: int) -> GameMetadata | APIError:
        """Fetch metadata for a specific AppID using Steam Store API.

        Args:
            appid: Steam AppID to fetch metadata for

        Returns:
            GameMetadata with game details, or APIError on failure
        """
        try:
            # Use get_with_metadata for freshness tracking (24 hour cache TTL)
            data, fetched_at, cache_age = await self.http.get_with_metadata(
                f"{self.BASE_URL}/appdetails",
                params={"appids": str(appid)},
                cache_ttl=86400
            )
            # Response: {"12345": {"success": true, "data": {...}}}
            app_data = data.get(str(appid), {})
            if not app_data.get("success"):
                return APIError(
                    error_code=404,
                    error_type="not_found",
                    message=f"AppID {appid} not found or unavailable"
                )

            details = app_data.get("data", {})
            # Extract tags from genres (Steam Store API uses genres, not tags)
            tags = [g.get("description", "") for g in details.get("genres", [])]
            # Get first developer or empty string
            developers = details.get("developers", [])
            developer = developers[0] if developers else ""

            return GameMetadata(
                appid=appid,
                name=details.get("name", "Unknown"),
                tags=tags,
                developer=developer,
                description=details.get("short_description", ""),
                fetched_at=fetched_at,
                cache_age_seconds=cache_age
            )
        except httpx.HTTPStatusError as e:
            status = e.response.status_code
            error_type = "rate_limit" if status == 429 else "api_error"
            return APIError(
                error_code=status,
                error_type=error_type,
                message=f"Steam Store API error: {status}"
            )
        except Exception as e:
            logger.error(f"Steam Store error: {e}")
            return APIError(
                error_code=502,
                error_type="api_error",
                message=str(e)
            )


class GamalyticClient:
    """Client for Gamalytic API (revenue estimation) with review-based fallback."""

    BASE_URL = "https://api.gamalytic.com"

    def __init__(self, http_client: CachedAPIClient):
        """Initialize Gamalytic client with cached HTTP client.

        Args:
            http_client: CachedAPIClient instance for API requests
        """
        self.http = http_client

    async def get_revenue_estimate(self, appid: int) -> CommercialData | APIError:
        """Get revenue estimate for a Steam game with fallback and triangulation.

        Primary path: Gamalytic API (no auth required)
        Fallback path: SteamSpy review-based estimation (reviews * 30-50)
        Triangulation: Compare Gamalytic with SteamSpy owner data when available

        Args:
            appid: Steam AppID to get revenue estimate for

        Returns:
            CommercialData with revenue range and metadata, or APIError on failure
        """
        # Primary path: Try Gamalytic API first
        try:
            data, fetched_at, cache_age = await self.http.get_with_metadata(
                f"{self.BASE_URL}/game/{appid}",
                params={},
                cache_ttl=21600  # 6 hours
            )

            # Gamalytic response: {"steamId": "504230", "name": "Celeste", "price": 19.99, "revenue": 17990584, ...}
            revenue = data.get("revenue", 0)
            price = data.get("price", 0.0)
            name = data.get("name", "")

            # Convert single revenue integer to +/-20% range
            revenue_min = revenue * 0.80
            revenue_max = revenue * 1.20

            # Create CommercialData with Gamalytic source
            commercial_data = CommercialData(
                appid=appid,
                name=name,
                price=price if price > 0 else None,
                revenue_min=revenue_min,
                revenue_max=revenue_max,
                currency="USD",
                confidence="high",
                source="gamalytic",
                fetched_at=fetched_at,
                cache_age_seconds=cache_age
            )

            # Triangulation: Compare with SteamSpy owner data
            try:
                await self._add_triangulation_warning(commercial_data, appid, revenue)
            except Exception as e:
                logger.error(f"Triangulation check failed for AppID {appid}: {e}")
                # Triangulation is informational only - continue without warning

            return commercial_data

        except httpx.HTTPStatusError as e:
            status = e.response.status_code
            logger.warning(f"Gamalytic API error {status} for AppID {appid}, falling back to review estimation")
            # Fall through to fallback
        except Exception as e:
            logger.warning(f"Gamalytic API error for AppID {appid}: {e}, falling back to review estimation")
            # Fall through to fallback

        # Fallback path: Review-based estimation using SteamSpy
        return await self._get_review_based_estimate(appid)

    async def _get_review_based_estimate(self, appid: int) -> CommercialData | APIError:
        """Fallback revenue estimation using SteamSpy review count.

        Args:
            appid: Steam AppID to estimate revenue for

        Returns:
            CommercialData with review-based estimate, or APIError on failure
        """
        try:
            data, fetched_at, cache_age = await self.http.get_with_metadata(
                "https://steamspy.com/api.php",
                params={"request": "appdetails", "appid": str(appid)},
                cache_ttl=3600  # 1 hour
            )

            # SteamSpy response: {"positive": 82000, "negative": 304, "price": "1999", ...}
            positive = data.get("positive", 0)
            negative = data.get("negative", 0)
            price_str = data.get("price", "0")

            total_reviews = positive + negative
            if total_reviews == 0:
                return APIError(
                    error_code=404,
                    error_type="not_found",
                    message=f"Cannot estimate revenue: no reviews for AppID {appid}"
                )

            # Revenue estimation: reviews * 30-50 (conservative to optimistic)
            revenue_min = total_reviews * 30
            revenue_max = total_reviews * 50

            # Convert SteamSpy price from cents to dollars
            price_dollars = int(price_str) / 100.0 if price_str and price_str != "0" else None

            return CommercialData(
                appid=appid,
                name="",  # SteamSpy doesn't provide name in appdetails
                price=price_dollars,
                revenue_min=revenue_min,
                revenue_max=revenue_max,
                currency="USD",
                confidence="low",
                source="review_estimate",
                method="review_multiplier",
                fetched_at=fetched_at,
                cache_age_seconds=cache_age
            )

        except httpx.HTTPStatusError as e:
            status = e.response.status_code
            error_type = "rate_limit" if status == 429 else "api_error"
            return APIError(
                error_code=status,
                error_type=error_type,
                message=f"SteamSpy API error: {status}"
            )
        except Exception as e:
            logger.error(f"SteamSpy fallback error for AppID {appid}: {e}")
            return APIError(
                error_code=502,
                error_type="api_error",
                message=f"Both Gamalytic and SteamSpy unavailable for AppID {appid}"
            )

    async def _add_triangulation_warning(
        self, commercial_data: CommercialData, appid: int, gamalytic_revenue: float
    ) -> None:
        """Add triangulation warning if SteamSpy owner data diverges from Gamalytic.

        Modifies commercial_data in place to add warning fields if divergence > 20%.

        Args:
            commercial_data: CommercialData object to potentially modify
            appid: Steam AppID
            gamalytic_revenue: Original Gamalytic revenue value
        """
        # Fetch SteamSpy data for owner count
        data, _, _ = await self.http.get_with_metadata(
            "https://steamspy.com/api.php",
            params={"request": "appdetails", "appid": str(appid)},
            cache_ttl=3600
        )

        # Parse owners string: "1,000,000 .. 2,000,000" -> low=1000000, high=2000000
        owners_str = data.get("owners", "0 .. 0")
        owners_str = owners_str.replace(",", "")
        parts = owners_str.split(" .. ")
        if len(parts) != 2:
            return  # Can't parse, skip triangulation

        low = int(parts[0])
        high = int(parts[1])
        owners_midpoint = (low + high) / 2

        # Get price from SteamSpy (in cents)
        price_str = data.get("price", "0")
        price_dollars = int(price_str) / 100.0 if price_str and price_str != "0" else 0

        # Skip triangulation for free games
        if price_dollars == 0:
            return

        # Calculate SteamSpy-implied revenue (owners * price * 70% Steam cut)
        steamspy_implied_revenue = owners_midpoint * price_dollars * 0.70

        # Calculate divergence from Gamalytic
        gamalytic_midpoint = gamalytic_revenue  # Single revenue value from API
        divergence = abs(steamspy_implied_revenue - gamalytic_midpoint) / max(gamalytic_midpoint, 1)

        # Add warning if divergence > 20%
        if divergence > 0.20:
            commercial_data.triangulation_warning = (
                f"SteamSpy owner-based estimate (${steamspy_implied_revenue:,.0f}) "
                f"diverges {divergence:.0%} from Gamalytic (${gamalytic_midpoint:,.0f}). "
                f"Cross-reference recommended."
            )
            commercial_data.steamspy_implied_revenue = steamspy_implied_revenue
            commercial_data.gamalytic_revenue = gamalytic_midpoint
