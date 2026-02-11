"""Tests for Steam API clients with mocked HTTP responses."""

import pytest
import httpx
from datetime import datetime
from unittest.mock import AsyncMock, patch

from src.schemas import GameMetadata, SearchResult, APIError
from src.steam_api import SteamSpyClient, SteamStoreClient


@pytest.fixture
def mock_http():
    client = AsyncMock()
    return client


class TestSteamSpyClient:
    @pytest.mark.asyncio
    async def test_search_by_tag_success(self, mock_http):
        mock_http.get.return_value = {
            "646570": {"name": "Slay the Spire"},
            "2379780": {"name": "Balatro"},
            "247080": {"name": "Crypt of the NecroDancer"},
        }
        client = SteamSpyClient(mock_http)
        result = await client.search_by_tag("Roguelike", limit=2)

        assert isinstance(result, SearchResult)
        assert len(result.appids) == 2
        assert result.tag == "Roguelike"
        assert result.total_found == 3

    @pytest.mark.asyncio
    async def test_search_by_tag_empty_response(self, mock_http):
        mock_http.get.return_value = {}
        client = SteamSpyClient(mock_http)
        result = await client.search_by_tag("NonexistentTag")

        assert isinstance(result, SearchResult)
        assert result.appids == []
        assert result.total_found == 0

    @pytest.mark.asyncio
    async def test_search_by_tag_http_429(self, mock_http):
        response = httpx.Response(429, request=httpx.Request("GET", "https://steamspy.com"))
        mock_http.get.side_effect = httpx.HTTPStatusError(
            "Rate limited", request=response.request, response=response
        )
        client = SteamSpyClient(mock_http)
        result = await client.search_by_tag("Action")

        assert isinstance(result, APIError)
        assert result.error_code == 429
        assert result.error_type == "rate_limit"

    @pytest.mark.asyncio
    async def test_search_by_tag_http_500(self, mock_http):
        response = httpx.Response(500, request=httpx.Request("GET", "https://steamspy.com"))
        mock_http.get.side_effect = httpx.HTTPStatusError(
            "Server error", request=response.request, response=response
        )
        client = SteamSpyClient(mock_http)
        result = await client.search_by_tag("Action")

        assert isinstance(result, APIError)
        assert result.error_code == 500
        assert result.error_type == "api_error"

    @pytest.mark.asyncio
    async def test_search_by_tag_unexpected_exception(self, mock_http):
        mock_http.get.side_effect = ConnectionError("DNS failed")
        client = SteamSpyClient(mock_http)
        result = await client.search_by_tag("Action")

        assert isinstance(result, APIError)
        assert result.error_code == 502
        assert "DNS failed" in result.message

    @pytest.mark.asyncio
    async def test_search_limit_caps_results(self, mock_http):
        mock_http.get.return_value = {str(i): {} for i in range(50)}
        client = SteamSpyClient(mock_http)
        result = await client.search_by_tag("RPG", limit=5)

        assert isinstance(result, SearchResult)
        assert len(result.appids) == 5
        assert result.total_found == 50


class TestSteamStoreClient:
    @pytest.mark.asyncio
    async def test_get_app_details_success(self, mock_http):
        mock_http.get.return_value = {
            "646570": {
                "success": True,
                "data": {
                    "name": "Slay the Spire",
                    "genres": [
                        {"id": "23", "description": "Indie"},
                        {"id": "2", "description": "Strategy"},
                    ],
                    "developers": ["Mega Crit"],
                    "short_description": "A deckbuilder roguelike",
                },
            }
        }
        client = SteamStoreClient(mock_http)
        result = await client.get_app_details(646570)

        assert isinstance(result, GameMetadata)
        assert result.appid == 646570
        assert result.name == "Slay the Spire"
        assert result.tags == ["Indie", "Strategy"]
        assert result.developer == "Mega Crit"
        assert result.description == "A deckbuilder roguelike"
        assert isinstance(result.fetched_at, datetime)

    @pytest.mark.asyncio
    async def test_get_app_details_not_found(self, mock_http):
        mock_http.get.return_value = {"99999999": {"success": False}}
        client = SteamStoreClient(mock_http)
        result = await client.get_app_details(99999999)

        assert isinstance(result, APIError)
        assert result.error_code == 404
        assert result.error_type == "not_found"

    @pytest.mark.asyncio
    async def test_get_app_details_missing_fields(self, mock_http):
        mock_http.get.return_value = {
            "123": {
                "success": True,
                "data": {"name": "Minimal Game"},
            }
        }
        client = SteamStoreClient(mock_http)
        result = await client.get_app_details(123)

        assert isinstance(result, GameMetadata)
        assert result.name == "Minimal Game"
        assert result.tags == []
        assert result.developer == ""
        assert result.description == ""
        assert isinstance(result.fetched_at, datetime)

    @pytest.mark.asyncio
    async def test_get_app_details_http_error(self, mock_http):
        response = httpx.Response(500, request=httpx.Request("GET", "https://store.steampowered.com"))
        mock_http.get.side_effect = httpx.HTTPStatusError(
            "Server error", request=response.request, response=response
        )
        client = SteamStoreClient(mock_http)
        result = await client.get_app_details(646570)

        assert isinstance(result, APIError)
        assert result.error_code == 500

    @pytest.mark.asyncio
    async def test_get_app_details_appid_missing_in_response(self, mock_http):
        mock_http.get.return_value = {}
        client = SteamStoreClient(mock_http)
        result = await client.get_app_details(646570)

        assert isinstance(result, APIError)
        assert result.error_type == "not_found"
