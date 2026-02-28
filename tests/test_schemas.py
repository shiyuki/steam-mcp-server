"""Tests for Pydantic schema parsing."""

import pytest
from datetime import datetime, timezone
from pydantic import ValidationError
from src.schemas import (
    GameMetadata,
    SearchResult,
    APIError,
    GameSummary,
    CommercialData,
    GamalyticHistoryEntry,
    CompetitorEntry,
    VisualProfile,
)


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
            average_forever=25.5,
            median_forever=12.0
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
        assert summary.average_forever == 0
        assert summary.median_forever == 0

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


# ---------------------------------------------------------------------------
# Phase 13: Tests for extended Pro API schemas
# ---------------------------------------------------------------------------

_BASE_COMMERCIAL_KWARGS = dict(
    appid=646570,
    fetched_at=datetime.now(timezone.utc),
    revenue_min=90_000_000.0,
    revenue_max=100_000_000.0,
    confidence="high",
    source="gamalytic",
)

_SLAY_THE_SPIRE_PLAYTIME_DISTRIBUTION = {
    "0-1h": 7.7,
    "1-2h": 4.6,
    "2-5h": 9.1,
    "5-10h": 11.0,
    "10-20h": 13.1,
    "20-50h": 22.5,
    "50-100h": 14.0,
    "100-500h": 16.6,
    "500-1000h": 1.4,
}


class TestCommercialDataPhase13:
    """Phase 13: Test CommercialData extended Pro API fields."""

    def test_all_new_fields_populated(self):
        """CommercialData can be constructed with all Phase 13 Pro fields set."""
        cd = CommercialData(
            **_BASE_COMMERCIAL_KWARGS,
            wishlists=719_400,
            avg_playtime=64.076,
            playtime_distribution=_SLAY_THE_SPIRE_PLAYTIME_DISTRIBUTION,
            playtime_median=24.27,
            reviews_steam=179_415,
            ea_exit_date="2019-01-23",
            playtime_source=None,
            ownership_source=None,
        )
        assert cd.wishlists == 719_400
        assert cd.avg_playtime == pytest.approx(64.076)
        assert cd.playtime_distribution["0-1h"] == pytest.approx(7.7)
        assert cd.playtime_distribution["500-1000h"] == pytest.approx(1.4)
        assert len(cd.playtime_distribution) == 9
        assert cd.playtime_median == pytest.approx(24.27)
        assert cd.reviews_steam == 179_415
        assert cd.ea_exit_date == "2019-01-23"
        assert cd.playtime_source is None
        assert cd.ownership_source is None

    def test_backward_compat_all_new_fields_default_to_none(self):
        """CommercialData constructed without Phase 13 fields keeps None defaults."""
        cd = CommercialData(**_BASE_COMMERCIAL_KWARGS)
        assert cd.wishlists is None
        assert cd.avg_playtime is None
        assert cd.playtime_distribution == {}
        assert cd.playtime_median is None
        assert cd.reviews_steam is None
        assert cd.ea_exit_date is None
        assert cd.playtime_source is None
        assert cd.ownership_source is None

    def test_playtime_distribution_accepts_dynamic_bucket_labels(self):
        """playtime_distribution dict accepts any string bucket label (robustness)."""
        cd = CommercialData(
            **_BASE_COMMERCIAL_KWARGS,
            playtime_distribution={"0-2h": 15, "2-20h": 50, "20h+": 35},
        )
        assert cd.playtime_distribution["0-2h"] == pytest.approx(15.0)
        assert cd.playtime_distribution["20h+"] == pytest.approx(35.0)

    def test_playtime_source_steamspy_fallback(self):
        """playtime_source and ownership_source accept 'steamspy' string."""
        cd = CommercialData(
            **_BASE_COMMERCIAL_KWARGS,
            playtime_source="steamspy",
            ownership_source="steamspy",
        )
        assert cd.playtime_source == "steamspy"
        assert cd.ownership_source == "steamspy"

    def test_serialization_round_trip(self):
        """model_dump() -> CommercialData(**data) round-trip preserves all Phase 13 fields."""
        original = CommercialData(
            **_BASE_COMMERCIAL_KWARGS,
            wishlists=719_400,
            avg_playtime=64.076,
            playtime_distribution=_SLAY_THE_SPIRE_PLAYTIME_DISTRIBUTION,
            playtime_median=24.27,
            reviews_steam=179_415,
            ea_exit_date="2019-01-23",
        )
        dumped = original.model_dump()
        restored = CommercialData(**dumped)
        assert restored.wishlists == 719_400
        assert restored.avg_playtime == pytest.approx(64.076)
        assert restored.playtime_distribution == _SLAY_THE_SPIRE_PLAYTIME_DISTRIBUTION
        assert restored.playtime_median == pytest.approx(24.27)
        assert restored.reviews_steam == 179_415
        assert restored.ea_exit_date == "2019-01-23"


