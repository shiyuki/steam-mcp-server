"""Tests for src/analytics.py — pure sync, no HTTP mocking needed."""
import pytest
from datetime import date
from src.analytics import (
    # Helpers
    _deduplicate, _coverage, _gini_coefficient, _assign_price_bracket,
    _assign_score_range, _assign_revenue_tier, _extract_year_quarter,
    _extract_month, _is_partial_period, _revenue_velocity,
    _classify_publisher, _get_top_tags, _tag_confidence,
    _safe_mean, _safe_median, get_computable_metrics, _build_methodology,
    # Compute functions
    compute_concentration, compute_temporal_trends, compute_price_brackets,
    compute_success_rates, compute_tag_multipliers, compute_score_revenue,
    compute_publisher_analysis, compute_release_timing, compute_sub_genres,
    compute_competitive_density,
    # Constants
    PRICE_BRACKETS, SCORE_RANGES, REVENUE_TIERS, STANDARD_BIASES,
    KNOWN_PUBLISHERS,
)


# ---------------------------------------------------------------------------
# Test fixtures (module-level factory functions)
# ---------------------------------------------------------------------------

def make_games(n=50, **overrides):
    """
    Factory generating n synthetic games with all required fields.
    Revenue decreases from top to bottom (game 0 = highest).
    Mix of prices across all brackets. Mix of scores.
    Release dates spanning 2015-2025. Mix of self-pub and third-party.
    Tags dict with 8 tags. Allow keyword overrides for any field.
    """
    games = []
    tags_pool = [
        {"Action": 100, "Adventure": 80, "RPG": 60, "Indie": 40, "Puzzle": 30, "Strategy": 20, "Horror": 10, "Sci-fi": 5},
        {"Roguelike": 120, "Action": 90, "Platformer": 70, "Indie": 50, "Pixel Art": 25, "Difficult": 15, "Retro": 8, "Comedy": 4},
        {"Strategy": 110, "Simulation": 95, "Management": 65, "Indie": 55, "Relaxing": 35, "Casual": 18, "Economy": 12, "City Builder": 6},
        {"Horror": 130, "Survival": 85, "Atmospheric": 75, "Indie": 45, "Dark": 28, "Mystery": 16, "Thriller": 9, "Psychological": 3},
    ]
    price_cycle = [0.0, 2.99, 7.99, 12.99, 17.99, 24.99, 49.99]
    year_range = list(range(2015, 2026))
    month_range = list(range(1, 13))

    for i in range(n):
        # Revenue decreases from top to bottom
        revenue = max(0.0, (n - i) * 50_000.0)
        price = price_cycle[i % len(price_cycle)]
        score = 70 + (i % 30)  # Scores 70-99
        year = year_range[i % len(year_range)]
        month = month_range[i % len(month_range)]
        day = min(28, (i % 27) + 1)
        release_date = f"{year}-{month:02d}-{day:02d}"
        tags = tags_pool[i % len(tags_pool)]
        # Alternate self-pub and third-party
        if i % 3 == 0:
            developer = f"Studio{i}"
            publisher = f"Studio{i}"  # self-published
        elif i % 3 == 1:
            developer = f"Studio{i}"
            publisher = "Team17"  # known publisher
        else:
            developer = f"Studio{i}"
            publisher = f"Publisher{i}"  # unknown third party

        game = {
            "appid": i + 1,
            "name": f"Game {i}",
            "developer": developer,
            "publisher": publisher,
            "price": price,
            "revenue": revenue,
            "review_score": float(score),
            "release_date": release_date,
            "tags": tags,
        }
        game.update(overrides)
        games.append(game)
    return games


def make_games_with_gaps(n=50):
    """
    Similar to make_games but ~20% of games have revenue=None,
    ~10% have review_score=None, ~5% have release_date=None.
    Tests coverage/missing data handling.
    """
    games = make_games(n)
    for i, g in enumerate(games):
        if i % 5 == 0:
            g["revenue"] = None
        if i % 10 == 0:
            g["review_score"] = None
        if i % 20 == 0:
            g["release_date"] = None
    return games


def make_minimal_game(**fields):
    """
    Creates a single game dict with only the provided fields.
    For testing graceful handling of missing fields.
    """
    return dict(**fields)


# ---------------------------------------------------------------------------
# Task 1: Helper function tests
# ---------------------------------------------------------------------------


