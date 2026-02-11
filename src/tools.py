"""MCP tool definitions for Steam API."""

import json
from mcp.types import TextContent, CallToolResult
from mcp.server import Server

from src.http_client import RateLimitedClient
from src.steam_api import SteamSpyClient, SteamStoreClient
from src.schemas import APIError
from src.config import Config
from src.logging_config import get_logger

logger = get_logger(__name__)

# Module-level clients (initialized in register_tools)
_http_client: RateLimitedClient | None = None
_steamspy: SteamSpyClient | None = None
_steam_store: SteamStoreClient | None = None


def register_tools(mcp: Server):
    """Register all MCP tools with the server."""
    global _http_client, _steamspy, _steam_store

    # Initialize shared clients
    _http_client = RateLimitedClient(rate_limit_delay=Config.RATE_LIMIT_DELAY)
    _steamspy = SteamSpyClient(_http_client)
    _steam_store = SteamStoreClient(_http_client)

    logger.info("Initializing MCP tools with rate limit: %.1fs", Config.RATE_LIMIT_DELAY)

    @mcp.call_tool()
    async def search_genre(genre: str, limit: int = 10) -> CallToolResult:
        """Search Steam games by genre/tag and return AppIDs.

        Args:
            genre: Steam tag to search (e.g., "Roguelike", "Action", "RPG")
            limit: Maximum number of results (1-100, default 10)

        Returns:
            List of Steam AppIDs matching the genre
        """
        # Input validation
        if not genre or not genre.strip():
            return CallToolResult(
                content=[TextContent(
                    type="text",
                    text=json.dumps({"error": "genre is required", "error_type": "validation"})
                )],
                isError=True
            )

        if not 1 <= limit <= 100:
            return CallToolResult(
                content=[TextContent(
                    type="text",
                    text=json.dumps({"error": "limit must be between 1 and 100", "error_type": "validation"})
                )],
                isError=True
            )

        logger.info("Searching for genre: %s, limit: %d", genre, limit)
        result = await _steamspy.search_by_tag(genre.strip(), limit)

        if isinstance(result, APIError):
            return CallToolResult(
                content=[TextContent(
                    type="text",
                    text=json.dumps(result.model_dump())
                )],
                isError=True
            )

        return CallToolResult(
            content=[TextContent(
                type="text",
                text=json.dumps({
                    "appids": result.appids,
                    "tag": result.tag,
                    "total_found": result.total_found
                })
            )]
        )

    @mcp.call_tool()
    async def fetch_metadata(appid: int) -> CallToolResult:
        """Fetch metadata for a Steam game by AppID.

        Args:
            appid: Steam AppID (e.g., 646570 for Slay the Spire)

        Returns:
            Game metadata including name, tags, developer, and description
        """
        # Input validation
        if appid <= 0:
            return CallToolResult(
                content=[TextContent(
                    type="text",
                    text=json.dumps({"error": "appid must be positive", "error_type": "validation"})
                )],
                isError=True
            )

        logger.info("Fetching metadata for AppID: %d", appid)
        result = await _steam_store.get_app_details(appid)

        if isinstance(result, APIError):
            return CallToolResult(
                content=[TextContent(
                    type="text",
                    text=json.dumps(result.model_dump())
                )],
                isError=True
            )

        return CallToolResult(
            content=[TextContent(
                type="text",
                text=json.dumps(result.model_dump())
            )]
        )
