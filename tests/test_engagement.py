"""Tests for engagement data pipeline: SteamSpyClient engagement methods and tools."""

import json
import pytest
import httpx
from datetime import datetime, timezone
from unittest.mock import AsyncMock

from src.schemas import EngagementData, AggregateEngagementData, APIError, GameEngagementSummary, SearchResult
from src.steam_api import SteamSpyClient, normalize_tag


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

        # Mock side_effect: bulk call returns tag data, then appdetails for each game
        def mock_side_effect(*args, **kwargs):
            params = kwargs.get("params", {})
            if params.get("request") == "tag":
                return (tag_data, datetime.now(timezone.utc), 0)
            elif params.get("request") == "appdetails":
                appid = params.get("appid")  # This is a string
                game_data = tag_data.get(appid, {})
                if not game_data:
                    # If lookup fails, create minimal data
                    return ({"appid": appid, "owners": "0 .. 0", "ccu": 0, "positive": 0, "negative": 0, "average_forever": 0, "average_2weeks": 0, "price": "0"}, datetime.now(timezone.utc), 0)
                # Add tags field to each game - all have "Rogue-like" tag for this test (normalized form)
                game_with_tags = dict(game_data)
                game_with_tags["tags"] = {"Rogue-like": 100, "Indie": 90}
                return (game_with_tags, datetime.now(timezone.utc), 0)

        mock_http.get_with_metadata.side_effect = mock_side_effect

        client = SteamSpyClient(mock_http)
        result = await client.aggregate_engagement(tag="Roguelike")

        assert isinstance(result, AggregateEngagementData)
        assert result.tag == "Rogue-like"  # Tag is normalized
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

        # Mock side_effect for tag + appdetails calls
        def mock_side_effect(*args, **kwargs):
            params = kwargs.get("params", {})
            if params.get("request") == "tag":
                return (tag_data, datetime.now(timezone.utc), 0)
            elif params.get("request") == "appdetails":
                appid = params.get("appid")
                game_data = tag_data.get(appid, {})
                game_with_tags = dict(game_data)
                game_with_tags["tags"] = {"Test": 100}
                return (game_with_tags, datetime.now(timezone.utc), 0)

        mock_http.get_with_metadata.side_effect = mock_side_effect

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

        # Mock side_effect for tag + appdetails calls
        def mock_side_effect(*args, **kwargs):
            params = kwargs.get("params", {})
            if params.get("request") == "tag":
                return (tag_data, datetime.now(timezone.utc), 0)
            elif params.get("request") == "appdetails":
                appid = params.get("appid")
                game_data = tag_data.get(appid, {})
                game_with_tags = dict(game_data)
                game_with_tags["tags"] = {"Test": 100}
                return (game_with_tags, datetime.now(timezone.utc), 0)

        mock_http.get_with_metadata.side_effect = mock_side_effect

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

        # Mock side_effect for tag + appdetails calls
        def mock_side_effect(*args, **kwargs):
            params = kwargs.get("params", {})
            if params.get("request") == "tag":
                return (tag_data, datetime.now(timezone.utc), 0)
            elif params.get("request") == "appdetails":
                appid = params.get("appid")
                game_data = tag_data.get(appid, {})
                game_with_tags = dict(game_data)
                game_with_tags["tags"] = {"Test": 100}
                return (game_with_tags, datetime.now(timezone.utc), 0)

        mock_http.get_with_metadata.side_effect = mock_side_effect

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

        # Mock side_effect for tag + appdetails calls
        def mock_side_effect(*args, **kwargs):
            params = kwargs.get("params", {})
            if params.get("request") == "tag":
                return (tag_data, datetime.now(timezone.utc), 0)
            elif params.get("request") == "appdetails":
                appid = params.get("appid")
                game_data = tag_data.get(appid, {})
                game_with_tags = dict(game_data)
                game_with_tags["tags"] = {"Test": 100}
                return (game_with_tags, datetime.now(timezone.utc), 0)

        mock_http.get_with_metadata.side_effect = mock_side_effect

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

        # Mock side_effect for tag + appdetails calls
        def mock_side_effect(*args, **kwargs):
            params = kwargs.get("params", {})
            if params.get("request") == "tag":
                return (tag_data, datetime.now(timezone.utc), 0)
            elif params.get("request") == "appdetails":
                appid = params.get("appid")
                game_data = tag_data.get(appid, {})
                game_with_tags = dict(game_data)
                game_with_tags["tags"] = {"Test": 100}
                return (game_with_tags, datetime.now(timezone.utc), 0)

        mock_http.get_with_metadata.side_effect = mock_side_effect

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


