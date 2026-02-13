"""Tests for Phase 5 data enrichment: GameSummary, enriched metadata, utilities, and backward compatibility."""

import pytest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

from src.schemas import GameMetadata, SearchResult, GameSummary, APIError
from src.steam_api import strip_html, parse_steam_date, SteamSpyClient, SteamStoreClient


class TestStripHtml:
    """Test HTML stripping utility for detailed descriptions."""

    def test_basic_tags_removed(self):
        html = "<b>Hello</b>"
        assert strip_html(html) == "Hello"

    def test_html_entities_unescaped(self):
        html = "&amp; &nbsp; &quot;"
        result = strip_html(html)
        assert "&" in result  # &amp; -> &
        assert '"' in result  # &quot; -> "

    def test_nested_tags(self):
        html = "<div><p>Hello <b>world</b></p></div>"
        assert strip_html(html) == "Hello world"

    def test_empty_string_returns_empty(self):
        assert strip_html("") == ""

    def test_none_safe(self):
        # Defensive: shouldn't happen but should be safe
        assert strip_html(None) == ""

    def test_steam_typical_html(self):
        html = """<h2>About This Game</h2><br>
        <p>A deckbuilder roguelike</p><br>
        <img src="screenshot.jpg">
        More text here"""
        result = strip_html(html)
        assert "About This Game" in result
        assert "A deckbuilder roguelike" in result
        assert "More text here" in result
        assert "<h2>" not in result
        assert "<br>" not in result
        assert "<img" not in result


class TestParseSteamDate:
    """Test Steam date parsing to ISO format."""

    def test_standard_format(self):
        assert parse_steam_date("23 Jan, 2019") == "2019-01-23"

    def test_us_format(self):
        assert parse_steam_date("Jan 23, 2019") == "2019-01-23"

    def test_coming_soon_returns_none(self):
        assert parse_steam_date("Coming soon") is None

    def test_tba_returns_none(self):
        assert parse_steam_date("TBA") is None

    def test_empty_string_returns_none(self):
        assert parse_steam_date("") is None

    def test_unparseable_returns_none(self):
        assert parse_steam_date("Q1 2024") is None

    def test_full_month_name(self):
        assert parse_steam_date("January 23, 2019") == "2019-01-23"

    def test_day_month_full_format(self):
        assert parse_steam_date("23 January, 2019") == "2019-01-23"


