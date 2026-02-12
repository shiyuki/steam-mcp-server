"""Steam API clients for SteamSpy and Steam Store."""

import httpx
from datetime import datetime, timezone
from src.http_client import CachedAPIClient
from src.schemas import GameMetadata, SearchResult, CommercialData, APIError
from src.logging_config import get_logger

logger = get_logger(__name__)


class SteamSpyClient:
    """Client for SteamSpy API (tag-based game search)."""

    BASE_URL = "https://steamspy.com/api.php"

    def __init__(self, http_client: CachedAPIClient):
        """Initialize SteamSpy client with cached HTTP client.

        Args:
            http_client: CachedAPIClient instance for API requests
        """
        self.http = http_client

    async def search_by_tag(self, tag: str, limit: int | None = 10) -> SearchResult | APIError:
        """Search for games by tag using SteamSpy API.

        Args:
            tag: Tag to search for (e.g., "roguelike", "indie")
            limit: Maximum number of AppIDs to return (default: 10). Use None to return all results.

        Returns:
            SearchResult with AppID list, or APIError on failure
        """
        try:
            # Use get_with_metadata for freshness tracking (1 hour cache TTL)
            data, fetched_at, cache_age = await self.http.get_with_metadata(
                self.BASE_URL,
                params={"request": "tag", "tag": tag},
                cache_ttl=3600
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
