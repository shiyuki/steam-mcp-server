"""Tests for engagement data pipeline: SteamSpyClient engagement methods and tools."""

import json
import pytest
import httpx
from datetime import datetime, timezone
from unittest.mock import AsyncMock

from src.schemas import EngagementData, AggregateEngagementData, APIError
from src.steam_api import SteamSpyClient


@pytest.fixture
def mock_http():
    """Mock CachedAPIClient with get_with_metadata method."""
    client = AsyncMock()
    return client


class TestGetEngagementData:
    """Test SteamSpyClient.get_engagement_data with various scenarios."""

    @pytest.mark.asyncio
    async def test_successful_engagement_data(self, mock_http):
        """Test successful SteamSpy response with all fields."""
        steamspy_data = {
            "appid": 646570,
            "name": "Slay the Spire",
            "developer": "MegaCrit",
            "publisher": "Humble Games",
            "ccu": 1500,
            "owners": "2,000,000 .. 5,000,000",
            "positive": 82000,
            "negative": 1500,
            "average_forever": 4212,  # minutes
            "average_2weeks": 420,  # minutes
            "median_forever": 2520,  # minutes
            "median_2weeks": 180,  # minutes
            "price": "2499",  # cents
            "score_rank": "98%",
            "genre": "Indie",
            "languages": "English, Spanish, French",
            "tags": {
                "Roguelike": 500,
                "Card Game": 450,
                "Strategy": 400
            }
        }
        mock_http.get_with_metadata.return_value = (
            steamspy_data,
            datetime.now(timezone.utc),
            0
        )

        client = SteamSpyClient(mock_http)
        result = await client.get_engagement_data(646570)

        assert isinstance(result, EngagementData)
        assert result.appid == 646570
        assert result.name == "Slay the Spire"
        assert result.developer == "MegaCrit"
        assert result.publisher == "Humble Games"
        assert result.ccu == 1500
        assert result.owners_min == 2000000
        assert result.owners_max == 5000000
        assert result.owners_midpoint == 3500000.0
        # Playtime converted from minutes to hours
        assert result.average_forever == 70.2
        assert result.average_2weeks == 7.0
        assert result.median_forever == 42.0
        assert result.median_2weeks == 3.0
        assert result.positive == 82000
        assert result.negative == 1500
        assert result.total_reviews == 83500
        # Price converted from cents to dollars
        assert result.price == 24.99
        assert result.score_rank == "98%"
        assert result.genre == "Indie"
        assert result.languages == "English, Spanish, French"
        # Tags parsed as dict with weights
        assert result.tags == {"Roguelike": 500, "Card Game": 450, "Strategy": 400}

    @pytest.mark.asyncio
    async def test_owner_range_parsing_with_commas(self, mock_http):
        """Test parsing of owner range with comma separators."""
        steamspy_data = {
            "appid": 12345,
            "name": "Test Game",
            "owners": "1,000,000 .. 2,000,000",
            "ccu": 100,
            "positive": 500,
            "negative": 50,
            "average_forever": 600,
            "average_2weeks": 60,
            "median_forever": 300,
            "median_2weeks": 30,
            "price": "999"
        }
        mock_http.get_with_metadata.return_value = (
            steamspy_data,
            datetime.now(timezone.utc),
            0
        )

        client = SteamSpyClient(mock_http)
        result = await client.get_engagement_data(12345)

        assert isinstance(result, EngagementData)
        assert result.owners_min == 1000000
        assert result.owners_max == 2000000
        assert result.owners_midpoint == 1500000.0

    @pytest.mark.asyncio
    async def test_owner_range_parsing_small_numbers(self, mock_http):
        """Test parsing of small owner ranges without commas."""
        steamspy_data = {
            "appid": 99999,
            "name": "Small Game",
            "owners": "100 .. 200",
            "ccu": 5,
            "positive": 10,
            "negative": 2,
            "average_forever": 120,
            "average_2weeks": 30,
            "median_forever": 90,
            "median_2weeks": 20,
            "price": "499"
        }
        mock_http.get_with_metadata.return_value = (
            steamspy_data,
            datetime.now(timezone.utc),
            0
        )

        client = SteamSpyClient(mock_http)
        result = await client.get_engagement_data(99999)

        assert isinstance(result, EngagementData)
        assert result.owners_min == 100
        assert result.owners_max == 200
        assert result.owners_midpoint == 150.0

    @pytest.mark.asyncio
    async def test_owner_range_malformed_defaults_to_zero(self, mock_http):
        """Test malformed owner range defaults to zero."""
        steamspy_data = {
            "appid": 11111,
            "name": "Unknown Owners",
            "owners": "unknown",
            "ccu": 0,
            "positive": 5,
            "negative": 1,
            "average_forever": 60,
            "average_2weeks": 15,
            "median_forever": 40,
            "median_2weeks": 10,
            "price": "0"
        }
        mock_http.get_with_metadata.return_value = (
            steamspy_data,
            datetime.now(timezone.utc),
            0
        )

        client = SteamSpyClient(mock_http)
        result = await client.get_engagement_data(11111)

        assert isinstance(result, EngagementData)
        assert result.owners_min == 0
        assert result.owners_max == 0
        assert result.owners_midpoint == 0.0

    @pytest.mark.asyncio
    async def test_ccu_ratio_calculation(self, mock_http):
        """Test CCU ratio calculation with proper rounding."""
        steamspy_data = {
            "appid": 22222,
            "name": "CCU Test",
            "owners": "5,000 .. 10,000",
            "ccu": 150,
            "positive": 100,
            "negative": 10,
            "average_forever": 600,
            "average_2weeks": 60,
            "median_forever": 300,
            "median_2weeks": 30,
            "price": "1999"
        }
        mock_http.get_with_metadata.return_value = (
            steamspy_data,
            datetime.now(timezone.utc),
            0
        )

        client = SteamSpyClient(mock_http)
        result = await client.get_engagement_data(22222)

        assert isinstance(result, EngagementData)
        # ccu_ratio = 150 / 7500 * 100 = 2.0
        assert result.ccu_ratio == 2.0

    @pytest.mark.asyncio
    async def test_ccu_ratio_zero_owners_returns_none(self, mock_http):
        """Test CCU ratio returns None when owners is zero (division by zero guard)."""
        steamspy_data = {
            "appid": 33333,
            "name": "No Owners",
            "owners": "0 .. 0",
            "ccu": 5,
            "positive": 0,
            "negative": 0,
            "average_forever": 0,
            "average_2weeks": 0,
            "median_forever": 0,
            "median_2weeks": 0,
            "price": "0"
        }
        mock_http.get_with_metadata.return_value = (
            steamspy_data,
            datetime.now(timezone.utc),
            0
        )

        client = SteamSpyClient(mock_http)
        result = await client.get_engagement_data(33333)

        assert isinstance(result, EngagementData)
        assert result.ccu_ratio is None

    @pytest.mark.asyncio
    async def test_review_score_calculation(self, mock_http):
        """Test review score calculation."""
        steamspy_data = {
            "appid": 44444,
            "name": "Review Test",
            "owners": "10,000 .. 20,000",
            "ccu": 50,
            "positive": 900,
            "negative": 100,
            "average_forever": 600,
            "average_2weeks": 60,
            "median_forever": 300,
            "median_2weeks": 30,
            "price": "1999"
        }
        mock_http.get_with_metadata.return_value = (
            steamspy_data,
            datetime.now(timezone.utc),
            0
        )

        client = SteamSpyClient(mock_http)
        result = await client.get_engagement_data(44444)

        assert isinstance(result, EngagementData)
        assert result.review_score == 90.0
        assert result.total_reviews == 1000

    @pytest.mark.asyncio
    async def test_review_score_zero_reviews_returns_none(self, mock_http):
        """Test review score returns None when no reviews exist."""
        steamspy_data = {
            "appid": 55555,
            "name": "No Reviews",
            "owners": "1,000 .. 2,000",
            "ccu": 10,
            "positive": 0,
            "negative": 0,
            "average_forever": 60,
            "average_2weeks": 15,
            "median_forever": 40,
            "median_2weeks": 10,
            "price": "999"
        }
        mock_http.get_with_metadata.return_value = (
            steamspy_data,
            datetime.now(timezone.utc),
            0
        )

        client = SteamSpyClient(mock_http)
        result = await client.get_engagement_data(55555)

        assert isinstance(result, EngagementData)
        assert result.review_score is None
        assert result.total_reviews == 0

    @pytest.mark.asyncio
    async def test_playtime_engagement_calculation(self, mock_http):
        """Test playtime engagement ratio calculation."""
        steamspy_data = {
            "appid": 66666,
            "name": "Playtime Test",
            "owners": "5,000 .. 10,000",
            "ccu": 50,
            "positive": 100,
            "negative": 10,
            "average_forever": 1200,  # 20 hours
            "average_2weeks": 120,
            "median_forever": 600,  # 10 hours
            "median_2weeks": 60,
            "price": "1999"
        }
        mock_http.get_with_metadata.return_value = (
            steamspy_data,
            datetime.now(timezone.utc),
            0
        )

        client = SteamSpyClient(mock_http)
        result = await client.get_engagement_data(66666)

        assert isinstance(result, EngagementData)
        # playtime_engagement = 10.0 / 20.0 = 0.5
        assert result.playtime_engagement == 0.5

    @pytest.mark.asyncio
    async def test_activity_ratio_calculation(self, mock_http):
        """Test activity ratio calculation."""
        steamspy_data = {
            "appid": 77777,
            "name": "Activity Test",
            "owners": "10,000 .. 20,000",
            "ccu": 100,
            "positive": 200,
            "negative": 20,
            "average_forever": 600,  # 10 hours
            "average_2weeks": 60,  # 1 hour
            "median_forever": 300,
            "median_2weeks": 30,
            "price": "1999"
        }
        mock_http.get_with_metadata.return_value = (
            steamspy_data,
            datetime.now(timezone.utc),
            0
        )

        client = SteamSpyClient(mock_http)
        result = await client.get_engagement_data(77777)

        assert isinstance(result, EngagementData)
        # activity_ratio = 1.0 / 10.0 * 100 = 10.0
        assert result.activity_ratio == 10.0

    @pytest.mark.asyncio
    async def test_activity_ratio_zero_forever_returns_none(self, mock_http):
        """Test activity ratio returns None when average_forever is zero."""
        steamspy_data = {
            "appid": 88888,
            "name": "No Forever Playtime",
            "owners": "1,000 .. 2,000",
            "ccu": 5,
            "positive": 10,
            "negative": 1,
            "average_forever": 0,
            "average_2weeks": 60,
            "median_forever": 0,
            "median_2weeks": 30,
            "price": "999"
        }
        mock_http.get_with_metadata.return_value = (
            steamspy_data,
            datetime.now(timezone.utc),
            0
        )

        client = SteamSpyClient(mock_http)
        result = await client.get_engagement_data(88888)

        assert isinstance(result, EngagementData)
        assert result.activity_ratio is None

    @pytest.mark.asyncio
    async def test_quality_flags_zero_ccu_with_owners(self, mock_http):
        """Test quality flags when CCU is zero but other data exists."""
        steamspy_data = {
            "appid": 99000,
            "name": "Zero CCU Game",
            "owners": "10,000 .. 20,000",
            "ccu": 0,
            "positive": 500,
            "negative": 50,
            "average_forever": 600,
            "average_2weeks": 60,
            "median_forever": 300,
            "median_2weeks": 30,
            "price": "1999"
        }
        mock_http.get_with_metadata.return_value = (
            steamspy_data,
            datetime.now(timezone.utc),
            0
        )

        client = SteamSpyClient(mock_http)
        result = await client.get_engagement_data(99000)

        assert isinstance(result, EngagementData)
        # Zero CCU with other data = "available" (real measurement)
        assert result.ccu_quality == "available"
        # CCU ratio should be 0.0, not None (0/15000 is valid computation)
        assert result.ccu_ratio == 0.0

    @pytest.mark.asyncio
    async def test_quality_flags_zero_everything(self, mock_http):
        """Test quality flags when all metrics are zero."""
        steamspy_data = {
            "appid": 99001,
            "name": "All Zeros",
            "owners": "0 .. 0",
            "ccu": 0,
            "positive": 0,
            "negative": 0,
            "average_forever": 0,
            "average_2weeks": 0,
            "median_forever": 0,
            "median_2weeks": 0,
            "price": "0"
        }
        mock_http.get_with_metadata.return_value = (
            steamspy_data,
            datetime.now(timezone.utc),
            0
        )

        client = SteamSpyClient(mock_http)
        result = await client.get_engagement_data(99001)

        assert isinstance(result, EngagementData)
        # All zeros with no other data = "zero_uncertain"
        assert result.ccu_quality == "zero_uncertain"
        assert result.owners_quality == "zero_uncertain"
        assert result.reviews_quality == "zero_uncertain"

    @pytest.mark.asyncio
    async def test_f2p_game_price_zero(self, mock_http):
        """Test free-to-play game has price 0.0, not None."""
        steamspy_data = {
            "appid": 570,
            "name": "Dota 2",
            "owners": "100,000,000 .. 200,000,000",
            "ccu": 500000,
            "positive": 800000,
            "negative": 200000,
            "average_forever": 3600,
            "average_2weeks": 360,
            "median_forever": 1800,
            "median_2weeks": 180,
            "price": "0"
        }
        mock_http.get_with_metadata.return_value = (
            steamspy_data,
            datetime.now(timezone.utc),
            0
        )

        client = SteamSpyClient(mock_http)
        result = await client.get_engagement_data(570)

        assert isinstance(result, EngagementData)
        # F2P games use 0.0, not None
        assert result.price == 0.0

    @pytest.mark.asyncio
    async def test_http_error_returns_api_error(self, mock_http):
        """Test HTTP error returns APIError with correct type."""
        request = httpx.Request("GET", "https://steamspy.com/api.php")
        response = httpx.Response(429, request=request)
        mock_http.get_with_metadata.side_effect = httpx.HTTPStatusError(
            "Rate limited",
            request=request,
            response=response
        )

        client = SteamSpyClient(mock_http)
        result = await client.get_engagement_data(12345)

        assert isinstance(result, APIError)
        assert result.error_type == "rate_limit"
        assert result.error_code == 429

    @pytest.mark.asyncio
    async def test_connection_error_returns_api_error(self, mock_http):
        """Test connection error returns APIError."""
        mock_http.get_with_metadata.side_effect = ConnectionError("DNS resolution failed")

        client = SteamSpyClient(mock_http)
        result = await client.get_engagement_data(12345)

        assert isinstance(result, APIError)
        assert result.error_code == 502
        assert result.error_type == "api_error"

    @pytest.mark.asyncio
    async def test_missing_optional_fields_use_defaults(self, mock_http):
        """Test missing optional fields use default values without crashing."""
        steamspy_data = {
            "appid": 99999,
            "name": "Minimal Data"
            # Missing: tags, genre, languages, developer, publisher, score_rank
        }
        mock_http.get_with_metadata.return_value = (
            steamspy_data,
            datetime.now(timezone.utc),
            0
        )

        client = SteamSpyClient(mock_http)
        result = await client.get_engagement_data(99999)

        assert isinstance(result, EngagementData)
        assert result.tags == {}
        assert result.genre == ""
        assert result.developer == ""
        assert result.publisher == ""
        assert result.languages == ""
        assert result.score_rank == ""


