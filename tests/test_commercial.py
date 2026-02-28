"""Tests for commercial data pipeline: GamalyticClient and fetch_commercial tool."""

import json
import pytest
import httpx
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch, call

from src.config import Config
from src.schemas import CommercialData, APIError
from src.steam_api import GamalyticClient


@pytest.fixture
def mock_http():
    """Mock CachedAPIClient with get_with_metadata method."""
    client = AsyncMock()
    return client


class TestGamalyticClient:
    """Test GamalyticClient directly with various scenarios."""

    @pytest.mark.asyncio
    async def test_gamalytic_api_success(self, mock_http):
        """Test successful Gamalytic API response with +/-20% range."""
        gamalytic_data = {
            "steamId": "504230",
            "name": "Celeste",
            "price": 19.99,
            "revenue": 5000000,
            "reviews": 8000,
            "reviewScore": 95
        }
        # First call: Gamalytic API (successful)
        # Second call: SteamSpy for triangulation (we'll mock it to avoid warning)
        steamspy_data = {
            "positive": 8000,
            "negative": 200,
            "price": "1999",
            "owners": "400,000 .. 600,000"  # Midpoint 500k * 19.99 * 0.7 = ~7M (within 20%)
        }
        mock_http.get_with_metadata.side_effect = [
            (gamalytic_data, datetime.now(timezone.utc), 0),
            (steamspy_data, datetime.now(timezone.utc), 0)
        ]

        client = GamalyticClient(mock_http)
        result = await client.get_revenue_estimate(504230)

        assert isinstance(result, CommercialData)
        assert result.appid == 504230
        assert result.name == "Celeste"
        assert result.price == 19.99
        assert result.source == "gamalytic"
        assert result.confidence == "high"
        # +/-20% range
        assert result.revenue_min == 5000000 * 0.80
        assert result.revenue_max == 5000000 * 1.20
        assert result.currency == "USD"

    @pytest.mark.asyncio
    async def test_gamalytic_api_unavailable_falls_back_to_reviews(self, mock_http):
        """Test fallback to review-based estimation when Gamalytic API is unavailable."""
        # First call: Gamalytic fails with 503
        gamalytic_request = httpx.Request("GET", "https://api.gamalytic.com/game/646570")
        gamalytic_response = httpx.Response(503, request=gamalytic_request)

        # Second call: SteamSpy succeeds
        steamspy_data = {
            "positive": 8000,
            "negative": 2000,
            "price": "2499",
            "owners": "500,000 .. 1,000,000"
        }

        # Third call: Steam Web API player count (second-tier fallback, fails silently)
        steam_web_api_request = httpx.Request(
            "GET",
            "https://api.steampowered.com/ISteamUserStats/GetNumberOfCurrentPlayers/v1/"
        )
        steam_web_api_response = httpx.Response(503, request=steam_web_api_request)

        mock_http.get_with_metadata.side_effect = [
            httpx.HTTPStatusError("Service unavailable", request=gamalytic_request, response=gamalytic_response),
            (steamspy_data, datetime.now(timezone.utc), 0),
            httpx.HTTPStatusError("Service unavailable", request=steam_web_api_request, response=steam_web_api_response),
        ]

        client = GamalyticClient(mock_http)
        result = await client.get_revenue_estimate(646570)

        assert isinstance(result, CommercialData)
        assert result.source == "review_estimate"
        assert result.method == "review_multiplier"
        assert result.confidence == "low"
        # Total reviews = 8000 + 2000 = 10000
        assert result.revenue_min == 10000 * 30
        assert result.revenue_max == 10000 * 50
        # SteamSpy price in cents -> dollars
        assert result.price == 24.99

    @pytest.mark.asyncio
    async def test_gamalytic_connection_error_falls_back(self, mock_http):
        """Test fallback when Gamalytic has connection error."""
        # First call: Gamalytic connection error
        # Second call: SteamSpy succeeds
        steamspy_data = {
            "positive": 5000,
            "negative": 500,
            "price": "999",
            "owners": "100,000 .. 200,000"
        }

        # Third call: Steam Web API player count (second-tier fallback, fails silently)
        steam_web_api_request = httpx.Request(
            "GET",
            "https://api.steampowered.com/ISteamUserStats/GetNumberOfCurrentPlayers/v1/"
        )
        steam_web_api_response = httpx.Response(503, request=steam_web_api_request)

        mock_http.get_with_metadata.side_effect = [
            ConnectionError("DNS resolution failed"),
            (steamspy_data, datetime.now(timezone.utc), 0),
            httpx.HTTPStatusError("Service unavailable", request=steam_web_api_request, response=steam_web_api_response),
        ]

        client = GamalyticClient(mock_http)
        result = await client.get_revenue_estimate(123456)

        assert isinstance(result, CommercialData)
        assert result.source == "review_estimate"
        assert result.confidence == "low"
        # Total reviews = 5500
        assert result.revenue_min == 5500 * 30
        assert result.revenue_max == 5500 * 50

    @pytest.mark.asyncio
    async def test_review_fallback_no_reviews_returns_error(self, mock_http):
        """Test that fallback returns error when game has no reviews."""
        # Gamalytic fails, SteamSpy has no reviews
        gamalytic_request = httpx.Request("GET", "https://api.gamalytic.com/game/999")
        gamalytic_response = httpx.Response(404, request=gamalytic_request)

        steamspy_data = {
            "positive": 0,
            "negative": 0,
            "price": "999"
        }

        mock_http.get_with_metadata.side_effect = [
            httpx.HTTPStatusError("Not found", request=gamalytic_request, response=gamalytic_response),
            (steamspy_data, datetime.now(timezone.utc), 0)
        ]

        client = GamalyticClient(mock_http)
        result = await client.get_revenue_estimate(999)

        assert isinstance(result, APIError)
        assert result.error_code == 404
        assert result.error_type == "not_found"
        assert "no reviews" in result.message.lower()

    @pytest.mark.asyncio
    async def test_review_fallback_free_to_play_price(self, mock_http):
        """Test review fallback handles free-to-play games (price=0)."""
        gamalytic_request = httpx.Request("GET", "https://api.gamalytic.com/game/570")
        gamalytic_response = httpx.Response(404, request=gamalytic_request)

        steamspy_data = {
            "positive": 5000,
            "negative": 500,
            "price": "0"  # Free-to-play
        }

        # Third call: Steam Web API player count (second-tier fallback, fails silently)
        steam_web_api_request = httpx.Request(
            "GET",
            "https://api.steampowered.com/ISteamUserStats/GetNumberOfCurrentPlayers/v1/"
        )
        steam_web_api_response = httpx.Response(503, request=steam_web_api_request)

        mock_http.get_with_metadata.side_effect = [
            httpx.HTTPStatusError("Not found", request=gamalytic_request, response=gamalytic_response),
            (steamspy_data, datetime.now(timezone.utc), 0),
            httpx.HTTPStatusError("Not found", request=steam_web_api_request, response=steam_web_api_response),
        ]

        client = GamalyticClient(mock_http)
        result = await client.get_revenue_estimate(570)

        assert isinstance(result, CommercialData)
        assert result.price is None  # Free-to-play
        # Revenue still calculated from reviews
        assert result.revenue_min == 5500 * 30
        assert result.revenue_max == 5500 * 50

    @pytest.mark.asyncio
    async def test_triangulation_warning_fires(self, mock_http):
        """Phase 13: Triangulation removed — Gamalytic is trusted primary source.

        Gamalytic success path no longer calls SteamSpy for triangulation.
        triangulation_warning is always None on success path (schema field retained for compat).
        """
        gamalytic_data = {
            "steamId": "12345",
            "name": "Test Game",
            "price": 24.99,
            "revenue": 10000000  # $10M
        }

        mock_http.get_with_metadata.side_effect = [
            (gamalytic_data, datetime.now(timezone.utc), 0),
        ]

        client = GamalyticClient(mock_http)
        result = await client.get_revenue_estimate(12345)

        assert isinstance(result, CommercialData)
        assert result.source == "gamalytic"
        # Phase 13: triangulation removed; warning is always None on success path
        assert result.triangulation_warning is None
        assert result.steamspy_implied_revenue is None
        assert result.gamalytic_revenue is None
        # Exactly 1 HTTP call (Gamalytic only — no SteamSpy triangulation)
        assert mock_http.get_with_metadata.call_count == 1

    @pytest.mark.asyncio
    async def test_triangulation_no_warning_when_close(self, mock_http):
        """Gamalytic success path: triangulation_warning always None (Phase 13: removed)."""
        gamalytic_data = {
            "steamId": "67890",
            "name": "Close Match Game",
            "price": 14.99,
            "revenue": 5000000  # $5M
        }

        mock_http.get_with_metadata.side_effect = [
            (gamalytic_data, datetime.now(timezone.utc), 0),
        ]

        client = GamalyticClient(mock_http)
        result = await client.get_revenue_estimate(67890)

        assert isinstance(result, CommercialData)
        assert result.triangulation_warning is None

    @pytest.mark.asyncio
    async def test_triangulation_failure_does_not_break_response(self, mock_http):
        """Gamalytic success path returns CommercialData; no triangulation call made."""
        gamalytic_data = {
            "steamId": "11111",
            "name": "Test Game",
            "price": 19.99,
            "revenue": 3000000
        }

        mock_http.get_with_metadata.side_effect = [
            (gamalytic_data, datetime.now(timezone.utc), 0),
        ]

        client = GamalyticClient(mock_http)
        result = await client.get_revenue_estimate(11111)

        assert isinstance(result, CommercialData)
        assert result.source == "gamalytic"
        # Phase 13: triangulation removed — no SteamSpy call, no warning
        assert result.triangulation_warning is None
        assert result.steamspy_implied_revenue is None
        assert result.gamalytic_revenue is None

    @pytest.mark.asyncio
    async def test_steamspy_owners_parsing_with_commas(self, mock_http):
        """Gamalytic success path: no SteamSpy call, triangulation removed (Phase 13)."""
        gamalytic_data = {
            "steamId": "22222",
            "name": "Popular Game",
            "price": 29.99,
            "revenue": 60000000  # $60M
        }

        mock_http.get_with_metadata.side_effect = [
            (gamalytic_data, datetime.now(timezone.utc), 0),
        ]

        client = GamalyticClient(mock_http)
        result = await client.get_revenue_estimate(22222)

        assert isinstance(result, CommercialData)
        assert result.source == "gamalytic"
        # Phase 13: triangulation removed — no SteamSpy call
        assert result.triangulation_warning is None
        assert result.steamspy_implied_revenue is None
        # Exactly 1 HTTP call (Gamalytic only)
        assert mock_http.get_with_metadata.call_count == 1

    @pytest.mark.asyncio
    async def test_gamalytic_revenue_range_calculation(self, mock_http):
        """Test +/-20% revenue range calculation from Gamalytic single value."""
        gamalytic_data = {
            "steamId": "33333",
            "name": "Range Test",
            "price": 9.99,
            "revenue": 1000000  # $1M
        }

        steamspy_data = {
            "positive": 1000,
            "negative": 100,
            "price": "999",
            "owners": "100,000 .. 150,000"  # Close enough to avoid warning
        }

        mock_http.get_with_metadata.side_effect = [
            (gamalytic_data, datetime.now(timezone.utc), 0),
            (steamspy_data, datetime.now(timezone.utc), 0)
        ]

        client = GamalyticClient(mock_http)
        result = await client.get_revenue_estimate(33333)

        assert isinstance(result, CommercialData)
        assert result.revenue_min == 1000000 * 0.80  # $800k
        assert result.revenue_max == 1000000 * 1.20  # $1.2M

    @pytest.mark.asyncio
    async def test_gamalytic_price_is_dollars(self, mock_http):
        """Test that Gamalytic returns price in dollars (not cents)."""
        gamalytic_data = {
            "steamId": "44444",
            "name": "Price Test",
            "price": 24.99,  # Dollars
            "revenue": 2000000
        }

        steamspy_data = {
            "positive": 2000,
            "negative": 200,
            "price": "2499",  # Cents
            "owners": "100,000 .. 150,000"
        }

        mock_http.get_with_metadata.side_effect = [
            (gamalytic_data, datetime.now(timezone.utc), 0),
            (steamspy_data, datetime.now(timezone.utc), 0)
        ]

        client = GamalyticClient(mock_http)
        result = await client.get_revenue_estimate(44444)

        assert isinstance(result, CommercialData)
        assert result.price == 24.99  # Not 2499, not 0.2499

    @pytest.mark.asyncio
    async def test_both_apis_fail_returns_error(self, mock_http):
        """Test error when both Gamalytic and SteamSpy fail."""
        gamalytic_request = httpx.Request("GET", "https://api.gamalytic.com/game/999")
        gamalytic_response = httpx.Response(503, request=gamalytic_request)

        steamspy_request = httpx.Request("GET", "https://steamspy.com/api.php")
        steamspy_response = httpx.Response(503, request=steamspy_request)

        mock_http.get_with_metadata.side_effect = [
            httpx.HTTPStatusError("Service unavailable", request=gamalytic_request, response=gamalytic_response),
            httpx.HTTPStatusError("Service unavailable", request=steamspy_request, response=steamspy_response)
        ]

        client = GamalyticClient(mock_http)
        result = await client.get_revenue_estimate(999)

        assert isinstance(result, APIError)
        assert result.error_code == 503
        assert "steamspy" in result.message.lower() or "api error" in result.message.lower()