class TestGamalyticHistoryEntryPhase13:
    """Phase 13: Test GamalyticHistoryEntry wishlists field."""

    def test_wishlists_field_accepted(self):
        """GamalyticHistoryEntry accepts wishlists field (recent entries have it)."""
        entry = GamalyticHistoryEntry(
            entry_type="daily",
            timestamp="2024-08-01",
            reviews=179_415,
            wishlists=719_400,
        )
        assert entry.wishlists == 719_400

    def test_wishlists_defaults_to_none(self):
        """GamalyticHistoryEntry without wishlists defaults to None (older entries)."""
        entry = GamalyticHistoryEntry(entry_type="daily", timestamp="2018-01-01")
        assert entry.wishlists is None

    def test_round_trip_with_wishlists(self):
        """GamalyticHistoryEntry model_dump() round-trip preserves wishlists."""
        entry = GamalyticHistoryEntry(
            entry_type="daily",
            timestamp="2024-08-01",
            wishlists=100_000,
            avg_playtime=64.0,
            sales=9_786_278,
        )
        dumped = entry.model_dump()
        restored = GamalyticHistoryEntry(**dumped)
        assert restored.wishlists == 100_000
        assert restored.avg_playtime == pytest.approx(64.0)


class TestCompetitorEntryPhase13:
    """Phase 13: Test CompetitorEntry extended sub-fields."""

    def test_all_new_fields_populated(self):
        """CompetitorEntry accepts all Phase 13 sub-fields from Pro API."""
        entry = CompetitorEntry(
            appid=262060,
            name="Darkest Dungeon",
            overlap_pct=0.4161,
            revenue=52_644_037.0,
            release_date="2016-01-19",
            price=24.99,
            genres=["Indie", "RPG", "Strategy"],
            copies_sold=7_481_736,
        )
        assert entry.appid == 262060
        assert entry.name == "Darkest Dungeon"
        assert entry.release_date == "2016-01-19"
        assert entry.price == pytest.approx(24.99)
        assert entry.genres == ["Indie", "RPG", "Strategy"]
        assert entry.copies_sold == 7_481_736

    def test_backward_compat_new_fields_default_to_none(self):
        """CompetitorEntry constructed without Phase 13 fields keeps defaults."""
        entry = CompetitorEntry(appid=730, name="Counter-Strike 2", overlap_pct=0.796)
        assert entry.release_date is None
        assert entry.price is None
        assert entry.genres == []
        assert entry.copies_sold is None

    def test_price_accepts_int_zero_for_f2p(self):
        """CompetitorEntry price accepts int 0 for F2P games (API can return int)."""
        # alsoPlayed[].price can be int 0 for F2P games — must not crash
        entry = CompetitorEntry(
            appid=730,
            name="Counter-Strike 2",
            price=float(0),  # parser converts int->float before passing
            genres=["Action", "Free to Play"],
            copies_sold=338_531_651,
        )
        assert entry.price == pytest.approx(0.0)
        assert entry.genres == ["Action", "Free to Play"]

    def test_new_fields_serialization_round_trip(self):
        """CompetitorEntry model_dump() -> CompetitorEntry(**data) round-trip."""
        original = CompetitorEntry(
            appid=262060,
            name="Darkest Dungeon",
            overlap_pct=0.4161,
            revenue=52_644_037.0,
            release_date="2016-01-19",
            price=24.99,
            genres=["Indie", "RPG", "Strategy"],
            copies_sold=7_481_736,
        )
        dumped = original.model_dump()
        restored = CompetitorEntry(**dumped)
        assert restored.release_date == "2016-01-19"
        assert restored.price == pytest.approx(24.99)
        assert restored.genres == ["Indie", "RPG", "Strategy"]
        assert restored.copies_sold == 7_481_736


