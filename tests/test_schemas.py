"""Tests for Pydantic schema parsing."""

import pytest
from datetime import datetime, timezone
from src.schemas import GameMetadata, SearchResult, APIError, GameSummary


class TestGameMetadata:
    def test_parses_complete_data(self):
        meta = GameMetadata(
            appid=646570,
            name="Slay the Spire",
            tags=["Indie", "Strategy"],
            developer="Mega Crit",
            description="A deckbuilder roguelike",
            fetched_at=datetime.now(timezone.utc)
        )
        assert meta.appid == 646570
        assert meta.name == "Slay the Spire"
        assert meta.tags == ["Indie", "Strategy"]
        assert meta.developer == "Mega Crit"

    def test_defaults_for_optional_fields(self):
        meta = GameMetadata(appid=1, name="Test", fetched_at=datetime.now(timezone.utc))
        assert meta.tags == []
        assert meta.developer == ""
        assert meta.description == ""

    def test_ignores_extra_fields(self):
        meta = GameMetadata(
            appid=1,
            name="Test",
            unknown_field="should be ignored",
            another_extra=123,
            fetched_at=datetime.now(timezone.utc)
        )
        assert meta.appid == 1
        assert not hasattr(meta, "unknown_field")


class TestSearchResult:
    def test_parses_search_result(self):
        result = SearchResult(appids=[1, 2, 3], tag="Roguelike", total_found=100, fetched_at=datetime.now(timezone.utc))
        assert result.appids == [1, 2, 3]
        assert result.tag == "Roguelike"
        assert result.total_found == 100

    def test_empty_appids(self):
        result = SearchResult(appids=[], tag="Nonexistent", total_found=0, fetched_at=datetime.now(timezone.utc))
        assert result.appids == []
        assert result.total_found == 0


class TestAPIError:
    def test_parses_error(self):
        err = APIError(error_code=404, error_type="not_found", message="Not found")
        assert err.error_code == 404
        assert err.error_type == "not_found"

    def test_model_dump(self):
        err = APIError(error_code=429, error_type="rate_limit", message="Too fast")
        d = err.model_dump()
        assert d == {
            "error_code": 429,
            "error_type": "rate_limit",
            "message": "Too fast",
        }


class TestGameSummary:
    """Phase 5: Test GameSummary model for enriched search results."""
    def test_parses_complete_data(self):
        summary = GameSummary(
            appid=646570,
            name="Slay the Spire",
            developer="Mega Crit",
            publisher="Humble Bundle",
            owners_midpoint=5000000,
            ccu=5000,
            price=24.99,
            review_score=95.5,
            positive=50000,
            negative=2000,
            average_playtime=25.5,
            median_playtime=12.0
        )
        assert summary.appid == 646570
        assert summary.name == "Slay the Spire"
        assert summary.owners_midpoint == 5000000
        assert summary.ccu == 5000
        assert summary.price == 24.99
        assert summary.review_score == 95.5

    def test_defaults_for_optional_fields(self):
        summary = GameSummary(appid=123)
        assert summary.name == ""
        assert summary.developer == ""
        assert summary.publisher == ""
        assert summary.owners_midpoint == 0
        assert summary.ccu == 0
        assert summary.price == 0
        assert summary.review_score is None
        assert summary.positive == 0
        assert summary.negative == 0
        assert summary.average_playtime == 0
        assert summary.median_playtime == 0

    def test_ignores_extra_fields(self):
        summary = GameSummary(
            appid=1,
            unknown_field="should be ignored",
            another_extra=123
        )
        assert summary.appid == 1
        assert not hasattr(summary, "unknown_field")


class TestEnrichedGameMetadata:
    """Phase 5: Test enriched GameMetadata with new Steam Store fields."""
    def test_new_fields_present(self):
        meta = GameMetadata(
            appid=646570,
            name="Slay the Spire",
            fetched_at=datetime.now(timezone.utc),
            price=24.99,
            is_free_to_play=False,
            platforms={"windows": True, "mac": True, "linux": False},
            release_date="2019-01-23",
            release_date_raw="23 Jan, 2019",
            metacritic_score=89,
            screenshots=["https://example.com/screenshot1.jpg"],
            movies=[{"name": "Trailer", "thumbnail": "thumb.jpg"}],
            dlc=[1234567, 7654321],
            publisher="Humble Bundle"
        )
        assert meta.price == 24.99
        assert meta.is_free_to_play is False
        assert meta.platforms == {"windows": True, "mac": True, "linux": False}
        assert meta.release_date == "2019-01-23"
        assert meta.metacritic_score == 89
        assert len(meta.screenshots) == 1
        assert len(meta.movies) == 1
        assert len(meta.dlc) == 2
        assert meta.publisher == "Humble Bundle"

    def test_new_fields_have_defaults(self):
        meta = GameMetadata(appid=1, name="Test", fetched_at=datetime.now(timezone.utc))
        assert meta.price is None
        assert meta.is_free_to_play is False
        assert meta.platforms == {}
        assert meta.release_date is None
        assert meta.release_date_raw == ""
        assert meta.metacritic_score is None
        assert meta.metacritic_url == ""
        assert meta.screenshots == []
        assert meta.screenshots_count == 0
        assert meta.movies == []
        assert meta.movies_count == 0
        assert meta.dlc == []
        assert meta.dlc_count == 0
        assert meta.publisher == ""
        assert meta.short_description == ""
        assert meta.detailed_description == ""