class TestFetchCommercialValidation:
    """Test fetch_commercial tool input validation."""

    def _make_fetch_commercial(self, gamalytic_mock):
        """Create standalone fetch_commercial with mocked GamalyticClient."""
        async def fetch_commercial(appid: int) -> str:
            if appid <= 0:
                return json.dumps({"error": "appid must be positive", "error_type": "validation"})
            result = await gamalytic_mock.get_revenue_estimate(appid)
            if isinstance(result, APIError):
                return json.dumps(result.model_dump())
            return json.dumps(result.model_dump())
        return fetch_commercial

    @pytest.mark.asyncio
    async def test_negative_appid_returns_validation_error(self):
        """Test negative AppID returns validation error."""
        fn = self._make_fetch_commercial(AsyncMock())
        result = json.loads(await fn(-1))
        assert result["error_type"] == "validation"
        assert "appid must be positive" in result["error"]

    @pytest.mark.asyncio
    async def test_zero_appid_returns_validation_error(self):
        """Test zero AppID returns validation error."""
        fn = self._make_fetch_commercial(AsyncMock())
        result = json.loads(await fn(0))
        assert result["error_type"] == "validation"

    @pytest.mark.asyncio
    async def test_valid_appid_returns_commercial_data(self):
        """Test valid AppID returns commercial data."""
        mock = AsyncMock()
        mock.get_revenue_estimate.return_value = CommercialData(
            appid=646570,
            name="Slay the Spire",
            price=24.99,
            revenue_min=8000000,
            revenue_max=12000000,
            confidence="high",
            source="gamalytic",
            fetched_at=datetime.now(timezone.utc)
        )
        fn = self._make_fetch_commercial(mock)
        result = json.loads(await fn(646570))

        assert result["appid"] == 646570
        assert result["revenue_min"] == 8000000
        assert result["revenue_max"] == 12000000
        assert result["source"] == "gamalytic"

    @pytest.mark.asyncio
    async def test_api_error_forwarded(self):
        """Test API error is forwarded properly."""
        mock = AsyncMock()
        mock.get_revenue_estimate.return_value = APIError(
            error_code=404,
            error_type="not_found",
            message="Game not found"
        )
        fn = self._make_fetch_commercial(mock)
        result = json.loads(await fn(999999))

        assert result["error_code"] == 404
        assert result["error_type"] == "not_found"


# ---------------------------------------------------------------------------
# Phase 8: Extended Gamalytic field tests
# ---------------------------------------------------------------------------

def _make_full_gamalytic_response(appid=646570):
    """Full Gamalytic API response fixture with all 40+ fields."""
    return {
        "steamId": str(appid),
        "name": "Slay the Spire",
        "price": 24.99,
        "revenue": 50000000,
        "copiesSold": 3000000,
        "followers": 450000,
        "accuracy": 0.85,
        "totalRevenue": True,
        "reviewScore": 97,
        "owners": 2800000,
        "players": 15000,
        "reviews": 82000,
        "isEarlyAccess": False,
        "earlyAccessDate": None,
        "history": [
            {  # Entry 0: launch snapshot
                "timeStamp": 1516665600000,  # 2018-01-23
                "reviews": 1000, "price": 15.99, "score": 90, "rank": 50,
                "followers": 50000, "players": 8000, "avgPlaytime": 5.2,
                "sales": 100000, "revenue": 1500000
            },
            {  # Entry 1: recent daily
                "timeStamp": 1708646400000,  # 2024-02-23
                "reviews": 82000, "price": 24.99, "score": 97, "rank": 15,
                "followers": 450000, "players": 14500, "avgPlaytime": 28.3,
                "sales": 3000000, "revenue": 50000000
            },
            {  # Entry 2: another daily
                "timeStamp": 1708560000000,  # 2024-02-22
                "reviews": 81900, "price": 24.99, "score": 97, "rank": 16,
                "followers": 449500, "players": 15200, "avgPlaytime": 28.1,
                "sales": 2990000, "revenue": 49800000
            },
        ],
        "countryData": [
            {"country": "US", "share": 0.35},
            {"country": "CN", "share": 0.15},
            {"country": "DE", "share": 0.08},
            {"country": "GB", "share": 0.07},
            {"country": "RU", "share": 0.06},
            {"country": "FR", "share": 0.05},
            {"country": "JP", "share": 0.04},
            {"country": "BR", "share": 0.04},
            {"country": "KR", "share": 0.03},
            {"country": "CA", "share": 0.03},
            {"country": "AU", "share": 0.02},
            {"country": "IT", "share": 0.02},
        ],
        "audienceOverlap": [
            {"appId": 261550, "name": "Mount & Blade II", "overlap": 0.45, "revenue": 30000000, "followers": 200000, "score": 85},
            {"appId": 322330, "name": "Don't Starve Together", "overlap": 0.38, "revenue": 25000000, "followers": 180000, "score": 91},
        ],
        "alsoPlayed": [
            {"appId": 1145360, "name": "Hades", "revenue": 40000000, "followers": 300000, "score": 95},
            {"appId": 2379780, "name": "Balatro", "revenue": 35000000, "followers": 250000, "score": 96},
        ],
        "estimateDetails": [
            {"model": "review_based", "estimate": 48000000},
            {"model": "owner_based", "estimate": 52000000},
            {"model": "engagement_based", "estimate": 50000000},
        ],
        "dlc": [
            {"name": "Slay the Spire: Downfall", "price": 0, "releaseDate": 1640995200000, "revenue": 0},
        ],
    }


def _make_steamspy_response():
    """Standard SteamSpy mock response for triangulation."""
    return {
        "positive": 78000, "negative": 4000, "price": "2499",
        "owners": "2,000,000 .. 5,000,000"
    }