def _make_visual_profile(**overrides):
    """Factory for VisualProfile with valid defaults for all required fields."""
    defaults = dict(
        appid=1,
        character_style_primary='pixel',
        environment_style_primary='pixel',
        production_fidelity='low',
        mood_primary='dark',
        palette='dark',
        ui_density='minimal',
        perspective='top_down',
        header_composition='logo_abstract',
    )
    defaults.update(overrides)
    return VisualProfile(**defaults)


class TestVisualProfile:
    def test_valid_minimal_profile(self):
        p = _make_visual_profile()
        assert p.appid == 1
        assert p.character_style_primary == 'pixel'
        assert p.environment_style_primary == 'pixel'
        assert p.production_fidelity == 'low'
        assert p.mood_primary == 'dark'
        assert p.palette == 'dark'
        assert p.ui_density == 'minimal'
        assert p.perspective == 'top_down'
        assert p.header_composition == 'logo_abstract'

    def test_valid_full_profile(self):
        p = _make_visual_profile(
            appid=646570,
            name="Slay the Spire",
            classified_at="2026-02-28T00:00:00Z",
            character_style_primary='illustrated',
            character_style_secondary='painterly',
            environment_style_primary='illustrated',
            environment_style_secondary='painterly',
            production_fidelity='moderate',
            mood_primary='dark',
            mood_secondary='mysterious',
            palette='muted_warm',
            ui_density='dense',
            ui_density_confidence='observed',
            perspective='top_down',
            header_composition='character_portrait',
            genre_tags=['roguelike', 'deckbuilder'],
            notes='Card-art style; heavy UI from card hand and map',
        )
        assert p.name == "Slay the Spire"
        assert p.character_style_secondary == 'painterly'
        assert p.environment_style_secondary == 'painterly'
        assert p.mood_secondary == 'mysterious'
        assert p.ui_density_confidence == 'observed'
        assert p.genre_tags == ['roguelike', 'deckbuilder']
        assert 'Card-art' in p.notes

    def test_invalid_character_style_rejected(self):
        with pytest.raises(ValidationError):
            _make_visual_profile(character_style_primary='watercolor')

    def test_invalid_environment_style_rejected(self):
        with pytest.raises(ValidationError):
            _make_visual_profile(environment_style_primary='crayon')

    def test_invalid_production_fidelity_rejected(self):
        with pytest.raises(ValidationError):
            _make_visual_profile(production_fidelity='ultra')

    def test_invalid_mood_rejected(self):
        with pytest.raises(ValidationError):
            _make_visual_profile(mood_primary='happy')

    def test_invalid_palette_rejected(self):
        with pytest.raises(ValidationError):
            _make_visual_profile(palette='neon')

    def test_invalid_perspective_rejected(self):
        with pytest.raises(ValidationError):
            _make_visual_profile(perspective='birds_eye')

    def test_invalid_header_composition_rejected(self):
        with pytest.raises(ValidationError):
            _make_visual_profile(header_composition='collage')

    def test_invalid_ui_density_rejected(self):
        with pytest.raises(ValidationError):
            _make_visual_profile(ui_density='cluttered')

    def test_optional_secondary_fields_default_none(self):
        p = _make_visual_profile()
        assert p.character_style_secondary is None
        assert p.environment_style_secondary is None
        assert p.mood_secondary is None

    def test_genre_tags_default_empty_list(self):
        p = _make_visual_profile()
        assert p.genre_tags == []

    def test_ui_density_confidence_default_inferred(self):
        p = _make_visual_profile()
        assert p.ui_density_confidence == 'inferred'

    def test_model_dump_roundtrip(self):
        original = _make_visual_profile(
            appid=646570,
            name="Slay the Spire",
            character_style_secondary='painterly',
            mood_secondary='mysterious',
            genre_tags=['roguelike'],
        )
        dumped = original.model_dump()
        restored = VisualProfile(**dumped)
        assert restored.appid == original.appid
        assert restored.character_style_primary == original.character_style_primary
        assert restored.character_style_secondary == original.character_style_secondary
        assert restored.mood_secondary == original.mood_secondary
        assert restored.genre_tags == original.genre_tags
