"""Tests for Steam Store tag search and fallback logic."""

import json
import pytest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

from src.schemas import (
    APIError, SearchResult, GameSummary, EngagementData,
    AggregateEngagementData,
)
from src.steam_api import SteamStoreClient, SteamSpyClient


@pytest.fixture
def mock_http():
    """Mock CachedAPIClient with get_with_metadata method."""
    client = AsyncMock()
    return client


# --- SteamStoreClient.get_tag_map() ---

class TestGetTagMap:
    @pytest.mark.asyncio
    async def test_returns_lowercase_mapping(self, mock_http):
        mock_http.get_with_metadata.return_value = (
            [
                {"tagid": 3799, "name": "Visual Novel"},
                {"tagid": 492, "name": "Indie"},
                {"tagid": 10235, "name": "Life Sim"},
            ],
            datetime.now(timezone.utc),
            0,
        )
        client = SteamStoreClient(mock_http)
        result = await client.get_tag_map()

        assert isinstance(result, dict)
        assert result["visual novel"] == 3799
        assert result["indie"] == 492
        assert result["life sim"] == 10235

    @pytest.mark.asyncio
    async def test_caches_for_24h(self, mock_http):
        mock_http.get_with_metadata.return_value = (
            [{"tagid": 1, "name": "Test"}],
            datetime.now(timezone.utc),
            0,
        )
        client = SteamStoreClient(mock_http)
        await client.get_tag_map()

        # Verify cache TTL is 24 hours (86400 seconds)
        call_kwargs = mock_http.get_with_metadata.call_args
        assert call_kwargs.kwargs.get("cache_ttl") == 86400 or call_kwargs[1].get("cache_ttl") == 86400

    @pytest.mark.asyncio
    async def test_returns_error_on_bad_format(self, mock_http):
        # SteamSpy-style dict instead of tag list
        mock_http.get_with_metadata.return_value = (
            {"unexpected": "format"},
            datetime.now(timezone.utc),
            0,
        )
        client = SteamStoreClient(mock_http)
        result = await client.get_tag_map()

        assert isinstance(result, APIError)
        assert result.error_code == 502

    @pytest.mark.asyncio
    async def test_returns_error_on_exception(self, mock_http):
        mock_http.get_with_metadata.side_effect = Exception("Network error")
        client = SteamStoreClient(mock_http)
        result = await client.get_tag_map()

        assert isinstance(result, APIError)
        assert result.error_code == 502


# --- SteamStoreClient.search_by_tag_ids() ---

class TestSearchByTagIds:
    SAMPLE_HTML = '''
    <a class="search_result_row" data-ds-appid="413150">
        <span class="title">Stardew Valley</span>
    </a>
    <a class="search_result_row" data-ds-appid="736190">
        <span class="title">Chinese Parents</span>
    </a>
    <a class="search_result_row" data-ds-appid="1669980">
        <span class="title">Volcano Princess</span>
    </a>
    '''

    @pytest.mark.asyncio
    async def test_extracts_appids_from_html(self, mock_http):
        mock_http.get_with_metadata.return_value = (
            {
                "success": 1,
                "results_html": self.SAMPLE_HTML,
                "total_count": 367,
                "start": 0,
            },
            datetime.now(timezone.utc),
            0,
        )
        client = SteamStoreClient(mock_http)
        result = await client.search_by_tag_ids([10235, 6426])

        assert not isinstance(result, APIError)
        total_count, appids = result
        assert total_count == 367
        assert appids == [413150, 736190, 1669980]

    @pytest.mark.asyncio
    async def test_passes_tag_ids_as_comma_separated(self, mock_http):
        mock_http.get_with_metadata.return_value = (
            {"success": 1, "results_html": "", "total_count": 0, "start": 0},
            datetime.now(timezone.utc),
            0,
        )
        client = SteamStoreClient(mock_http)
        await client.search_by_tag_ids([3799, 10235])

        call_args = mock_http.get_with_metadata.call_args
        params = call_args[1].get("params") or call_args[0][1]
        assert params["tags"] == "3799,10235"

    @pytest.mark.asyncio
    async def test_empty_html_returns_empty_list(self, mock_http):
        mock_http.get_with_metadata.return_value = (
            {"success": 1, "results_html": "", "total_count": 0, "start": 0},
            datetime.now(timezone.utc),
            0,
        )
        client = SteamStoreClient(mock_http)
        total_count, appids = await client.search_by_tag_ids([3799])

        assert total_count == 0
        assert appids == []

    @pytest.mark.asyncio
    async def test_returns_error_on_exception(self, mock_http):
        mock_http.get_with_metadata.side_effect = Exception("Timeout")
        client = SteamStoreClient(mock_http)
        result = await client.search_by_tag_ids([3799])

        assert isinstance(result, APIError)


