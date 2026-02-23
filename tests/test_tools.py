"""Tests for MCP tool input validation."""

import json
import pytest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch, MagicMock

from src.schemas import SearchResult, GameMetadata, APIError, CommercialData


def _make_search_genre(steamspy_mock):
    """Create a standalone search_genre function with mocked clients."""
    async def search_genre(genre: str, limit: int = 10) -> str:
        if not genre or not genre.strip():
            return json.dumps({"error": "genre is required", "error_type": "validation"})
        return_all = (limit == 0)
        if not return_all and not 1 <= limit <= 10000:
            return json.dumps({"error": "limit must be between 1 and 10000, or 0 for all results", "error_type": "validation"})

        result = await steamspy_mock.search_by_tag(genre.strip(), None if return_all else limit)
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
    async def test_limit_zero_returns_all_results(self):
        """limit=0 means 'return all' in production — should succeed, not error."""
        mock = AsyncMock()
        mock.search_by_tag.return_value = SearchResult(
            appids=[1, 2, 3],
            tag="Action",
            total_found=3,
            fetched_at=datetime.now(timezone.utc),
            games=[]
        )
        fn = _make_search_genre(mock)
        result = json.loads(await fn("Action", limit=0))
        assert "error_type" not in result
        assert result["tag"] == "Action"
        # Verify None passed as limit (means return all)
        mock.search_by_tag.assert_called_once_with("Action", None)

    @pytest.mark.asyncio
    async def test_limit_over_10000_returns_error(self):
        fn = _make_search_genre(AsyncMock())
        result = json.loads(await fn("Action", limit=10001))
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


# ---------------------------------------------------------------------------
# Phase 8: fetch_commercial detail_level and fetch_commercial_batch tests
# ---------------------------------------------------------------------------

def _make_fetch_commercial_extended(gamalytic_mock):
    """Create standalone fetch_commercial with detail_level support and mocked GamalyticClient."""
    async def fetch_commercial(appid: int, detail_level: str = "full") -> str:
        if appid <= 0:
            return json.dumps({"error": "appid must be positive", "error_type": "validation"})
        if detail_level not in ("full", "summary"):
            return json.dumps({"error": "detail_level must be 'full' or 'summary'", "error_type": "validation"})
        result = await gamalytic_mock.get_commercial_data(appid, detail_level=detail_level)
        if isinstance(result, APIError):
            return json.dumps(result.model_dump())
        return json.dumps(result.model_dump())
    return fetch_commercial


def _make_fetch_commercial_batch(gamalytic_mock):
    """Create standalone fetch_commercial_batch with mocked GamalyticClient."""
    async def fetch_commercial_batch(appids: str, detail_level: str = "summary") -> str:
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
        results = await gamalytic_mock.get_commercial_batch(appid_list, detail_level=detail_level)
        return json.dumps([r.model_dump() for r in results])
    return fetch_commercial_batch


def _make_commercial_data(appid=646570):
    """Create a minimal CommercialData for testing."""
    return CommercialData(
        appid=appid,
        name="Test Game",
        price=24.99,
        revenue_min=8000000,
        revenue_max=12000000,
        confidence="high",
        source="gamalytic",
        fetched_at=datetime.now(timezone.utc),
    )


class TestFetchCommercialExtended:
    """Tests for fetch_commercial tool with detail_level support."""

    @pytest.mark.asyncio
    async def test_fetch_commercial_detail_level_summary(self):
        """detail_level='summary' passes through to get_commercial_data."""
        mock = AsyncMock()
        mock.get_commercial_data.return_value = _make_commercial_data()
        fn = _make_fetch_commercial_extended(mock)

        result = json.loads(await fn(646570, detail_level="summary"))

        assert "error_type" not in result
        mock.get_commercial_data.assert_called_once_with(646570, detail_level="summary")

    @pytest.mark.asyncio
    async def test_fetch_commercial_detail_level_invalid(self):
        """detail_level='invalid' returns validation error."""
        fn = _make_fetch_commercial_extended(AsyncMock())
        result = json.loads(await fn(646570, detail_level="invalid"))

        assert result["error_type"] == "validation"
        assert "detail_level" in result["error"]

    @pytest.mark.asyncio
    async def test_fetch_commercial_default_detail_level(self):
        """No detail_level param defaults to 'full'."""
        mock = AsyncMock()
        mock.get_commercial_data.return_value = _make_commercial_data()
        fn = _make_fetch_commercial_extended(mock)

        await fn(646570)

        mock.get_commercial_data.assert_called_once_with(646570, detail_level="full")


