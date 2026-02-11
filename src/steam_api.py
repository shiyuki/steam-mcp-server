"""Steam API clients for SteamSpy and Steam Store."""

import httpx
from src.http_client import RateLimitedClient
from src.schemas import GameMetadata, SearchResult, APIError
from src.logging_config import get_logger

logger = get_logger(__name__)


class SteamSpyClient:
    """Client for SteamSpy API (tag-based game search)."""

    BASE_URL = "https://steamspy.com/api.php"

    def __init__(self, http_client: RateLimitedClient):
        """Initialize SteamSpy client with rate-limited HTTP client.

        Args:
            http_client: RateLimitedClient instance for API requests
        """
        self.http = http_client

    async def search_by_tag(self, tag: str, limit: int = 10) -> SearchResult | APIError:
        """Search for games by tag using SteamSpy API.

        Args:
            tag: Tag to search for (e.g., "roguelike", "indie")
            limit: Maximum number of AppIDs to return (default: 10)

        Returns:
            SearchResult with AppID list, or APIError on failure
        """
        try:
            data = await self.http.get(
                self.BASE_URL,
                params={"request": "tag", "tag": tag}
            )
            # SteamSpy returns {appid: {game_data}, ...}
            appids = [int(appid) for appid in list(data.keys())[:limit]]
            return SearchResult(
                appids=appids,
                tag=tag,
                total_found=len(data)
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

    def __init__(self, http_client: RateLimitedClient):
        """Initialize Steam Store client with rate-limited HTTP client.

        Args:
            http_client: RateLimitedClient instance for API requests
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
            data = await self.http.get(
                f"{self.BASE_URL}/appdetails",
                params={"appids": str(appid)}
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
                description=details.get("short_description", "")
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