class TestDeduplicate:
    def test_removes_duplicates(self):
        games = [
            {"appid": 1, "name": "Game A"},
            {"appid": 1, "name": "Game A Duplicate"},
            {"appid": 2, "name": "Game B"},
        ]
        result = _deduplicate(games)
        assert len(result) == 2
        assert result[0]["name"] == "Game A"

    def test_preserves_order(self):
        games = [
            {"appid": 3, "name": "C"},
            {"appid": 1, "name": "A"},
            {"appid": 2, "name": "B"},
        ]
        result = _deduplicate(games)
        assert [g["appid"] for g in result] == [3, 1, 2]

    def test_none_appid_skipped(self):
        games = [
            {"appid": None, "name": "No ID"},
            {"appid": 1, "name": "Has ID"},
        ]
        result = _deduplicate(games)
        assert len(result) == 1
        assert result[0]["appid"] == 1

    def test_empty_input(self):
        result = _deduplicate([])
        assert result == []


class TestCoverage:
    def test_full_coverage(self):
        games = [{"revenue": 100}, {"revenue": 200}, {"revenue": 300}]
        assert _coverage(games, "revenue") == 100.0

    def test_partial_coverage(self):
        games = [{"revenue": 100}, {"revenue": None}, {"revenue": 300}, {"revenue": None}]
        result = _coverage(games, "revenue")
        assert result == 50.0

    def test_no_coverage(self):
        games = [{"revenue": None}, {"revenue": None}]
        assert _coverage(games, "revenue") == 0.0

    def test_empty_games(self):
        assert _coverage([], "revenue") == 0.0


class TestGiniCoefficient:
    def test_perfect_equality(self):
        result = _gini_coefficient([100.0, 100.0, 100.0])
        assert result == pytest.approx(0.0, abs=0.01)

    def test_high_inequality(self):
        # [0,0,0,0,100]: top 1 has all revenue
        result = _gini_coefficient([0.0, 0.0, 0.0, 0.0, 100.0])
        assert result == pytest.approx(0.8, abs=0.01)

    def test_empty_returns_none(self):
        assert _gini_coefficient([]) is None

    def test_all_zeros(self):
        result = _gini_coefficient([0.0, 0.0, 0.0])
        assert result == 0.0

    def test_single_value(self):
        result = _gini_coefficient([42.0])
        assert result == 0.0

    def test_range_0_to_1(self):
        values = [10.0, 50.0, 200.0, 1000.0, 50000.0]
        result = _gini_coefficient(values)
        assert result is not None
        assert 0.0 <= result <= 1.0


class TestPriceBracketAssignment:
    def test_f2p(self):
        assert _assign_price_bracket(0.0) == "f2p"

    def test_bracket_0_01_4_99(self):
        assert _assign_price_bracket(2.99) == "0.01_4.99"

    def test_bracket_5_9_99(self):
        assert _assign_price_bracket(7.99) == "5_9.99"

    def test_bracket_10_14_99(self):
        assert _assign_price_bracket(12.99) == "10_14.99"

    def test_bracket_15_19_99(self):
        assert _assign_price_bracket(17.99) == "15_19.99"

    def test_bracket_20_29_99(self):
        assert _assign_price_bracket(24.99) == "20_29.99"

    def test_bracket_30_plus(self):
        assert _assign_price_bracket(49.99) == "30_plus"

    def test_boundary_5_0_inclusive(self):
        assert _assign_price_bracket(5.0) == "5_9.99"

    def test_boundary_4_99(self):
        assert _assign_price_bracket(4.99) == "0.01_4.99"

    def test_none_returns_none(self):
        assert _assign_price_bracket(None) is None


class TestScoreRangeAssignment:
    def test_below_70(self):
        assert _assign_score_range(65.0) == "below_70"

    def test_boundary_at_70(self):
        assert _assign_score_range(70.0) == "70_74"

    def test_range_75_79(self):
        assert _assign_score_range(77.0) == "75_79"

    def test_range_80_84(self):
        assert _assign_score_range(82.0) == "80_84"

    def test_range_85_89(self):
        assert _assign_score_range(87.0) == "85_89"

    def test_range_90_94(self):
        assert _assign_score_range(92.0) == "90_94"

    def test_100_included(self):
        assert _assign_score_range(100.0) == "95_100"

    def test_none_returns_none(self):
        assert _assign_score_range(None) is None