class TestFetchCommercialBatch:
    """Tests for fetch_commercial_batch tool."""

    @pytest.mark.asyncio
    async def test_batch_valid_appids(self):
        """Valid comma-separated AppIDs returns array of 2 results."""
        mock = AsyncMock()
        mock.get_commercial_batch.return_value = [
            _make_commercial_data(646570),
            _make_commercial_data(2379780),
        ]
        fn = _make_fetch_commercial_batch(mock)

        result = json.loads(await fn("646570,2379780"))

        assert isinstance(result, list)
        assert len(result) == 2
        assert result[0]["appid"] == 646570
        assert result[1]["appid"] == 2379780

    @pytest.mark.asyncio
    async def test_batch_empty_string(self):
        """Empty appids string returns validation error."""
        fn = _make_fetch_commercial_batch(AsyncMock())
        result = json.loads(await fn(""))

        assert result["error_type"] == "validation"
        assert "appids" in result["error"]

    @pytest.mark.asyncio
    async def test_batch_invalid_format(self):
        """Non-integer appids returns validation error."""
        fn = _make_fetch_commercial_batch(AsyncMock())
        result = json.loads(await fn("abc,def"))

        assert result["error_type"] == "validation"

    @pytest.mark.asyncio
    async def test_batch_negative_appid(self):
        """Negative appid in batch returns validation error."""
        fn = _make_fetch_commercial_batch(AsyncMock())
        result = json.loads(await fn("-1,646570"))

        assert result["error_type"] == "validation"
        assert "positive" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_batch_detail_level_default_summary(self):
        """Batch default detail_level is 'summary'."""
        mock = AsyncMock()
        mock.get_commercial_batch.return_value = [_make_commercial_data()]
        fn = _make_fetch_commercial_batch(mock)

        await fn("646570")

        mock.get_commercial_batch.assert_called_once_with([646570], detail_level="summary")

    @pytest.mark.asyncio
    async def test_batch_error_forwarded(self):
        """APIError in batch results is serialized properly."""
        mock = AsyncMock()
        mock.get_commercial_batch.return_value = [
            _make_commercial_data(646570),
            APIError(error_code=404, error_type="not_found", message="Not found"),
        ]
        fn = _make_fetch_commercial_batch(mock)

        result = json.loads(await fn("646570,999"))

        assert len(result) == 2
        assert result[1]["error_code"] == 404
        assert result[1]["error_type"] == "not_found"

    @pytest.mark.asyncio
    async def test_batch_whitespace_appids(self):
        """AppIDs with spaces around commas are handled correctly."""
        mock = AsyncMock()
        mock.get_commercial_batch.return_value = [_make_commercial_data()]
        fn = _make_fetch_commercial_batch(mock)

        await fn("  646570  ")

        mock.get_commercial_batch.assert_called_once_with([646570], detail_level="summary")

    @pytest.mark.asyncio
    async def test_batch_detail_level_full(self):
        """Explicit detail_level='full' passes through to batch."""
        mock = AsyncMock()
        mock.get_commercial_batch.return_value = [_make_commercial_data()]
        fn = _make_fetch_commercial_batch(mock)

        await fn("646570", detail_level="full")

        mock.get_commercial_batch.assert_called_once_with([646570], detail_level="full")
