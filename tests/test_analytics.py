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
    compute_visual_multipliers,
    compute_dlc_analytics, compute_launch_metrics, compute_update_impact,
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


# ---------------------------------------------------------------------------
# Phase 14: Visual Intelligence analytics tests
# ---------------------------------------------------------------------------

def _make_profile(appid, character_style='pixel', environment_style='pixel',
                  fidelity='low', mood='dark', palette='dark',
                  ui_density='minimal', perspective='top_down',
                  header='logo_abstract', genre_tags=None):
    return {
        "appid": appid,
        "character_style_primary": character_style,
        "environment_style_primary": environment_style,
        "production_fidelity": fidelity,
        "mood_primary": mood,
        "palette": palette,
        "ui_density": ui_density,
        "perspective": perspective,
        "header_composition": header,
        "genre_tags": genre_tags or [],
    }


class TestComputeVisualMultipliers:
    def test_empty_profiles_returns_empty_dict(self):
        assert compute_visual_multipliers([], {}) == {}

    def test_no_commercial_overlap_returns_empty_dict(self):
        profiles = [_make_profile(1), _make_profile(2)]
        commercial = {3: 100_000, 4: 200_000}
        assert compute_visual_multipliers(profiles, commercial) == {}

    def test_basic_multiplier_computation(self):
        # 3 pixel games (low rev) + 3 illustrated games (high rev)
        profiles = [
            _make_profile(1, character_style='pixel'),
            _make_profile(2, character_style='pixel'),
            _make_profile(3, character_style='pixel'),
            _make_profile(4, character_style='illustrated'),
            _make_profile(5, character_style='illustrated'),
            _make_profile(6, character_style='illustrated'),
        ]
        commercial = {1: 100_000, 2: 200_000, 3: 300_000, 4: 400_000, 5: 500_000, 6: 600_000}
        result = compute_visual_multipliers(profiles, commercial)

        assert "character_style_primary" in result
        entries = {e["value"]: e for e in result["character_style_primary"]}
        assert entries["pixel"]["mean_multiplier"] < 1.0
        assert entries["illustrated"]["mean_multiplier"] > 1.0
        assert entries["pixel"]["game_count"] == 3
        assert entries["illustrated"]["game_count"] == 3

    def test_min_games_filters_small_buckets(self):
        profiles = [
            _make_profile(1, character_style='pixel'),
            _make_profile(2, character_style='pixel'),
            _make_profile(3, character_style='pixel'),
            _make_profile(4, character_style='illustrated'),  # only 1
        ]
        commercial = {1: 100_000, 2: 200_000, 3: 300_000, 4: 500_000}

        # With default min_games=3, illustrated excluded
        result = compute_visual_multipliers(profiles, commercial)
        assert "character_style_primary" in result
        values = [e["value"] for e in result["character_style_primary"]]
        assert "illustrated" not in values

        # With min_games=1, illustrated included
        result = compute_visual_multipliers(profiles, commercial, min_games=1)
        values = [e["value"] for e in result["character_style_primary"]]
        assert "illustrated" in values

    def test_all_eight_attributes_analyzed(self):
        # 3 identical profiles with commercial data — all 8 attrs should appear
        profiles = [_make_profile(i) for i in range(1, 4)]
        commercial = {1: 100_000, 2: 200_000, 3: 300_000}
        result = compute_visual_multipliers(profiles, commercial)

        expected_attrs = [
            "character_style_primary", "environment_style_primary",
            "production_fidelity", "mood_primary", "palette",
            "ui_density", "perspective", "header_composition",
        ]
        for attr in expected_attrs:
            assert attr in result, f"Missing attribute: {attr}"

    def test_multiplier_sorted_descending(self):
        profiles = [
            _make_profile(1, mood='dark'),
            _make_profile(2, mood='dark'),
            _make_profile(3, mood='dark'),
            _make_profile(4, mood='whimsical'),
            _make_profile(5, mood='whimsical'),
            _make_profile(6, mood='whimsical'),
        ]
        commercial = {1: 50_000, 2: 100_000, 3: 150_000, 4: 400_000, 5: 500_000, 6: 600_000}
        result = compute_visual_multipliers(profiles, commercial)

        entries = result["mood_primary"]
        multipliers = [e["mean_multiplier"] for e in entries]
        assert multipliers == sorted(multipliers, reverse=True)

    def test_none_revenue_excluded(self):
        profiles = [_make_profile(1), _make_profile(2), _make_profile(3)]
        commercial = {1: 100_000, 2: None, 3: 300_000}
        result = compute_visual_multipliers(profiles, commercial)
        # Only 2 profiles with valid revenue — below min_games=3
        # Should return empty since no bucket has 3+ games
        assert result == {}

    def test_zero_genre_avg_returns_empty(self):
        profiles = [_make_profile(1), _make_profile(2), _make_profile(3)]
        commercial = {1: 0, 2: 0, 3: 0}
        assert compute_visual_multipliers(profiles, commercial) == {}

    def test_avg_revenue_field_present(self):
        profiles = [_make_profile(i) for i in range(1, 4)]
        commercial = {1: 100_000, 2: 200_000, 3: 300_000}
        result = compute_visual_multipliers(profiles, commercial)
        for attr_entries in result.values():
            for entry in attr_entries:
                assert "avg_revenue" in entry
                assert isinstance(entry["avg_revenue"], (int, float))