class TestTagNormalization:
    """Test normalize_tag function for tag alias mapping."""

    def test_roguelike_normalized_to_hyphenated(self):
        """Test 'Roguelike' normalizes to 'Rogue-like'."""
        assert normalize_tag("Roguelike") == "Rogue-like"

    def test_roguelite_normalized_to_hyphenated(self):
        """Test 'Roguelite' normalizes to 'Rogue-lite'."""
        assert normalize_tag("Roguelite") == "Rogue-lite"

    def test_soulslike_normalized_to_hyphenated(self):
        """Test 'soulslike' normalizes to 'Souls-like'."""
        assert normalize_tag("soulslike") == "Souls-like"

    def test_case_insensitive_normalization(self):
        """Test normalization is case-insensitive."""
        assert normalize_tag("ROGUELIKE") == "Rogue-like"
        assert normalize_tag("roguelike") == "Rogue-like"
        assert normalize_tag("RoGuElIkE") == "Rogue-like"

    def test_unknown_tag_passes_through(self):
        """Test unknown tag returns original value unchanged."""
        assert normalize_tag("Action") == "Action"
        assert normalize_tag("Puzzle") == "Puzzle"

    def test_whitespace_stripped(self):
        """Test whitespace is stripped from tag."""
        assert normalize_tag("  Roguelike  ") == "Rogue-like"
        assert normalize_tag("\tco op\n") == "Co-op"

    def test_already_correct_tag_unchanged(self):
        """Test tag already in correct format passes through."""
        # "Rogue-like" (with hyphen and capital R) is not in the alias map
        # so it should pass through unchanged
        assert normalize_tag("Rogue-like") == "Rogue-like"
        assert normalize_tag("Metroidvania") == "Metroidvania"

    def test_co_op_normalized(self):
        """Test 'co op' normalizes to 'Co-op'."""
        assert normalize_tag("co op") == "Co-op"
        assert normalize_tag("CO OP") == "Co-op"


def _create_bowling_fallback_data() -> dict:
    """Helper to generate mock bowling fallback data (~70 games with known bowling AppIDs)."""
    data = {}
    # Add known bowling AppIDs (fingerprint) - real AppIDs from SteamSpy's bowling fallback
    bowling_appids = [901583, 12210, 2990, 891040, 22230]
    for appid in bowling_appids:
        data[str(appid)] = {
            "appid": str(appid),
            "name": f"Bowling Game {appid}",
            "owners": "1,000 .. 2,000",
            "ccu": 5,
            "positive": 100,
            "negative": 10,
            "average_forever": 600,
            "average_2weeks": 60,
            "price": "999"
        }

    # Add filler games to reach ~70 total
    for i in range(100000, 100065):
        data[str(i)] = {
            "appid": str(i),
            "name": f"Game {i}",
            "owners": "500 .. 1,000",
            "ccu": 3,
            "positive": 50,
            "negative": 5,
            "average_forever": 300,
            "average_2weeks": 30,
            "price": "499"
        }

    return data