class TestPublisherClassification:
    def test_self_published(self):
        assert _classify_publisher("Indie Dev", "Indie Dev") == "self_published"

    def test_known_publisher(self):
        # Known publisher in KNOWN_PUBLISHERS but dev != pub -> third_party
        assert _classify_publisher("Small Dev", "Team17") == "third_party"

    def test_unknown_publisher(self):
        # Different dev/pub, publisher not in known list
        assert _classify_publisher("Dev Studio", "Unknown Publisher Inc") == "third_party"

    def test_empty_strings(self):
        assert _classify_publisher("", "") == "unknown"

    def test_empty_developer(self):
        assert _classify_publisher("", "Publisher") == "unknown"

    def test_empty_publisher(self):
        assert _classify_publisher("Developer", "") == "unknown"


class TestTagHelpers:
    def test_get_top_tags_dict(self):
        tags = {"RPG": 100, "Action": 200, "Indie": 50, "Strategy": 150}
        result = _get_top_tags(tags, top_n=2)
        assert result == ["Action", "Strategy"]

    def test_get_top_tags_list(self):
        tags = ["Action", "RPG", "Strategy", "Indie"]
        result = _get_top_tags(tags, top_n=2)
        assert result == ["Action", "RPG"]

    def test_get_top_tags_empty(self):
        assert _get_top_tags({}, top_n=5) == []
        assert _get_top_tags([], top_n=5) == []

    def test_tag_confidence_high(self):
        tags = {"Action": 300, "RPG": 200}  # total 500
        assert _tag_confidence(tags) == "high"

    def test_tag_confidence_medium(self):
        tags = {"Action": 60, "RPG": 40}  # total 100
        assert _tag_confidence(tags) == "medium"

    def test_tag_confidence_low(self):
        tags = {"Action": 30, "RPG": 20}  # total 50 < 100
        assert _tag_confidence(tags) == "low"

    def test_tag_confidence_list_always_low(self):
        # List format has no vote counts
        tags = ["Action", "RPG", "Strategy"]
        assert _tag_confidence(tags) == "low"


class TestTemporalHelpers:
    def test_extract_year_quarter(self):
        assert _extract_year_quarter("2023-06-15") == (2023, 2)

    def test_extract_year_quarter_q1(self):
        assert _extract_year_quarter("2023-01-15") == (2023, 1)

    def test_extract_year_quarter_q3(self):
        assert _extract_year_quarter("2023-08-15") == (2023, 3)

    def test_extract_year_quarter_q4(self):
        assert _extract_year_quarter("2023-11-15") == (2023, 4)

    def test_extract_year_quarter_none(self):
        assert _extract_year_quarter(None) is None

    def test_extract_year_quarter_invalid(self):
        assert _extract_year_quarter("not-a-date") is None

    def test_extract_month(self):
        assert _extract_month("2023-06-15") == 6

    def test_extract_month_none(self):
        assert _extract_month(None) is None

    def test_is_partial_current_year(self):
        today = date(2026, 2, 21)
        assert _is_partial_period(2026, quarter=None, today=today) is True

    def test_is_partial_past_year(self):
        today = date(2026, 2, 21)
        assert _is_partial_period(2024, quarter=None, today=today) is False

    def test_is_partial_future_year(self):
        today = date(2026, 2, 21)
        assert _is_partial_period(2027, quarter=None, today=today) is True

    def test_is_partial_current_quarter(self):
        # Q1 of 2026 is current quarter on 2026-02-21
        today = date(2026, 2, 21)
        assert _is_partial_period(2026, quarter=1, today=today) is True

    def test_is_partial_past_quarter(self):
        today = date(2026, 2, 21)
        assert _is_partial_period(2025, quarter=4, today=today) is False


class TestRevenueVelocity:
    def test_normal_case(self):
        # 1200 revenue / 12 months ~ 100/month
        result = _revenue_velocity(1200.0, "2025-02-21", today=date(2026, 2, 21))
        assert result is not None
        assert result == pytest.approx(99.97, abs=1.0)

    def test_minimum_one_month(self):
        # Very recent release (same day) uses minimum 1 month
        result = _revenue_velocity(500.0, "2026-02-21", today=date(2026, 2, 21))
        assert result is not None
        assert result == pytest.approx(500.0, abs=1.0)

    def test_none_revenue(self):
        result = _revenue_velocity(None, "2023-01-01")
        assert result is None

    def test_none_date(self):
        result = _revenue_velocity(1000.0, None)
        assert result is None

    def test_invalid_date(self):
        result = _revenue_velocity(1000.0, "not-a-date")
        assert result is None