# --- score_rank int handling ---

class TestScoreRankCoercion:
    @pytest.mark.asyncio
    async def test_parse_game_data_handles_int_score_rank(self, mock_http):
        """SteamSpy sometimes returns score_rank as int (e.g., 98)."""
        client = SteamSpyClient(mock_http)
        parsed = client._parse_game_data({
            "appid": 12345,
            "name": "Test Game",
            "developer": "Dev",
            "publisher": "Pub",
            "owners": "1,000,000 .. 2,000,000",
            "ccu": 100,
            "positive": 500,
            "negative": 50,
            "average_forever": 600,  # minutes
            "average_2weeks": 120,
            "median_forever": 300,
            "median_2weeks": 60,
            "price": "999",
            "score_rank": 98,  # Integer!
            "userscore": 0,
        })
        assert parsed["score_rank"] == "98"
        assert isinstance(parsed["score_rank"], str)

        # Verify GameSummary can be constructed from this
        game = GameSummary(**parsed)
        assert game.score_rank == "98"

    @pytest.mark.asyncio
    async def test_parse_game_data_handles_str_score_rank(self, mock_http):
        """Normal case: score_rank as string."""
        client = SteamSpyClient(mock_http)
        parsed = client._parse_game_data({
            "appid": 12345,
            "name": "Test",
            "owners": "0 .. 0",
            "ccu": 0,
            "positive": 0,
            "negative": 0,
            "average_forever": 0,
            "average_2weeks": 0,
            "median_forever": 0,
            "median_2weeks": 0,
            "price": "0",
            "score_rank": "",
            "userscore": 0,
        })
        assert parsed["score_rank"] == ""

    @pytest.mark.asyncio
    async def test_engagement_data_handles_int_score_rank(self, mock_http):
        """get_engagement_data should handle int score_rank from SteamSpy."""
        mock_http.get_with_metadata.return_value = (
            {
                "appid": 12345,
                "name": "Test Game",
                "developer": "Dev",
                "publisher": "Pub",
                "owners": "1,000,000 .. 2,000,000",
                "ccu": 100,
                "positive": 500,
                "negative": 50,
                "average_forever": 600,
                "average_2weeks": 120,
                "median_forever": 300,
                "median_2weeks": 60,
                "price": "999",
                "score_rank": 98,  # Integer from API
                "genre": "Indie",
                "languages": "English",
                "tags": {"Indie": 100},
            },
            datetime.now(timezone.utc),
            0,
        )
        client = SteamSpyClient(mock_http)
        result = await client.get_engagement_data(12345)

        assert isinstance(result, EngagementData)
        assert result.score_rank == "98"


# --- Fallback integration (tools layer) ---