# ---------------------------------------------------------------------------
# Phase 15: Strategy Data Sources compute function tests
# ---------------------------------------------------------------------------

# ---- helpers ---------------------------------------------------------------

def _make_dlc(name="Expansion Pack", price=9.99, release_date="2020-06-01"):
    return {"name": name, "price": price, "release_date": release_date}


def _make_history_entry(timestamp, revenue, followers=100, sales=500):
    """Create a history entry dict (fields match GamalyticHistoryEntry)."""
    return {
        "entry_type": "daily",
        "timestamp": timestamp,
        "revenue": revenue,
        "followers": followers,
        "sales": sales,
    }


def _make_news_activity(total=20, last_365=10, last_90=4, post_dates=None):
    return {
        "developer_posts_total": total,
        "developer_posts_last_365d": last_365,
        "developer_posts_last_90d": last_90,
        "developer_post_dates": post_dates or [],
    }


# ============================================================================
# TestComputeDLCAnalytics
# ============================================================================

class TestComputeDLCAnalytics:

    def test_basic_dlc_analytics(self):
        """3 games, 2 with DLC — verify penetration, count, and price."""
        games = [
            {
                "appid": 1,
                "gamalytic_dlc": [
                    _make_dlc("Expansion", price=9.99),
                    _make_dlc("Soundtrack", price=4.99, release_date="2020-09-01"),
                ],
            },
            {
                "appid": 2,
                "gamalytic_dlc": [
                    _make_dlc("DLC 1", price=14.99),
                ],
            },
            {
                "appid": 3,
                "gamalytic_dlc": [],  # no DLC
            },
        ]
        result = compute_dlc_analytics(games)
        assert result is not None
        assert result["total_games"] == 3
        assert result["games_with_dlc"] == 2
        assert result["dlc_penetration_pct"] == pytest.approx(66.7, abs=0.1)
        assert result["avg_dlc_count"] == pytest.approx(1.5, abs=0.1)
        assert result["dlc_revenue_available"] is False

    def test_empty_games_list(self):
        """Empty input returns None."""
        assert compute_dlc_analytics([]) is None

    def test_no_games_with_dlc(self):
        """All games have empty DLC lists → penetration 0%."""
        games = [
            {"appid": 1, "gamalytic_dlc": []},
            {"appid": 2, "gamalytic_dlc": []},
        ]
        result = compute_dlc_analytics(games)
        assert result is not None
        assert result["dlc_penetration_pct"] == 0.0
        assert result["games_with_dlc"] == 0

    def test_price_tiers(self):
        """Verify binning at $5/$15/$30 boundaries."""
        games = [
            {
                "appid": 1,
                "gamalytic_dlc": [
                    _make_dlc("Free DLC", price=0.0),
                    _make_dlc("Cheap", price=2.99),       # under_5
                    _make_dlc("Medium", price=9.99),      # 5_to_15
                    _make_dlc("Premium", price=19.99),    # 15_to_30
                    _make_dlc("Expensive", price=39.99),  # 30_plus
                ],
            }
        ]
        result = compute_dlc_analytics(games)
        assert result is not None
        tiers = result["price_tiers"]
        assert tiers["free"] == 1
        assert tiers["under_5"] == 1
        assert tiers["5_to_15"] == 1
        assert tiers["15_to_30"] == 1
        assert tiers["30_plus"] == 1

    def test_cadence_computation(self):
        """3 DLC ~90 days apart → avg cadence ~90 days."""
        games = [
            {
                "appid": 1,
                "gamalytic_dlc": [
                    _make_dlc("DLC 1", release_date="2020-01-01"),
                    _make_dlc("DLC 2", release_date="2020-04-01"),  # 91 days
                    _make_dlc("DLC 3", release_date="2020-07-01"),  # 91 days
                ],
            }
        ]
        result = compute_dlc_analytics(games)
        assert result is not None
        # 2020-01-01 to 2020-04-01 = 91 days, 2020-04-01 to 2020-07-01 = 91 days
        assert result["avg_cadence_days"] == pytest.approx(91.0, abs=1.0)
        assert result["median_cadence_days"] == pytest.approx(91.0, abs=1.0)

    def test_single_dlc_no_cadence(self):
        """Game with exactly 1 DLC has no cadence data (None)."""
        games = [{"appid": 1, "gamalytic_dlc": [_make_dlc("Solo DLC")]}]
        result = compute_dlc_analytics(games)
        assert result is not None
        assert result["avg_cadence_days"] is None
        assert result["median_cadence_days"] is None

    def test_soundtrack_detection(self):
        """Case-insensitive 'Soundtrack' match; has_soundtrack_pct computed correctly."""
        games = [
            {"appid": 1, "gamalytic_dlc": [_make_dlc("SOUNDTRACK")], },      # match
            {"appid": 2, "gamalytic_dlc": [_make_dlc("Original Soundtrack")]},  # match
            {"appid": 3, "gamalytic_dlc": [_make_dlc("Character Pack")]},     # no match
        ]
        result = compute_dlc_analytics(games)
        assert result is not None
        # 2 of 3 games with DLC have soundtrack
        assert result["has_soundtrack_pct"] == pytest.approx(66.7, abs=0.1)

    def test_null_prices_handled(self):
        """None prices are binned as free (not excluded)."""
        games = [
            {
                "appid": 1,
                "gamalytic_dlc": [
                    {"name": "Free DLC", "price": None, "release_date": "2020-01-01"},
                    {"name": "Paid", "price": 7.99, "release_date": "2020-02-01"},
                ],
            }
        ]
        result = compute_dlc_analytics(games)
        assert result is not None
        assert result["price_tiers"]["free"] == 1
        assert result["price_tiers"]["5_to_15"] == 1
        # avg_dlc_price only includes paid DLC (7.99)
        assert result["avg_dlc_price"] == pytest.approx(7.99, abs=0.01)