class TestSmartMetricSelection:
    def test_below_20(self):
        available = get_computable_metrics(10)
        assert available == {"descriptive_stats", "tag_frequency"}

    def test_at_20(self):
        available = get_computable_metrics(20)
        assert "concentration" in available
        assert "success_rates" in available
        assert "price_brackets" in available
        assert "publisher_analysis" in available
        assert "temporal_trends" in available
        assert "tag_multipliers" in available
        assert "score_revenue" in available
        assert "competitive_density" in available
        assert "release_timing" in available
        assert "sub_genres" in available
        assert len(available) == 12

    def test_at_50(self):
        # Single tier: N>=50 returns the same 12 metrics as N>=20
        assert get_computable_metrics(50) == get_computable_metrics(20)

    def test_at_25_unlocks_all(self):
        """Any N >= 20 unlocks all 10 analytics metrics (plus 2 always-on)."""
        available = get_computable_metrics(25)
        assert len(available) == 12
        assert "temporal_trends" in available
        assert "tag_multipliers" in available
        assert "score_revenue" in available

    def test_requested_filter(self):
        requested = ["concentration", "tag_frequency", "nonexistent_metric"]
        available = get_computable_metrics(50, requested=requested)
        assert "concentration" in available
        assert "tag_frequency" in available
        assert "nonexistent_metric" not in available

    def test_requested_filter_limits_below_threshold(self):
        # N=10 only has descriptive_stats and tag_frequency
        requested = ["concentration", "tag_frequency"]
        available = get_computable_metrics(10, requested=requested)
        assert available == {"tag_frequency"}


# ---------------------------------------------------------------------------
# Task 2: Compute function tests
# ---------------------------------------------------------------------------


class TestComputeConcentration:
    def test_returns_result_with_valid_data(self):
        games = make_games(100)
        result = compute_concentration(games)
        assert result is not None
        assert isinstance(result.gini, float)
        assert 0.0 <= result.gini <= 1.0
        assert len(result.top_shares) == 4
        assert all(k in result.top_shares for k in ("top_10", "top_25", "top_50", "top_100"))
        assert len(result.revenue_tiers) == 7

    def test_top10_share_dominates(self):
        # Strongly skewed distribution: top 10 have massive revenue, rest tiny
        top_games = [{"appid": i, "revenue": 1_000_000.0} for i in range(10)]
        rest_games = [{"appid": i + 10, "revenue": 1_000.0} for i in range(90)]
        games = top_games + rest_games
        result = compute_concentration(games)
        assert result is not None
        # Top 10 have 1_000_000 * 10 = 10M out of 10M + 90K total -> ~99%
        assert result.top_shares["top_10"].share_pct > 90.0

    def test_empty_returns_none(self):
        assert compute_concentration([]) is None

    def test_all_none_revenue_returns_none(self):
        games = [{"appid": i, "revenue": None} for i in range(5)]
        assert compute_concentration(games) is None

    def test_gini_increases_with_inequality(self):
        equal_games = [{"appid": i, "revenue": 100.0} for i in range(20)]
        skewed_games = [{"appid": i, "revenue": 1.0 if i < 19 else 10_000.0} for i in range(20)]
        equal_result = compute_concentration(equal_games)
        skewed_result = compute_concentration(skewed_games)
        assert equal_result is not None
        assert skewed_result is not None
        assert equal_result.gini < skewed_result.gini

    def test_revenue_tiers_count_correctly(self):
        # Create games with specific revenue values mapping to each tier:
        # failed: [0, 1_000), minimal: [1_000, 10_000), small: [10_000, 100_000)
        # moderate: [100_000, 1_000_000), breakout: [5_000_000, 10_000_000)
        games = [
            {"appid": 1, "revenue": 500.0},        # failed (< 1000)
            {"appid": 2, "revenue": 5_000.0},       # minimal (1000-9999)
            {"appid": 3, "revenue": 50_000.0},      # small (10k-100k)
            {"appid": 4, "revenue": 500_000.0},     # moderate (100k-1m)
            {"appid": 5, "revenue": 7_000_000.0},   # breakout (5m-10m)
        ]
        result = compute_concentration(games)
        assert result is not None
        tier_map = {t.label: t.count for t in result.revenue_tiers}
        assert tier_map["failed"] == 1
        assert tier_map["minimal"] == 1
        assert tier_map["small"] == 1
        assert tier_map["moderate"] == 1
        assert tier_map["breakout"] == 1

    def test_confidence_varies_by_sample_size(self):
        small_games = [{"appid": i, "revenue": 100.0} for i in range(10)]
        medium_games = [{"appid": i, "revenue": 100.0} for i in range(50)]
        large_games = [{"appid": i, "revenue": 100.0} for i in range(200)]
        assert compute_concentration(small_games).meta.confidence == "low"
        assert compute_concentration(medium_games).meta.confidence == "medium"
        assert compute_concentration(large_games).meta.confidence == "high"