class TestSearchGenreEnrichment:
    """Test enriched search_by_tag with GameSummary objects."""

    @pytest.mark.asyncio
    async def test_enriched_search_returns_game_summaries(self):
        """Verify GameSummary objects have all bulk endpoint fields."""
        mock_http = AsyncMock()
        bulk_data = {
            "646570": {
                "appid": 646570,
                "name": "Slay the Spire",
                "developer": "Mega Crit",
                "publisher": "Humble Bundle",
                "owners": "5,000,000 .. 10,000,000",
                "ccu": 5000,
                "price": 2499,  # cents
                "positive": 50000,
                "negative": 2000,
                "userscore": 96,
                "average_forever": 1530,  # minutes
                "median_forever": 720,  # minutes
                "average_2weeks": 180,  # minutes
                "median_2weeks": 60,  # minutes
            },
            "2379780": {
                "appid": 2379780,
                "name": "Balatro",
                "developer": "LocalThunk",
                "publisher": "Playstack",
                "owners": "2,000,000 .. 5,000,000",
                "ccu": 3000,
                "price": 1499,
                "positive": 30000,
                "negative": 500,
                "userscore": 98,
                "average_forever": 900,
                "median_forever": 600,
                "average_2weeks": 120,
                "median_2weeks": 40,
            }
        }
        mock_http.get_with_metadata.return_value = (bulk_data, datetime.now(timezone.utc), 0)

        client = SteamSpyClient(mock_http)
        result = await client.search_by_tag("Roguelike", limit=10)

        assert isinstance(result, SearchResult)
        assert len(result.games) == 2

        # Verify first game has correct enriched fields
        game1 = result.games[0]
        assert isinstance(game1, GameSummary)
        assert game1.appid == 646570
        assert game1.name == "Slay the Spire"
        assert game1.developer == "Mega Crit"
        assert game1.publisher == "Humble Bundle"
        assert game1.owners_midpoint == 7_500_000  # midpoint of 5M-10M
        assert game1.ccu == 5000
        assert game1.price == 24.99  # converted from cents
        assert game1.positive == 50000
        assert game1.negative == 2000
        assert game1.review_score == pytest.approx(96.15, abs=0.1)  # positive/(positive+negative)*100
        assert game1.average_playtime == pytest.approx(25.5, abs=0.1)  # hours
        assert game1.median_playtime == pytest.approx(12.0, abs=0.1)  # hours

    @pytest.mark.asyncio
    async def test_sort_by_ccu(self):
        """Verify sorting by CCU orders games correctly."""
        mock_http = AsyncMock()
        bulk_data = {
            "1": {"appid": 1, "ccu": 1000, "owners": "0 .. 20000"},
            "2": {"appid": 2, "ccu": 5000, "owners": "0 .. 20000"},
            "3": {"appid": 3, "ccu": 2000, "owners": "0 .. 20000"},
        }
        mock_http.get_with_metadata.return_value = (bulk_data, datetime.now(timezone.utc), 0)

        client = SteamSpyClient(mock_http)
        result = await client.search_by_tag("Action", sort_by="ccu", limit=None)

        # Should be sorted by CCU descending: 5000, 2000, 1000
        assert result.games[0].ccu == 5000
        assert result.games[1].ccu == 2000
        assert result.games[2].ccu == 1000

    @pytest.mark.asyncio
    async def test_sort_by_price(self):
        """Verify sorting by price orders games correctly."""
        mock_http = AsyncMock()
        bulk_data = {
            "1": {"appid": 1, "price": 1999, "owners": "0 .. 20000"},  # $19.99
            "2": {"appid": 2, "price": 2999, "owners": "0 .. 20000"},  # $29.99
            "3": {"appid": 3, "price": 999, "owners": "0 .. 20000"},   # $9.99
        }
        mock_http.get_with_metadata.return_value = (bulk_data, datetime.now(timezone.utc), 0)

        client = SteamSpyClient(mock_http)
        result = await client.search_by_tag("Indie", sort_by="price", limit=None)

        # Should be sorted by price descending: 29.99, 19.99, 9.99
        assert result.games[0].price == pytest.approx(29.99, abs=0.01)
        assert result.games[1].price == pytest.approx(19.99, abs=0.01)
        assert result.games[2].price == pytest.approx(9.99, abs=0.01)

    @pytest.mark.asyncio
    async def test_sort_by_review_score(self):
        """Verify sorting by review_score orders games correctly."""
        mock_http = AsyncMock()
        bulk_data = {
            "1": {"appid": 1, "positive": 80, "negative": 20, "owners": "0 .. 20000"},  # 80% score
            "2": {"appid": 2, "positive": 95, "negative": 5, "owners": "0 .. 20000"},   # 95% score
            "3": {"appid": 3, "positive": 70, "negative": 30, "owners": "0 .. 20000"},  # 70% score
        }
        mock_http.get_with_metadata.return_value = (bulk_data, datetime.now(timezone.utc), 0)

        client = SteamSpyClient(mock_http)
        result = await client.search_by_tag("Strategy", sort_by="review_score", limit=None)

        # Should be sorted by review_score descending: 95%, 80%, 70%
        assert result.games[0].review_score == pytest.approx(95.0, abs=0.1)
        assert result.games[1].review_score == pytest.approx(80.0, abs=0.1)
        assert result.games[2].review_score == pytest.approx(70.0, abs=0.1)

    @pytest.mark.asyncio
    async def test_limit_applied_after_sort(self):
        """Verify limit returns top N games by sort criteria."""
        mock_http = AsyncMock()
        bulk_data = {
            str(i): {"appid": i, "owners": f"{i*100000} .. {(i+1)*100000}"}
            for i in range(1, 6)  # 5 games
        }
        mock_http.get_with_metadata.return_value = (bulk_data, datetime.now(timezone.utc), 0)

        client = SteamSpyClient(mock_http)
        result = await client.search_by_tag("RPG", limit=2)

        # Should return top 2 by owners (default sort)
        assert len(result.games) == 2
        assert len(result.appids) == 2
        assert result.total_found == 5
        # Top 2 should be appids 5 and 4 (highest owners)
        assert result.games[0].owners_midpoint > result.games[1].owners_midpoint

    @pytest.mark.asyncio
    async def test_large_response_warning(self):
        """Verify result_size_warning for large result sets (>5000 games)."""
        mock_http = AsyncMock()
        bulk_data = {str(i): {"appid": i, "owners": "0 .. 20000"} for i in range(6000)}
        mock_http.get_with_metadata.return_value = (bulk_data, datetime.now(timezone.utc), 0)

        client = SteamSpyClient(mock_http)
        result = await client.search_by_tag("Action", limit=10)

        assert result.result_size_warning is not None
        assert "6000" in result.result_size_warning

    @pytest.mark.asyncio
    async def test_no_warning_below_threshold(self):
        """Verify no warning for result sets <5000 games."""
        mock_http = AsyncMock()
        bulk_data = {str(i): {"appid": i, "owners": "0 .. 20000"} for i in range(100)}
        mock_http.get_with_metadata.return_value = (bulk_data, datetime.now(timezone.utc), 0)

        client = SteamSpyClient(mock_http)
        result = await client.search_by_tag("Niche", limit=10)

        assert result.result_size_warning is None

    @pytest.mark.asyncio
    async def test_normalized_tag_in_result(self):
        """Verify normalized_tag shows what was actually queried when tag is normalized."""
        mock_http = AsyncMock()
        bulk_data = {"1": {"appid": 1, "owners": "0 .. 20000"}}
        mock_http.get_with_metadata.return_value = (bulk_data, datetime.now(timezone.utc), 0)

        client = SteamSpyClient(mock_http)
        result = await client.search_by_tag("roguelike", limit=10)  # lowercase

        assert result.tag == "Rogue-like"  # normalized
        assert result.normalized_tag == "Rogue-like"  # tag was normalized

    @pytest.mark.asyncio
    async def test_backward_compat_appids_matches_games(self):
        """Verify backward compat: appids field matches games[].appid."""
        mock_http = AsyncMock()
        bulk_data = {
            "100": {"appid": 100, "owners": "0 .. 20000"},
            "200": {"appid": 200, "owners": "0 .. 20000"},
            "300": {"appid": 300, "owners": "0 .. 20000"},
        }
        mock_http.get_with_metadata.return_value = (bulk_data, datetime.now(timezone.utc), 0)

        client = SteamSpyClient(mock_http)
        result = await client.search_by_tag("Test", limit=None)

        assert result.appids == [g.appid for g in result.games]


