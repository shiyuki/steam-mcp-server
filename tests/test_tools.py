"""Tests for MCP tool input validation."""

import json
import pytest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch, MagicMock

from src.schemas import SearchResult, GameMetadata, APIError


def _make_search_genre(steamspy_mock):
    """Create a standalone search_genre function with mocked clients."""
    async def search_genre(genre: str, limit: int = 10) -> str:
        if not genre or not genre.strip():
            return json.dumps({"error": "genre is required", "error_type": "validation"})
        if not 1 <= limit <= 100:
            return json.dumps({"error": "limit must be between 1 and 100", "error_type": "validation"})

        result = await steamspy_mock.search_by_tag(genre.strip(), limit)
        if isinstance(result, APIError):
            return json.dumps(result.model_dump())
        # Use model_dump with mode='json' to serialize datetime
        dumped = result.model_dump(mode='json')
        return json.dumps({"appids": dumped["appids"], "tag": dumped["tag"], "total_found": dumped["total_found"]})

    return search_genre


def _make_fetch_metadata(store_mock):
    """Create a standalone fetch_metadata function with mocked clients."""
    async def fetch_metadata(appid: int) -> str:
        if appid <= 0:
            return json.dumps({"error": "appid must be positive", "error_type": "validation"})

        result = await store_mock.get_app_details(appid)
        if isinstance(result, APIError):
            return json.dumps(result.model_dump())
        return result.model_dump_json()

    return fetch_metadata


class TestSearchGenreValidation:
    @pytest.mark.asyncio
    async def test_empty_genre_returns_error(self):
        fn = _make_search_genre(AsyncMock())
        result = json.loads(await fn(""))
        assert result["error_type"] == "validation"
        assert "genre is required" in result["error"]

    @pytest.mark.asyncio
    async def test_whitespace_genre_returns_error(self):
        fn = _make_search_genre(AsyncMock())
        result = json.loads(await fn("   "))
        assert result["error_type"] == "validation"

    @pytest.mark.asyncio
    async def test_limit_zero_returns_error(self):
        fn = _make_search_genre(AsyncMock())
        result = json.loads(await fn("Action", limit=0))
        assert result["error_type"] == "validation"
        assert "limit must be between 1 and 100" in result["error"]

    @pytest.mark.asyncio
    async def test_limit_over_100_returns_error(self):
        fn = _make_search_genre(AsyncMock())
        result = json.loads(await fn("Action", limit=101))
        assert result["error_type"] == "validation"

    @pytest.mark.asyncio
    async def test_limit_negative_returns_error(self):
        fn = _make_search_genre(AsyncMock())
        result = json.loads(await fn("Action", limit=-5))
        assert result["error_type"] == "validation"

    @pytest.mark.asyncio
    async def test_valid_request_returns_results(self):
        mock = AsyncMock()
        mock.search_by_tag.return_value = SearchResult(
            appids=[1, 2],
            tag="RPG",
            total_found=2,
            fetched_at=datetime.now(timezone.utc),
            games=[]  # Phase 5: Include games field (can be empty for tool tests)
        )
        fn = _make_search_genre(mock)
        result = json.loads(await fn("RPG", limit=10))

        assert result["appids"] == [1, 2]
        assert result["tag"] == "RPG"
        assert result["total_found"] == 2

    @pytest.mark.asyncio
    async def test_genre_is_stripped(self):
        mock = AsyncMock()
        mock.search_by_tag.return_value = SearchResult(
            appids=[],
            tag="RPG",
            total_found=0,
            fetched_at=datetime.now(timezone.utc),
            games=[]  # Phase 5: Include games field
        )
        fn = _make_search_genre(mock)
        await fn("  RPG  ", limit=10)

        mock.search_by_tag.assert_called_once_with("RPG", 10)

    @pytest.mark.asyncio
    async def test_api_error_forwarded(self):
        mock = AsyncMock()
        mock.search_by_tag.return_value = APIError(
            error_code=429, error_type="rate_limit", message="Too fast"
        )
        fn = _make_search_genre(mock)
        result = json.loads(await fn("Action"))

        assert result["error_code"] == 429
        assert result["error_type"] == "rate_limit"


class TestFetchMetadataValidation:
    @pytest.mark.asyncio
    async def test_negative_appid_returns_error(self):
        fn = _make_fetch_metadata(AsyncMock())
        result = json.loads(await fn(-1))
        assert result["error_type"] == "validation"
        assert "appid must be positive" in result["error"]

    @pytest.mark.asyncio
    async def test_zero_appid_returns_error(self):
        fn = _make_fetch_metadata(AsyncMock())
        result = json.loads(await fn(0))
        assert result["error_type"] == "validation"

    @pytest.mark.asyncio
    async def test_valid_appid_returns_metadata(self):
        mock = AsyncMock()
        mock.get_app_details.return_value = GameMetadata(
            appid=646570,
            name="Slay the Spire",
            tags=["Indie"],
            developer="Mega Crit",
            description="A deckbuilder",
            fetched_at=datetime.now(timezone.utc)
        )
        fn = _make_fetch_metadata(mock)
        result = json.loads(await fn(646570))

        assert result["appid"] == 646570
        assert result["name"] == "Slay the Spire"

    @pytest.mark.asyncio
    async def test_api_error_forwarded(self):
        mock = AsyncMock()
        mock.get_app_details.return_value = APIError(
            error_code=404, error_type="not_found", message="Not found"
        )
        fn = _make_fetch_metadata(mock)
        result = json.loads(await fn(99999999))

        assert result["error_code"] == 404
        assert result["error_type"] == "not_found"