class TestGamalyticExtendedFields:
    """Tests for Phase 8 extended Gamalytic fields."""

    @pytest.mark.asyncio
    async def test_full_response_parses_all_fields(self, mock_http):
        """Full Gamalytic response returns CommercialData with all extended fields."""
        gamalytic_data = _make_full_gamalytic_response()
        steamspy_data = _make_steamspy_response()
        mock_http.get_with_metadata.side_effect = [
            (gamalytic_data, datetime.now(timezone.utc), 0),
            (steamspy_data, datetime.now(timezone.utc), 0),
        ]

        client = GamalyticClient(mock_http)
        result = await client.get_commercial_data(646570)

        assert isinstance(result, CommercialData)
        assert result.copies_sold == 3000000
        assert result.followers == 450000
        assert result.accuracy == 0.85
        assert result.total_revenue is True
        assert result.review_score == 97.0
        assert result.gamalytic_owners == 2800000
        assert result.gamalytic_players == 15000
        assert result.gamalytic_reviews == 82000

    @pytest.mark.asyncio
    async def test_history_entry_types(self, mock_http):
        """History array tags entry 0 as 'launch', entries 1+ as 'daily'."""
        gamalytic_data = _make_full_gamalytic_response()
        steamspy_data = _make_steamspy_response()
        mock_http.get_with_metadata.side_effect = [
            (gamalytic_data, datetime.now(timezone.utc), 0),
            (steamspy_data, datetime.now(timezone.utc), 0),
        ]

        client = GamalyticClient(mock_http)
        result = await client.get_commercial_data(646570)

        assert result.history[0].entry_type == "launch"
        assert result.history[1].entry_type == "daily"
        assert result.history[2].entry_type == "daily"

    @pytest.mark.asyncio
    async def test_history_timestamps_iso(self, mock_http):
        """History timestamps are converted to ISO 8601 date strings."""
        gamalytic_data = _make_full_gamalytic_response()
        steamspy_data = _make_steamspy_response()
        mock_http.get_with_metadata.side_effect = [
            (gamalytic_data, datetime.now(timezone.utc), 0),
            (steamspy_data, datetime.now(timezone.utc), 0),
        ]

        client = GamalyticClient(mock_http)
        result = await client.get_commercial_data(646570)

        # 1516665600000 ms = 2018-01-23 UTC
        assert result.history[0].timestamp == "2018-01-23"
        # 1708646400000 ms = 2024-02-23 UTC
        assert result.history[1].timestamp == "2024-02-23"

    @pytest.mark.asyncio
    async def test_history_all_fields_present(self, mock_http):
        """Each history entry has all 10 expected fields."""
        gamalytic_data = _make_full_gamalytic_response()
        steamspy_data = _make_steamspy_response()
        mock_http.get_with_metadata.side_effect = [
            (gamalytic_data, datetime.now(timezone.utc), 0),
            (steamspy_data, datetime.now(timezone.utc), 0),
        ]

        client = GamalyticClient(mock_http)
        result = await client.get_commercial_data(646570)

        for entry in result.history:
            assert hasattr(entry, "entry_type")
            assert hasattr(entry, "timestamp")
            assert hasattr(entry, "reviews")
            assert hasattr(entry, "price")
            assert hasattr(entry, "score")
            assert hasattr(entry, "rank")
            assert hasattr(entry, "followers")
            assert hasattr(entry, "gamalytic_players")
            assert hasattr(entry, "avg_playtime")
            assert hasattr(entry, "sales")
            assert hasattr(entry, "revenue")

    @pytest.mark.asyncio
    async def test_country_data_top_10_cap(self, mock_http):
        """Phase 13: Country cap lifted to 50. 12 entries in fixture -> all 12 returned."""
        gamalytic_data = _make_full_gamalytic_response()  # has 12 countries
        mock_http.get_with_metadata.side_effect = [
            (gamalytic_data, datetime.now(timezone.utc), 0),
        ]

        client = GamalyticClient(mock_http)
        result = await client.get_commercial_data(646570)

        # Cap lifted to 50: all 12 fixture entries are returned
        assert len(result.country_data) == 12

    @pytest.mark.asyncio
    async def test_country_data_sorted(self, mock_http):
        """Country data is sorted by share_pct descending (highest first)."""
        gamalytic_data = _make_full_gamalytic_response()
        steamspy_data = _make_steamspy_response()
        mock_http.get_with_metadata.side_effect = [
            (gamalytic_data, datetime.now(timezone.utc), 0),
            (steamspy_data, datetime.now(timezone.utc), 0),
        ]

        client = GamalyticClient(mock_http)
        result = await client.get_commercial_data(646570)

        # US has 0.35 share, should be first
        assert result.country_data[0].country_code == "US"
        assert result.country_data[0].share_pct == 0.35

    @pytest.mark.asyncio
    async def test_audience_overlap_parsed(self, mock_http):
        """2 competitor entries parsed with appid, name, overlap_pct, revenue."""
        gamalytic_data = _make_full_gamalytic_response()
        steamspy_data = _make_steamspy_response()
        mock_http.get_with_metadata.side_effect = [
            (gamalytic_data, datetime.now(timezone.utc), 0),
            (steamspy_data, datetime.now(timezone.utc), 0),
        ]

        client = GamalyticClient(mock_http)
        result = await client.get_commercial_data(646570)

        assert len(result.audience_overlap) == 2
        entry = result.audience_overlap[0]
        assert entry.appid == 261550
        assert "Mount" in entry.name
        assert entry.overlap_pct == 0.45
        assert entry.revenue == 30000000

    @pytest.mark.asyncio
    async def test_also_played_parsed(self, mock_http):
        """Also played entries have None overlap_pct (no overlap field in alsoPlayed)."""
        gamalytic_data = _make_full_gamalytic_response()
        steamspy_data = _make_steamspy_response()
        mock_http.get_with_metadata.side_effect = [
            (gamalytic_data, datetime.now(timezone.utc), 0),
            (steamspy_data, datetime.now(timezone.utc), 0),
        ]

        client = GamalyticClient(mock_http)
        result = await client.get_commercial_data(646570)

        assert len(result.also_played) == 2
        entry = result.also_played[0]
        assert entry.appid == 1145360
        assert entry.name == "Hades"
        assert entry.overlap_pct is None  # alsoPlayed has no overlap field

    @pytest.mark.asyncio
    async def test_estimate_details_parsed(self, mock_http):
        """3 estimate models parsed with model_name and estimate."""
        gamalytic_data = _make_full_gamalytic_response()
        steamspy_data = _make_steamspy_response()
        mock_http.get_with_metadata.side_effect = [
            (gamalytic_data, datetime.now(timezone.utc), 0),
            (steamspy_data, datetime.now(timezone.utc), 0),
        ]

        client = GamalyticClient(mock_http)
        result = await client.get_commercial_data(646570)

        assert len(result.estimate_details) == 3
        model_names = {e.model_name for e in result.estimate_details}
        assert "review_based" in model_names
        assert "owner_based" in model_names
        assert "engagement_based" in model_names

    @pytest.mark.asyncio
    async def test_dlc_parsed(self, mock_http):
        """DLC entry parsed with name, price, release_date, revenue."""
        gamalytic_data = _make_full_gamalytic_response()
        steamspy_data = _make_steamspy_response()
        mock_http.get_with_metadata.side_effect = [
            (gamalytic_data, datetime.now(timezone.utc), 0),
            (steamspy_data, datetime.now(timezone.utc), 0),
        ]

        client = GamalyticClient(mock_http)
        result = await client.get_commercial_data(646570)

        assert len(result.gamalytic_dlc) == 1
        dlc = result.gamalytic_dlc[0]
        assert "Downfall" in dlc.name or "Spire" in dlc.name
        assert dlc.price == 0
        assert dlc.release_date == "2022-01-01"  # 1640995200000 ms = 2022-01-01
        assert dlc.revenue == 0

    @pytest.mark.asyncio
    async def test_gamalytic_prefix_fields(self, mock_http):
        """gamalytic_owners/players/reviews are populated and distinct from CommercialData."""
        gamalytic_data = _make_full_gamalytic_response()
        steamspy_data = _make_steamspy_response()
        mock_http.get_with_metadata.side_effect = [
            (gamalytic_data, datetime.now(timezone.utc), 0),
            (steamspy_data, datetime.now(timezone.utc), 0),
        ]

        client = GamalyticClient(mock_http)
        result = await client.get_commercial_data(646570)

        # All three gamalytic_ prefix fields should be populated
        assert result.gamalytic_owners == 2800000
        assert result.gamalytic_players == 15000
        assert result.gamalytic_reviews == 82000
        # They should be accessible as separate fields
        assert result.gamalytic_owners != result.gamalytic_players
        assert result.gamalytic_owners != result.gamalytic_reviews

    @pytest.mark.asyncio
    async def test_detail_level_summary(self, mock_http):
        """Summary mode strips heavy arrays; core fields still present."""
        gamalytic_data = _make_full_gamalytic_response()
        steamspy_data = _make_steamspy_response()
        mock_http.get_with_metadata.side_effect = [
            (gamalytic_data, datetime.now(timezone.utc), 0),
            (steamspy_data, datetime.now(timezone.utc), 0),
        ]

        client = GamalyticClient(mock_http)
        result = await client.get_commercial_data(646570, detail_level="summary")

        assert isinstance(result, CommercialData)
        # Heavy arrays stripped
        assert result.history == []
        assert result.audience_overlap == []
        assert result.also_played == []
        assert result.gamalytic_dlc == []
        assert result.estimate_details == []
        # Country data capped at 3
        assert len(result.country_data) <= 3
        # Core fields still present
        assert result.copies_sold == 3000000
        assert result.followers == 450000
        assert result.accuracy == 0.85
        assert result.revenue_min == 50000000 * 0.80

    @pytest.mark.asyncio
    async def test_detail_level_full_default(self, mock_http):
        """Default (no detail_level) returns full arrays."""
        gamalytic_data = _make_full_gamalytic_response()
        steamspy_data = _make_steamspy_response()
        mock_http.get_with_metadata.side_effect = [
            (gamalytic_data, datetime.now(timezone.utc), 0),
            (steamspy_data, datetime.now(timezone.utc), 0),
        ]

        client = GamalyticClient(mock_http)
        result = await client.get_commercial_data(646570)  # No detail_level = "full"

        assert len(result.history) == 3
        assert len(result.audience_overlap) == 2
        assert len(result.also_played) == 2
        assert len(result.gamalytic_dlc) == 1
        assert len(result.estimate_details) == 3

    @pytest.mark.asyncio
    async def test_negative_revenue_rejected(self, mock_http):
        """Negative copiesSold is rejected and set to None with warning."""
        gamalytic_data = _make_full_gamalytic_response()
        gamalytic_data["copiesSold"] = -500
        steamspy_data = _make_steamspy_response()
        mock_http.get_with_metadata.side_effect = [
            (gamalytic_data, datetime.now(timezone.utc), 0),
            (steamspy_data, datetime.now(timezone.utc), 0),
        ]

        client = GamalyticClient(mock_http)
        result = await client.get_commercial_data(646570)

        assert result.copies_sold is None

    @pytest.mark.asyncio
    async def test_accuracy_outside_range_rejected(self, mock_http):
        """accuracy=1.5 is out of [0, 1] range and should be set to None."""
        gamalytic_data = _make_full_gamalytic_response()
        gamalytic_data["accuracy"] = 1.5
        steamspy_data = _make_steamspy_response()
        mock_http.get_with_metadata.side_effect = [
            (gamalytic_data, datetime.now(timezone.utc), 0),
            (steamspy_data, datetime.now(timezone.utc), 0),
        ]

        client = GamalyticClient(mock_http)
        result = await client.get_commercial_data(646570)

        assert result.accuracy is None

    @pytest.mark.asyncio
    async def test_missing_extended_fields_prominent_warning(self, mock_http):
        """When extended fields are absent but revenue present, logger.error is called."""
        gamalytic_data = {
            "steamId": "646570",
            "name": "Slay the Spire",
            "price": 24.99,
            "revenue": 50000000,
            # No copiesSold, no history, no countryData
        }
        steamspy_data = _make_steamspy_response()
        mock_http.get_with_metadata.side_effect = [
            (gamalytic_data, datetime.now(timezone.utc), 0),
            (steamspy_data, datetime.now(timezone.utc), 0),
        ]

        import logging
        from src.steam_api import logger as steam_logger
        with patch.object(steam_logger, "error") as mock_error:
            client = GamalyticClient(mock_http)
            result = await client.get_commercial_data(646570)

        assert isinstance(result, CommercialData)
        # Should have called logger.error with prominent warning
        assert mock_error.called
        all_args = " ".join(str(a) for call in mock_error.call_args_list for a in call[0])
        assert "GAMALYTIC EXTENDED FIELDS UNAVAILABLE" in all_args

    @pytest.mark.asyncio
    async def test_player_count_from_history(self, mock_http):
        """current_player_count extracted from latest daily history entry."""
        gamalytic_data = _make_full_gamalytic_response()
        steamspy_data = _make_steamspy_response()
        mock_http.get_with_metadata.side_effect = [
            (gamalytic_data, datetime.now(timezone.utc), 0),
            (steamspy_data, datetime.now(timezone.utc), 0),
        ]

        client = GamalyticClient(mock_http)
        result = await client.get_commercial_data(646570)

        # Latest daily entry (index 2) has players=15200
        assert result.current_player_count == 15200
        assert result.player_count_source == "gamalytic_history"

    @pytest.mark.asyncio
    async def test_steam_web_api_fallback(self, mock_http):
        """Gamalytic fails, Steam Web API returns player count with correct source."""
        gamalytic_request = httpx.Request("GET", "https://api.gamalytic.com/game/646570")
        gamalytic_response = httpx.Response(503, request=gamalytic_request)

        steamspy_data = {
            "positive": 8000, "negative": 2000, "price": "2499",
            "owners": "500,000 .. 1,000,000", "ccu": 0
        }

        steam_web_api_data = {
            "response": {"result": 1, "player_count": 12345}
        }

        mock_http.get_with_metadata.side_effect = [
            httpx.HTTPStatusError("Service unavailable", request=gamalytic_request, response=gamalytic_response),
            (steamspy_data, datetime.now(timezone.utc), 0),
            (steam_web_api_data, datetime.now(timezone.utc), 0),
        ]

        client = GamalyticClient(mock_http)
        result = await client.get_commercial_data(646570)

        assert isinstance(result, CommercialData)
        assert result.current_player_count == 12345
        assert result.player_count_source == "steam_web_api"
        assert result.player_count_note is not None
        assert "Gamalytic" in result.player_count_note

    @pytest.mark.asyncio
    async def test_steam_web_api_fallback_also_fails(self, mock_http):
        """Both Gamalytic and Steam Web API fail; SteamSpy CCU used as third fallback."""
        gamalytic_request = httpx.Request("GET", "https://api.gamalytic.com/game/646570")
        gamalytic_response = httpx.Response(503, request=gamalytic_request)

        steam_web_api_request = httpx.Request(
            "GET", "https://api.steampowered.com/ISteamUserStats/GetNumberOfCurrentPlayers/v1/"
        )
        steam_web_api_response = httpx.Response(503, request=steam_web_api_request)

        # SteamSpy has no CCU in this scenario (ccu=0)
        steamspy_data = {
            "positive": 8000, "negative": 2000, "price": "2499",
            "owners": "500,000 .. 1,000,000", "ccu": 0
        }

        mock_http.get_with_metadata.side_effect = [
            httpx.HTTPStatusError("Service unavailable", request=gamalytic_request, response=gamalytic_response),
            (steamspy_data, datetime.now(timezone.utc), 0),
            httpx.HTTPStatusError("Service unavailable", request=steam_web_api_request, response=steam_web_api_response),
        ]

        client = GamalyticClient(mock_http)
        result = await client.get_commercial_data(646570)

        assert isinstance(result, CommercialData)
        assert result.source == "review_estimate"
        # Both APIs failed, no player count available (SteamSpy ccu=0)
        assert result.current_player_count is None

    @pytest.mark.asyncio
    async def test_all_fallbacks_fail_no_player_count(self, mock_http):
        """Gamalytic fails, Steam Web API fails, SteamSpy ccu=0 -> no player count."""
        gamalytic_request = httpx.Request("GET", "https://api.gamalytic.com/game/646570")
        gamalytic_response = httpx.Response(503, request=gamalytic_request)

        steam_web_api_request = httpx.Request(
            "GET", "https://api.steampowered.com/ISteamUserStats/GetNumberOfCurrentPlayers/v1/"
        )
        steam_web_api_response = httpx.Response(503, request=steam_web_api_request)

        steamspy_data = {
            "positive": 5000, "negative": 500, "price": "999",
            "owners": "100,000 .. 200,000", "ccu": 0
        }

        mock_http.get_with_metadata.side_effect = [
            httpx.HTTPStatusError("Gamalytic down", request=gamalytic_request, response=gamalytic_response),
            (steamspy_data, datetime.now(timezone.utc), 0),
            httpx.HTTPStatusError("Steam Web API down", request=steam_web_api_request, response=steam_web_api_response),
        ]

        client = GamalyticClient(mock_http)
        result = await client.get_commercial_data(646570)

        assert isinstance(result, CommercialData)
        assert result.current_player_count is None
        assert result.player_count_source is None

    @pytest.mark.asyncio
    async def test_early_access_fields(self, mock_http):
        """earlyAccess=True and EAReleaseDate are parsed into CommercialData.

        Phase 13 bug fix: API uses 'earlyAccess' (not 'isEarlyAccess').
        """
        gamalytic_data = _make_full_gamalytic_response()
        # Phase 13 fix: correct field name is 'earlyAccess' (not 'isEarlyAccess')
        gamalytic_data["earlyAccess"] = True
        gamalytic_data["EAReleaseDate"] = 1516665600000  # 2018-01-23
        mock_http.get_with_metadata.side_effect = [
            (gamalytic_data, datetime.now(timezone.utc), 0),
        ]

        client = GamalyticClient(mock_http)
        result = await client.get_commercial_data(646570)

        assert result.gamalytic_is_early_access is True
        assert result.gamalytic_ea_date == "2018-01-23"

    @pytest.mark.asyncio
    async def test_backward_compat_alias(self, mock_http):
        """get_revenue_estimate still works and returns same result as get_commercial_data."""
        gamalytic_data = _make_full_gamalytic_response()
        steamspy_data = _make_steamspy_response()
        mock_http.get_with_metadata.side_effect = [
            (gamalytic_data, datetime.now(timezone.utc), 0),
            (steamspy_data, datetime.now(timezone.utc), 0),
        ]

        client = GamalyticClient(mock_http)
        result = await client.get_revenue_estimate(646570)

        assert isinstance(result, CommercialData)
        assert result.appid == 646570
        assert result.source == "gamalytic"

    @pytest.mark.asyncio
    async def test_batch_mode(self, mock_http):
        """get_commercial_batch returns list of 2 CommercialData objects.

        Phase 13: 2 calls only (1 per game — no SteamSpy triangulation).
        """
        gamalytic_data1 = _make_full_gamalytic_response(appid=646570)
        gamalytic_data2 = _make_full_gamalytic_response(appid=2379780)

        # Phase 13: 2 calls only (Gamalytic per game, no SteamSpy triangulation)
        mock_http.get_with_metadata.side_effect = [
            (gamalytic_data1, datetime.now(timezone.utc), 0),
            (gamalytic_data2, datetime.now(timezone.utc), 0),
        ]

        client = GamalyticClient(mock_http)
        results = await client.get_commercial_batch([646570, 2379780])

        assert len(results) == 2
        assert all(isinstance(r, CommercialData) for r in results)

    @pytest.mark.asyncio
    async def test_batch_mode_partial_failure(self, mock_http):
        """One AppID succeeds, one fails: returns [CommercialData, APIError].

        Phase 13: Gamalytic success no longer calls SteamSpy. Failure still falls back.
        """
        gamalytic_data = _make_full_gamalytic_response(appid=646570)

        gamalytic_request2 = httpx.Request("GET", "https://api.gamalytic.com/game/999")
        gamalytic_response2 = httpx.Response(404, request=gamalytic_request2)

        steamspy_data_fail = {
            "positive": 0, "negative": 0, "price": "0"
        }

        mock_http.get_with_metadata.side_effect = [
            # AppID 646570: Gamalytic succeeds (no SteamSpy call on success path)
            (gamalytic_data, datetime.now(timezone.utc), 0),
            # AppID 999: Gamalytic fails, SteamSpy fallback (no reviews -> APIError)
            httpx.HTTPStatusError("Not found", request=gamalytic_request2, response=gamalytic_response2),
            (steamspy_data_fail, datetime.now(timezone.utc), 0),
        ]

        client = GamalyticClient(mock_http)
        results = await client.get_commercial_batch([646570, 999])

        assert len(results) == 2
        assert isinstance(results[0], CommercialData)
        assert isinstance(results[1], APIError)

    @pytest.mark.asyncio
    async def test_steamspy_ccu_fallback(self, mock_http):
        """Gamalytic fails, Steam Web API fails, SteamSpy ccu=5000 -> fallback with source='steamspy'."""
        gamalytic_request = httpx.Request("GET", "https://api.gamalytic.com/game/646570")
        gamalytic_response = httpx.Response(503, request=gamalytic_request)

        steam_web_api_request = httpx.Request(
            "GET", "https://api.steampowered.com/ISteamUserStats/GetNumberOfCurrentPlayers/v1/"
        )
        steam_web_api_response = httpx.Response(503, request=steam_web_api_request)

        steamspy_data = {
            "positive": 8000, "negative": 2000, "price": "2499",
            "owners": "500,000 .. 1,000,000", "ccu": 5000
        }

        mock_http.get_with_metadata.side_effect = [
            httpx.HTTPStatusError("Service unavailable", request=gamalytic_request, response=gamalytic_response),
            (steamspy_data, datetime.now(timezone.utc), 0),
            httpx.HTTPStatusError("Steam Web API down", request=steam_web_api_request, response=steam_web_api_response),
        ]

        client = GamalyticClient(mock_http)
        result = await client.get_commercial_data(646570)

        assert isinstance(result, CommercialData)
        assert result.current_player_count == 5000
        assert result.player_count_source == "steamspy"
        assert result.player_count_note is not None
        assert "SteamSpy" in result.player_count_note
        assert "stale" in result.player_count_note.lower()


