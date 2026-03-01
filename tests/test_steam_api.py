"""Tests for Steam API clients with mocked HTTP responses."""

import pytest
import httpx
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

from src.schemas import GameMetadata, SearchResult, APIError, NewsActivity
from src.steam_api import SteamSpyClient, SteamStoreClient, SteamNewsClient, parse_supported_languages


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


# ---------------------------------------------------------------------------
# Phase 8: Malformed Steam Store extended field tests
# ---------------------------------------------------------------------------

class TestSteamStoreMalformedFields:
    """Tests for defensive parsing of malformed Steam Store extended fields."""

    @pytest.mark.asyncio
    async def test_achievements_not_dict(self, mock_http):
        """achievements=[] (list instead of dict) — achievement_count stays None."""
        data = {
            "646570": {
                "success": True,
                "data": {
                    "name": "Test Game",
                    "achievements": [],  # Invalid type
                },
            }
        }
        mock_http.get_with_metadata.return_value = (data, datetime.now(timezone.utc), 0)
        client = SteamStoreClient(mock_http)
        result = await client.get_app_details(646570)

        assert isinstance(result, GameMetadata)
        assert result.achievement_count is None

    @pytest.mark.asyncio
    async def test_achievements_missing_total(self, mock_http):
        """achievements dict without 'total' key — achievement_count is None."""
        data = {
            "646570": {
                "success": True,
                "data": {
                    "name": "Test Game",
                    "achievements": {"highlighted": [{"name": "Win"}]},
                },
            }
        }
        mock_http.get_with_metadata.return_value = (data, datetime.now(timezone.utc), 0)
        client = SteamStoreClient(mock_http)
        result = await client.get_app_details(646570)

        assert isinstance(result, GameMetadata)
        assert result.achievement_count is None

    @pytest.mark.asyncio
    async def test_ratings_not_dict(self, mock_http):
        """ratings='string' — ratings defaults to empty dict."""
        data = {
            "646570": {
                "success": True,
                "data": {
                    "name": "Test Game",
                    "ratings": "not a dict",
                },
            }
        }
        mock_http.get_with_metadata.return_value = (data, datetime.now(timezone.utc), 0)
        client = SteamStoreClient(mock_http)
        result = await client.get_app_details(646570)

        assert isinstance(result, GameMetadata)
        assert result.ratings == {}

    @pytest.mark.asyncio
    async def test_ratings_nested_value_not_dict(self, mock_http):
        """Rating authority value is string, not dict — skipped gracefully."""
        data = {
            "646570": {
                "success": True,
                "data": {
                    "name": "Test Game",
                    "ratings": {
                        "esrb": "E10+",  # Should be dict, not string
                        "pegi": {"rating": "12", "descriptors": "Violence"},
                    },
                },
            }
        }
        mock_http.get_with_metadata.return_value = (data, datetime.now(timezone.utc), 0)
        client = SteamStoreClient(mock_http)
        result = await client.get_app_details(646570)

        assert isinstance(result, GameMetadata)
        # PEGI should be parsed, ESRB skipped (not a dict)
        assert "pegi" in result.ratings
        assert "esrb" not in result.ratings

    @pytest.mark.asyncio
    async def test_pc_requirements_string(self, mock_http):
        """pc_requirements is a bare string — falls through to empty defaults."""
        data = {
            "646570": {
                "success": True,
                "data": {
                    "name": "Test Game",
                    "pc_requirements": "Windows 7 required",  # Not dict or list
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
    async def test_pc_requirements_dict_missing_keys(self, mock_http):
        """pc_requirements dict with no minimum/recommended keys — empty strings."""
        data = {
            "646570": {
                "success": True,
                "data": {
                    "name": "Test Game",
                    "pc_requirements": {},  # Empty dict
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
    async def test_controller_support_unexpected_value(self, mock_http):
        """controller_support='gamepad' (unexpected) — stored but logged warning."""
        data = {
            "646570": {
                "success": True,
                "data": {
                    "name": "Test Game",
                    "controller_support": "gamepad",
                },
            }
        }
        mock_http.get_with_metadata.return_value = (data, datetime.now(timezone.utc), 0)
        client = SteamStoreClient(mock_http)

        import logging
        from src.steam_api import logger as steam_logger
        with patch.object(steam_logger, "warning") as mock_warn:
            result = await client.get_app_details(646570)

        assert isinstance(result, GameMetadata)
        # Value is stored even though unexpected
        assert result.controller_support == "gamepad"
        # Warning should be logged
        assert mock_warn.called

    @pytest.mark.asyncio
    async def test_supported_languages_malformed_html(self, mock_http):
        """Malformed HTML in supported_languages — best-effort parsing."""
        data = {
            "646570": {
                "success": True,
                "data": {
                    "name": "Test Game",
                    "supported_languages": "<b>English</b>, <i>French</i>",
                },
            }
        }
        mock_http.get_with_metadata.return_value = (data, datetime.now(timezone.utc), 0)
        client = SteamStoreClient(mock_http)
        result = await client.get_app_details(646570)

        assert isinstance(result, GameMetadata)
        # Should still extract language names despite unusual HTML
        assert len(result.supported_languages) >= 2

    @pytest.mark.asyncio
    async def test_website_empty_string(self, mock_http):
        """website='' — developer_website should be None (not empty string)."""
        data = {
            "646570": {
                "success": True,
                "data": {
                    "name": "Test Game",
                    "website": "",
                },
            }
        }
        mock_http.get_with_metadata.return_value = (data, datetime.now(timezone.utc), 0)
        client = SteamStoreClient(mock_http)
        result = await client.get_app_details(646570)

        assert isinstance(result, GameMetadata)
        assert result.developer_website is None

    @pytest.mark.asyncio
    async def test_all_extended_fields_missing(self, mock_http):
        """No extended fields at all — all defaults, no crash."""
        data = {
            "646570": {
                "success": True,
                "data": {
                    "name": "Bare Minimum Game",
                    # No achievements, ratings, languages, requirements, controller, website
                },
            }
        }
        mock_http.get_with_metadata.return_value = (data, datetime.now(timezone.utc), 0)
        client = SteamStoreClient(mock_http)
        result = await client.get_app_details(646570)

        assert isinstance(result, GameMetadata)
        assert result.achievement_count is None
        assert result.ratings == {}
        assert result.supported_languages == []
        assert result.developer_website is None
        assert result.pc_requirements_min == ""
        assert result.pc_requirements_rec == ""
        assert result.controller_support is None


# ---------------------------------------------------------------------------
# Phase 15: SteamNewsClient tests
# ---------------------------------------------------------------------------

def _make_news_item(
    gid: str,
    title: str,
    date: int,
    feed_type: int,
    feedname: str = "steam_community_announcements",
    appid: int = 646570,
) -> dict:
    """Create a mock Steam News API newsitem dict."""
    return {
        "gid": gid,
        "title": title,
        "url": f"https://store.steampowered.com/news/{gid}",
        "is_external_url": feed_type == 0,
        "author": "developer",
        "contents": "",
        "feedlabel": "Community Announcements" if feed_type == 1 else "PC Gamer",
        "date": date,
        "feedname": feedname,
        "feed_type": feed_type,
        "appid": appid,
    }


class TestSteamNewsClient:
    """Tests for SteamNewsClient.get_news_activity()."""

    @pytest.mark.asyncio
    async def test_get_news_activity_success(self, mock_http):
        """Mixed feed: 3 developer posts (feed_type=1), 2 press (feed_type=0)."""
        now = int(datetime.now(timezone.utc).timestamp())
        items = [
            _make_news_item("1", "Patch 1.2", now - 10000, feed_type=1),
            _make_news_item("2", "Patch 1.1", now - 200000, feed_type=1),
            _make_news_item("3", "EA Launch", now - 3000000, feed_type=1),
            _make_news_item("4", "PC Gamer Review", now - 50000, feed_type=0, feedname="pcgamer"),
            _make_news_item("5", "RPS Preview", now - 90000, feed_type=0, feedname="rockpapershotgun"),
        ]
        data = {"appnews": {"appid": 646570, "count": 5, "newsitems": items}}
        mock_http.get_with_metadata.return_value = (data, datetime.now(timezone.utc), 0)

        client = SteamNewsClient(mock_http)
        result = await client.get_news_activity(646570)

        assert isinstance(result, NewsActivity)
        assert result.appid == 646570
        assert result.developer_posts_total == 3
        assert result.press_mentions_total == 2
        assert result.total_news_items == 5
        assert result.developer_last_post_date is not None
        assert result.developer_recent_titles == ["Patch 1.2", "Patch 1.1", "EA Launch"]

    @pytest.mark.asyncio
    async def test_get_news_activity_empty_feed(self, mock_http):
        """Empty newsitems list — all counts should be zero."""
        data = {"appnews": {"appid": 646570, "count": 0, "newsitems": []}}
        mock_http.get_with_metadata.return_value = (data, datetime.now(timezone.utc), 0)

        client = SteamNewsClient(mock_http)
        result = await client.get_news_activity(646570)

        assert isinstance(result, NewsActivity)
        assert result.developer_posts_total == 0
        assert result.press_mentions_total == 0
        assert result.total_news_items == 0
        assert result.developer_avg_days_between_posts is None
        assert result.developer_last_post_date is None
        assert result.first_activity_date is None
        assert result.last_activity_date is None

    @pytest.mark.asyncio
    async def test_get_news_activity_api_error(self, mock_http):
        """HTTPStatusError should return APIError with correct status code."""
        mock_response = httpx.Response(403)
        mock_http.get_with_metadata.side_effect = httpx.HTTPStatusError(
            "403 Forbidden", request=httpx.Request("GET", "https://example.com"), response=mock_response
        )

        client = SteamNewsClient(mock_http)
        result = await client.get_news_activity(646570)

        assert isinstance(result, APIError)
        assert result.error_code == 403

    @pytest.mark.asyncio
    async def test_get_news_activity_date_is_seconds_not_ms(self, mock_http):
        """Date=1700000000 (Nov 2023) should parse to ~2023, NOT ~2053."""
        # Nov 14, 2023 in Unix seconds
        ts_seconds = 1700000000
        items = [_make_news_item("1", "Update", ts_seconds, feed_type=1)]
        data = {"appnews": {"appid": 1, "count": 1, "newsitems": items}}
        mock_http.get_with_metadata.return_value = (data, datetime.now(timezone.utc), 0)

        client = SteamNewsClient(mock_http)
        result = await client.get_news_activity(1)

        assert isinstance(result, NewsActivity)
        # Should be 2023, not 2053 (which would happen if treating seconds as ms)
        assert result.developer_last_post_date is not None
        year = int(result.developer_last_post_date[:4])
        assert year == 2023, f"Expected year 2023, got {year} — timestamp treated as ms?"

    @pytest.mark.asyncio
    async def test_press_coverage_split(self, mock_http):
        """Verify developer and press counts are independent and correct."""
        now = int(datetime.now(timezone.utc).timestamp())
        items = [
            _make_news_item("1", "Dev Post A", now - 1000, feed_type=1),
            _make_news_item("2", "Dev Post B", now - 2000, feed_type=1),
            _make_news_item("3", "PC Gamer", now - 3000, feed_type=0),
            _make_news_item("4", "Rock Paper Shotgun", now - 4000, feed_type=0),
            _make_news_item("5", "Kotaku", now - 5000, feed_type=0),
        ]
        data = {"appnews": {"appid": 1, "count": 5, "newsitems": items}}
        mock_http.get_with_metadata.return_value = (data, datetime.now(timezone.utc), 0)

        client = SteamNewsClient(mock_http)
        result = await client.get_news_activity(1)

        assert isinstance(result, NewsActivity)
        # Independent counts
        assert result.developer_posts_total == 2
        assert result.press_mentions_total == 3
        # Total is sum
        assert result.total_news_items == 5
        # Developer recents only has developer titles
        assert len(result.developer_recent_titles) == 2
        assert "PC Gamer" not in result.developer_recent_titles
        assert "Dev Post A" in result.developer_recent_titles

    @pytest.mark.asyncio
    async def test_no_developer_posts_only_press(self, mock_http):
        """Only press items (feed_type=0) — developer metrics should be zero."""
        now = int(datetime.now(timezone.utc).timestamp())
        items = [
            _make_news_item("1", "Ars Technica", now - 1000, feed_type=0),
            _make_news_item("2", "Eurogamer", now - 2000, feed_type=0),
        ]
        data = {"appnews": {"appid": 1, "count": 2, "newsitems": items}}
        mock_http.get_with_metadata.return_value = (data, datetime.now(timezone.utc), 0)

        client = SteamNewsClient(mock_http)
        result = await client.get_news_activity(1)

        assert isinstance(result, NewsActivity)
        assert result.developer_posts_total == 0
        assert result.developer_last_post_date is None
        assert result.developer_avg_days_between_posts is None
        assert result.developer_recent_titles == []
        assert result.press_mentions_total == 2
        assert result.total_news_items == 2

    @pytest.mark.asyncio
    async def test_avg_days_between_posts_none_for_single_post(self, mock_http):
        """Single developer post — avg_days_between_posts should be None (<2 posts required)."""
        now = int(datetime.now(timezone.utc).timestamp())
        items = [_make_news_item("1", "Only Post", now - 1000, feed_type=1)]
        data = {"appnews": {"appid": 1, "count": 1, "newsitems": items}}
        mock_http.get_with_metadata.return_value = (data, datetime.now(timezone.utc), 0)

        client = SteamNewsClient(mock_http)
        result = await client.get_news_activity(1)

        assert isinstance(result, NewsActivity)
        assert result.developer_posts_total == 1
        assert result.developer_avg_days_between_posts is None

    @pytest.mark.asyncio
    async def test_avg_days_between_posts_computed_for_two_posts(self, mock_http):
        """Two developer posts ~10 days apart — avg should be ~10 days."""
        now = int(datetime.now(timezone.utc).timestamp())
        ten_days_ago = now - (10 * 86400)
        items = [
            _make_news_item("1", "Recent Post", now - 1000, feed_type=1),
            _make_news_item("2", "Old Post", ten_days_ago, feed_type=1),
        ]
        data = {"appnews": {"appid": 1, "count": 2, "newsitems": items}}
        mock_http.get_with_metadata.return_value = (data, datetime.now(timezone.utc), 0)

        client = SteamNewsClient(mock_http)
        result = await client.get_news_activity(1)

        assert isinstance(result, NewsActivity)
        assert result.developer_avg_days_between_posts is not None
        # Should be approximately 10 days (allowing for the 1000 second offset)
        assert 9.0 < result.developer_avg_days_between_posts < 11.0

    @pytest.mark.asyncio
    async def test_time_window_counts_last_90d(self, mock_http):
        """Items within 90d and older than 90d are counted correctly."""
        now = int(datetime.now(timezone.utc).timestamp())
        within_90d = now - (30 * 86400)   # 30 days ago
        outside_90d = now - (200 * 86400)  # 200 days ago

        items = [
            _make_news_item("1", "Recent Dev", within_90d, feed_type=1),
            _make_news_item("2", "Old Dev", outside_90d, feed_type=1),
            _make_news_item("3", "Recent Press", within_90d, feed_type=0),
            _make_news_item("4", "Old Press", outside_90d, feed_type=0),
        ]
        data = {"appnews": {"appid": 1, "count": 4, "newsitems": items}}
        mock_http.get_with_metadata.return_value = (data, datetime.now(timezone.utc), 0)

        client = SteamNewsClient(mock_http)
        result = await client.get_news_activity(1)

        assert isinstance(result, NewsActivity)
        assert result.developer_posts_last_90d == 1
        assert result.developer_posts_last_365d == 2  # both within 365d
        assert result.press_mentions_last_90d == 1
        assert result.press_mentions_last_365d == 2

    @pytest.mark.asyncio
    async def test_developer_recent_titles_capped_at_5(self, mock_http):
        """developer_recent_titles should contain at most 5 entries."""
        now = int(datetime.now(timezone.utc).timestamp())
        items = [
            _make_news_item(str(i), f"Post {i}", now - (i * 1000), feed_type=1)
            for i in range(10)  # 10 developer posts
        ]
        data = {"appnews": {"appid": 1, "count": 10, "newsitems": items}}
        mock_http.get_with_metadata.return_value = (data, datetime.now(timezone.utc), 0)

        client = SteamNewsClient(mock_http)
        result = await client.get_news_activity(1)

        assert isinstance(result, NewsActivity)
        assert result.developer_posts_total == 10
        assert len(result.developer_recent_titles) == 5


class TestDemosParsingInGetAppDetails:
    """Tests for demos field parsing added to SteamStoreClient.get_app_details()."""

    @pytest.mark.asyncio
    async def test_demos_present(self, mock_http):
        """Game with a demo — demos_available=True and demos list populated."""
        data = {
            "12345": {
                "success": True,
                "data": {
                    "name": "Demo Game",
                    "demos": [
                        {"appid": 12346, "description": ""}
                    ],
                },
            }
        }
        mock_http.get_with_metadata.return_value = (data, datetime.now(timezone.utc), 0)
        client = SteamStoreClient(mock_http)
        result = await client.get_app_details(12345)

        assert isinstance(result, GameMetadata)
        assert result.demos_available is True
        assert len(result.demos) == 1
        assert result.demos[0]["appid"] == 12346

    @pytest.mark.asyncio
    async def test_demos_absent(self, mock_http):
        """Game without demos — demos_available=False and demos=[]."""
        data = {
            "646570": {
                "success": True,
                "data": {
                    "name": "Slay the Spire",
                    # No demos field — should be absent, not null
                },
            }
        }
        mock_http.get_with_metadata.return_value = (data, datetime.now(timezone.utc), 0)
        client = SteamStoreClient(mock_http)
        result = await client.get_app_details(646570)

        assert isinstance(result, GameMetadata)
        assert result.demos_available is False
        assert result.demos == []

    @pytest.mark.asyncio
    async def test_demos_empty_list(self, mock_http):
        """Game with empty demos array — demos_available=False."""
        data = {
            "1": {
                "success": True,
                "data": {
                    "name": "Test Game",
                    "demos": [],
                },
            }
        }
        mock_http.get_with_metadata.return_value = (data, datetime.now(timezone.utc), 0)
        client = SteamStoreClient(mock_http)
        result = await client.get_app_details(1)

        assert isinstance(result, GameMetadata)
        assert result.demos_available is False
        assert result.demos == []

    @pytest.mark.asyncio
    async def test_demos_multiple(self, mock_http):
        """Game with multiple demos — all captured."""
        data = {
            "1": {
                "success": True,
                "data": {
                    "name": "Multi Demo Game",
                    "demos": [
                        {"appid": 100, "description": "Chapter 1 Demo"},
                        {"appid": 101, "description": "Chapter 2 Demo"},
                    ],
                },
            }
        }
        mock_http.get_with_metadata.return_value = (data, datetime.now(timezone.utc), 0)
        client = SteamStoreClient(mock_http)
        result = await client.get_app_details(1)

        assert isinstance(result, GameMetadata)
        assert result.demos_available is True
        assert len(result.demos) == 2
        assert result.demos[1]["description"] == "Chapter 2 Demo"