class TestAggregateEngagement:
    """Test SteamSpyClient.aggregate_engagement with various scenarios."""

    @pytest.mark.asyncio
    async def test_tag_aggregation_basic_stats(self, mock_http):
        """Test tag-based aggregation computes correct statistics."""
        tag_data = {
            "646570": {
                "appid": "646570",
                "name": "Slay the Spire",
                "owners": "2,000,000 .. 5,000,000",
                "ccu": 1500,
                "positive": 82000,
                "negative": 1500,
                "average_forever": 4200,  # 70 hours
                "average_2weeks": 420,  # 7 hours
                "price": "2499"
            },
            "2379780": {
                "appid": "2379780",
                "name": "Balatro",
                "owners": "1,000,000 .. 2,000,000",
                "ccu": 800,
                "positive": 50000,
                "negative": 500,
                "average_forever": 3000,  # 50 hours
                "average_2weeks": 600,  # 10 hours
                "price": "1499"
            },
            "247080": {
                "appid": "247080",
                "name": "Crypt of the NecroDancer",
                "owners": "500,000 .. 1,000,000",
                "ccu": 200,
                "positive": 15000,
                "negative": 500,
                "average_forever": 1800,  # 30 hours
                "average_2weeks": 180,  # 3 hours
                "price": "1499"
            },
            "262060": {
                "appid": "262060",
                "name": "Darkest Dungeon",
                "owners": "2,000,000 .. 5,000,000",
                "ccu": 1000,
                "positive": 60000,
                "negative": 5000,
                "average_forever": 2400,  # 40 hours
                "average_2weeks": 240,  # 4 hours
                "price": "2499"
            },
            "113200": {
                "appid": "113200",
                "name": "The Binding of Isaac",
                "owners": "5,000,000 .. 10,000,000",
                "ccu": 3000,
                "positive": 100000,
                "negative": 5000,
                "average_forever": 6000,  # 100 hours
                "average_2weeks": 600,  # 10 hours
                "price": "1499"
            }
        }
        mock_http.get_with_metadata.return_value = (
            tag_data,
            datetime.now(timezone.utc),
            0
        )

        client = SteamSpyClient(mock_http)
        result = await client.aggregate_engagement(tag="Roguelike")

        assert isinstance(result, AggregateEngagementData)
        assert result.tag == "Roguelike"
        assert result.games_total == 5
        assert result.games_analyzed == 5

        # Verify CCU stats
        assert result.ccu_stats is not None
        assert result.ccu_stats.games_with_data == 5
        # CCU values: [1500, 800, 200, 1000, 3000]
        assert result.ccu_stats.mean == 1300.0
        assert result.ccu_stats.median == 1000.0
        assert result.ccu_stats.min == 200.0
        assert result.ccu_stats.max == 3000.0

        # Verify owners stats
        assert result.owners_stats is not None
        assert result.owners_stats.games_with_data == 5
        # Midpoints: [3500000, 1500000, 750000, 3500000, 7500000]
        assert result.owners_stats.min == 750000.0
        assert result.owners_stats.max == 7500000.0

        # Verify playtime stats (in hours)
        assert result.playtime_stats is not None
        # Playtime values: [70, 50, 30, 40, 100]
        assert result.playtime_stats.mean == 58.0
        assert result.playtime_stats.median == 50.0

        # Verify review score stats
        assert result.review_score_stats is not None

        # Verify per-game list
        assert len(result.games) == 5
        assert all(isinstance(g.appid, int) for g in result.games)

    @pytest.mark.asyncio
    async def test_tag_aggregation_dual_metrics(self, mock_http):
        """Test dual metrics produce different values for different analytical perspectives."""
        tag_data = {
            "1": {
                "appid": "1",
                "name": "Game 1",
                "owners": "100,000 .. 200,000",  # midpoint 150k
                "ccu": 150,
                "positive": 1000,
                "negative": 100,
                "average_forever": 600,  # 10 hours
                "average_2weeks": 60,  # 1 hour
                "price": "1000"
            },
            "2": {
                "appid": "2",
                "name": "Game 2",
                "owners": "500,000 .. 1,000,000",  # midpoint 750k
                "ccu": 1500,
                "positive": 5000,
                "negative": 500,
                "average_forever": 1200,  # 20 hours
                "average_2weeks": 240,  # 4 hours
                "price": "2000"
            },
            "3": {
                "appid": "3",
                "name": "Game 3",
                "owners": "1,000,000 .. 2,000,000",  # midpoint 1.5M
                "ccu": 3000,
                "positive": 10000,
                "negative": 1000,
                "average_forever": 1800,  # 30 hours
                "average_2weeks": 360,  # 6 hours
                "price": "1500"
            }
        }
        mock_http.get_with_metadata.return_value = (
            tag_data,
            datetime.now(timezone.utc),
            0
        )

        client = SteamSpyClient(mock_http)
        result = await client.aggregate_engagement(tag="Test")

        assert isinstance(result, AggregateEngagementData)

        # Market-weighted CCU ratio: sum(ccu) / sum(owners_midpoint) * 100
        # (150 + 1500 + 3000) / (150000 + 750000 + 1500000) * 100
        # = 4650 / 2400000 * 100 = 0.19375
        market_weighted_ccu = result.market_weighted["ccu_ratio"]
        assert market_weighted_ccu is not None
        assert 0.19 <= market_weighted_ccu <= 0.20

        # Per-game average CCU ratio: mean of individual ratios
        # Game 1: 150/150000 * 100 = 0.1
        # Game 2: 1500/750000 * 100 = 0.2
        # Game 3: 3000/1500000 * 100 = 0.2
        # Mean: (0.1 + 0.2 + 0.2) / 3 = 0.167
        per_game_ccu = result.per_game_average["ccu_ratio"]
        assert per_game_ccu is not None
        assert 0.16 <= per_game_ccu <= 0.17

        # Values should be different
        assert market_weighted_ccu != per_game_ccu

    @pytest.mark.asyncio
    async def test_tag_aggregation_empty_tag_returns_error(self, mock_http):
        """Test empty tag returns error."""
        mock_http.get_with_metadata.return_value = (
            {},
            datetime.now(timezone.utc),
            0
        )

        client = SteamSpyClient(mock_http)
        result = await client.aggregate_engagement(tag="NonexistentTag")

        assert isinstance(result, APIError)
        assert result.error_type == "not_found"
        assert "no games found" in result.message.lower()

    @pytest.mark.asyncio
    async def test_appid_list_aggregation(self, mock_http):
        """Test AppID-list aggregation with partial failures."""
        # Mock individual get_engagement_data calls
        successful_data_1 = {
            "appid": 646570,
            "name": "Slay the Spire",
            "owners": "2,000,000 .. 5,000,000",
            "ccu": 1500,
            "positive": 82000,
            "negative": 1500,
            "average_forever": 4200,
            "average_2weeks": 420,
            "median_forever": 2520,
            "median_2weeks": 180,
            "price": "2499"
        }
        successful_data_2 = {
            "appid": 2379780,
            "name": "Balatro",
            "owners": "1,000,000 .. 2,000,000",
            "ccu": 800,
            "positive": 50000,
            "negative": 500,
            "average_forever": 3000,
            "average_2weeks": 600,
            "median_forever": 1800,
            "median_2weeks": 360,
            "price": "1499"
        }

        # First call succeeds, second fails, third succeeds
        mock_http.get_with_metadata.side_effect = [
            (successful_data_1, datetime.now(timezone.utc), 0),
            httpx.HTTPStatusError(
                "Not found",
                request=httpx.Request("GET", "https://steamspy.com/api.php"),
                response=httpx.Response(404, request=httpx.Request("GET", "https://steamspy.com/api.php"))
            ),
            (successful_data_2, datetime.now(timezone.utc), 0)
        ]

        client = SteamSpyClient(mock_http)
        result = await client.aggregate_engagement(appids=[646570, 999999, 2379780])

        assert isinstance(result, AggregateEngagementData)
        assert result.appids_requested == [646570, 999999, 2379780]
        assert result.games_total == 3
        assert result.games_analyzed == 2  # Only 2 succeeded
        assert len(result.failures) == 1
        assert result.failures[0]["appid"] == 999999
        # Stats computed from 2 successful games
        assert result.ccu_stats is not None
        assert result.ccu_stats.games_with_data == 2

    @pytest.mark.asyncio
    async def test_appid_list_all_fail_returns_error(self, mock_http):
        """Test AppID list with all failures returns error."""
        request = httpx.Request("GET", "https://steamspy.com/api.php")
        response = httpx.Response(404, request=request)
        mock_http.get_with_metadata.side_effect = [
            httpx.HTTPStatusError("Not found", request=request, response=response),
            httpx.HTTPStatusError("Not found", request=request, response=response)
        ]

        client = SteamSpyClient(mock_http)
        result = await client.aggregate_engagement(appids=[999991, 999992])

        assert isinstance(result, APIError)
        assert result.error_type == "not_found"

    @pytest.mark.asyncio
    async def test_aggregation_sort_by_owners(self, mock_http):
        """Test sorting by owners_midpoint."""
        tag_data = {
            "1": {
                "appid": "1",
                "name": "Small Game",
                "owners": "100 .. 200",  # midpoint 150
                "ccu": 1,
                "positive": 10,
                "negative": 1,
                "average_forever": 60,
                "average_2weeks": 6,
                "price": "500"
            },
            "2": {
                "appid": "2",
                "name": "Big Game",
                "owners": "1,000,000 .. 2,000,000",  # midpoint 1.5M
                "ccu": 1000,
                "positive": 10000,
                "negative": 100,
                "average_forever": 600,
                "average_2weeks": 60,
                "price": "2000"
            },
            "3": {
                "appid": "3",
                "name": "Medium Game",
                "owners": "10,000 .. 20,000",  # midpoint 15k
                "ccu": 50,
                "positive": 500,
                "negative": 50,
                "average_forever": 300,
                "average_2weeks": 30,
                "price": "1000"
            }
        }
        mock_http.get_with_metadata.return_value = (
            tag_data,
            datetime.now(timezone.utc),
            0
        )

        client = SteamSpyClient(mock_http)
        result = await client.aggregate_engagement(tag="Test", sort_by="owners")

        assert isinstance(result, AggregateEngagementData)
        assert len(result.games) == 3
        # Sorted descending by owners_midpoint
        assert result.games[0].appid == 2  # 1.5M
        assert result.games[1].appid == 3  # 15k
        assert result.games[2].appid == 1  # 150

    @pytest.mark.asyncio
    async def test_aggregation_sort_by_ccu(self, mock_http):
        """Test sorting by CCU."""
        tag_data = {
            "1": {
                "appid": "1",
                "name": "Low CCU",
                "owners": "100,000 .. 200,000",
                "ccu": 50,
                "positive": 1000,
                "negative": 100,
                "average_forever": 600,
                "average_2weeks": 60,
                "price": "1000"
            },
            "2": {
                "appid": "2",
                "name": "High CCU",
                "owners": "500,000 .. 1,000,000",
                "ccu": 5000,
                "positive": 5000,
                "negative": 500,
                "average_forever": 1200,
                "average_2weeks": 120,
                "price": "2000"
            },
            "3": {
                "appid": "3",
                "name": "Mid CCU",
                "owners": "200,000 .. 400,000",
                "ccu": 500,
                "positive": 2000,
                "negative": 200,
                "average_forever": 900,
                "average_2weeks": 90,
                "price": "1500"
            }
        }
        mock_http.get_with_metadata.return_value = (
            tag_data,
            datetime.now(timezone.utc),
            0
        )

        client = SteamSpyClient(mock_http)
        result = await client.aggregate_engagement(tag="Test", sort_by="ccu")

        assert isinstance(result, AggregateEngagementData)
        assert len(result.games) == 3
        # Sorted descending by CCU
        assert result.games[0].appid == 2  # 5000
        assert result.games[1].appid == 3  # 500
        assert result.games[2].appid == 1  # 50

    @pytest.mark.asyncio
    async def test_aggregation_percentile_calculation(self, mock_http):
        """Test percentile calculation with known values."""
        tag_data = {}
        for i in range(1, 11):
            tag_data[str(i)] = {
                "appid": str(i),
                "name": f"Game {i}",
                "owners": "10,000 .. 20,000",
                "ccu": i * 10,  # [10, 20, 30, 40, 50, 60, 70, 80, 90, 100]
                "positive": 100,
                "negative": 10,
                "average_forever": 600,
                "average_2weeks": 60,
                "price": "1000"
            }

        mock_http.get_with_metadata.return_value = (
            tag_data,
            datetime.now(timezone.utc),
            0
        )

        client = SteamSpyClient(mock_http)
        result = await client.aggregate_engagement(tag="Test")

        assert isinstance(result, AggregateEngagementData)
        assert result.ccu_stats is not None
        assert result.ccu_stats.mean == 55.0
        assert result.ccu_stats.median == 55.0
        # p25 should be around 25-30, p75 around 75-80
        assert 20 <= result.ccu_stats.p25 <= 35
        assert 70 <= result.ccu_stats.p75 <= 85
        assert result.ccu_stats.min == 10.0
        assert result.ccu_stats.max == 100.0

    @pytest.mark.asyncio
    async def test_aggregation_two_games_minimum(self, mock_http):
        """Test aggregation with minimum 2 games computes stats."""
        tag_data = {
            "1": {
                "appid": "1",
                "name": "Game 1",
                "owners": "100,000 .. 200,000",
                "ccu": 100,
                "positive": 1000,
                "negative": 100,
                "average_forever": 600,
                "average_2weeks": 60,
                "price": "1000"
            },
            "2": {
                "appid": "2",
                "name": "Game 2",
                "owners": "200,000 .. 400,000",
                "ccu": 200,
                "positive": 2000,
                "negative": 200,
                "average_forever": 1200,
                "average_2weeks": 120,
                "price": "2000"
            }
        }
        mock_http.get_with_metadata.return_value = (
            tag_data,
            datetime.now(timezone.utc),
            0
        )

        client = SteamSpyClient(mock_http)
        result = await client.aggregate_engagement(tag="Test")

        assert isinstance(result, AggregateEngagementData)
        assert result.games_analyzed == 2
        # Stats should be computed (quantiles work with 2 data points)
        assert result.ccu_stats is not None
        assert result.ccu_stats.mean == 150.0