class TestBowlingFallbackDetection:
    """Test bowling fallback detection in search_by_tag and aggregate_engagement."""

    @pytest.mark.asyncio
    async def test_bowling_fallback_detected_returns_error(self, mock_http):
        """Test bowling fallback (70 games with known AppIDs) returns tag_not_found error."""
        fallback_data = _create_bowling_fallback_data()
        mock_http.get_with_metadata.return_value = (
            fallback_data,
            datetime.now(timezone.utc),
            0
        )

        client = SteamSpyClient(mock_http)
        result = await client.search_by_tag("NonexistentTag123")

        assert isinstance(result, APIError)
        assert result.error_type == "tag_not_found"
        assert "not recognized by SteamSpy" in result.message

    @pytest.mark.asyncio
    async def test_legitimate_small_tag_not_flagged(self, mock_http):
        """Test legitimate small tag (70 games, no bowling AppIDs) passes through."""
        # Create 70 games WITHOUT bowling AppIDs
        legitimate_data = {}
        for i in range(200000, 200070):
            legitimate_data[str(i)] = {
                "appid": str(i),
                "name": f"Obscure Game {i}",
                "owners": "500 .. 1,000",
                "ccu": 5,
                "positive": 50,
                "negative": 5,
                "average_forever": 300,
                "average_2weeks": 30,
                "price": "999"
            }

        mock_http.get_with_metadata.return_value = (
            legitimate_data,
            datetime.now(timezone.utc),
            0
        )

        client = SteamSpyClient(mock_http)
        result = await client.search_by_tag("ObscureButRealTag")

        # Should NOT be flagged as fallback - legitimate small tag
        assert isinstance(result, SearchResult)
        assert len(result.appids) == 10  # default limit

    @pytest.mark.asyncio
    async def test_large_result_set_not_flagged(self, mock_http):
        """Test large result set (500+ games) not flagged even if bowling AppIDs present."""
        # Create 500 games including bowling AppIDs (real AppIDs from SteamSpy's bowling fallback)
        large_data = {}
        bowling_appids = [901583, 12210, 2990, 891040, 22230]
        for appid in bowling_appids:
            large_data[str(appid)] = {
                "appid": str(appid),
                "name": f"Bowling Game {appid}",
                "owners": "1,000 .. 2,000",
                "ccu": 5,
                "positive": 100,
                "negative": 10,
                "average_forever": 600,
                "average_2weeks": 60,
                "price": "999"
            }

        for i in range(300000, 300495):
            large_data[str(i)] = {
                "appid": str(i),
                "name": f"Game {i}",
                "owners": "10,000 .. 20,000",
                "ccu": 100,
                "positive": 1000,
                "negative": 100,
                "average_forever": 1200,
                "average_2weeks": 120,
                "price": "1999"
            }

        mock_http.get_with_metadata.return_value = (
            large_data,
            datetime.now(timezone.utc),
            0
        )

        client = SteamSpyClient(mock_http)
        result = await client.search_by_tag("PopularTag")

        # Should NOT be flagged - large result sets are legitimate
        assert isinstance(result, SearchResult)
        assert result.total_found == 500

    @pytest.mark.asyncio
    async def test_aggregate_bowling_fallback_returns_error(self, mock_http):
        """Test bowling fallback in aggregate_engagement returns tag_not_found error."""
        fallback_data = _create_bowling_fallback_data()
        mock_http.get_with_metadata.return_value = (
            fallback_data,
            datetime.now(timezone.utc),
            0
        )

        client = SteamSpyClient(mock_http)
        result = await client.aggregate_engagement(tag="FakeTag")

        assert isinstance(result, APIError)
        assert result.error_type == "tag_not_found"
        assert "not recognized by SteamSpy" in result.message

    @pytest.mark.asyncio
    async def test_tag_normalization_applied_in_search(self, mock_http):
        """Test tag normalization is applied before API call in search_by_tag."""
        # Return valid data (2 games triggers cross-validation <5000 threshold)
        valid_data = {
            "646570": {"name": "Slay the Spire"},
            "2379780": {"name": "Balatro"},
        }
        mock_http.get_with_metadata.return_value = (
            valid_data,
            datetime.now(timezone.utc),
            0
        )

        client = SteamSpyClient(mock_http)
        result = await client.search_by_tag("Roguelike")

        # Verify the FIRST API call (bulk tag search) used normalized tag
        # Note: Cross-validation for niche tags makes additional appdetails calls
        assert mock_http.get_with_metadata.call_count >= 1
        first_call_args = mock_http.get_with_metadata.call_args_list[0]
        assert first_call_args[1]["params"]["tag"] == "Rogue-like"

    @pytest.mark.asyncio
    async def test_tag_normalization_applied_in_aggregation(self, mock_http):
        """Test tag normalization is applied before API call in aggregate_engagement."""
        # Return valid data with at least 2 games for stats
        valid_data = {
            "646570": {
                "appid": "646570",
                "name": "Slay the Spire",
                "owners": "2,000,000 .. 5,000,000",
                "ccu": 1500,
                "positive": 82000,
                "negative": 1500,
                "average_forever": 4200,
                "average_2weeks": 420,
                "price": "2499"
            },
            "2379780": {
                "appid": "2379780",
                "name": "Balatro",
                "owners": "1,000,000 .. 2,000,000",
                "ccu": 800,
                "positive": 50000,
                "negative": 500,
                "average_forever": 3000,
                "average_2weeks": 600,
                "price": "1499"
            }
        }

        # Mock side_effect for tag + appdetails calls
        def mock_side_effect(*args, **kwargs):
            params = kwargs.get("params", {})
            if params.get("request") == "tag":
                return (valid_data, datetime.now(timezone.utc), 0)
            elif params.get("request") == "appdetails":
                appid = params.get("appid")
                game_data = valid_data.get(appid, {})
                game_with_tags = dict(game_data)
                game_with_tags["tags"] = {"Rogue-like": 100, "Indie": 90}
                return (game_with_tags, datetime.now(timezone.utc), 0)

        mock_http.get_with_metadata.side_effect = mock_side_effect

        client = SteamSpyClient(mock_http)
        result = await client.aggregate_engagement(tag="Roguelike")

        # Verify the API was called with normalized tag
        # First call should be the bulk tag call
        first_call_args = mock_http.get_with_metadata.call_args_list[0]
        assert first_call_args[1]["params"]["tag"] == "Rogue-like"