# ---------------------------------------------------------------------------
# Phase 8: Batch error recovery tests
# ---------------------------------------------------------------------------

class TestBatchErrorRecovery:
    """Tests for get_commercial_batch error recovery and ordering."""

    @pytest.mark.asyncio
    async def test_batch_mixed_success_failure_ordering(self, mock_http):
        """3 AppIDs: success, failure, success — results preserve input order.

        Phase 13: success path makes 1 call (no SteamSpy triangulation).
        """
        gamalytic1 = _make_full_gamalytic_response(appid=111)
        gamalytic3 = _make_full_gamalytic_response(appid=333)

        gamalytic_req2 = httpx.Request("GET", "https://api.gamalytic.com/game/222")
        gamalytic_resp2 = httpx.Response(404, request=gamalytic_req2)

        steamspy_fail = {"positive": 0, "negative": 0, "price": "0"}

        # Concurrent calls — mock returns in order of invocation
        mock_http.get_with_metadata.side_effect = [
            # AppID 111: Gamalytic OK (no SteamSpy triangulation)
            (gamalytic1, datetime.now(timezone.utc), 0),
            # AppID 222: Gamalytic 404 + SteamSpy fallback (no reviews -> APIError)
            httpx.HTTPStatusError("Not found", request=gamalytic_req2, response=gamalytic_resp2),
            (steamspy_fail, datetime.now(timezone.utc), 0),
            # AppID 333: Gamalytic OK (no SteamSpy triangulation)
            (gamalytic3, datetime.now(timezone.utc), 0),
        ]

        client = GamalyticClient(mock_http)
        results = await client.get_commercial_batch([111, 222, 333])

        assert len(results) == 3
        # First and third should succeed
        assert isinstance(results[0], CommercialData)
        assert results[0].appid == 111
        # Second should be an error
        assert isinstance(results[1], APIError)
        # Third should succeed and be in correct position
        assert isinstance(results[2], CommercialData)
        assert results[2].appid == 333

    @pytest.mark.asyncio
    async def test_batch_all_failures(self, mock_http):
        """All AppIDs fail — returns list of APIError objects."""
        req1 = httpx.Request("GET", "https://api.gamalytic.com/game/111")
        resp1 = httpx.Response(503, request=req1)
        req2 = httpx.Request("GET", "https://api.gamalytic.com/game/222")
        resp2 = httpx.Response(503, request=req2)

        steamspy_req = httpx.Request("GET", "https://steamspy.com/api.php")
        steamspy_resp = httpx.Response(503, request=steamspy_req)

        mock_http.get_with_metadata.side_effect = [
            # AppID 111: both fail
            httpx.HTTPStatusError("Down", request=req1, response=resp1),
            httpx.HTTPStatusError("Down", request=steamspy_req, response=steamspy_resp),
            # AppID 222: both fail
            httpx.HTTPStatusError("Down", request=req2, response=resp2),
            httpx.HTTPStatusError("Down", request=steamspy_req, response=steamspy_resp),
        ]

        client = GamalyticClient(mock_http)
        results = await client.get_commercial_batch([111, 222])

        assert len(results) == 2
        assert all(isinstance(r, APIError) for r in results)

    @pytest.mark.asyncio
    async def test_batch_unhandled_exception_wrapped_as_api_error(self, mock_http):
        """Unhandled exception (e.g. RuntimeError) is wrapped as APIError.

        RuntimeError in Gamalytic triggers fallback to SteamSpy. If SteamSpy also
        fails, the error surfaces as APIError.

        Phase 13: AppID 111 success makes only 1 call (no SteamSpy triangulation).
        """
        gamalytic_data = _make_full_gamalytic_response(appid=111)

        steamspy_req = httpx.Request("GET", "https://steamspy.com/api.php")
        steamspy_resp = httpx.Response(503, request=steamspy_req)

        mock_http.get_with_metadata.side_effect = [
            # AppID 111: Gamalytic OK (no SteamSpy triangulation)
            (gamalytic_data, datetime.now(timezone.utc), 0),
            # AppID 222: Gamalytic throws RuntimeError, then SteamSpy fallback also fails
            RuntimeError("Unexpected internal error"),
            httpx.HTTPStatusError("Down", request=steamspy_req, response=steamspy_resp),
        ]

        client = GamalyticClient(mock_http)
        results = await client.get_commercial_batch([111, 222])

        assert len(results) == 2
        assert isinstance(results[0], CommercialData)
        assert isinstance(results[1], APIError)
        # SteamSpy fallback fails with 503 -> error_code 503
        assert results[1].error_code == 503

    @pytest.mark.asyncio
    async def test_batch_detail_level_threading(self, mock_http):
        """detail_level='summary' is passed through to each individual call.

        Phase 13: 2 calls only (1 per game — no SteamSpy triangulation).
        """
        gamalytic1 = _make_full_gamalytic_response(appid=111)
        gamalytic2 = _make_full_gamalytic_response(appid=222)

        # Phase 13: 2 calls only (no SteamSpy triangulation)
        mock_http.get_with_metadata.side_effect = [
            (gamalytic1, datetime.now(timezone.utc), 0),
            (gamalytic2, datetime.now(timezone.utc), 0),
        ]

        client = GamalyticClient(mock_http)
        results = await client.get_commercial_batch([111, 222], detail_level="summary")

        assert len(results) == 2
        # Summary mode should strip heavy arrays
        for r in results:
            assert isinstance(r, CommercialData)
            assert r.history == []
            assert r.audience_overlap == []
            assert r.also_played == []

    @pytest.mark.asyncio
    async def test_batch_single_appid(self, mock_http):
        """Batch with single AppID behaves like individual call."""
        gamalytic_data = _make_full_gamalytic_response(appid=646570)
        steamspy = _make_steamspy_response()

        mock_http.get_with_metadata.side_effect = [
            (gamalytic_data, datetime.now(timezone.utc), 0),
            (steamspy, datetime.now(timezone.utc), 0),
        ]

        client = GamalyticClient(mock_http)
        results = await client.get_commercial_batch([646570])

        assert len(results) == 1
        assert isinstance(results[0], CommercialData)
        assert results[0].appid == 646570

    @pytest.mark.asyncio
    async def test_batch_empty_list(self, mock_http):
        """Batch with empty AppID list returns empty list."""
        client = GamalyticClient(mock_http)
        results = await client.get_commercial_batch([])

        assert results == []

    @pytest.mark.asyncio
    async def test_batch_connection_error_mid_batch(self, mock_http):
        """First AppID succeeds, second gets ConnectionError — wrapped as APIError."""
        gamalytic_data = _make_full_gamalytic_response(appid=111)
        steamspy = _make_steamspy_response()

        mock_http.get_with_metadata.side_effect = [
            # AppID 111: OK
            (gamalytic_data, datetime.now(timezone.utc), 0),
            (steamspy, datetime.now(timezone.utc), 0),
            # AppID 222: connection drops
            ConnectionError("Connection reset by peer"),
            # SteamSpy fallback also fails
            ConnectionError("Connection reset by peer"),
        ]

        client = GamalyticClient(mock_http)
        results = await client.get_commercial_batch([111, 222])

        assert len(results) == 2
        assert isinstance(results[0], CommercialData)
        # ConnectionError goes through review fallback which catches it
        assert isinstance(results[1], APIError)