# ============================================================================
# TestComputeLaunchMetrics
# ============================================================================

class TestComputeLaunchMetrics:

    def _make_game_with_history(self, appid=1, release_date="2020-01-01",
                                 revenues=None, followers_list=None,
                                 is_ea=False, ea_exit_date=None, ea_date=None,
                                 followers=500, copies_sold=5000):
        """Build a game dict with a realistic history array."""
        if revenues is None:
            # Default: 1000/month cumulative for 6 months
            revenues = [1000, 5000, 8000, 10000, 11000, 12000]
        if followers_list is None:
            followers_list = [100] * len(revenues)
        timestamps = [
            f"2020-{i+1:02d}-01" for i in range(len(revenues))
        ]
        history = [
            _make_history_entry(ts, rev, fol)
            for ts, rev, fol in zip(timestamps, revenues, followers_list)
        ]
        return {
            "appid": appid,
            "release_date": release_date,
            "gamalytic_ea_date": ea_date,
            "ea_exit_date": ea_exit_date,
            "gamalytic_is_early_access": is_ea,
            "followers": followers,
            "copies_sold": copies_sold,
            "history": history,
        }

    def test_basic_launch_velocity(self):
        """Verify 30d and 90d percentages from cumulative history.

        anchor = 2020-01-01.
        30d cutoff = 2020-01-31 → entries <= Jan 31 → [Jan 1: 2000] → velocity = 2000/10000 = 20%
        90d cutoff = 2020-03-31 → entries <= Mar 31 → [Jan 1: 2000, Feb 1: 5000] → velocity = 5000/10000 = 50%
        Apr 1 entry (7000) is NOT included (90+1 = Apr 1 > Mar 31 cutoff).
        """
        game = {
            "appid": 1,
            "release_date": "2020-01-01",
            "gamalytic_ea_date": None,
            "ea_exit_date": None,
            "gamalytic_is_early_access": False,
            "followers": 1000,
            "copies_sold": 2000,
            "history": [
                _make_history_entry("2020-01-01", 2000),
                _make_history_entry("2020-02-01", 5000),   # 31 days — within 90d but not 30d window
                _make_history_entry("2020-04-01", 7000),   # 91 days — outside 90d window (cutoff = Mar 31)
                _make_history_entry("2020-12-01", 10000),  # lifetime
            ],
        }
        result = compute_launch_metrics([game])
        assert result is not None
        # 30d window: entries with timestamp <= 2020-01-31 → [2020-01-01] → revenue=2000
        # velocity_30d = 2000/10000 * 100 = 20.0
        assert result["launch_velocity_30d"]["mean"] == pytest.approx(20.0, abs=0.5)
        # 90d window (cutoff = 2020-03-31): entries <= Mar 31 → [Jan 1, Feb 1] → last revenue=5000
        # velocity_90d = 5000/10000 * 100 = 50.0
        assert result["launch_velocity_90d"]["mean"] == pytest.approx(50.0, abs=0.5)

    def test_empty_games_list(self):
        """Empty input returns None."""
        assert compute_launch_metrics([]) is None

    def test_no_history(self):
        """Games with no history → games_with_history = 0."""
        games = [
            {"appid": 1, "release_date": "2020-01-01", "history": [],
             "gamalytic_is_early_access": False, "ea_exit_date": None,
             "gamalytic_ea_date": None, "followers": None, "copies_sold": None},
        ]
        result = compute_launch_metrics(games)
        assert result is not None
        assert result["games_with_history"] == 0
        assert result["launch_velocity_30d"]["sample_size"] == 0

    def test_ea_game_uses_exit_date(self):
        """EA game uses ea_exit_date as velocity anchor, not release_date.

        Design: If release_date (2019-01-01) were used as anchor, the 30d window
        (cutoff 2019-01-31) would catch only the 2019-01-01 entry (rev=1000),
        giving velocity = 1000/300_000 * 100 ≈ 0.33%.
        With ea_exit_date (2020-06-01) as anchor, 30d window (cutoff 2020-07-01)
        catches entries <= 2020-07-01 → last = 2020-07-01 with revenue=150_000.
        velocity_30d = 150_000/300_000 * 100 = 50%.
        The test asserts 50% to confirm ea_exit_date is used as anchor.
        """
        game = {
            "appid": 1,
            "release_date": "2019-01-01",    # EA launch — should NOT be anchor
            "gamalytic_ea_date": "2019-01-01",
            "ea_exit_date": "2020-06-01",    # Full launch — SHOULD be anchor
            "gamalytic_is_early_access": True,
            "followers": 2000,
            "copies_sold": 5000,
            "history": [
                # During EA period
                _make_history_entry("2019-01-01", 1_000),   # EA launch
                _make_history_entry("2019-06-01", 50_000),  # mid-EA
                # At full launch (anchor = 2020-06-01):
                _make_history_entry("2020-06-01", 100_000),  # 0d from full launch anchor
                _make_history_entry("2020-07-01", 150_000),  # 30d from anchor (cutoff = 2020-07-01, inclusive)
                _make_history_entry("2021-06-01", 300_000),  # lifetime
            ],
        }
        result = compute_launch_metrics([game])
        assert result is not None
        # 30d cutoff from 2020-06-01 = 2020-07-01; entries <= "2020-07-01" → last=150_000
        # velocity_30d = 150_000/300_000 * 100 = 50.0
        assert result["launch_velocity_30d"]["mean"] == pytest.approx(50.0, abs=1.0)
        # Without ea_exit_date anchor (if wrongly using 2019-01-01):
        # 30d window = 2019-01-31; only "2019-01-01" (1000) → velocity = 1000/300_000 = 0.3%
        # We assert 50% (not 0.3%) to confirm correct anchor used
        assert result["launch_velocity_30d"]["mean"] > 40.0  # clearly not 0.3%

    def test_wishlist_proxy(self):
        """followers / copies_sold produces correct ratio."""
        game = self._make_game_with_history(followers=2000, copies_sold=10000)
        result = compute_launch_metrics([game])
        assert result is not None
        # 2000 / 10000 = 0.2
        assert result["wishlist_proxy"]["mean"] == pytest.approx(0.2, abs=0.01)
        assert result["wishlist_proxy"]["sample_size"] == 1

    def test_wishlist_proxy_missing_data(self):
        """Games with followers=None or copies_sold=0 excluded from proxy sample."""
        games = [
            self._make_game_with_history(appid=1, followers=None, copies_sold=5000),
            self._make_game_with_history(appid=2, followers=500, copies_sold=0),
            self._make_game_with_history(appid=3, followers=1000, copies_sold=2000),
        ]
        result = compute_launch_metrics(games)
        assert result is not None
        # Only game 3 qualifies
        assert result["wishlist_proxy"]["sample_size"] == 1
        assert result["wishlist_proxy"]["mean"] == pytest.approx(0.5, abs=0.01)

    def test_ea_duration(self):
        """EA duration calculated from gamalytic_ea_date to ea_exit_date."""
        game = {
            "appid": 1,
            "release_date": "2019-01-01",
            "gamalytic_ea_date": "2019-01-01",
            "ea_exit_date": "2020-01-01",  # exactly 1 year = 366 days (2019 is not leap)
            "gamalytic_is_early_access": True,
            "followers": 1000,
            "copies_sold": 5000,
            "history": [
                _make_history_entry("2019-01-01", 10_000),
                _make_history_entry("2019-07-01", 50_000),
                _make_history_entry("2020-01-01", 100_000),
            ],
        }
        result = compute_launch_metrics([game])
        assert result is not None
        ea = result["ea_stats"]
        assert ea["ea_game_count"] == 1
        assert ea["avg_ea_duration_days"] == pytest.approx(365.0, abs=2.0)

    def test_ea_duration_missing_exit(self):
        """EA games without ea_exit_date excluded from duration sample."""
        game = {
            "appid": 1,
            "release_date": "2019-01-01",
            "gamalytic_ea_date": "2019-01-01",
            "ea_exit_date": None,   # no exit date
            "gamalytic_is_early_access": True,
            "followers": 500,
            "copies_sold": 5000,
            "history": [
                _make_history_entry("2019-01-01", 10_000),
                _make_history_entry("2020-01-01", 50_000),
            ],
        }
        result = compute_launch_metrics([game])
        assert result is not None
        ea = result["ea_stats"]
        assert ea["ea_game_count"] == 1
        assert ea["sample_size"] == 0         # excluded from duration sample
        assert ea["avg_ea_duration_days"] is None

    def test_cumulative_revenue_not_summed(self):
        """CRITICAL: lifetime revenue = last entry, not sum of all entries."""
        # If revenue were summed: 100+200+300+400 = 1000
        # Correct (last entry): 400
        game = {
            "appid": 1,
            "release_date": "2020-01-01",
            "gamalytic_ea_date": None,
            "ea_exit_date": None,
            "gamalytic_is_early_access": False,
            "followers": 400,
            "copies_sold": 2000,
            "history": [
                _make_history_entry("2020-01-01", 100),
                _make_history_entry("2020-02-01", 200),
                _make_history_entry("2020-04-01", 300),
                _make_history_entry("2020-12-01", 400),  # LIFETIME = 400
            ],
        }
        result = compute_launch_metrics([game])
        assert result is not None
        # 30d: entries <= 2020-01-31 → revenue=100. velocity = 100/400 * 100 = 25.0
        # If incorrectly summed: 100/1000 * 100 = 10.0 — different result
        assert result["launch_velocity_30d"]["mean"] == pytest.approx(25.0, abs=0.5)

    def test_history_too_short(self):
        """Games with < 2 history entries are excluded."""
        game = {
            "appid": 1,
            "release_date": "2020-01-01",
            "gamalytic_ea_date": None,
            "ea_exit_date": None,
            "gamalytic_is_early_access": False,
            "followers": 100,
            "copies_sold": 500,
            "history": [_make_history_entry("2020-01-01", 5000)],  # only 1 entry
        }
        result = compute_launch_metrics([game])
        assert result is not None
        assert result["games_with_history"] == 0
        assert result["launch_velocity_30d"]["sample_size"] == 0

    def test_mixed_ea_and_non_ea(self):
        """ea_pct calculated correctly with mixed EA and non-EA games."""
        ea_game = self._make_game_with_history(
            appid=1, is_ea=True, ea_date="2019-01-01",
            ea_exit_date="2020-01-01", release_date="2019-01-01"
        )
        non_ea1 = self._make_game_with_history(appid=2, is_ea=False)
        non_ea2 = self._make_game_with_history(appid=3, is_ea=False)
        result = compute_launch_metrics([ea_game, non_ea1, non_ea2])
        assert result is not None
        assert result["ea_stats"]["ea_game_count"] == 1
        assert result["ea_stats"]["ea_pct"] == pytest.approx(33.3, abs=0.1)