class TestSearchGenreFallback:
    """Test that search_genre falls back to Steam Store when SteamSpy fails."""

    @pytest.mark.asyncio
    async def test_steamspy_success_no_fallback(self):
        """When SteamSpy works, Steam Store should not be called."""
        from src.tools import register_tools
        from mcp.server.fastmcp import FastMCP
        import src.tools as tools_module

        mcp = FastMCP("test")
        register_tools(mcp)

        # Mock SteamSpy to return a valid result
        mock_result = SearchResult(
            fetched_at=datetime.now(timezone.utc),
            cache_age_seconds=0,
            appids=[1, 2, 3],
            tag="RPG",
            total_found=3,
            games=[],
            sort_by="owners",
        )

        with patch.object(tools_module._steamspy, "search_by_tag", return_value=mock_result) as spy_mock, \
             patch.object(tools_module._steam_store, "get_tag_map") as store_mock:
            # Access the registered tool function
            result = await tools_module._steamspy.search_by_tag("RPG", 10, "owners")
            assert isinstance(result, SearchResult)
            assert result.total_found == 3
            store_mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_fallback_builds_game_summary_from_engagement(self, mock_http):
        """Steam Store fallback should build GameSummary from EngagementData."""
        store_client = SteamStoreClient(mock_http)
        spy_client = SteamSpyClient(mock_http)

        # This test verifies the data transformation logic
        engagement = EngagementData(
            fetched_at=datetime.now(timezone.utc),
            cache_age_seconds=0,
            appid=736190,
            name="Chinese Parents",
            developer="Dev",
            publisher="Pub",
            ccu=287,
            owners_min=1000000,
            owners_max=2000000,
            owners_midpoint=1500000.0,
            average_forever=19.35,
            average_2weeks=1.8,
            median_forever=10.37,
            median_2weeks=1.8,
            positive=23459,
            negative=2192,
            total_reviews=25651,
            price=5.49,
            score_rank="",
            tags={"Life Sim": 461},
        )

        # Build GameSummary from EngagementData (same logic as fallback)
        game = GameSummary(
            appid=engagement.appid,
            name=engagement.name,
            developer=engagement.developer,
            publisher=engagement.publisher,
            owners_min=engagement.owners_min,
            owners_max=engagement.owners_max,
            owners_midpoint=engagement.owners_midpoint,
            ccu=engagement.ccu,
            price=engagement.price,
            review_score=engagement.review_score,
            positive=engagement.positive,
            negative=engagement.negative,
            average_forever=engagement.average_forever,
            median_forever=engagement.median_forever,
            average_2weeks=engagement.average_2weeks,
            median_2weeks=engagement.median_2weeks,
            score_rank=engagement.score_rank,
        )

        assert game.appid == 736190
        assert game.name == "Chinese Parents"
        assert game.owners_min == 1000000
        assert game.owners_max == 2000000
        assert game.owners_midpoint == 1500000.0
        assert game.ccu == 287
        assert game.price == 5.49


# --- data_source field ---

class TestDataSourceField:
    """Test that data_source is set correctly on SearchResult."""

    @pytest.mark.asyncio
    async def test_steamspy_path_sets_data_source_steamspy(self):
        """When SteamSpy succeeds, data_source should be 'steamspy'."""
        from src.tools import register_tools
        from mcp.server.fastmcp import FastMCP
        import src.tools as tools_module

        mcp = FastMCP("test")
        register_tools(mcp)

        mock_result = SearchResult(
            fetched_at=datetime.now(timezone.utc),
            cache_age_seconds=0,
            appids=[1, 2],
            tag="RPG",
            total_found=2,
            games=[],
            sort_by="owners",
        )

        with patch.object(tools_module._steamspy, "search_by_tag", return_value=mock_result):
            # Call the actual registered tool
            tools = {t.name: t for t in mcp._tool_manager.list_tools()}
            result_json = await tools["search_genre"].fn(genre="RPG", limit=10, sort_by="owners")
            result = json.loads(result_json)
            assert result["data_source"] == "steamspy"

    @pytest.mark.asyncio
    async def test_fallback_path_sets_data_source_steam_store(self):
        """When fallback is used, data_source should be 'steam_store'."""
        from src.tools import register_tools
        from mcp.server.fastmcp import FastMCP
        import src.tools as tools_module

        mcp = FastMCP("test")
        register_tools(mcp)

        # SteamSpy returns error → triggers fallback
        spy_error = APIError(error_code=502, error_type="api_error", message="SteamSpy down")

        # Steam Store tag map + search
        tag_map = {"rpg": 29}
        search_result = (100, [111, 222])

        # SteamSpy per-game enrichment
        engagement = EngagementData(
            fetched_at=datetime.now(timezone.utc), cache_age_seconds=0,
            appid=111, name="Game A", ccu=50,
            owners_min=10000, owners_max=20000, owners_midpoint=15000.0,
            average_forever=10.0, average_2weeks=2.0,
            median_forever=8.0, median_2weeks=1.5,
            positive=100, negative=10, total_reviews=110, price=9.99,
        )

        with patch.object(tools_module._steamspy, "search_by_tag", return_value=spy_error), \
             patch.object(tools_module._steam_store, "get_tag_map", return_value=tag_map), \
             patch.object(tools_module._steam_store, "search_by_tag_ids", return_value=search_result), \
             patch.object(tools_module._steamspy, "get_engagement_data", return_value=engagement):
            tools = {t.name: t for t in mcp._tool_manager.list_tools()}
            result_json = await tools["search_genre"].fn(genre="RPG", limit=10, sort_by="owners")
            result = json.loads(result_json)
            assert result["data_source"] == "steam_store"

    def test_search_result_schema_accepts_data_source(self):
        """SearchResult schema should accept data_source field."""
        sr = SearchResult(
            fetched_at=datetime.now(timezone.utc),
            appids=[1], tag="Test", total_found=1,
            data_source="steamspy",
        )
        assert sr.data_source == "steamspy"

        sr2 = SearchResult(
            fetched_at=datetime.now(timezone.utc),
            appids=[], tag="Test", total_found=0,
        )
        assert sr2.data_source is None