# ---------------------------------------------------------------------------
# Phase 8: Malformed Gamalytic extended field tests
# ---------------------------------------------------------------------------

class TestMalformedGamalyticFields:
    """Tests for defensive parsing of malformed/missing Gamalytic extended fields."""

    @pytest.mark.asyncio
    async def test_missing_history_array_entirely(self, mock_http):
        """No 'history' key in response — history defaults to empty list."""
        gamalytic_data = _make_full_gamalytic_response()
        del gamalytic_data["history"]
        steamspy = _make_steamspy_response()
        mock_http.get_with_metadata.side_effect = [
            (gamalytic_data, datetime.now(timezone.utc), 0),
            (steamspy, datetime.now(timezone.utc), 0),
        ]

        client = GamalyticClient(mock_http)
        result = await client.get_commercial_data(646570)

        assert isinstance(result, CommercialData)
        assert result.history == []
        # Player count can't come from history
        assert result.player_count_source != "gamalytic_history"

    @pytest.mark.asyncio
    async def test_history_null_value(self, mock_http):
        """history=null in response — treated same as missing."""
        gamalytic_data = _make_full_gamalytic_response()
        gamalytic_data["history"] = None
        steamspy = _make_steamspy_response()
        mock_http.get_with_metadata.side_effect = [
            (gamalytic_data, datetime.now(timezone.utc), 0),
            (steamspy, datetime.now(timezone.utc), 0),
        ]

        client = GamalyticClient(mock_http)
        result = await client.get_commercial_data(646570)

        assert isinstance(result, CommercialData)
        assert result.history == []

    @pytest.mark.asyncio
    async def test_history_empty_array(self, mock_http):
        """history=[] in response — empty list, no player count from history."""
        gamalytic_data = _make_full_gamalytic_response()
        gamalytic_data["history"] = []
        steamspy = _make_steamspy_response()
        mock_http.get_with_metadata.side_effect = [
            (gamalytic_data, datetime.now(timezone.utc), 0),
            (steamspy, datetime.now(timezone.utc), 0),
        ]

        client = GamalyticClient(mock_http)
        result = await client.get_commercial_data(646570)

        assert isinstance(result, CommercialData)
        assert result.history == []
        assert result.current_player_count is None or result.player_count_source != "gamalytic_history"

    @pytest.mark.asyncio
    async def test_history_entry_null_timestamp(self, mock_http):
        """History entry with timeStamp=null — timestamp set to None, entry still parsed."""
        gamalytic_data = _make_full_gamalytic_response()
        gamalytic_data["history"] = [
            {"timeStamp": None, "reviews": 100, "price": 9.99, "score": 80,
             "rank": 50, "followers": 1000, "players": 500, "avgPlaytime": 2.0,
             "sales": 5000, "revenue": 50000},
        ]
        steamspy = _make_steamspy_response()
        mock_http.get_with_metadata.side_effect = [
            (gamalytic_data, datetime.now(timezone.utc), 0),
            (steamspy, datetime.now(timezone.utc), 0),
        ]

        client = GamalyticClient(mock_http)
        result = await client.get_commercial_data(646570)

        assert isinstance(result, CommercialData)
        assert len(result.history) == 1
        assert result.history[0].timestamp is None
        assert result.history[0].reviews == 100

    @pytest.mark.asyncio
    async def test_history_entry_zero_timestamp(self, mock_http):
        """History entry with timeStamp=0 — _ms_to_iso returns None for falsy values."""
        gamalytic_data = _make_full_gamalytic_response()
        gamalytic_data["history"] = [
            {"timeStamp": 0, "reviews": 50, "price": 14.99, "score": 70,
             "rank": 100, "followers": 500, "players": 200, "avgPlaytime": 1.5,
             "sales": 2000, "revenue": 30000},
        ]
        steamspy = _make_steamspy_response()
        mock_http.get_with_metadata.side_effect = [
            (gamalytic_data, datetime.now(timezone.utc), 0),
            (steamspy, datetime.now(timezone.utc), 0),
        ]

        client = GamalyticClient(mock_http)
        result = await client.get_commercial_data(646570)

        assert isinstance(result, CommercialData)
        assert len(result.history) == 1
        assert result.history[0].timestamp is None

    @pytest.mark.asyncio
    async def test_history_pre_2003_timestamp_rejected(self, mock_http):
        """Timestamp before 2003 is rejected (set to None)."""
        gamalytic_data = _make_full_gamalytic_response()
        gamalytic_data["history"] = [
            {"timeStamp": 946684800000, "reviews": 10, "price": 5.0, "score": 60,  # 2000-01-01
             "rank": 200, "followers": 100, "players": 50, "avgPlaytime": 0.5,
             "sales": 500, "revenue": 2500},
        ]
        steamspy = _make_steamspy_response()
        mock_http.get_with_metadata.side_effect = [
            (gamalytic_data, datetime.now(timezone.utc), 0),
            (steamspy, datetime.now(timezone.utc), 0),
        ]

        client = GamalyticClient(mock_http)
        result = await client.get_commercial_data(646570)

        assert isinstance(result, CommercialData)
        assert len(result.history) == 1
        assert result.history[0].timestamp is None  # Rejected as too old

    @pytest.mark.asyncio
    async def test_country_data_empty(self, mock_http):
        """countryData=[] returns empty country_data list."""
        gamalytic_data = _make_full_gamalytic_response()
        gamalytic_data["countryData"] = []
        steamspy = _make_steamspy_response()
        mock_http.get_with_metadata.side_effect = [
            (gamalytic_data, datetime.now(timezone.utc), 0),
            (steamspy, datetime.now(timezone.utc), 0),
        ]

        client = GamalyticClient(mock_http)
        result = await client.get_commercial_data(646570)

        assert isinstance(result, CommercialData)
        assert result.country_data == []

    @pytest.mark.asyncio
    async def test_country_data_missing_key(self, mock_http):
        """No 'countryData' key — defaults to empty list."""
        gamalytic_data = _make_full_gamalytic_response()
        del gamalytic_data["countryData"]
        steamspy = _make_steamspy_response()
        mock_http.get_with_metadata.side_effect = [
            (gamalytic_data, datetime.now(timezone.utc), 0),
            (steamspy, datetime.now(timezone.utc), 0),
        ]

        client = GamalyticClient(mock_http)
        result = await client.get_commercial_data(646570)

        assert isinstance(result, CommercialData)
        assert result.country_data == []

    @pytest.mark.asyncio
    async def test_country_data_null_share(self, mock_http):
        """Country entry with share=null — share_pct is None, doesn't crash."""
        gamalytic_data = _make_full_gamalytic_response()
        gamalytic_data["countryData"] = [
            {"country": "US", "share": None},
            {"country": "CN", "share": 0.15},
        ]
        steamspy = _make_steamspy_response()
        mock_http.get_with_metadata.side_effect = [
            (gamalytic_data, datetime.now(timezone.utc), 0),
            (steamspy, datetime.now(timezone.utc), 0),
        ]

        client = GamalyticClient(mock_http)
        result = await client.get_commercial_data(646570)

        assert isinstance(result, CommercialData)
        assert len(result.country_data) == 2
        # CN with real share should be sorted first
        assert result.country_data[0].country_code == "CN"
        assert result.country_data[0].share_pct == 0.15

    @pytest.mark.asyncio
    async def test_copies_sold_null(self, mock_http):
        """copiesSold=null — copies_sold is None (not crash)."""
        gamalytic_data = _make_full_gamalytic_response()
        gamalytic_data["copiesSold"] = None
        steamspy = _make_steamspy_response()
        mock_http.get_with_metadata.side_effect = [
            (gamalytic_data, datetime.now(timezone.utc), 0),
            (steamspy, datetime.now(timezone.utc), 0),
        ]

        client = GamalyticClient(mock_http)
        result = await client.get_commercial_data(646570)

        assert isinstance(result, CommercialData)
        assert result.copies_sold is None

    @pytest.mark.asyncio
    async def test_copies_sold_zero(self, mock_http):
        """copiesSold=0 — treated as valid zero, not None."""
        gamalytic_data = _make_full_gamalytic_response()
        gamalytic_data["copiesSold"] = 0
        steamspy = _make_steamspy_response()
        mock_http.get_with_metadata.side_effect = [
            (gamalytic_data, datetime.now(timezone.utc), 0),
            (steamspy, datetime.now(timezone.utc), 0),
        ]

        client = GamalyticClient(mock_http)
        result = await client.get_commercial_data(646570)

        assert isinstance(result, CommercialData)
        assert result.copies_sold == 0

    @pytest.mark.asyncio
    async def test_followers_negative_rejected(self, mock_http):
        """Negative followers value is rejected (set to None)."""
        gamalytic_data = _make_full_gamalytic_response()
        gamalytic_data["followers"] = -100
        steamspy = _make_steamspy_response()
        mock_http.get_with_metadata.side_effect = [
            (gamalytic_data, datetime.now(timezone.utc), 0),
            (steamspy, datetime.now(timezone.utc), 0),
        ]

        client = GamalyticClient(mock_http)
        result = await client.get_commercial_data(646570)

        assert isinstance(result, CommercialData)
        assert result.followers is None

    @pytest.mark.asyncio
    async def test_audience_overlap_empty(self, mock_http):
        """Missing audienceOverlap — defaults to empty list."""
        gamalytic_data = _make_full_gamalytic_response()
        del gamalytic_data["audienceOverlap"]
        steamspy = _make_steamspy_response()
        mock_http.get_with_metadata.side_effect = [
            (gamalytic_data, datetime.now(timezone.utc), 0),
            (steamspy, datetime.now(timezone.utc), 0),
        ]

        client = GamalyticClient(mock_http)
        result = await client.get_commercial_data(646570)

        assert isinstance(result, CommercialData)
        assert result.audience_overlap == []

    @pytest.mark.asyncio
    async def test_estimate_details_non_list(self, mock_http):
        """estimateDetails is a string instead of list — falls back to empty."""
        gamalytic_data = _make_full_gamalytic_response()
        gamalytic_data["estimateDetails"] = "not a list"
        steamspy = _make_steamspy_response()
        mock_http.get_with_metadata.side_effect = [
            (gamalytic_data, datetime.now(timezone.utc), 0),
            (steamspy, datetime.now(timezone.utc), 0),
        ]

        client = GamalyticClient(mock_http)
        result = await client.get_commercial_data(646570)

        assert isinstance(result, CommercialData)
        assert result.estimate_details == []

    @pytest.mark.asyncio
    async def test_accuracy_negative_rejected(self, mock_http):
        """accuracy=-0.5 is out of [0, 1] range — set to None."""
        gamalytic_data = _make_full_gamalytic_response()
        gamalytic_data["accuracy"] = -0.5
        steamspy = _make_steamspy_response()
        mock_http.get_with_metadata.side_effect = [
            (gamalytic_data, datetime.now(timezone.utc), 0),
            (steamspy, datetime.now(timezone.utc), 0),
        ]

        client = GamalyticClient(mock_http)
        result = await client.get_commercial_data(646570)

        assert isinstance(result, CommercialData)
        assert result.accuracy is None