class TestFetchMetadataEnrichment:
    """Test enriched get_app_details with all Steam Store fields."""

    @pytest.mark.asyncio
    async def test_full_enriched_response(self):
        """Verify all new GameMetadata fields are populated correctly."""
        mock_http = AsyncMock()
        full_data = {
            "646570": {
                "success": True,
                "data": {
                    "name": "Slay the Spire",
                    "genres": [{"description": "Indie"}, {"description": "Strategy"}],
                    "developers": ["Mega Crit"],
                    "publishers": ["Humble Bundle"],
                    "short_description": "A deckbuilder roguelike",
                    "detailed_description": "<h2>About</h2><p>Detailed info</p>",
                    "header_image": "https://cdn.akamai.steamstatic.com/header.jpg",
                    "price_overview": {"final": 2499, "currency": "USD"},
                    "is_free": False,
                    "platforms": {"windows": True, "mac": True, "linux": False},
                    "release_date": {"date": "23 Jan, 2019"},
                    "metacritic": {"score": 89, "url": "https://metacritic.com/game"},
                    "categories": [{"description": "Single-player"}, {"description": "Steam Achievements"}],
                    "screenshots": [
                        {"path_full": "https://example.com/screenshot1.jpg"},
                        {"path_full": "https://example.com/screenshot2.jpg"}
                    ],
                    "movies": [
                        {
                            "name": "Launch Trailer",
                            "thumbnail": "https://example.com/thumb.jpg",
                            "webm": {"max": "https://example.com/trailer.webm"},
                            "mp4": {"max": "https://example.com/trailer.mp4"}
                        }
                    ],
                    "dlc": [1234567, 7654321],
                    "recommendations": {"total": 75000},
                    "content_descriptors": {"notes": "Fantasy Violence"},
                    "supported_languages": "English, French, German"
                }
            }
        }
        mock_http.get_with_metadata.return_value = (full_data, datetime.now(timezone.utc), 0)

        client = SteamStoreClient(mock_http)
        result = await client.get_app_details(646570)

        assert isinstance(result, GameMetadata)
        assert result.appid == 646570
        assert result.name == "Slay the Spire"
        assert result.developer == "Mega Crit"
        assert result.publisher == "Humble Bundle"
        assert result.short_description == "A deckbuilder roguelike"
        assert "Detailed info" in result.detailed_description  # HTML stripped
        assert "<h2>" not in result.detailed_description
        assert result.header_image == "https://cdn.akamai.steamstatic.com/header.jpg"
        assert result.price == pytest.approx(24.99, abs=0.01)
        assert result.is_free_to_play is False
        assert result.platforms == {"windows": True, "mac": True, "linux": False}
        assert result.release_date == "2019-01-23"
        assert result.release_date_raw == "23 Jan, 2019"
        assert result.metacritic_score == 89
        assert result.metacritic_url == "https://metacritic.com/game"
        assert result.categories == ["Single-player", "Steam Achievements"]
        assert result.genres == ["Indie", "Strategy"]
        assert len(result.screenshots) == 2
        assert result.screenshots_count == 2
        assert len(result.movies) == 1
        assert result.movies_count == 1
        assert result.movies[0]["name"] == "Launch Trailer"
        assert len(result.dlc) == 2
        assert result.dlc_count == 2
        assert result.recommendations == 75000

    @pytest.mark.asyncio
    async def test_free_to_play_game(self):
        """Verify F2P game: price=0.0, is_free_to_play=True, no price_overview."""
        mock_http = AsyncMock()
        f2p_data = {
            "570": {
                "success": True,
                "data": {
                    "name": "Dota 2",
                    "is_free": True,
                    "genres": [{"description": "Action"}],
                    "developers": ["Valve"],
                    "short_description": "Dota is fun"
                }
            }
        }
        mock_http.get_with_metadata.return_value = (f2p_data, datetime.now(timezone.utc), 0)

        client = SteamStoreClient(mock_http)
        result = await client.get_app_details(570)

        assert result.is_free_to_play is True
        assert result.price == 0.0  # F2P games have price 0

    @pytest.mark.asyncio
    async def test_paid_game_price(self):
        """Verify paid game with price_overview correctly parsed."""
        mock_http = AsyncMock()
        paid_data = {
            "123": {
                "success": True,
                "data": {
                    "name": "Paid Game",
                    "price_overview": {"final": 4999, "currency": "USD"},
                    "is_free": False,
                    "genres": [{"description": "RPG"}],
                    "developers": ["Studio"],
                    "short_description": "Description"
                }
            }
        }
        mock_http.get_with_metadata.return_value = (paid_data, datetime.now(timezone.utc), 0)

        client = SteamStoreClient(mock_http)
        result = await client.get_app_details(123)

        assert result.is_free_to_play is False
        assert result.price == pytest.approx(49.99, abs=0.01)

    @pytest.mark.asyncio
    async def test_release_date_parsed(self):
        """Verify release date is parsed to ISO format."""
        mock_http = AsyncMock()
        data = {
            "123": {
                "success": True,
                "data": {
                    "name": "Game",
                    "release_date": {"date": "23 Jan, 2019"},
                    "genres": [{"description": "Action"}],
                    "developers": ["Dev"],
                    "short_description": "Desc"
                }
            }
        }
        mock_http.get_with_metadata.return_value = (data, datetime.now(timezone.utc), 0)

        client = SteamStoreClient(mock_http)
        result = await client.get_app_details(123)

        assert result.release_date == "2019-01-23"
        assert result.release_date_raw == "23 Jan, 2019"

    @pytest.mark.asyncio
    async def test_release_date_coming_soon(self):
        """Verify 'Coming soon' releases have None as release_date."""
        mock_http = AsyncMock()
        data = {
            "123": {
                "success": True,
                "data": {
                    "name": "Game",
                    "release_date": {"date": "Coming soon"},
                    "genres": [{"description": "Action"}],
                    "developers": ["Dev"],
                    "short_description": "Desc"
                }
            }
        }
        mock_http.get_with_metadata.return_value = (data, datetime.now(timezone.utc), 0)

        client = SteamStoreClient(mock_http)
        result = await client.get_app_details(123)

        assert result.release_date is None
        assert result.release_date_raw == "Coming soon"

    @pytest.mark.asyncio
    async def test_metacritic_present(self):
        """Verify metacritic data is parsed correctly."""
        mock_http = AsyncMock()
        data = {
            "123": {
                "success": True,
                "data": {
                    "name": "Game",
                    "metacritic": {"score": 92, "url": "https://metacritic.com"},
                    "genres": [{"description": "Action"}],
                    "developers": ["Dev"],
                    "short_description": "Desc"
                }
            }
        }
        mock_http.get_with_metadata.return_value = (data, datetime.now(timezone.utc), 0)

        client = SteamStoreClient(mock_http)
        result = await client.get_app_details(123)

        assert result.metacritic_score == 92
        assert result.metacritic_url == "https://metacritic.com"

    @pytest.mark.asyncio
    async def test_metacritic_absent(self):
        """Verify games without metacritic data have None."""
        mock_http = AsyncMock()
        data = {
            "123": {
                "success": True,
                "data": {
                    "name": "Game",
                    "genres": [{"description": "Action"}],
                    "developers": ["Dev"],
                    "short_description": "Desc"
                }
            }
        }
        mock_http.get_with_metadata.return_value = (data, datetime.now(timezone.utc), 0)

        client = SteamStoreClient(mock_http)
        result = await client.get_app_details(123)

        assert result.metacritic_score is None
        assert result.metacritic_url == ""

    @pytest.mark.asyncio
    async def test_screenshots_parsed(self):
        """Verify screenshots list is parsed correctly."""
        mock_http = AsyncMock()
        data = {
            "123": {
                "success": True,
                "data": {
                    "name": "Game",
                    "screenshots": [
                        {"path_full": "https://example.com/s1.jpg"},
                        {"path_full": "https://example.com/s2.jpg"},
                        {"path_full": "https://example.com/s3.jpg"}
                    ],
                    "genres": [{"description": "Action"}],
                    "developers": ["Dev"],
                    "short_description": "Desc"
                }
            }
        }
        mock_http.get_with_metadata.return_value = (data, datetime.now(timezone.utc), 0)

        client = SteamStoreClient(mock_http)
        result = await client.get_app_details(123)

        assert len(result.screenshots) == 3
        assert result.screenshots_count == 3
        assert result.screenshots[0] == "https://example.com/s1.jpg"

    @pytest.mark.asyncio
    async def test_movies_parsed(self):
        """Verify movies list with all fields is parsed correctly."""
        mock_http = AsyncMock()
        data = {
            "123": {
                "success": True,
                "data": {
                    "name": "Game",
                    "movies": [
                        {
                            "name": "Trailer 1",
                            "thumbnail": "https://example.com/thumb1.jpg",
                            "webm": {"max": "https://example.com/t1.webm"},
                            "mp4": {"max": "https://example.com/t1.mp4"}
                        },
                        {
                            "name": "Trailer 2",
                            "thumbnail": "https://example.com/thumb2.jpg",
                            "webm": {"max": "https://example.com/t2.webm"},
                            "mp4": {"max": "https://example.com/t2.mp4"}
                        }
                    ],
                    "genres": [{"description": "Action"}],
                    "developers": ["Dev"],
                    "short_description": "Desc"
                }
            }
        }
        mock_http.get_with_metadata.return_value = (data, datetime.now(timezone.utc), 0)

        client = SteamStoreClient(mock_http)
        result = await client.get_app_details(123)

        assert len(result.movies) == 2
        assert result.movies_count == 2
        assert result.movies[0]["name"] == "Trailer 1"
        assert result.movies[0]["thumbnail"] == "https://example.com/thumb1.jpg"
        assert result.movies[0]["webm_url"] == "https://example.com/t1.webm"
        assert result.movies[0]["mp4_url"] == "https://example.com/t1.mp4"

    @pytest.mark.asyncio
    async def test_dlc_list(self):
        """Verify DLC AppIDs are parsed with count."""
        mock_http = AsyncMock()
        data = {
            "123": {
                "success": True,
                "data": {
                    "name": "Game",
                    "dlc": [111, 222, 333, 444],
                    "genres": [{"description": "Action"}],
                    "developers": ["Dev"],
                    "short_description": "Desc"
                }
            }
        }
        mock_http.get_with_metadata.return_value = (data, datetime.now(timezone.utc), 0)

        client = SteamStoreClient(mock_http)
        result = await client.get_app_details(123)

        assert len(result.dlc) == 4
        assert result.dlc_count == 4
        assert 111 in result.dlc

    @pytest.mark.asyncio
    async def test_platforms_parsed(self):
        """Verify platform support dict is parsed correctly."""
        mock_http = AsyncMock()
        data = {
            "123": {
                "success": True,
                "data": {
                    "name": "Game",
                    "platforms": {"windows": True, "mac": False, "linux": True},
                    "genres": [{"description": "Action"}],
                    "developers": ["Dev"],
                    "short_description": "Desc"
                }
            }
        }
        mock_http.get_with_metadata.return_value = (data, datetime.now(timezone.utc), 0)

        client = SteamStoreClient(mock_http)
        result = await client.get_app_details(123)

        assert result.platforms["windows"] is True
        assert result.platforms["mac"] is False
        assert result.platforms["linux"] is True

    @pytest.mark.asyncio
    async def test_detailed_description_html_stripped(self):
        """Verify detailed_description has HTML stripped and entities unescaped."""
        mock_http = AsyncMock()
        data = {
            "123": {
                "success": True,
                "data": {
                    "name": "Game",
                    "detailed_description": "<h1>Title</h1><p>This is <b>bold</b> text &amp; more</p>",
                    "genres": [{"description": "Action"}],
                    "developers": ["Dev"],
                    "short_description": "Desc"
                }
            }
        }
        mock_http.get_with_metadata.return_value = (data, datetime.now(timezone.utc), 0)

        client = SteamStoreClient(mock_http)
        result = await client.get_app_details(123)

        assert "Title" in result.detailed_description
        assert "This is bold text & more" in result.detailed_description
        assert "<h1>" not in result.detailed_description
        assert "<b>" not in result.detailed_description
        assert "&amp;" not in result.detailed_description

    @pytest.mark.asyncio
    async def test_categories_and_genres_parsed(self):
        """Verify categories and genres lists are populated."""
        mock_http = AsyncMock()
        data = {
            "123": {
                "success": True,
                "data": {
                    "name": "Game",
                    "categories": [
                        {"description": "Single-player"},
                        {"description": "Multi-player"},
                        {"description": "Steam Achievements"}
                    ],
                    "genres": [
                        {"description": "Action"},
                        {"description": "Adventure"},
                        {"description": "Indie"}
                    ],
                    "developers": ["Dev"],
                    "short_description": "Desc"
                }
            }
        }
        mock_http.get_with_metadata.return_value = (data, datetime.now(timezone.utc), 0)

        client = SteamStoreClient(mock_http)
        result = await client.get_app_details(123)

        assert result.categories == ["Single-player", "Multi-player", "Steam Achievements"]
        assert result.genres == ["Action", "Adventure", "Indie"]