# ============================================================================
# TestComputeUpdateImpact
# ============================================================================

class TestComputeUpdateImpact:

    def _make_game(self, appid, history, news=None, posts_365=10):
        """Build a game dict for update impact testing."""
        if news is None:
            news = _make_news_activity(last_365=posts_365)
        return {
            "appid": appid,
            "history": history,
            "news_activity": news,
        }

    def test_active_vs_quiet_periods(self):
        """5 history entries, classify active (post in period) vs. quiet (no post)."""
        # History: 5 entries spanning Jan-May 2020
        history = [
            _make_history_entry("2020-01-01", 10_000, followers=100),
            _make_history_entry("2020-02-01", 15_000, followers=120),  # period 1: Jan→Feb (active — post Jan 15)
            _make_history_entry("2020-03-01", 16_000, followers=121),  # period 2: Feb→Mar (quiet)
            _make_history_entry("2020-04-01", 20_000, followers=150),  # period 3: Mar→Apr (active — post Mar 10)
            _make_history_entry("2020-05-01", 21_000, followers=152),  # period 4: Apr→May (quiet)
        ]
        news = _make_news_activity(
            last_365=2,
            post_dates=["2020-01-15", "2020-03-10"],  # 1 post in Jan→Feb period, 1 in Mar→Apr period
        )
        game = self._make_game(1, history, news=news)
        result = compute_update_impact([game])
        assert result is not None
        assert result["games_analyzed"] == 1

        avq = result["active_vs_quiet"]
        # Active periods: Jan→Feb (delta=5000, growth=50%), Mar→Apr (delta=4000, growth=25%)
        # avg active growth ≈ (50 + 25) / 2 = 37.5%
        assert avq["active_period_avg_revenue_growth_pct"] is not None
        assert avq["active_period_avg_revenue_growth_pct"] == pytest.approx(37.5, abs=1.0)
        # Quiet periods: Feb→Mar (delta=1000, growth≈6.7%), Apr→May (delta=1000, growth=5%)
        assert avq["quiet_period_avg_revenue_growth_pct"] is not None
        assert avq["active_period_count"] == 2
        assert avq["quiet_period_count"] == 2

    def test_empty_games_list(self):
        """Empty input returns None."""
        assert compute_update_impact([]) is None

    def test_no_news_data(self):
        """Games without news_activity → games_analyzed = 0."""
        game = {
            "appid": 1,
            "history": [
                _make_history_entry("2020-01-01", 10_000),
                _make_history_entry("2020-02-01", 20_000),
                _make_history_entry("2020-03-01", 30_000),
            ],
            "news_activity": None,
        }
        result = compute_update_impact([game])
        assert result is not None
        assert result["games_analyzed"] == 0

    def test_history_too_short(self):
        """Games with history < 3 entries excluded (need >= 3)."""
        game = {
            "appid": 1,
            "history": [
                _make_history_entry("2020-01-01", 10_000),
                _make_history_entry("2020-02-01", 20_000),  # only 2 entries
            ],
            "news_activity": _make_news_activity(),
        }
        result = compute_update_impact([game])
        assert result is not None
        assert result["games_analyzed"] == 0

    def test_update_frequency_tiers(self):
        """4 games each land in different frequency tiers based on posts_last_365d."""
        history_base = [
            _make_history_entry("2020-01-01", 100_000),
            _make_history_entry("2020-06-01", 200_000),
            _make_history_entry("2020-12-01", 300_000),
        ]
        game_high = self._make_game(1, history_base, posts_365=15)      # >12 → high
        game_mod  = self._make_game(2, history_base, posts_365=8)       # 4-12 → moderate
        game_low  = self._make_game(3, history_base, posts_365=2)       # 1-3 → low
        game_inac = self._make_game(4, history_base, posts_365=0)       # 0 → inactive

        result = compute_update_impact([game_high, game_mod, game_low, game_inac])
        assert result is not None
        tiers = result["update_frequency_tiers"]
        assert tiers["high"]["game_count"] == 1
        assert tiers["moderate"]["game_count"] == 1
        assert tiers["low"]["game_count"] == 1
        assert tiers["inactive"]["game_count"] == 1
        # Each tier has 1 game with lifetime revenue = 300_000
        assert tiers["high"]["avg_lifetime_revenue"] == pytest.approx(300_000.0, rel=0.01)

    def test_revenue_deltas_from_cumulative(self):
        """Period revenue computed as delta (subtraction), not raw cumulative value."""
        # history: 100 → 300 → 600 → 1000
        # Period deltas: 200, 300, 400 (NOT 300, 600, 1000)
        history = [
            _make_history_entry("2020-01-01", 100),
            _make_history_entry("2020-02-01", 300),
            _make_history_entry("2020-03-01", 600),
            _make_history_entry("2020-04-01", 1000),
        ]
        news = _make_news_activity(
            last_365=1,
            post_dates=["2020-01-15"],  # active only in Jan→Feb period
        )
        game = self._make_game(1, history, news=news)
        result = compute_update_impact([game])
        assert result is not None
        avq = result["active_vs_quiet"]
        # Active period Jan→Feb: delta=300-100=200, growth=200/100*100=200%
        assert avq["active_period_avg_revenue_growth_pct"] == pytest.approx(200.0, abs=0.5)
        # Quiet periods Feb→Mar: delta=300, growth=300/300*100=100%
        # Quiet period Mar→Apr: delta=400, growth=400/600*100≈66.7%
        # avg quiet = (100 + 66.67) / 2 ≈ 83.3
        assert avq["quiet_period_avg_revenue_growth_pct"] == pytest.approx(83.33, abs=1.0)

    def test_follower_growth_active_periods(self):
        """Active periods show higher follower growth than quiet periods."""
        history = [
            _make_history_entry("2020-01-01", 10_000, followers=1000),
            _make_history_entry("2020-02-01", 20_000, followers=1200),   # period 1: +200 (active)
            _make_history_entry("2020-03-01", 21_000, followers=1201),   # period 2: +1 (quiet)
            _make_history_entry("2020-04-01", 30_000, followers=1400),   # period 3: +199 (active)
            _make_history_entry("2020-05-01", 31_000, followers=1401),   # period 4: +1 (quiet)
        ]
        news = _make_news_activity(
            last_365=2,
            post_dates=["2020-01-15", "2020-03-10"],
        )
        game = self._make_game(1, history, news=news)
        result = compute_update_impact([game])
        assert result is not None
        avq = result["active_vs_quiet"]
        # Active avg follower growth: (200 + 199) / 2 = 199.5
        # Quiet avg follower growth: (1 + 1) / 2 = 1
        assert avq["active_period_avg_follower_growth"] == pytest.approx(199.5, abs=1.0)
        assert avq["quiet_period_avg_follower_growth"] == pytest.approx(1.0, abs=0.1)
        assert avq["active_period_avg_follower_growth"] > avq["quiet_period_avg_follower_growth"]

    def test_single_game_all_active(self):
        """All periods active → quiet_period_count = 0."""
        history = [
            _make_history_entry("2020-01-01", 100_000),
            _make_history_entry("2020-02-01", 200_000),
            _make_history_entry("2020-03-01", 300_000),
        ]
        # Posts in every inter-entry period
        news = _make_news_activity(
            last_365=2,
            post_dates=["2020-01-15", "2020-02-15"],
        )
        game = self._make_game(1, history, news=news)
        result = compute_update_impact([game])
        assert result is not None
        assert result["active_vs_quiet"]["quiet_period_count"] == 0
        assert result["active_vs_quiet"]["active_period_count"] == 2