class TestTagCrossValidation:
    """Test that aggregate_engagement uses bulk data directly (no per-game cross-validation)."""

    @pytest.mark.asyncio
    async def test_aggregate_filters_games_without_searched_tag(self, mock_http):
        """Niche tags use bulk data directly without per-game validation.

        After removing cross-validation, ALL bulk games are returned regardless of
        whether each individual game has the tag in its per-game tag list.
        """
        # Bulk endpoint returns 5 games
        bulk_response = {
            "1": {"appid": 1, "name": "Game1", "owners": "100000 .. 200000", "ccu": 100, "positive": 500, "negative": 50, "average_forever": 600, "average_2weeks": 60, "median_forever": 300, "median_2weeks": 30, "price": "999"},
            "2": {"appid": 2, "name": "Game2", "owners": "90000 .. 180000", "ccu": 90, "positive": 450, "negative": 45, "average_forever": 540, "average_2weeks": 54, "median_forever": 270, "median_2weeks": 27, "price": "899"},
            "3": {"appid": 3, "name": "Game3", "owners": "80000 .. 160000", "ccu": 80, "positive": 400, "negative": 40, "average_forever": 480, "average_2weeks": 48, "median_forever": 240, "median_2weeks": 24, "price": "799"},
            "4": {"appid": 4, "name": "Game4", "owners": "70000 .. 140000", "ccu": 70, "positive": 350, "negative": 35, "average_forever": 420, "average_2weeks": 42, "median_forever": 210, "median_2weeks": 21, "price": "699"},
            "5": {"appid": 5, "name": "Game5", "owners": "60000 .. 120000", "ccu": 60, "positive": 300, "negative": 30, "average_forever": 360, "average_2weeks": 36, "median_forever": 180, "median_2weeks": 18, "price": "599"},
        }

        # Only the bulk tag call — no appdetails calls needed
        mock_http.get_with_metadata.return_value = (bulk_response, datetime.now(timezone.utc), 0)

        client = SteamSpyClient(mock_http)
        result = await client.aggregate_engagement(tag="Indie")

        # All 5 bulk games returned (no filtering)
        assert isinstance(result, AggregateEngagementData)
        assert result.games_analyzed == 5
        assert len(result.games) == 5
        # Verify all appids are in results
        appids_in_results = {g.appid for g in result.games}
        assert appids_in_results == {1, 2, 3, 4, 5}

    @pytest.mark.asyncio
    async def test_aggregate_tag_validation_bounded(self, mock_http):
        """Niche tag returns all bulk games without per-game validation calls.

        After removing cross-validation, only a single bulk endpoint call is made
        (no per-game appdetails calls). All 200 games are returned directly.
        """
        # Bulk endpoint returns 200 games (niche tag: < TAG_CROSSVAL_THRESHOLD)
        bulk_response = {
            str(i): {
                "appid": i,
                "name": f"Game{i}",
                "owners": f"{100000 - i*100} .. {200000 - i*100}",
                "ccu": 100,
                "positive": 500,
                "negative": 50,
                "average_forever": 600,
                "average_2weeks": 60,
                "median_forever": 300,
                "median_2weeks": 30,
                "price": "999"
            }
            for i in range(1, 201)
        }

        mock_http.get_with_metadata.return_value = (bulk_response, datetime.now(timezone.utc), 0)

        client = SteamSpyClient(mock_http)
        result = await client.aggregate_engagement(tag="Indie")

        # Only 1 HTTP call: the bulk tag call — no per-game appdetails calls
        assert mock_http.get_with_metadata.call_count == 1
        assert isinstance(result, AggregateEngagementData)
        # All 200 games returned
        assert result.games_analyzed == 200

    @pytest.mark.asyncio
    async def test_aggregate_includes_tags_in_summary(self, mock_http):
        """Bulk endpoint does not provide per-game tags (tags require individual appdetails calls).

        After removing cross-validation, bulk-parsed GameEngagementSummary objects
        have tags={} since the SteamSpy tag bulk endpoint does not return per-game tag dicts.
        """
        bulk_response = {
            "1": {"appid": 1, "name": "Game1", "owners": "100000 .. 200000", "ccu": 100, "positive": 500, "negative": 50, "average_forever": 600, "average_2weeks": 60, "median_forever": 300, "median_2weeks": 30, "price": "999"},
            "2": {"appid": 2, "name": "Game2", "owners": "90000 .. 180000", "ccu": 90, "positive": 450, "negative": 45, "average_forever": 540, "average_2weeks": 54, "median_forever": 270, "median_2weeks": 27, "price": "899"},
        }

        # Only the bulk tag call
        mock_http.get_with_metadata.return_value = (bulk_response, datetime.now(timezone.utc), 0)

        client = SteamSpyClient(mock_http)
        result = await client.aggregate_engagement(tag="Indie")

        assert isinstance(result, AggregateEngagementData)
        assert len(result.games) == 2
        # Bulk endpoint does not return per-game tags — tags={} for all games
        for game in result.games:
            assert isinstance(game.tags, dict)
            assert game.tags == {}