class TestFetchEngagementValidation:
    """Test fetch_engagement tool input validation."""

    def _make_fetch_engagement(self, steamspy_mock):
        """Create standalone fetch_engagement with mocked SteamSpyClient."""
        async def fetch_engagement(appid: int) -> str:
            if appid <= 0:
                return json.dumps({"error": "appid must be positive", "error_type": "validation"})
            result = await steamspy_mock.get_engagement_data(appid)
            if isinstance(result, APIError):
                return json.dumps(result.model_dump())
            return json.dumps(result.model_dump())
        return fetch_engagement

    @pytest.mark.asyncio
    async def test_negative_appid_returns_validation_error(self):
        """Test negative AppID returns validation error."""
        fn = self._make_fetch_engagement(AsyncMock())
        result = json.loads(await fn(-1))
        assert result["error_type"] == "validation"
        assert "appid must be positive" in result["error"]

    @pytest.mark.asyncio
    async def test_zero_appid_returns_validation_error(self):
        """Test zero AppID returns validation error."""
        fn = self._make_fetch_engagement(AsyncMock())
        result = json.loads(await fn(0))
        assert result["error_type"] == "validation"

    @pytest.mark.asyncio
    async def test_valid_appid_returns_engagement_data(self):
        """Test valid AppID returns engagement data."""
        mock = AsyncMock()
        mock.get_engagement_data.return_value = EngagementData(
            appid=646570,
            name="Slay the Spire",
            ccu=1500,
            owners_min=2000000,
            owners_max=5000000,
            owners_midpoint=3500000,
            average_forever=70.2,
            average_2weeks=7.0,
            median_forever=42.0,
            median_2weeks=3.0,
            positive=82000,
            negative=1500,
            total_reviews=83500,
            price=24.99,
            fetched_at=datetime.now(timezone.utc)
        )
        fn = self._make_fetch_engagement(mock)
        result = json.loads(await fn(646570))

        assert result["appid"] == 646570
        assert result["ccu"] == 1500
        assert result["total_reviews"] == 83500


