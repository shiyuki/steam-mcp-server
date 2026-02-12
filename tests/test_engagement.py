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