# ---------------------------------------------------------------------------
# Helper for aggregate tests with tag path mock
# ---------------------------------------------------------------------------

def _make_tag_mock_side_effect(tag_data, tag_name="Test"):
    """Create a mock_side_effect function for tag-based aggregate tests.

    Args:
        tag_data: Dict of appid_str -> game data for the bulk endpoint
        tag_name: Tag name that should appear in appdetails tags
    """
    def mock_side_effect(*args, **kwargs):
        params = kwargs.get("params", {})
        if params.get("request") == "tag":
            return (tag_data, datetime.now(timezone.utc), 0)
        elif params.get("request") == "appdetails":
            appid = params.get("appid")
            game_data = tag_data.get(appid, {})
            game_with_tags = dict(game_data)
            game_with_tags["tags"] = {tag_name: 100}
            return (game_with_tags, datetime.now(timezone.utc), 0)
    return mock_side_effect


def _make_game(appid, owners="100,000 .. 200,000", ccu=100, positive=500,
               negative=50, average_forever=600, average_2weeks=60, price="999"):
    """Create a minimal game data dict for tag_data."""
    return {
        "appid": str(appid),
        "name": f"Game {appid}",
        "owners": owners,
        "ccu": ccu,
        "positive": positive,
        "negative": negative,
        "average_forever": average_forever,
        "average_2weeks": average_2weeks,
        "price": price,
    }


# ---------------------------------------------------------------------------
# TestAggregateSortOptions
# ---------------------------------------------------------------------------