class TestListGamesByTag:
    """Tests for GamalyticClient.list_games_by_tag bulk list endpoint."""

    @pytest.mark.asyncio
    async def test_single_page_returns_all_games(self):
        """Tag with fewer games than limit returns all in one page."""
        mock_http = AsyncMock()
        mock_http.get_with_metadata.return_value = (
            {
                "pages": 1,
                "total": 3,
                "result": [
                    {"steamId": 588650, "name": "Dead Cells", "revenue": 85945035, "copiesSold": 6894914},
                    {"steamId": 646570, "name": "Slay the Spire", "revenue": 60000000, "copiesSold": 4000000},
                    {"steamId": 1145360, "name": "Hades", "revenue": 50000000, "copiesSold": 3500000},
                ],
                "next": {"limit": 200, "page": 0},
                "prev": {"limit": 200, "page": 0},
            },
            datetime.now(timezone.utc),
            0,
        )

        client = GamalyticClient(mock_http)
        result = await client.list_games_by_tag("Roguelike Deckbuilder")

        assert not isinstance(result, APIError)
        games, meta = result
        assert len(games) == 3
        assert games[0]["steamId"] == 588650
        assert games[0]["revenue"] == 85945035
        assert meta["is_partial"] is False
        assert meta["pages_fetched"] == 1
        mock_http.get_with_metadata.assert_called_once()
        assert "tags" in str(mock_http.get_with_metadata.call_args)

    @pytest.mark.asyncio
    async def test_multi_page_pagination(self):
        """Paginated fetch across multiple pages collects all results."""
        mock_http = AsyncMock()
        page1_response = {
            "pages": 2, "total": 3,
            "result": [
                {"steamId": 1, "name": "Game1", "revenue": 100},
                {"steamId": 2, "name": "Game2", "revenue": 90},
            ],
            "next": {"limit": 2, "page": 2},
        }
        page2_response = {
            "pages": 2, "total": 3,
            "result": [
                {"steamId": 3, "name": "Game3", "revenue": 80},
            ],
            "next": {"limit": 2, "page": 0},
        }
        mock_http.get_with_metadata.side_effect = [
            (page1_response, datetime.now(timezone.utc), 0),
            (page2_response, datetime.now(timezone.utc), 0),
        ]

        client = GamalyticClient(mock_http)
        result = await client.list_games_by_tag("Roguelike", limit_per_page=2)

        games, meta = result
        assert len(games) == 3
        assert [g["steamId"] for g in games] == [1, 2, 3]
        assert meta["is_partial"] is False
        assert meta["pages_fetched"] == 2
        assert mock_http.get_with_metadata.call_count == 2

    @pytest.mark.asyncio
    async def test_zero_results_returns_empty_list(self):
        """Tag with no matching games returns empty list."""
        mock_http = AsyncMock()
        mock_http.get_with_metadata.return_value = (
            {"pages": 0, "total": 0, "result": []},
            datetime.now(timezone.utc),
            0,
        )

        client = GamalyticClient(mock_http)
        result = await client.list_games_by_tag("Rogue-like")

        assert not isinstance(result, APIError)
        games, meta = result
        assert games == []
        assert meta["is_partial"] is False

    @pytest.mark.asyncio
    async def test_max_pages_cap(self):
        """max_pages parameter stops pagination early."""
        mock_http = AsyncMock()
        mock_http.get_with_metadata.return_value = (
            {
                "pages": 50, "total": 10000,
                "result": [{"steamId": i, "name": f"G{i}", "revenue": 100} for i in range(200)],
                "next": {"limit": 200, "page": 2},
            },
            datetime.now(timezone.utc),
            0,
        )

        client = GamalyticClient(mock_http)
        result = await client.list_games_by_tag("Action", max_pages=1)

        games, meta = result
        assert len(games) == 200
        assert meta["is_partial"] is False
        assert mock_http.get_with_metadata.call_count == 1

    @pytest.mark.asyncio
    async def test_http_error_returns_api_error(self):
        """HTTP error with no partial results returns APIError."""
        mock_http = AsyncMock()
        mock_http.get_with_metadata.side_effect = httpx.HTTPStatusError(
            "429", request=httpx.Request("GET", "http://test"), response=httpx.Response(429)
        )

        client = GamalyticClient(mock_http)
        result = await client.list_games_by_tag("Roguelike")

        assert isinstance(result, APIError)
        assert result.error_code == 429

    @pytest.mark.asyncio
    async def test_steamid_normalized_to_int(self):
        """steamId values are normalized to int regardless of API response type."""
        mock_http = AsyncMock()
        mock_http.get_with_metadata.return_value = (
            {
                "pages": 1, "total": 1,
                "result": [{"steamId": "588650", "name": "Dead Cells", "revenue": 85945035}],
            },
            datetime.now(timezone.utc),
            0,
        )

        client = GamalyticClient(mock_http)
        result = await client.list_games_by_tag("Roguelike")

        games, meta = result
        assert games[0]["steamId"] == 588650  # int, not string


class TestGamalyticAuthHeader:
    """Tests for Gamalytic API key header injection (KEY-01, KEY-02)."""

    # Minimal Gamalytic API response for get_commercial_data
    _GAMALYTIC_DATA = {
        "steamId": "646570",
        "name": "Slay the Spire",
        "price": 24.99,
        "revenue": 10000000,
        "reviews": 50000,
        "reviewScore": 97,
    }
    # Minimal SteamSpy response for triangulation pass-through
    _STEAMSPY_DATA = {
        "positive": 50000,
        "negative": 1500,
        "price": "2499",
        "owners": "500,000 .. 1,000,000",
    }

    @pytest.mark.asyncio
    async def test_gamalytic_api_key_header_when_key_set(self):
        """When GAMALYTIC_API_KEY is set, api-key header is included in Gamalytic calls."""
        mock_http = AsyncMock()
        mock_http.get_with_metadata.side_effect = [
            (self._GAMALYTIC_DATA, datetime.now(timezone.utc), 0),
            (self._STEAMSPY_DATA, datetime.now(timezone.utc), 0),
        ]

        with patch.object(Config, "GAMALYTIC_API_KEY", "test-pro-key-abc123"):
            client = GamalyticClient(mock_http)
            result = await client.get_commercial_data(646570)

        # Verify Gamalytic call included api-key header
        first_call = mock_http.get_with_metadata.call_args_list[0]
        headers_passed = first_call.kwargs.get("headers", {})
        assert headers_passed is not None
        assert "api-key" in headers_passed
        assert headers_passed["api-key"] == "test-pro-key-abc123"

    @pytest.mark.asyncio
    async def test_gamalytic_no_header_when_key_empty(self):
        """When GAMALYTIC_API_KEY is empty, no api-key header is sent (free tier)."""
        mock_http = AsyncMock()
        mock_http.get_with_metadata.side_effect = [
            (self._GAMALYTIC_DATA, datetime.now(timezone.utc), 0),
            (self._STEAMSPY_DATA, datetime.now(timezone.utc), 0),
        ]

        with patch.object(Config, "GAMALYTIC_API_KEY", ""):
            client = GamalyticClient(mock_http)
            result = await client.get_commercial_data(646570)

        # Verify Gamalytic call has no headers (free tier)
        first_call = mock_http.get_with_metadata.call_args_list[0]
        headers_passed = first_call.kwargs.get("headers")
        assert headers_passed is None

    @pytest.mark.asyncio
    async def test_gamalytic_list_games_api_key_header_when_key_set(self):
        """list_games_by_tag includes api-key header when key is set."""
        mock_http = AsyncMock()
        mock_http.get_with_metadata.return_value = (
            {
                "pages": 1, "total": 1,
                "result": [{"steamId": "588650", "name": "Dead Cells", "revenue": 85000000}],
            },
            datetime.now(timezone.utc),
            0,
        )

        with patch.object(Config, "GAMALYTIC_API_KEY", "test-pro-key-xyz"):
            client = GamalyticClient(mock_http)
            result = await client.list_games_by_tag("Roguelike")

        first_call = mock_http.get_with_metadata.call_args_list[0]
        headers_passed = first_call.kwargs.get("headers", {})
        assert headers_passed is not None
        assert "api-key" in headers_passed
        assert headers_passed["api-key"] == "test-pro-key-xyz"

    @pytest.mark.asyncio
    async def test_gamalytic_list_games_no_header_when_key_empty(self):
        """list_games_by_tag does not include api-key header when key is absent."""
        mock_http = AsyncMock()
        mock_http.get_with_metadata.return_value = (
            {
                "pages": 1, "total": 1,
                "result": [{"steamId": "588650", "name": "Dead Cells", "revenue": 85000000}],
            },
            datetime.now(timezone.utc),
            0,
        )

        with patch.object(Config, "GAMALYTIC_API_KEY", ""):
            client = GamalyticClient(mock_http)
            result = await client.list_games_by_tag("Roguelike")

        first_call = mock_http.get_with_metadata.call_args_list[0]
        headers_passed = first_call.kwargs.get("headers")
        assert headers_passed is None


