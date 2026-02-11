"""Tests for Pydantic schema parsing."""

import pytest
from src.schemas import GameMetadata, SearchResult, APIError


class TestGameMetadata:
    def test_parses_complete_data(self):
        meta = GameMetadata(
            appid=646570,
            name="Slay the Spire",
            tags=["Indie", "Strategy"],
            developer="Mega Crit",
            description="A deckbuilder roguelike",
        )
        assert meta.appid == 646570
        assert meta.name == "Slay the Spire"
        assert meta.tags == ["Indie", "Strategy"]
        assert meta.developer == "Mega Crit"

    def test_defaults_for_optional_fields(self):
        meta = GameMetadata(appid=1, name="Test")
        assert meta.tags == []
        assert meta.developer == ""
        assert meta.description == ""

    def test_ignores_extra_fields(self):
        meta = GameMetadata(
            appid=1,
            name="Test",
            unknown_field="should be ignored",
            another_extra=123,
        )
        assert meta.appid == 1
        assert not hasattr(meta, "unknown_field")


class TestSearchResult:
    def test_parses_search_result(self):
        result = SearchResult(appids=[1, 2, 3], tag="Roguelike", total_found=100)
        assert result.appids == [1, 2, 3]
        assert result.tag == "Roguelike"
        assert result.total_found == 100

    def test_empty_appids(self):
        result = SearchResult(appids=[], tag="Nonexistent", total_found=0)
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