class TestAggregateSortOptions:
    """Test sort_by='reviews' and sort_by='playtime' options."""

    @pytest.mark.asyncio
    async def test_sort_by_reviews(self, mock_http):
        """Sorting by reviews orders by total reviews (positive + negative) descending."""
        tag_data = {
            "1": _make_game(1, positive=100, negative=10),     # 110 total
            "2": _make_game(2, positive=5000, negative=500),   # 5500 total
            "3": _make_game(3, positive=1000, negative=100),   # 1100 total
        }
        mock_http.get_with_metadata.side_effect = _make_tag_mock_side_effect(tag_data)

        client = SteamSpyClient(mock_http)
        result = await client.aggregate_engagement(tag="Test", sort_by="reviews")

        assert isinstance(result, AggregateEngagementData)
        assert result.sort_by == "reviews"
        assert result.games[0].appid == 2   # 5500
        assert result.games[1].appid == 3   # 1100
        assert result.games[2].appid == 1   # 110

    @pytest.mark.asyncio
    async def test_sort_by_playtime(self, mock_http):
        """Sorting by playtime orders by average_forever descending."""
        tag_data = {
            "1": _make_game(1, average_forever=60),    # 1 hour
            "2": _make_game(2, average_forever=6000),  # 100 hours
            "3": _make_game(3, average_forever=1200),  # 20 hours
        }
        mock_http.get_with_metadata.side_effect = _make_tag_mock_side_effect(tag_data)

        client = SteamSpyClient(mock_http)
        result = await client.aggregate_engagement(tag="Test", sort_by="playtime")

        assert isinstance(result, AggregateEngagementData)
        assert result.sort_by == "playtime"
        assert result.games[0].appid == 2   # 100 hrs
        assert result.games[1].appid == 3   # 20 hrs
        assert result.games[2].appid == 1   # 1 hr

    @pytest.mark.asyncio
    async def test_unknown_sort_by_defaults_to_owners(self, mock_http):
        """Unknown sort_by value silently defaults to owners sorting."""
        tag_data = {
            "1": _make_game(1, owners="100 .. 200"),               # midpoint 150
            "2": _make_game(2, owners="1,000,000 .. 2,000,000"),   # midpoint 1.5M
            "3": _make_game(3, owners="10,000 .. 20,000"),         # midpoint 15k
        }
        mock_http.get_with_metadata.side_effect = _make_tag_mock_side_effect(tag_data)

        client = SteamSpyClient(mock_http)
        result = await client.aggregate_engagement(tag="Test", sort_by="invalid_sort")

        assert isinstance(result, AggregateEngagementData)
        # Should fall back to owners sort
        assert result.games[0].appid == 2   # 1.5M
        assert result.games[1].appid == 3   # 15k
        assert result.games[2].appid == 1   # 150


# ---------------------------------------------------------------------------
# TestAggregateAllZeroMetrics
# ---------------------------------------------------------------------------

class TestAggregateAllZeroMetrics:
    """Test stats computation when all games have zero values for a metric."""

    @pytest.mark.asyncio
    async def test_all_zero_owners_returns_none_stats(self, mock_http):
        """All games with 0 owners → owners_stats is None (filtered out)."""
        tag_data = {
            "1": _make_game(1, owners="0 .. 0", ccu=10),
            "2": _make_game(2, owners="0 .. 0", ccu=20),
            "3": _make_game(3, owners="0 .. 0", ccu=30),
        }
        mock_http.get_with_metadata.side_effect = _make_tag_mock_side_effect(tag_data)

        client = SteamSpyClient(mock_http)
        result = await client.aggregate_engagement(tag="Test")

        assert isinstance(result, AggregateEngagementData)
        assert result.owners_stats is None
        # CCU stats should still work (zeros included for CCU)
        assert result.ccu_stats is not None

    @pytest.mark.asyncio
    async def test_all_zero_playtime_returns_none_stats(self, mock_http):
        """All games with 0 playtime → playtime_stats is None."""
        tag_data = {
            "1": _make_game(1, average_forever=0, average_2weeks=0),
            "2": _make_game(2, average_forever=0, average_2weeks=0),
            "3": _make_game(3, average_forever=0, average_2weeks=0),
        }
        mock_http.get_with_metadata.side_effect = _make_tag_mock_side_effect(tag_data)

        client = SteamSpyClient(mock_http)
        result = await client.aggregate_engagement(tag="Test")

        assert isinstance(result, AggregateEngagementData)
        assert result.playtime_stats is None

    @pytest.mark.asyncio
    async def test_all_zero_ccu_still_computes_stats(self, mock_http):
        """All games with 0 CCU → ccu_stats still computed (zeros are real measurements)."""
        tag_data = {
            "1": _make_game(1, ccu=0),
            "2": _make_game(2, ccu=0),
            "3": _make_game(3, ccu=0),
        }
        mock_http.get_with_metadata.side_effect = _make_tag_mock_side_effect(tag_data)

        client = SteamSpyClient(mock_http)
        result = await client.aggregate_engagement(tag="Test")

        assert isinstance(result, AggregateEngagementData)
        assert result.ccu_stats is not None
        assert result.ccu_stats.mean == 0.0
        assert result.ccu_stats.median == 0.0
        assert result.ccu_stats.min == 0.0
        assert result.ccu_stats.max == 0.0


# ---------------------------------------------------------------------------
# TestAggregateMarketWeightedZeroDivision
# ---------------------------------------------------------------------------