# ---------------------------------------------------------------------------
# Phase 10: Warning surfacing and data quality field tests
# ---------------------------------------------------------------------------

class TestApiWarningsAndDataQuality:
    """Test Phase 10 warning surfacing: api_warnings and data_quality on CommercialData."""

    @pytest.mark.asyncio
    async def test_gamalytic_429_surfaces_api_warning(self, mock_http):
        """WARN-01 + PROV-01: 429 from Gamalytic produces api_warnings and data_quality='review_estimate'."""
        gamalytic_request = httpx.Request("GET", "https://api.gamalytic.com/game/646570")
        gamalytic_response = httpx.Response(429, request=gamalytic_request)
        steamspy_data = {
            "positive": 8000,
            "negative": 2000,
            "price": "2499",
            "ccu": 500,
            "owners": "500,000 .. 1,000,000",
        }
        steam_web_request = httpx.Request(
            "GET",
            "https://api.steampowered.com/ISteamUserStats/GetNumberOfCurrentPlayers/v1/",
        )
        steam_web_response = httpx.Response(500, request=steam_web_request)
        mock_http.get_with_metadata.side_effect = [
            httpx.HTTPStatusError("Too Many Requests", request=gamalytic_request, response=gamalytic_response),
            (steamspy_data, datetime.now(timezone.utc), 0),
            httpx.HTTPStatusError("Server Error", request=steam_web_request, response=steam_web_response),
        ]

        client = GamalyticClient(mock_http)
        result = await client.get_commercial_data(646570)

        assert isinstance(result, CommercialData), "Fallback should still return CommercialData, not APIError"
        assert len(result.api_warnings) > 0, "429 should produce at least one api_warning"
        warning_text = result.api_warnings[0]
        assert "429" in warning_text or "rate limit" in warning_text.lower(), (
            f"Warning should mention 429 or rate limit, got: {warning_text!r}"
        )
        assert result.data_quality == "review_estimate", (
            f"data_quality should be 'review_estimate' on fallback, got: {result.data_quality!r}"
        )
        assert result.source == "review_estimate", "Existing source field should still be 'review_estimate'"

    @pytest.mark.asyncio
    async def test_gamalytic_success_has_no_warnings(self, mock_http):
        """PROV-01: Successful Gamalytic response produces empty api_warnings and data_quality='gamalytic'."""
        gamalytic_data = {
            "steamId": "504230",
            "name": "Celeste",
            "price": 19.99,
            "revenue": 5000000,
            "reviews": 8000,
            "reviewScore": 95,
        }
        steamspy_data = {
            "positive": 8000,
            "negative": 200,
            "price": "1999",
            "owners": "400,000 .. 600,000",
        }
        mock_http.get_with_metadata.side_effect = [
            (gamalytic_data, datetime.now(timezone.utc), 0),
            (steamspy_data, datetime.now(timezone.utc), 0),
        ]

        client = GamalyticClient(mock_http)
        result = await client.get_commercial_data(504230)

        assert isinstance(result, CommercialData)
        assert result.api_warnings == [], (
            f"Successful Gamalytic call should produce empty api_warnings, got: {result.api_warnings!r}"
        )
        assert result.data_quality == "gamalytic", (
            f"data_quality should be 'gamalytic' on success, got: {result.data_quality!r}"
        )

    @pytest.mark.asyncio
    async def test_gamalytic_generic_error_surfaces_warning(self, mock_http):
        """Generic (non-HTTP) Gamalytic error also surfaces an api_warning with failure info."""
        steamspy_data = {
            "positive": 5000,
            "negative": 500,
            "price": "999",
            "owners": "100,000 .. 200,000",
        }
        steam_web_request = httpx.Request(
            "GET",
            "https://api.steampowered.com/ISteamUserStats/GetNumberOfCurrentPlayers/v1/",
        )
        steam_web_response = httpx.Response(503, request=steam_web_request)
        mock_http.get_with_metadata.side_effect = [
            Exception("Connection refused"),
            (steamspy_data, datetime.now(timezone.utc), 0),
            httpx.HTTPStatusError("Service unavailable", request=steam_web_request, response=steam_web_response),
        ]

        client = GamalyticClient(mock_http)
        result = await client.get_commercial_data(123456)

        assert isinstance(result, CommercialData)
        assert len(result.api_warnings) > 0, "Generic error should produce api_warnings"
        warning_text = result.api_warnings[0]
        assert "Gamalytic" in warning_text, (
            f"Warning should mention Gamalytic, got: {warning_text!r}"
        )
        assert result.data_quality == "review_estimate", (
            f"data_quality should be 'review_estimate' on fallback, got: {result.data_quality!r}"
        )

    def test_data_quality_in_model_dump(self):
        """WARN-03: api_warnings and data_quality are included in model_dump() output.

        This confirms fetch_commercial_batch per-game entries include the fields
        without any additional tool-layer changes.
        """
        commercial = CommercialData(
            appid=1,
            revenue_min=100,
            revenue_max=200,
            confidence="low",
            source="review_estimate",
            fetched_at=datetime.now(timezone.utc),
            api_warnings=["test warning"],
            data_quality="review_estimate",
        )
        dumped = commercial.model_dump()

        assert "api_warnings" in dumped, "api_warnings must be present in model_dump()"
        assert "data_quality" in dumped, "data_quality must be present in model_dump()"
        assert dumped["api_warnings"] == ["test warning"], (
            f"api_warnings should serialize correctly, got: {dumped['api_warnings']!r}"
        )
        assert dumped["data_quality"] == "review_estimate", (
            f"data_quality should serialize correctly, got: {dumped['data_quality']!r}"
        )


# ---------------------------------------------------------------------------
# Phase 13: Data migration — preference flip, cap lifts, new Pro fields
# ---------------------------------------------------------------------------

