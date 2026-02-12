"""Tests for commercial data pipeline: GamalyticClient and fetch_commercial tool."""

import json
import pytest
import httpx
from datetime import datetime, timezone
from unittest.mock import AsyncMock

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

        mock_http.get_with_metadata.side_effect = [
            httpx.HTTPStatusError("Service unavailable", request=gamalytic_request, response=gamalytic_response),
            (steamspy_data, datetime.now(timezone.utc), 0)
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

        mock_http.get_with_metadata.side_effect = [
            ConnectionError("DNS resolution failed"),
            (steamspy_data, datetime.now(timezone.utc), 0)
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

        mock_http.get_with_metadata.side_effect = [
            httpx.HTTPStatusError("Not found", request=gamalytic_request, response=gamalytic_response),
            (steamspy_data, datetime.now(timezone.utc), 0)
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
        """Test triangulation warning when Gamalytic and SteamSpy diverge >20%."""
        gamalytic_data = {
            "steamId": "12345",
            "name": "Test Game",
            "price": 24.99,
            "revenue": 10000000  # $10M
        }

        # SteamSpy: midpoint 300k owners * $24.99 * 0.7 = ~$5.25M (47% divergence)
        steamspy_data = {
            "positive": 3000,
            "negative": 300,
            "price": "2499",
            "owners": "200,000 .. 400,000"
        }

        mock_http.get_with_metadata.side_effect = [
            (gamalytic_data, datetime.now(timezone.utc), 0),
            (steamspy_data, datetime.now(timezone.utc), 0)
        ]

        client = GamalyticClient(mock_http)
        result = await client.get_revenue_estimate(12345)

        assert isinstance(result, CommercialData)
        assert result.source == "gamalytic"
        assert result.triangulation_warning is not None
        assert "diverges" in result.triangulation_warning.lower()
        assert result.steamspy_implied_revenue is not None
        assert result.gamalytic_revenue is not None
        # Check rough calculation: 300k * 24.99 * 0.7 ≈ 5,247,900
        assert 5_200_000 < result.steamspy_implied_revenue < 5_300_000
        assert result.gamalytic_revenue == 10000000

    @pytest.mark.asyncio
    async def test_triangulation_no_warning_when_close(self, mock_http):
        """Test no triangulation warning when sources agree within 20%."""
        gamalytic_data = {
            "steamId": "67890",
            "name": "Close Match Game",
            "price": 14.99,
            "revenue": 5000000  # $5M
        }

        # SteamSpy: midpoint 500k * $14.99 * 0.7 = ~$5.25M (~5% divergence)
        steamspy_data = {
            "positive": 4500,
            "negative": 500,
            "price": "1499",
            "owners": "400,000 .. 600,000"
        }

        mock_http.get_with_metadata.side_effect = [
            (gamalytic_data, datetime.now(timezone.utc), 0),
            (steamspy_data, datetime.now(timezone.utc), 0)
        ]

        client = GamalyticClient(mock_http)
        result = await client.get_revenue_estimate(67890)

        assert isinstance(result, CommercialData)
        assert result.triangulation_warning is None

    @pytest.mark.asyncio
    async def test_triangulation_failure_does_not_break_response(self, mock_http):
        """Test that triangulation failure doesn't break Gamalytic response."""
        gamalytic_data = {
            "steamId": "11111",
            "name": "Test Game",
            "price": 19.99,
            "revenue": 3000000
        }

        # Gamalytic succeeds, SteamSpy fails
        steamspy_request = httpx.Request("GET", "https://steamspy.com/api.php")
        steamspy_response = httpx.Response(500, request=steamspy_request)

        mock_http.get_with_metadata.side_effect = [
            (gamalytic_data, datetime.now(timezone.utc), 0),
            httpx.HTTPStatusError("Server error", request=steamspy_request, response=steamspy_response)
        ]

        client = GamalyticClient(mock_http)
        result = await client.get_revenue_estimate(11111)

        assert isinstance(result, CommercialData)
        assert result.source == "gamalytic"
        # Triangulation silently skipped
        assert result.triangulation_warning is None
        assert result.steamspy_implied_revenue is None
        assert result.gamalytic_revenue is None

    @pytest.mark.asyncio
    async def test_steamspy_owners_parsing_with_commas(self, mock_http):
        """Test correct parsing of SteamSpy owners field with commas."""
        gamalytic_data = {
            "steamId": "22222",
            "name": "Popular Game",
            "price": 29.99,
            "revenue": 60000000  # $60M
        }

        # SteamSpy with comma separators: midpoint 1.5M * $29.99 * 0.7 = ~$31.5M
        steamspy_data = {
            "positive": 15000,
            "negative": 1000,
            "price": "2999",
            "owners": "1,000,000 .. 2,000,000"  # Commas as thousands separators
        }

        mock_http.get_with_metadata.side_effect = [
            (gamalytic_data, datetime.now(timezone.utc), 0),
            (steamspy_data, datetime.now(timezone.utc), 0)
        ]

        client = GamalyticClient(mock_http)
        result = await client.get_revenue_estimate(22222)

        assert isinstance(result, CommercialData)
        # Should calculate properly: 1.5M owners, not parse error
        # Divergence: (31.5M vs 60M) / 60M = 47% -> warning should fire
        assert result.triangulation_warning is not None
        assert result.steamspy_implied_revenue is not None
        # Check: 1,500,000 * 29.99 * 0.7 ≈ 31,489,500
        assert 31_000_000 < result.steamspy_implied_revenue < 32_000_000

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