class TestAggregateMarketWeightedZeroDivision:
    """Test market-weighted ratios with zero denominators."""

    @pytest.mark.asyncio
    async def test_zero_total_owners_ccu_ratio_none(self, mock_http):
        """All games with 0 owners → market-weighted ccu_ratio is None."""
        tag_data = {
            "1": _make_game(1, owners="0 .. 0", ccu=10),
            "2": _make_game(2, owners="0 .. 0", ccu=20),
        }
        mock_http.get_with_metadata.side_effect = _make_tag_mock_side_effect(tag_data)

        client = SteamSpyClient(mock_http)
        result = await client.aggregate_engagement(tag="Test")

        assert result.market_weighted["ccu_ratio"] is None
        # Per-game also None (all owners are 0, no valid ratios)
        assert result.per_game_average["ccu_ratio"] is None

    @pytest.mark.asyncio
    async def test_zero_total_reviews_review_score_none(self, mock_http):
        """All games with 0 reviews → market-weighted review_score is None."""
        tag_data = {
            "1": _make_game(1, positive=0, negative=0),
            "2": _make_game(2, positive=0, negative=0),
        }
        mock_http.get_with_metadata.side_effect = _make_tag_mock_side_effect(tag_data)

        client = SteamSpyClient(mock_http)
        result = await client.aggregate_engagement(tag="Test")

        assert result.market_weighted["review_score"] is None
        assert result.per_game_average["review_score"] is None

    @pytest.mark.asyncio
    async def test_zero_total_playtime_activity_ratio_none(self, mock_http):
        """All games with 0 average_forever → market-weighted activity_ratio is None."""
        tag_data = {
            "1": _make_game(1, average_forever=0, average_2weeks=0),
            "2": _make_game(2, average_forever=0, average_2weeks=0),
        }
        mock_http.get_with_metadata.side_effect = _make_tag_mock_side_effect(tag_data)

        client = SteamSpyClient(mock_http)
        result = await client.aggregate_engagement(tag="Test")

        assert result.market_weighted["activity_ratio"] is None
        assert result.per_game_average["activity_ratio"] is None


# ---------------------------------------------------------------------------
# TestAggregateSingleGame
# ---------------------------------------------------------------------------

class TestAggregateSingleGame:
    """Test aggregation with only 1 game (n=1 edge case)."""

    @pytest.mark.asyncio
    async def test_single_game_tag_stats_none(self, mock_http):
        """Single game in tag results → all stats None (need >= 2 for quantiles)."""
        tag_data = {
            "1": _make_game(1, ccu=500, owners="100,000 .. 200,000"),
        }
        mock_http.get_with_metadata.side_effect = _make_tag_mock_side_effect(tag_data)

        client = SteamSpyClient(mock_http)
        result = await client.aggregate_engagement(tag="Test")

        assert isinstance(result, AggregateEngagementData)
        assert result.games_analyzed == 1
        assert result.ccu_stats is None
        assert result.owners_stats is None
        assert result.playtime_stats is None
        assert result.review_score_stats is None

    @pytest.mark.asyncio
    async def test_single_game_appid_stats_none(self, mock_http):
        """Single AppID → all stats None but market_weighted still computed."""
        game_data = {
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
            "price": "2499",
        }
        mock_http.get_with_metadata.return_value = (
            game_data, datetime.now(timezone.utc), 0
        )

        client = SteamSpyClient(mock_http)
        result = await client.aggregate_engagement(appids=[646570])

        assert isinstance(result, AggregateEngagementData)
        assert result.games_analyzed == 1
        assert result.ccu_stats is None
        # Market-weighted ratios still computed with single game
        assert result.market_weighted["ccu_ratio"] is not None
        assert result.market_weighted["review_score"] is not None

    @pytest.mark.asyncio
    async def test_single_game_per_game_list_populated(self, mock_http):
        """Single game aggregation still returns 1 game in the games list."""
        tag_data = {
            "1": _make_game(1, ccu=500),
        }
        mock_http.get_with_metadata.side_effect = _make_tag_mock_side_effect(tag_data)

        client = SteamSpyClient(mock_http)
        result = await client.aggregate_engagement(tag="Test")

        assert len(result.games) == 1
        assert result.games[0].appid == 1


# ---------------------------------------------------------------------------
# TestAggregateAppIdDuplicates
# ---------------------------------------------------------------------------