class TestComputeTemporalTrends:
    def test_returns_yearly_and_quarterly(self):
        games = make_games(50)
        result = compute_temporal_trends(games)
        assert result is not None
        assert len(result.yearly) > 0
        assert len(result.quarterly) > 0

    def test_partial_flag_set(self):
        # Current year (2026) should have partial=True
        games = make_games(50)
        # Add a game from 2026
        games.append({
            "appid": 9999,
            "name": "Current Year Game",
            "revenue": 100_000.0,
            "release_date": "2026-01-15",
            "review_score": 80.0,
        })
        result = compute_temporal_trends(games)
        assert result is not None
        current_year_periods = [p for p in result.yearly if p.year == 2026]
        if current_year_periods:
            assert current_year_periods[0].partial is True

    def test_empty_returns_none(self):
        assert compute_temporal_trends([]) is None

    def test_games_without_dates_skipped(self):
        games = [
            {"appid": 1, "release_date": None, "revenue": 100.0},
            {"appid": 2, "release_date": "invalid", "revenue": 200.0},
            {"appid": 3, "release_date": "2020-06-15", "revenue": 300.0},
        ]
        result = compute_temporal_trends(games)
        assert result is not None
        # Only game 3 has valid date
        assert sum(p.game_count for p in result.yearly) == 1

    def test_yearly_sorted_ascending(self):
        games = make_games(50)
        result = compute_temporal_trends(games)
        assert result is not None
        years = [p.year for p in result.yearly]
        assert years == sorted(years)

    def test_quarterly_has_events(self):
        # Add games in Q2 months (Apr-Jun) — June triggers next_fest + summer_sale
        games = [
            {"appid": i, "release_date": f"2022-0{m}-15", "revenue": 100.0, "review_score": 80.0}
            for i, m in enumerate([4, 5, 6], start=1)
        ]
        result = compute_temporal_trends(games)
        assert result is not None
        q2_periods = [p for p in result.quarterly if p.quarter == 2]
        if q2_periods:
            # Q2 should have events from June (next_fest, summer_sale)
            assert len(q2_periods[0].events) > 0


class TestComputePriceBrackets:
    def test_all_7_brackets_present(self):
        games = make_games(50)
        result = compute_price_brackets(games)
        assert result is not None
        assert len(result.brackets) == 7

    def test_empty_brackets_have_no_data(self):
        # Only include $5-9.99 games
        games = [{"appid": i, "price": 7.99, "revenue": 100_000.0} for i in range(5)]
        result = compute_price_brackets(games)
        assert result is not None
        no_data_brackets = [b for b in result.brackets if b.no_data]
        data_brackets = [b for b in result.brackets if not b.no_data]
        assert len(data_brackets) == 1
        assert data_brackets[0].label == "5_9.99"
        assert len(no_data_brackets) == 6

    def test_f2p_bracket(self):
        games = [{"appid": i, "price": 0.0, "revenue": 50_000.0} for i in range(3)]
        result = compute_price_brackets(games)
        assert result is not None
        f2p = next(b for b in result.brackets if b.label == "f2p")
        assert f2p.count == 3
        assert not f2p.no_data

    def test_success_rate_calculated(self):
        # 2 games with revenue >= 100K, 2 with less
        games = [
            {"appid": 1, "price": 9.99, "revenue": 200_000.0},
            {"appid": 2, "price": 9.99, "revenue": 500_000.0},
            {"appid": 3, "price": 9.99, "revenue": 50_000.0},
            {"appid": 4, "price": 9.99, "revenue": 10_000.0},
        ]
        result = compute_price_brackets(games)
        assert result is not None
        bracket_5_10 = next(b for b in result.brackets if b.label == "5_9.99")
        assert bracket_5_10.success_rate == 50.0

    def test_empty_returns_none(self):
        assert compute_price_brackets([]) is None

    def test_none_price_games_skipped(self):
        games = [{"appid": i, "price": None, "revenue": 100.0} for i in range(5)]
        assert compute_price_brackets(games) is None


