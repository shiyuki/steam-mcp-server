"""Tests for Steam API clients with mocked HTTP responses."""

import pytest
import httpx
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

from src.schemas import GameMetadata, SearchResult, APIError
from src.steam_api import SteamSpyClient, SteamStoreClient, parse_supported_languages


@pytest.fixture
def mock_http():
    """Mock CachedAPIClient with get_with_metadata method."""
    client = AsyncMock()
    return client


class TestSteamSpyClient:
    @pytest.mark.asyncio
    async def test_search_by_tag_success(self, mock_http):
        data = {
            "646570": {"name": "Slay the Spire"},
            "2379780": {"name": "Balatro"},
            "247080": {"name": "Crypt of the NecroDancer"},
        }
        mock_http.get_with_metadata.return_value = (data, datetime.now(timezone.utc), 0)
        client = SteamSpyClient(mock_http)
        result = await client.search_by_tag("Roguelike", limit=2)

        assert isinstance(result, SearchResult)
        assert len(result.appids) == 2
        assert result.tag == "Rogue-like"  # Tag is normalized
        assert result.total_found == 3
        assert isinstance(result.fetched_at, datetime)
        assert result.cache_age_seconds == 0
        # Phase 5: Verify enriched games list
        assert hasattr(result, 'games')
        assert len(result.games) == 2
        assert len(result.games) == len(result.appids)

    @pytest.mark.asyncio
    async def test_search_by_tag_empty_response(self, mock_http):
        mock_http.get_with_metadata.return_value = ({}, datetime.now(timezone.utc), 0)
        client = SteamSpyClient(mock_http)
        result = await client.search_by_tag("NonexistentTag")

        assert isinstance(result, SearchResult)
        assert result.appids == []
        assert result.total_found == 0
        # Phase 5: Empty games list for empty response
        assert result.games == []

    @pytest.mark.asyncio
    async def test_search_by_tag_http_429(self, mock_http):
        response = httpx.Response(429, request=httpx.Request("GET", "https://steamspy.com"))
        mock_http.get_with_metadata.side_effect = httpx.HTTPStatusError(
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
        mock_http.get_with_metadata.side_effect = httpx.HTTPStatusError(
            "Server error", request=response.request, response=response
        )
        client = SteamSpyClient(mock_http)
        result = await client.search_by_tag("Action")

        assert isinstance(result, APIError)
        assert result.error_code == 500
        assert result.error_type == "api_error"

    @pytest.mark.asyncio
    async def test_search_by_tag_unexpected_exception(self, mock_http):
        mock_http.get_with_metadata.side_effect = ConnectionError("DNS failed")
        client = SteamSpyClient(mock_http)
        result = await client.search_by_tag("Action")

        assert isinstance(result, APIError)
        assert result.error_code == 502
        assert "DNS failed" in result.message

    @pytest.mark.asyncio
    async def test_search_limit_caps_results(self, mock_http):
        data = {str(i): {} for i in range(50)}
        mock_http.get_with_metadata.return_value = (data, datetime.now(timezone.utc), 0)
        client = SteamSpyClient(mock_http)
        result = await client.search_by_tag("RPG", limit=5)

        assert isinstance(result, SearchResult)
        assert len(result.appids) == 5
        assert result.total_found == 50
        # Phase 5: Games list also capped by limit
        assert len(result.games) == 5
        assert len(result.games) == len(result.appids)


class TestSteamStoreClient:
    @pytest.mark.asyncio
    async def test_get_app_details_success(self, mock_http):
        data = {
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
        mock_http.get_with_metadata.return_value = (data, datetime.now(timezone.utc), 0)
        client = SteamStoreClient(mock_http)
        result = await client.get_app_details(646570)

        assert isinstance(result, GameMetadata)
        assert result.appid == 646570
        assert result.name == "Slay the Spire"
        assert result.tags == ["Indie", "Strategy"]
        assert result.developer == "Mega Crit"
        assert result.description == "A deckbuilder roguelike"
        assert isinstance(result.fetched_at, datetime)
        assert result.cache_age_seconds == 0
        # Phase 5: New fields should have defaults when not in API response
        assert result.price is None  # No price_overview in mock data
        assert result.is_free_to_play is False
        assert result.platforms == {"windows": False, "mac": False, "linux": False}
        assert result.release_date is None
        assert result.metacritic_score is None
        assert result.screenshots == []
        assert result.movies == []
        assert result.dlc == []

    @pytest.mark.asyncio
    async def test_get_app_details_not_found(self, mock_http):
        data = {"99999999": {"success": False}}
        mock_http.get_with_metadata.return_value = (data, datetime.now(timezone.utc), 0)
        client = SteamStoreClient(mock_http)
        result = await client.get_app_details(99999999)

        assert isinstance(result, APIError)
        assert result.error_code == 404
        assert result.error_type == "not_found"

    @pytest.mark.asyncio
    async def test_get_app_details_missing_fields(self, mock_http):
        data = {
            "123": {
                "success": True,
                "data": {"name": "Minimal Game"},
            }
        }
        mock_http.get_with_metadata.return_value = (data, datetime.now(timezone.utc), 0)
        client = SteamStoreClient(mock_http)
        result = await client.get_app_details(123)

        assert isinstance(result, GameMetadata)
        assert result.name == "Minimal Game"
        assert result.tags == []
        assert result.developer == ""
        assert result.description == ""
        assert isinstance(result.fetched_at, datetime)
        # Phase 5: All new fields should have safe defaults
        assert result.price is None
        assert result.is_free_to_play is False
        assert result.platforms == {"windows": False, "mac": False, "linux": False}
        assert result.release_date is None
        assert result.metacritic_score is None
        assert result.screenshots == []
        assert result.movies == []
        assert result.dlc == []
        assert result.publisher == ""
        assert result.short_description == ""
        assert result.detailed_description == ""

    @pytest.mark.asyncio
    async def test_get_app_details_http_error(self, mock_http):
        response = httpx.Response(500, request=httpx.Request("GET", "https://store.steampowered.com"))
        mock_http.get_with_metadata.side_effect = httpx.HTTPStatusError(
            "Server error", request=response.request, response=response
        )
        client = SteamStoreClient(mock_http)
        result = await client.get_app_details(646570)

        assert isinstance(result, APIError)
        assert result.error_code == 500

    @pytest.mark.asyncio
    async def test_get_app_details_appid_missing_in_response(self, mock_http):
        mock_http.get_with_metadata.return_value = ({}, datetime.now(timezone.utc), 0)
        client = SteamStoreClient(mock_http)
        result = await client.get_app_details(646570)

        assert isinstance(result, APIError)
        assert result.error_type == "not_found"


# ---------------------------------------------------------------------------
# Phase 8: Steam Store extended field tests
# ---------------------------------------------------------------------------

class TestParsesSupportedLanguages:
    """Tests for parse_supported_languages helper function."""

    def test_parse_supported_languages_with_audio(self):
        """Language with <strong>*</strong> marker has full_audio=True."""
        raw = "English<strong>*</strong>, French<strong>*</strong>, German"
        result = parse_supported_languages(raw)
        assert len(result) == 3
        english = next(r for r in result if r["language"] == "English")
        assert english["full_audio"] is True
        french = next(r for r in result if r["language"] == "French")
        assert french["full_audio"] is True
        german = next(r for r in result if r["language"] == "German")
        assert german["full_audio"] is False

    def test_parse_supported_languages_no_audio(self):
        """Language without marker has full_audio=False."""
        raw = "Spanish, Portuguese"
        result = parse_supported_languages(raw)
        assert len(result) == 2
        assert all(r["full_audio"] is False for r in result)

    def test_parse_supported_languages_trailing_note_stripped(self):
        """Trailing '<br><strong>*</strong>languages with full audio support' is removed."""
        raw = (
            "English<strong>*</strong>, French<strong>*</strong>, German"
            "<br><strong>*</strong>languages with full audio support"
        )
        result = parse_supported_languages(raw)
        # Should not include a "languages with full audio support" language entry
        names = [r["language"] for r in result]
        assert not any("audio support" in n.lower() for n in names)
        assert len(result) == 3  # Only English, French, German

    def test_parse_supported_languages_empty(self):
        """Empty string returns empty list."""
        assert parse_supported_languages("") == []
        assert parse_supported_languages(None) == []

    def test_parse_supported_languages_all_fields_present(self):
        """Every returned entry has 'language' and 'full_audio' keys."""
        raw = "English<strong>*</strong>, Japanese, Korean"
        result = parse_supported_languages(raw)
        for entry in result:
            assert "language" in entry
            assert "full_audio" in entry


class TestSteamStoreExtendedFields:
    """Tests for Phase 8 Steam Store extended fields parsing."""

    @pytest.mark.asyncio
    async def test_achievement_count_parsed(self, mock_http):
        """details.achievements.total = 42 returns achievement_count=42."""
        data = {
            "646570": {
                "success": True,
                "data": {
                    "name": "Slay the Spire",
                    "achievements": {"total": 42, "highlighted": []},
                },
            }
        }
        mock_http.get_with_metadata.return_value = (data, datetime.now(timezone.utc), 0)
        client = SteamStoreClient(mock_http)
        result = await client.get_app_details(646570)

        assert isinstance(result, GameMetadata)
        assert result.achievement_count == 42

    @pytest.mark.asyncio
    async def test_ratings_parsed(self, mock_http):
        """ESRB and PEGI entries are parsed into the ratings dict."""
        data = {
            "646570": {
                "success": True,
                "data": {
                    "name": "Slay the Spire",
                    "ratings": {
                        "esrb": {"rating": "E10+", "descriptors": "Fantasy Violence"},
                        "pegi": {"rating": "12", "descriptors": "Violence"},
                    },
                },
            }
        }
        mock_http.get_with_metadata.return_value = (data, datetime.now(timezone.utc), 0)
        client = SteamStoreClient(mock_http)
        result = await client.get_app_details(646570)

        assert isinstance(result, GameMetadata)
        assert "esrb" in result.ratings
        assert result.ratings["esrb"]["rating"] == "E10+"
        assert "pegi" in result.ratings
        assert result.ratings["pegi"]["rating"] == "12"

    @pytest.mark.asyncio
    async def test_pc_requirements_dict(self, mock_http):
        """Dict with minimum and recommended HTML is stripped to plain text."""
        data = {
            "646570": {
                "success": True,
                "data": {
                    "name": "Slay the Spire",
                    "pc_requirements": {
                        "minimum": "<p><strong>Minimum:</strong><br>OS: Windows 7<br>CPU: 2GHz</p>",
                        "recommended": "<p><strong>Recommended:</strong><br>OS: Windows 10</p>",
                    },
                },
            }
        }
        mock_http.get_with_metadata.return_value = (data, datetime.now(timezone.utc), 0)
        client = SteamStoreClient(mock_http)
        result = await client.get_app_details(646570)

        assert isinstance(result, GameMetadata)
        assert "<" not in result.pc_requirements_min  # HTML stripped
        assert "Windows 7" in result.pc_requirements_min or "Minimum" in result.pc_requirements_min
        assert "<" not in result.pc_requirements_rec

    @pytest.mark.asyncio
    async def test_pc_requirements_list(self, mock_http):
        """Empty list [] for pc_requirements returns empty strings (no crash)."""
        data = {
            "646570": {
                "success": True,
                "data": {
                    "name": "Slay the Spire",
                    "pc_requirements": [],  # Some games return empty list
                },
            }
        }
        mock_http.get_with_metadata.return_value = (data, datetime.now(timezone.utc), 0)
        client = SteamStoreClient(mock_http)
        result = await client.get_app_details(646570)

        assert isinstance(result, GameMetadata)
        assert result.pc_requirements_min == ""
        assert result.pc_requirements_rec == ""

    @pytest.mark.asyncio
    async def test_controller_support_full(self, mock_http):
        """controller_support='full' returns 'full'."""
        data = {
            "646570": {
                "success": True,
                "data": {
                    "name": "Slay the Spire",
                    "controller_support": "full",
                },
            }
        }
        mock_http.get_with_metadata.return_value = (data, datetime.now(timezone.utc), 0)
        client = SteamStoreClient(mock_http)
        result = await client.get_app_details(646570)

        assert isinstance(result, GameMetadata)
        assert result.controller_support == "full"

    @pytest.mark.asyncio
    async def test_controller_support_absent(self, mock_http):
        """Missing controller_support field returns None."""
        data = {
            "646570": {
                "success": True,
                "data": {"name": "Slay the Spire"},
            }
        }
        mock_http.get_with_metadata.return_value = (data, datetime.now(timezone.utc), 0)
        client = SteamStoreClient(mock_http)
        result = await client.get_app_details(646570)

        assert isinstance(result, GameMetadata)
        assert result.controller_support is None

    @pytest.mark.asyncio
    async def test_developer_website(self, mock_http):
        """Developer website parsed from details.website."""
        data = {
            "646570": {
                "success": True,
                "data": {
                    "name": "Slay the Spire",
                    "website": "https://www.megacrit.com",
                },
            }
        }
        mock_http.get_with_metadata.return_value = (data, datetime.now(timezone.utc), 0)
        client = SteamStoreClient(mock_http)
        result = await client.get_app_details(646570)

        assert isinstance(result, GameMetadata)
        assert result.developer_website == "https://www.megacrit.com"

    @pytest.mark.asyncio
    async def test_steam_store_cache_ttl_7_days(self, mock_http):
        """get_app_details calls get_with_metadata with cache_ttl=604800 (7 days)."""
        data = {
            "646570": {
                "success": True,
                "data": {"name": "Slay the Spire"},
            }
        }
        mock_http.get_with_metadata.return_value = (data, datetime.now(timezone.utc), 0)
        client = SteamStoreClient(mock_http)
        await client.get_app_details(646570)

        call_kwargs = mock_http.get_with_metadata.call_args
        assert call_kwargs.kwargs.get("cache_ttl") == 604800

    @pytest.mark.asyncio
    async def test_supported_languages_structured(self, mock_http):
        """supported_languages returns structured list with language and full_audio fields."""
        data = {
            "646570": {
                "success": True,
                "data": {
                    "name": "Slay the Spire",
                    "supported_languages": (
                        "English<strong>*</strong>, French<strong>*</strong>, German"
                        "<br><strong>*</strong>languages with full audio support"
                    ),
                },
            }
        }
        mock_http.get_with_metadata.return_value = (data, datetime.now(timezone.utc), 0)
        client = SteamStoreClient(mock_http)
        result = await client.get_app_details(646570)

        assert isinstance(result, GameMetadata)
        assert len(result.supported_languages) == 3
        # supported_languages contains LanguageEntry objects with .language and .full_audio attributes
        lang_map = {lang.language: lang.full_audio for lang in result.supported_languages}
        assert lang_map.get("English") is True
        assert lang_map.get("French") is True
        assert lang_map.get("German") is False