class TestAggregateAppIdDuplicates:
    """Test AppID list with duplicate entries."""

    @pytest.mark.asyncio
    async def test_duplicate_appids_fetched_twice(self, mock_http):
        """Duplicate AppIDs in list are fetched separately (no dedup in client)."""
        game_data = {
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
            "price": "2499",
        }
        mock_http.get_with_metadata.return_value = (
            game_data, datetime.now(timezone.utc), 0
        )

        client = SteamSpyClient(mock_http)
        result = await client.aggregate_engagement(appids=[646570, 646570])

        assert isinstance(result, AggregateEngagementData)
        # Both fetches succeed — client doesn't dedup
        assert result.games_total == 2
        assert result.games_analyzed == 2


# ---------------------------------------------------------------------------
# TestAggregateIdenticalValues
# ---------------------------------------------------------------------------

class TestAggregateIdenticalValues:
    """Test aggregation when all games have identical metric values."""

    @pytest.mark.asyncio
    async def test_identical_ccu_stats(self, mock_http):
        """All games with same CCU → mean=median=p25=p75=min=max."""
        tag_data = {
            str(i): _make_game(i, ccu=500)
            for i in range(1, 6)
        }
        mock_http.get_with_metadata.side_effect = _make_tag_mock_side_effect(tag_data)

        client = SteamSpyClient(mock_http)
        result = await client.aggregate_engagement(tag="Test")

        assert result.ccu_stats is not None
        assert result.ccu_stats.mean == 500.0
        assert result.ccu_stats.median == 500.0
        assert result.ccu_stats.min == 500.0
        assert result.ccu_stats.max == 500.0
        assert result.ccu_stats.p25 == 500.0
        assert result.ccu_stats.p75 == 500.0

    @pytest.mark.asyncio
    async def test_identical_review_scores(self, mock_http):
        """All games with same review ratio → identical stats."""
        tag_data = {
            str(i): _make_game(i, positive=900, negative=100)
            for i in range(1, 6)
        }
        mock_http.get_with_metadata.side_effect = _make_tag_mock_side_effect(tag_data)

        client = SteamSpyClient(mock_http)
        result = await client.aggregate_engagement(tag="Test")

        assert result.review_score_stats is not None
        assert result.review_score_stats.mean == 90.0
        assert result.review_score_stats.median == 90.0


# ---------------------------------------------------------------------------
# TestAggregateMixedNoneReviewScore
# ---------------------------------------------------------------------------

class TestAggregateMixedNoneReviewScore:
    """Test stats when some games have review_score=None (0 reviews)."""

    @pytest.mark.asyncio
    async def test_mixed_none_review_scores(self, mock_http):
        """Games with 0 reviews (review_score=None) excluded from review_score_stats."""
        tag_data = {
            "1": _make_game(1, positive=900, negative=100),   # score = 90.0
            "2": _make_game(2, positive=0, negative=0),       # score = None
            "3": _make_game(3, positive=800, negative=200),   # score = 80.0
            "4": _make_game(4, positive=0, negative=0),       # score = None
            "5": _make_game(5, positive=950, negative=50),    # score = 95.0
        }
        mock_http.get_with_metadata.side_effect = _make_tag_mock_side_effect(tag_data)

        client = SteamSpyClient(mock_http)
        result = await client.aggregate_engagement(tag="Test")

        assert result.review_score_stats is not None
        # Only 3 games with valid review scores: 90, 80, 95
        assert result.review_score_stats.games_with_data == 3
        # Mean = (90 + 80 + 95) / 3 = 88.33
        assert result.review_score_stats.mean == pytest.approx(88.33, abs=0.01)
        assert result.review_score_stats.min == 80.0
        assert result.review_score_stats.max == 95.0

    @pytest.mark.asyncio
    async def test_only_one_valid_review_score_returns_none(self, mock_http):
        """Only 1 game with reviews, rest have 0 → review_score_stats None (need >= 2)."""
        tag_data = {
            "1": _make_game(1, positive=900, negative=100),   # score = 90.0
            "2": _make_game(2, positive=0, negative=0),       # score = None
            "3": _make_game(3, positive=0, negative=0),       # score = None
        }
        mock_http.get_with_metadata.side_effect = _make_tag_mock_side_effect(tag_data)

        client = SteamSpyClient(mock_http)
        result = await client.aggregate_engagement(tag="Test")

        # Only 1 valid review score → below minimum of 2
        assert result.review_score_stats is None
        # Per-game average still computed (1 valid score)
        assert result.per_game_average["review_score"] is not None
        assert result.per_game_average["review_score"] == 90.0