class TestComputeSuccessRates:
    def test_default_thresholds(self):
        games = make_games(50)
        result = compute_success_rates(games)
        assert result is not None
        assert "100k" in result.thresholds
        assert "1m" in result.thresholds
        assert "10m" in result.thresholds

    def test_custom_thresholds(self):
        games = make_games(50)
        result = compute_success_rates(games, thresholds=[500_000, 5_000_000])
        assert result is not None
        assert "500k" in result.thresholds
        assert "5m" in result.thresholds
        assert "100k" not in result.thresholds

    def test_rates_are_percentages(self):
        games = make_games(50)
        result = compute_success_rates(games)
        assert result is not None
        for label, rate in result.thresholds.items():
            assert 0.0 <= rate <= 100.0

    def test_empty_returns_none(self):
        assert compute_success_rates([]) is None

    def test_all_above_threshold(self):
        # All games have revenue >= 100K
        games = [{"appid": i, "revenue": 200_000.0} for i in range(20)]
        result = compute_success_rates(games, thresholds=[100_000])
        assert result is not None
        assert result.thresholds["100k"] == 100.0

    def test_none_below_threshold(self):
        # No games have revenue >= threshold
        games = [{"appid": i, "revenue": 1_000.0} for i in range(20)]
        result = compute_success_rates(games, thresholds=[100_000])
        assert result is not None
        assert result.thresholds["100k"] == 0.0

    def test_all_none_revenue_returns_none(self):
        games = [{"appid": i, "revenue": None} for i in range(10)]
        assert compute_success_rates(games) is None