class TestPhase13Migration:
    """Tests for Phase 13 migration changes: triangulation removal, cap lifts, new Pro fields."""

    @pytest.mark.asyncio
    async def test_no_triangulation_call_on_success(self, mock_http):
        """PREF-01: Gamalytic success makes exactly 1 HTTP call — no SteamSpy triangulation."""
        gamalytic_data = {
            "steamId": "646570",
            "name": "Slay the Spire",
            "price": 24.99,
            "revenue": 50000000,
        }
        mock_http.get_with_metadata.side_effect = [
            (gamalytic_data, datetime.now(timezone.utc), 0),
        ]

        client = GamalyticClient(mock_http)
        result = await client.get_commercial_data(646570)

        assert isinstance(result, CommercialData)
        assert result.source == "gamalytic"
        # Exactly 1 HTTP call — no SteamSpy triangulation on success path
        assert mock_http.get_with_metadata.call_count == 1
        # Triangulation fields are always None on success path (schema retained for compat)
        assert result.triangulation_warning is None
        assert result.steamspy_implied_revenue is None
        assert result.gamalytic_revenue is None

    @pytest.mark.asyncio
    async def test_country_cap_50(self, mock_http):
        """CAP-01: countryData with 60 entries -> country_data capped at 50, sorted by share_pct."""
        # Build 60 country entries with sequential share values
        country_data = {f"c{i:02d}": float(60 - i) for i in range(60)}  # c00=60.0, c01=59.0, ...
        gamalytic_data = {
            "steamId": "646570",
            "name": "Slay the Spire",
            "price": 24.99,
            "revenue": 50000000,
            "countryData": country_data,
        }
        mock_http.get_with_metadata.side_effect = [
            (gamalytic_data, datetime.now(timezone.utc), 0),
        ]

        client = GamalyticClient(mock_http)
        result = await client.get_commercial_data(646570)

        assert isinstance(result, CommercialData)
        assert len(result.country_data) == 50
        # First entry should have highest share_pct (60.0)
        assert result.country_data[0].share_pct == 60.0
        # Last entry should be 50th highest
        assert result.country_data[-1].share_pct == 11.0

    @pytest.mark.asyncio
    async def test_overlap_cap_500(self, mock_http):
        """CAP-02: audienceOverlap with 600 entries -> audience_overlap capped at 500."""
        overlap_entries = [
            {"steamId": str(i), "name": f"Game {i}", "link": float(i) / 600, "revenue": i * 1000}
            for i in range(600)
        ]
        gamalytic_data = {
            "steamId": "646570",
            "name": "Slay the Spire",
            "price": 24.99,
            "revenue": 50000000,
            "audienceOverlap": overlap_entries,
        }
        mock_http.get_with_metadata.side_effect = [
            (gamalytic_data, datetime.now(timezone.utc), 0),
        ]

        client = GamalyticClient(mock_http)
        result = await client.get_commercial_data(646570)

        assert isinstance(result, CommercialData)
        assert len(result.audience_overlap) == 500

    @pytest.mark.asyncio
    async def test_also_played_cap_500(self, mock_http):
        """CAP-03: alsoPlayed with 600 entries -> also_played capped at 500."""
        also_played_entries = [
            {"steamId": str(i), "name": f"Game {i}", "link": float(i) / 600, "revenue": i * 1000}
            for i in range(600)
        ]
        gamalytic_data = {
            "steamId": "646570",
            "name": "Slay the Spire",
            "price": 24.99,
            "revenue": 50000000,
            "alsoPlayed": also_played_entries,
        }
        mock_http.get_with_metadata.side_effect = [
            (gamalytic_data, datetime.now(timezone.utc), 0),
        ]

        client = GamalyticClient(mock_http)
        result = await client.get_commercial_data(646570)

        assert isinstance(result, CommercialData)
        assert len(result.also_played) == 500

    @pytest.mark.asyncio
    async def test_new_pro_fields_parsed(self, mock_http):
        """PRO-01: New Pro fields (wishlists, avgPlaytime, playtimeData, reviewsSteam, earlyAccessExitDate) parsed."""
        gamalytic_data = {
            "steamId": "646570",
            "name": "Slay the Spire",
            "price": 24.99,
            "revenue": 50000000,
            "wishlists": 719400,
            "avgPlaytime": 64.076,
            "reviewsSteam": 179415,
            "earlyAccess": False,
            "earlyAccessExitDate": 1548219600000,  # 2019-01-23
            "playtimeData": {
                "distribution": {
                    "0-1h": 7.7, "1-2h": 4.6, "2-5h": 9.1, "5-10h": 11.0,
                    "10-20h": 13.1, "20-50h": 22.5, "50-100h": 14.0,
                    "100-500h": 16.6, "500-1000h": 1.4
                },
                "median": 24.266666666666666
            },
        }
        mock_http.get_with_metadata.side_effect = [
            (gamalytic_data, datetime.now(timezone.utc), 0),
        ]

        client = GamalyticClient(mock_http)
        result = await client.get_commercial_data(646570)

        assert isinstance(result, CommercialData)
        assert result.wishlists == 719400
        assert result.avg_playtime == pytest.approx(64.076, rel=1e-3)
        assert result.reviews_steam == 179415
        assert result.gamalytic_is_early_access is False
        assert result.ea_exit_date == "2019-01-23"
        assert result.playtime_distribution == {
            "0-1h": 7.7, "1-2h": 4.6, "2-5h": 9.1, "5-10h": 11.0,
            "10-20h": 13.1, "20-50h": 22.5, "50-100h": 14.0,
            "100-500h": 16.6, "500-1000h": 1.4
        }
        assert result.playtime_median == pytest.approx(24.266, rel=1e-3)

    @pytest.mark.asyncio
    async def test_playtime_data_dynamic_buckets(self, mock_http):
        """PRO-02: playtimeData with custom/different bucket labels stored verbatim."""
        gamalytic_data = {
            "steamId": "646570",
            "name": "Test Game",
            "price": 9.99,
            "revenue": 1000000,
            "playtimeData": {
                "distribution": {
                    "0-2h": 15.0,
                    "2-10h": 30.0,
                    "50h+": 5.0
                },
                "median": 8.5
            },
        }
        mock_http.get_with_metadata.side_effect = [
            (gamalytic_data, datetime.now(timezone.utc), 0),
        ]

        client = GamalyticClient(mock_http)
        result = await client.get_commercial_data(646570)

        assert isinstance(result, CommercialData)
        assert result.playtime_distribution == {"0-2h": 15.0, "2-10h": 30.0, "50h+": 5.0}
        assert result.playtime_median == 8.5

    @pytest.mark.asyncio
    async def test_competitor_extended_fields(self, mock_http):
        """PRO-03: audienceOverlap and alsoPlayed entries include extended sub-fields."""
        gamalytic_data = {
            "steamId": "646570",
            "name": "Slay the Spire",
            "price": 24.99,
            "revenue": 50000000,
            "audienceOverlap": [
                {
                    "steamId": "262060",
                    "name": "Darkest Dungeon",
                    "link": 0.4161,
                    "revenue": 52644037,
                    "releaseDate": 1453179600000,  # 2016-01-19
                    "price": 24.99,
                    "genres": ["Indie", "RPG", "Strategy"],
                    "copiesSold": 7481736,
                }
            ],
            "alsoPlayed": [
                {
                    "steamId": "730",
                    "name": "Counter-Strike 2",
                    "link": 0.7963,
                    "revenue": 10816375598.71,
                    "releaseDate": 1345521600000,  # 2012-08-21
                    "price": 0,  # F2P — int 0 (not float)
                    "genres": ["Action", "Free to Play"],
                    "copiesSold": 338531651,
                }
            ],
        }
        mock_http.get_with_metadata.side_effect = [
            (gamalytic_data, datetime.now(timezone.utc), 0),
        ]

        client = GamalyticClient(mock_http)
        result = await client.get_commercial_data(646570)

        assert isinstance(result, CommercialData)
        assert len(result.audience_overlap) == 1
        overlap = result.audience_overlap[0]
        assert overlap.appid == 262060
        assert overlap.name == "Darkest Dungeon"
        assert overlap.overlap_pct == pytest.approx(0.4161, rel=1e-3)
        assert overlap.release_date == "2016-01-19"
        assert overlap.price == 24.99
        assert overlap.genres == ["Indie", "RPG", "Strategy"]
        assert overlap.copies_sold == 7481736

        assert len(result.also_played) == 1
        also = result.also_played[0]
        assert also.appid == 730
        assert also.name == "Counter-Strike 2"
        assert also.price == 0.0  # F2P: int 0 -> float 0.0
        assert also.genres == ["Action", "Free to Play"]
        assert also.copies_sold == 338531651

    @pytest.mark.asyncio
    async def test_history_wishlists(self, mock_http):
        """PRO-04: History entries include wishlists when present; absent entry -> None."""
        gamalytic_data = {
            "steamId": "646570",
            "name": "Slay the Spire",
            "price": 24.99,
            "revenue": 50000000,
            "history": [
                {  # Old entry: no wishlists field (absent, not null)
                    "timeStamp": 1516665600000,
                    "reviews": 1000, "price": 15.99, "score": 90, "rank": 50,
                    "followers": 50000, "players": 8000, "avgPlaytime": 5.2,
                    "sales": 100000, "revenue": 1500000,
                },
                {  # Recent entry: wishlists present
                    "timeStamp": 1708646400000,
                    "reviews": 82000, "price": 24.99, "score": 97, "rank": 15,
                    "followers": 450000, "wishlists": 719400, "players": 14500,
                    "avgPlaytime": 28.3, "sales": 3000000, "revenue": 50000000,
                },
            ],
        }
        mock_http.get_with_metadata.side_effect = [
            (gamalytic_data, datetime.now(timezone.utc), 0),
        ]

        client = GamalyticClient(mock_http)
        result = await client.get_commercial_data(646570)

        assert isinstance(result, CommercialData)
        assert len(result.history) == 2
        # Old entry has no wishlists -> None
        assert result.history[0].wishlists is None
        # Recent entry has wishlists
        assert result.history[1].wishlists == 719400

    @pytest.mark.asyncio
    async def test_fallback_path_source_tags(self, mock_http):
        """SRC-01: Fallback path (SteamSpy) sets playtime_source and ownership_source to 'steamspy'."""
        gamalytic_request = httpx.Request("GET", "https://api.gamalytic.com/game/646570")
        gamalytic_response = httpx.Response(503, request=gamalytic_request)

        steamspy_data = {
            "positive": 8000,
            "negative": 2000,
            "price": "2499",
            "owners": "500,000 .. 1,000,000",
            "ccu": 0,
        }
        steam_web_request = httpx.Request(
            "GET",
            "https://api.steampowered.com/ISteamUserStats/GetNumberOfCurrentPlayers/v1/",
        )
        steam_web_response = httpx.Response(503, request=steam_web_request)

        mock_http.get_with_metadata.side_effect = [
            httpx.HTTPStatusError("Service unavailable", request=gamalytic_request, response=gamalytic_response),
            (steamspy_data, datetime.now(timezone.utc), 0),
            httpx.HTTPStatusError("Service unavailable", request=steam_web_request, response=steam_web_response),
        ]

        client = GamalyticClient(mock_http)
        result = await client.get_commercial_data(646570)

        assert isinstance(result, CommercialData)
        assert result.source == "review_estimate"
        assert result.playtime_source == "steamspy"
        assert result.ownership_source == "steamspy"

    @pytest.mark.asyncio
    async def test_success_path_no_source_tags(self, mock_http):
        """SRC-02: Gamalytic success path leaves playtime_source and ownership_source as None."""
        gamalytic_data = {
            "steamId": "646570",
            "name": "Slay the Spire",
            "price": 24.99,
            "revenue": 50000000,
        }
        mock_http.get_with_metadata.side_effect = [
            (gamalytic_data, datetime.now(timezone.utc), 0),
        ]

        client = GamalyticClient(mock_http)
        result = await client.get_commercial_data(646570)

        assert isinstance(result, CommercialData)
        assert result.source == "gamalytic"
        # Gamalytic is default trusted source — no tags needed
        assert result.playtime_source is None
        assert result.ownership_source is None

    @pytest.mark.asyncio
    async def test_early_access_field_bug_fix(self, mock_http):
        """BUG-01: API uses 'earlyAccess' field (not 'isEarlyAccess'); gamalytic_is_early_access correctly populated."""
        gamalytic_data = {
            "steamId": "739630",
            "name": "Phasmophobia",
            "price": 13.99,
            "revenue": 50000000,
            "earlyAccess": True,   # Correct Pro API field name
        }
        mock_http.get_with_metadata.side_effect = [
            (gamalytic_data, datetime.now(timezone.utc), 0),
        ]

        client = GamalyticClient(mock_http)
        result = await client.get_commercial_data(739630)

        assert isinstance(result, CommercialData)
        # Before fix: isEarlyAccess (wrong) -> always None
        # After fix: earlyAccess (correct) -> True
        assert result.gamalytic_is_early_access is True

    @pytest.mark.asyncio
    async def test_ea_exit_date_parsed(self, mock_http):
        """PRO-05: earlyAccessExitDate parsed to ISO string; absent for currently-in-EA games."""
        # Graduated EA game — has exit date
        gamalytic_data_graduated = {
            "steamId": "646570",
            "name": "Slay the Spire",
            "price": 24.99,
            "revenue": 50000000,
            "earlyAccess": False,
            "earlyAccessExitDate": 1548219600000,  # 2019-01-23
        }
        # Currently-in-EA game — no exit date
        gamalytic_data_in_ea = {
            "steamId": "739630",
            "name": "Phasmophobia",
            "price": 13.99,
            "revenue": 30000000,
            "earlyAccess": True,
            # earlyAccessExitDate absent
        }
        mock_http.get_with_metadata.side_effect = [
            (gamalytic_data_graduated, datetime.now(timezone.utc), 0),
        ]

        client = GamalyticClient(mock_http)
        result = await client.get_commercial_data(646570)
        assert result.ea_exit_date == "2019-01-23"

        mock_http.get_with_metadata.side_effect = [
            (gamalytic_data_in_ea, datetime.now(timezone.utc), 0),
        ]
        result2 = await client.get_commercial_data(739630)
        assert result2.ea_exit_date is None

    @pytest.mark.asyncio
    async def test_pro_fields_absent_defaults_to_none(self, mock_http):
        """PRO-06: Pro fields absent from response default to None/empty (not crash)."""
        gamalytic_data = {
            "steamId": "646570",
            "name": "Minimal Game",
            "price": 9.99,
            "revenue": 500000,
            # No wishlists, avgPlaytime, playtimeData, reviewsSteam, earlyAccessExitDate
        }
        mock_http.get_with_metadata.side_effect = [
            (gamalytic_data, datetime.now(timezone.utc), 0),
        ]

        client = GamalyticClient(mock_http)
        result = await client.get_commercial_data(646570)

        assert isinstance(result, CommercialData)
        assert result.wishlists is None
        assert result.avg_playtime is None
        assert result.playtime_distribution == {}
        assert result.playtime_median is None
        assert result.reviews_steam is None
        assert result.ea_exit_date is None