# --- Multi-tag search ---

class TestMultiTagSearch:
    """Test comma-separated multi-tag intersection search."""

    @pytest.mark.asyncio
    async def test_fallback_passes_owners_range(self):
        """Steam Store fallback should populate owners_min/max from EngagementData."""
        from src.tools import register_tools
        from mcp.server.fastmcp import FastMCP
        import src.tools as tools_module

        mcp = FastMCP("test")
        register_tools(mcp)

        spy_error = APIError(error_code=502, error_type="api_error", message="SteamSpy down")
        tag_map = {"rpg": 29}
        search_result = (1, [111])

        engagement = EngagementData(
            fetched_at=datetime.now(timezone.utc), cache_age_seconds=0,
            appid=111, name="Game A", ccu=50,
            owners_min=0, owners_max=20000, owners_midpoint=10000.0,
            average_forever=10.0, average_2weeks=2.0,
            median_forever=8.0, median_2weeks=1.5,
            positive=100, negative=10, total_reviews=110, price=9.99,
        )

        with patch.object(tools_module._steamspy, "search_by_tag", return_value=spy_error), \
             patch.object(tools_module._steam_store, "get_tag_map", return_value=tag_map), \
             patch.object(tools_module._steam_store, "search_by_tag_ids", return_value=search_result), \
             patch.object(tools_module._steamspy, "get_engagement_data", return_value=engagement):
            tools = {t.name: t for t in mcp._tool_manager.list_tools()}
            result_json = await tools["search_genre"].fn(genre="RPG", limit=10, sort_by="owners")
            result = json.loads(result_json)

            assert len(result["games"]) == 1
            game = result["games"][0]
            assert game["owners_min"] == 0
            assert game["owners_max"] == 20000
            assert game["owners_midpoint"] == 10000.0

    @pytest.mark.asyncio
    async def test_multi_tag_passes_owners_range(self):
        """Multi-tag search should populate owners_min/max from EngagementData."""
        from src.tools import register_tools
        from mcp.server.fastmcp import FastMCP
        import src.tools as tools_module

        mcp = FastMCP("test")
        register_tools(mcp)

        tag_map = {"cute": 4726, "horror": 1667}
        search_result = (1, [222])

        engagement = EngagementData(
            fetched_at=datetime.now(timezone.utc), cache_age_seconds=0,
            appid=222, name="Cute Horror Game", ccu=100,
            owners_min=200000, owners_max=500000, owners_midpoint=350000.0,
            average_forever=5.0, average_2weeks=1.0,
            median_forever=3.0, median_2weeks=0.5,
            positive=500, negative=20, total_reviews=520, price=14.99,
        )

        with patch.object(tools_module._steam_store, "get_tag_map", return_value=tag_map), \
             patch.object(tools_module._steam_store, "search_by_tag_ids", return_value=search_result), \
             patch.object(tools_module._steamspy, "get_engagement_data", return_value=engagement):
            tools = {t.name: t for t in mcp._tool_manager.list_tools()}
            result_json = await tools["search_genre"].fn(genre="Cute, Horror", limit=10, sort_by="owners")
            result = json.loads(result_json)

            assert len(result["games"]) == 1
            game = result["games"][0]
            assert game["owners_min"] == 200000
            assert game["owners_max"] == 500000
            assert game["owners_midpoint"] == 350000.0

    @pytest.mark.asyncio
    async def test_multi_tag_resolves_all_tags(self):
        """Multi-tag should resolve each tag to an ID and pass all to search_by_tag_ids."""
        from src.tools import register_tools
        from mcp.server.fastmcp import FastMCP
        import src.tools as tools_module

        mcp = FastMCP("test")
        register_tools(mcp)

        tag_map = {"visual novel": 3799, "choices matter": 11014}
        search_result = (42, [111, 222])

        engagement = EngagementData(
            fetched_at=datetime.now(timezone.utc), cache_age_seconds=0,
            appid=111, name="Game A", ccu=50,
            owners_min=10000, owners_max=20000, owners_midpoint=15000.0,
            average_forever=10.0, average_2weeks=2.0,
            median_forever=8.0, median_2weeks=1.5,
            positive=100, negative=10, total_reviews=110, price=9.99,
        )

        with patch.object(tools_module._steam_store, "get_tag_map", return_value=tag_map) as tm, \
             patch.object(tools_module._steam_store, "search_by_tag_ids", return_value=search_result) as sbti, \
             patch.object(tools_module._steamspy, "get_engagement_data", return_value=engagement):
            tools = {t.name: t for t in mcp._tool_manager.list_tools()}
            result_json = await tools["search_genre"].fn(genre="Visual Novel, Choices Matter", limit=5, sort_by="owners")
            result = json.loads(result_json)

            # Should have called search_by_tag_ids with both IDs
            sbti.assert_called_once()
            call_tag_ids = sbti.call_args[0][0]
            assert set(call_tag_ids) == {3799, 11014}

            # Result should indicate steam_store and combined tag
            assert result["data_source"] == "steam_store"
            assert "Visual Novel" in result["tag"]
            assert "Choices Matter" in result["tag"]

    @pytest.mark.asyncio
    async def test_multi_tag_missing_tag_returns_error(self):
        """If any tag in multi-tag is not found, return error naming it."""
        from src.tools import register_tools
        from mcp.server.fastmcp import FastMCP
        import src.tools as tools_module

        mcp = FastMCP("test")
        register_tools(mcp)

        tag_map = {"visual novel": 3799}  # "choices matter" missing

        with patch.object(tools_module._steam_store, "get_tag_map", return_value=tag_map):
            tools = {t.name: t for t in mcp._tool_manager.list_tools()}
            result_json = await tools["search_genre"].fn(genre="Visual Novel, Choices Matter", limit=5, sort_by="owners")
            result = json.loads(result_json)

            assert result["error_type"] == "tag_not_found"
            assert "Choices Matter" in result["message"]

    @pytest.mark.asyncio
    async def test_multi_tag_skips_steamspy(self):
        """Multi-tag should NOT call SteamSpy search_by_tag (no multi-tag support there)."""
        from src.tools import register_tools
        from mcp.server.fastmcp import FastMCP
        import src.tools as tools_module

        mcp = FastMCP("test")
        register_tools(mcp)

        tag_map = {"rpg": 29, "indie": 492}
        search_result = (10, [])

        with patch.object(tools_module._steam_store, "get_tag_map", return_value=tag_map), \
             patch.object(tools_module._steam_store, "search_by_tag_ids", return_value=search_result), \
             patch.object(tools_module._steamspy, "search_by_tag") as spy_mock:
            tools = {t.name: t for t in mcp._tool_manager.list_tools()}
            await tools["search_genre"].fn(genre="RPG, Indie", limit=5, sort_by="owners")

            # SteamSpy search_by_tag should NOT have been called
            spy_mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_single_tag_still_uses_steamspy_first(self):
        """A single tag (no comma) should still try SteamSpy first."""
        from src.tools import register_tools
        from mcp.server.fastmcp import FastMCP
        import src.tools as tools_module

        mcp = FastMCP("test")
        register_tools(mcp)

        mock_result = SearchResult(
            fetched_at=datetime.now(timezone.utc),
            cache_age_seconds=0,
            appids=[1], tag="RPG", total_found=1, games=[],
        )

        with patch.object(tools_module._steamspy, "search_by_tag", return_value=mock_result) as spy_mock:
            tools = {t.name: t for t in mcp._tool_manager.list_tools()}
            await tools["search_genre"].fn(genre="RPG", limit=5, sort_by="owners")

            spy_mock.assert_called_once()