class TestComputeTagMultipliers:
    def test_returns_boosters_and_penalties(self):
        games = make_games(100)
        result = compute_tag_multipliers(games)
        assert result is not None
        assert isinstance(result.boosters, list)
        assert isinstance(result.penalties, list)

    def test_primary_tags_excluded(self):
        games = make_games(100)
        primary = {"Action"}
        result = compute_tag_multipliers(games, primary_tags=primary)
        assert result is not None
        all_tags = {e.tag for e in result.boosters} | {e.tag for e in result.penalties}
        assert "Action" not in all_tags

    def test_dynamic_min_games(self):
        games = make_games(100)
        result = compute_tag_multipliers(games)
        assert result is not None
        # min_games = max(5, games_with_tags // 20) — all make_games() have tags
        n_with_tags = sum(1 for g in games if g.get("tags") and g.get("revenue") is not None)
        expected_min = max(5, n_with_tags // 20)
        assert result.min_games_threshold == expected_min

    def test_min_games_based_on_tagged_games_not_total(self):
        """Threshold uses count of games-with-tag-data, not all revenue games."""
        # 20 games with tags, 80 without — threshold should be max(5, 20//20) = 5, not max(5, 100//20) = 5
        # Use larger numbers to make the difference visible:
        # 40 tagged + 460 untagged = 500 total revenue games
        # Old: max(5, 500//20) = 25
        # New: max(5, 40//20) = 5
        tagged_games = [
            {"appid": i, "revenue": 100_000.0, "tags": {"Action": 100, "Indie": 50}}
            for i in range(40)
        ]
        untagged_games = [
            {"appid": i + 40, "revenue": 50_000.0, "tags": {}}
            for i in range(460)
        ]
        result = compute_tag_multipliers(tagged_games + untagged_games)
        assert result is not None
        # Threshold based on 40 tagged games: max(5, 40//20) = 5
        assert result.min_games_threshold == max(5, 40 // 20)
        # "Action" appears in all 40 tagged games, should pass threshold
        booster_tags = {e.tag for e in result.boosters}
        penalty_tags = {e.tag for e in result.penalties}
        all_tags = booster_tags | penalty_tags
        assert "Action" in all_tags

    def test_empty_returns_none(self):
        assert compute_tag_multipliers([]) is None

    def test_genre_avg_reported(self):
        games = make_games(50)
        result = compute_tag_multipliers(games)
        assert result is not None
        assert result.genre_avg_revenue is not None
        assert result.genre_avg_revenue > 0

    def test_all_none_revenue_returns_none(self):
        games = [{"appid": i, "revenue": None, "tags": {"Action": 100}} for i in range(20)]
        assert compute_tag_multipliers(games) is None


class TestComputeScoreRevenue:
    def test_all_7_ranges_present(self):
        games = make_games(50)
        result = compute_score_revenue(games)
        assert result is not None
        assert len(result.buckets) == 7
        labels = [b.label for b in result.buckets]
        expected = ["below_70", "70_74", "75_79", "80_84", "85_89", "90_94", "95_100"]
        assert labels == expected

    def test_correlation_computed(self):
        games = make_games(50)
        result = compute_score_revenue(games)
        assert result is not None
        # correlation should be a float
        assert isinstance(result.correlation, float)

    def test_empty_returns_none(self):
        assert compute_score_revenue([]) is None

    def test_games_without_score_skipped(self):
        games = [
            {"appid": 1, "review_score": None, "revenue": 100_000.0},
            {"appid": 2, "review_score": 80.0, "revenue": 200_000.0},
        ]
        result = compute_score_revenue(games)
        assert result is not None
        assert result.meta.sample_size == 1

    def test_games_without_revenue_skipped(self):
        games = [
            {"appid": 1, "review_score": 80.0, "revenue": None},
            {"appid": 2, "review_score": 75.0, "revenue": 150_000.0},
        ]
        result = compute_score_revenue(games)
        assert result is not None
        assert result.meta.sample_size == 1

    def test_correlation_none_for_single_point(self):
        games = [{"appid": 1, "review_score": 85.0, "revenue": 100_000.0}]
        result = compute_score_revenue(games)
        assert result is not None
        # Correlation requires at least 2 data points
        assert result.correlation is None


class TestComputePublisherAnalysis:
    def test_three_groups_present(self):
        games = make_games(50)
        result = compute_publisher_analysis(games)
        assert result is not None
        labels = [g.label for g in result.groups]
        assert "self_published" in labels
        assert "third_party" in labels
        assert "unknown" in labels

    def test_self_published_detected(self):
        games = [
            {"appid": 1, "developer": "Studio A", "publisher": "Studio A", "revenue": 100_000.0},
            {"appid": 2, "developer": "Studio B", "publisher": "Studio B", "revenue": 200_000.0},
        ]
        result = compute_publisher_analysis(games)
        assert result is not None
        self_pub = next(g for g in result.groups if g.label == "self_published")
        assert self_pub.count == 2

    def test_known_publisher_detected(self):
        games = [
            {"appid": 1, "developer": "Indie Dev", "publisher": "Team17", "revenue": 500_000.0},
        ]
        result = compute_publisher_analysis(games)
        assert result is not None
        third_party = next(g for g in result.groups if g.label == "third_party")
        assert third_party.count == 1

    def test_empty_returns_none(self):
        assert compute_publisher_analysis([]) is None

    def test_missing_developer_publisher_goes_unknown(self):
        games = [{"appid": 1, "revenue": 100_000.0}]  # no developer/publisher keys
        result = compute_publisher_analysis(games)
        assert result is not None
        unknown = next(g for g in result.groups if g.label == "unknown")
        assert unknown.count == 1


class TestComputeReleaseTiming:
    def test_4_quarters_present(self):
        games = make_games(50)
        result = compute_release_timing(games)
        assert result is not None
        assert len(result.quarterly) == 4
        labels = [q.label for q in result.quarterly]
        assert labels == ["Q1", "Q2", "Q3", "Q4"]

    def test_12_months_present(self):
        games = make_games(50)
        result = compute_release_timing(games)
        assert result is not None
        assert len(result.monthly) == 12

    def test_empty_returns_none(self):
        assert compute_release_timing([]) is None

    def test_no_dates_returns_none(self):
        games = [{"appid": i, "release_date": None, "revenue": 100.0} for i in range(10)]
        assert compute_release_timing(games) is None

    def test_month_labels_correct(self):
        games = make_games(50)
        result = compute_release_timing(games)
        assert result is not None
        expected_labels = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
        actual_labels = [m.label for m in result.monthly]
        assert actual_labels == expected_labels


class TestComputeSubGenres:
    def test_sorted_by_frequency(self):
        games = make_games(50)
        result = compute_sub_genres(games)
        assert result is not None
        counts = [e.game_count for e in result.sub_genres]
        assert counts == sorted(counts, reverse=True)

    def test_primary_tags_excluded(self):
        games = make_games(50)
        primary = {"Action", "Roguelike"}
        result = compute_sub_genres(games, primary_tags=primary)
        assert result is not None
        tag_names = [e.tag for e in result.sub_genres]
        assert "Action" not in tag_names
        assert "Roguelike" not in tag_names

    def test_empty_returns_none(self):
        assert compute_sub_genres([]) is None

    def test_min_games_threshold(self):
        # Use 100 games so min_games = max(3, 5) = 5
        games = make_games(100)
        result = compute_sub_genres(games)
        assert result is not None
        min_threshold = max(3, len(games) // 20)
        for entry in result.sub_genres:
            assert entry.game_count >= min_threshold

    def test_top_n_limit(self):
        games = make_games(100)
        result = compute_sub_genres(games, top_n=5)
        assert result is not None
        assert len(result.sub_genres) <= 5


class TestComputeCompetitiveDensity:
    def test_periods_sorted_by_year(self):
        games = make_games(50)
        result = compute_competitive_density(games)
        assert result is not None
        years = [p.year for p in result.periods]
        assert years == sorted(years)

    def test_partial_current_year(self):
        # Add a game from 2026 — current year should be partial
        games = [
            {"appid": 1, "release_date": "2026-01-15", "revenue": 100_000.0},
            {"appid": 2, "release_date": "2025-06-15", "revenue": 200_000.0},
        ]
        result = compute_competitive_density(games)
        assert result is not None
        current_periods = [p for p in result.periods if p.year == 2026]
        if current_periods:
            assert current_periods[0].partial is True

    def test_empty_returns_none(self):
        assert compute_competitive_density([]) is None

    def test_pipeline_count(self):
        # Future-dated game = pipeline
        games = [
            {"appid": 1, "release_date": "2027-06-15", "revenue": None},
            {"appid": 2, "release_date": "2025-06-15", "revenue": 200_000.0},
        ]
        result = compute_competitive_density(games)
        assert result is not None
        assert result.pipeline_count >= 1

    def test_game_count_per_period(self):
        # 3 games in 2020, 2 in 2021
        games = (
            [{"appid": i, "release_date": "2020-06-15", "revenue": float(i * 1000)} for i in range(1, 4)] +
            [{"appid": i + 10, "release_date": "2021-06-15", "revenue": float(i * 1000)} for i in range(1, 3)]
        )
        result = compute_competitive_density(games)
        assert result is not None
        period_map = {p.year: p.game_count for p in result.periods}
        assert period_map[2020] == 3
        assert period_map[2021] == 2


# ---------------------------------------------------------------------------
# Integration-level tests
# ---------------------------------------------------------------------------


class TestEndToEnd:
    def test_full_analytics_run(self):
        games = make_games(100)
        # All 10 functions should return non-None with 100 games
        assert compute_concentration(games) is not None
        assert compute_temporal_trends(games) is not None
        assert compute_price_brackets(games) is not None
        assert compute_success_rates(games) is not None
        assert compute_tag_multipliers(games) is not None
        assert compute_score_revenue(games) is not None
        assert compute_publisher_analysis(games) is not None
        assert compute_release_timing(games) is not None
        assert compute_sub_genres(games) is not None
        assert compute_competitive_density(games) is not None

    def test_missing_data_handling(self):
        games = make_games_with_gaps(50)
        # All functions should handle gracefully without raising exceptions
        compute_concentration(games)
        compute_temporal_trends(games)
        compute_price_brackets(games)
        compute_success_rates(games)
        compute_tag_multipliers(games)
        compute_score_revenue(games)
        compute_publisher_analysis(games)
        compute_release_timing(games)
        compute_sub_genres(games)
        compute_competitive_density(games)

    def test_deduplication_before_analytics(self):
        # Make games with duplicates
        games = make_games(20)
        # Duplicate first 5 games
        duplicated = games + games[:5]
        deduplicated = _deduplicate(duplicated)
        # Deduplication should remove the 5 duplicates
        assert len(deduplicated) == 20

    def test_single_game_analytics(self):
        games = make_games(1)
        # Single game should not crash any function
        compute_concentration(games)
        compute_temporal_trends(games)
        compute_price_brackets(games)
        compute_success_rates(games)
        compute_tag_multipliers(games)
        compute_score_revenue(games)
        compute_publisher_analysis(games)
        compute_release_timing(games)
        compute_sub_genres(games)
        compute_competitive_density(games)

    def test_result_types(self):
        from src.schemas import (
            ConcentrationResult, TemporalTrendsResult, PriceBracketResult,
            SuccessRateResult, TagMultiplierResult, ScoreRevenueResult,
            PublisherAnalysisResult, ReleaseTimingResult, SubGenreResult,
            CompetitiveDensityResult,
        )
        games = make_games(100)
        assert isinstance(compute_concentration(games), ConcentrationResult)
        assert isinstance(compute_temporal_trends(games), TemporalTrendsResult)
        assert isinstance(compute_price_brackets(games), PriceBracketResult)
        assert isinstance(compute_success_rates(games), SuccessRateResult)
        assert isinstance(compute_tag_multipliers(games), TagMultiplierResult)
        assert isinstance(compute_score_revenue(games), ScoreRevenueResult)
        assert isinstance(compute_publisher_analysis(games), PublisherAnalysisResult)
        assert isinstance(compute_release_timing(games), ReleaseTimingResult)
        assert isinstance(compute_sub_genres(games), SubGenreResult)
        assert isinstance(compute_competitive_density(games), CompetitiveDensityResult)