class TestAggregateEngagementValidation:
    """Test aggregate_engagement tool input validation."""

    def _make_aggregate_engagement(self, steamspy_mock):
        """Create standalone aggregate_engagement with mocked SteamSpyClient."""
        async def aggregate_engagement(tag: str = "", appids: str = "", sort_by: str = "owners") -> str:
            tag = tag.strip()
            appids_str = appids.strip()

            if not tag and not appids_str:
                return json.dumps({"error": "Either tag or appids must be provided", "error_type": "validation"})

            if tag and appids_str:
                return json.dumps({"error": "Provide either tag or appids, not both", "error_type": "validation"})

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

            result = await steamspy_mock.aggregate_engagement(
                tag=tag if tag else None,
                appids=appid_list,
                sort_by=sort_by
            )
            if isinstance(result, APIError):
                return json.dumps(result.model_dump())
            return json.dumps(result.model_dump())
        return aggregate_engagement

    @pytest.mark.asyncio
    async def test_no_input_returns_validation_error(self):
        """Test neither tag nor appids returns validation error."""
        fn = self._make_aggregate_engagement(AsyncMock())
        result = json.loads(await fn())
        assert result["error_type"] == "validation"
        assert "either tag or appids must be provided" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_both_inputs_returns_validation_error(self):
        """Test both tag and appids returns validation error."""
        fn = self._make_aggregate_engagement(AsyncMock())
        result = json.loads(await fn(tag="Roguelike", appids="646570,2379780"))
        assert result["error_type"] == "validation"
        assert "either tag or appids" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_invalid_appids_format_returns_error(self):
        """Test invalid AppID format returns validation error."""
        fn = self._make_aggregate_engagement(AsyncMock())
        result = json.loads(await fn(appids="abc,def"))
        assert result["error_type"] == "validation"
        assert "comma-separated integers" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_negative_appid_in_list_returns_error(self):
        """Test negative AppID in list returns validation error."""
        fn = self._make_aggregate_engagement(AsyncMock())
        result = json.loads(await fn(appids="123,-1,456"))
        assert result["error_type"] == "validation"
        assert "positive" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_empty_appids_string_returns_error(self):
        """Test empty appids string returns validation error."""
        fn = self._make_aggregate_engagement(AsyncMock())
        result = json.loads(await fn(appids=""))
        assert result["error_type"] == "validation"
        assert "either tag or appids must be provided" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_valid_tag_input(self):
        """Test valid tag input calls aggregate_engagement correctly."""
        mock = AsyncMock()
        mock.aggregate_engagement.return_value = AggregateEngagementData(
            tag="Roguelike",
            games_total=5,
            games_analyzed=5,
            fetched_at=datetime.now(timezone.utc)
        )
        fn = self._make_aggregate_engagement(mock)
        result = json.loads(await fn(tag="Roguelike"))

        assert result["tag"] == "Roguelike"
        assert result["games_total"] == 5
        # Verify the mock was called with correct parameters
        mock.aggregate_engagement.assert_called_once_with(
            tag="Roguelike",
            appids=None,
            sort_by="owners"
        )