class TestBackwardCompatibility:
    """Test that old code continues to work with enriched schemas."""

    def test_search_result_old_format_still_works(self):
        """Verify SearchResult with only old fields uses defaults for new fields."""
        result = SearchResult(
            appids=[1, 2, 3],
            tag="RPG",
            total_found=3,
            fetched_at=datetime.now(timezone.utc)
            # No games, sort_by, result_size_warning, normalized_tag
        )

        assert result.appids == [1, 2, 3]
        assert result.tag == "RPG"
        assert result.total_found == 3
        # New fields should have defaults
        assert result.games == []
        assert result.sort_by == "owners"
        assert result.result_size_warning is None
        assert result.normalized_tag is None

    def test_game_metadata_old_format_still_works(self):
        """Verify GameMetadata with only old fields uses defaults for new fields."""
        meta = GameMetadata(
            appid=646570,
            name="Slay the Spire",
            tags=["Indie", "Strategy"],
            developer="Mega Crit",
            description="A deckbuilder roguelike",
            fetched_at=datetime.now(timezone.utc)
            # No new Phase 5 fields
        )

        assert meta.appid == 646570
        assert meta.name == "Slay the Spire"
        # All new fields should have safe defaults
        assert meta.price is None
        assert meta.is_free_to_play is False
        assert meta.platforms == {}
        assert meta.release_date is None
        assert meta.metacritic_score is None
        assert meta.screenshots == []
        assert meta.movies == []
        assert meta.dlc == []
        assert meta.publisher == ""
        assert meta.short_description == ""
        assert meta.detailed_description == ""

    @pytest.mark.asyncio
    async def test_old_steam_store_response_without_new_fields(self):
        """Verify old Steam Store API responses (missing new fields) still parse correctly."""
        mock_http = AsyncMock()
        old_format_data = {
            "646570": {
                "success": True,
                "data": {
                    "name": "Slay the Spire",
                    "genres": [{"description": "Indie"}],
                    "developers": ["Mega Crit"],
                    "short_description": "A deckbuilder roguelike"
                    # Missing: price_overview, platforms, release_date, metacritic, screenshots, movies, dlc, etc.
                }
            }
        }
        mock_http.get_with_metadata.return_value = (old_format_data, datetime.now(timezone.utc), 0)

        client = SteamStoreClient(mock_http)
        result = await client.get_app_details(646570)

        # Old fields should work
        assert result.name == "Slay the Spire"
        assert result.developer == "Mega Crit"
        assert result.description == "A deckbuilder roguelike"

        # New fields should have safe defaults
        assert result.price is None
        assert result.is_free_to_play is False
        assert result.platforms == {"windows": False, "mac": False, "linux": False}
        assert result.release_date is None
        assert result.metacritic_score is None
        assert result.screenshots == []
        assert result.movies == []
        assert result.dlc == []

    @pytest.mark.asyncio
    async def test_old_steamspy_response_without_enriched_fields(self):
        """Verify minimal SteamSpy responses work with enriched SearchResult."""
        mock_http = AsyncMock()
        minimal_data = {
            "646570": {"appid": 646570, "owners": "0 .. 20000"},
            "2379780": {"appid": 2379780, "owners": "0 .. 20000"}
            # Missing: name, developer, publisher, ccu, price, reviews, playtime, etc.
        }
        mock_http.get_with_metadata.return_value = (minimal_data, datetime.now(timezone.utc), 0)

        client = SteamSpyClient(mock_http)
        result = await client.search_by_tag("Test", limit=None)

        assert len(result.games) == 2
        # GameSummary objects should have defaults for missing fields
        game = result.games[0]
        assert game.appid in [646570, 2379780]
        assert game.name == ""
        assert game.developer == ""
        assert game.publisher == ""
        assert game.ccu == 0
        assert game.price == 0
        assert game.review_score is None
        assert game.positive == 0
        assert game.negative == 0
