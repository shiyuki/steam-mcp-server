"""Tests for src/market_tools.py — pure sync, no HTTP mocking needed.

Tests schemas, helpers, _run_market_analysis orchestration, and health scores.
"""
import time
import pytest

from src.schemas import (
    AnalyzeMarketResult,
    ComputeTimingInfo,
    EvaluateGameResult,
    GamePercentiles,
    GameProfile,
    MarketHealthScores,
    SkippedMetric,
)
from src.market_tools import (
    ANALYSIS_CACHE_TTL,
    GAMALYTIC_QUOTA_HARD_WARN,
    GAMALYTIC_QUOTA_WARN_THRESHOLD,
    MIN_GAMES_THRESHOLD,
    _analysis_cache,
    _build_game_profile,
    _cache_get,
    _cache_set,
    _compute_descriptive_stats,
    _compute_health_scores,
    _compute_tag_frequency,
    _get_analysis_cache_key,
    _percentile_rank,
    _run_market_analysis,
    _std_deviations_from_mean,
    clear_analysis_cache,
    get_gamalytic_call_count,
    increment_gamalytic_counter,
    reset_gamalytic_counter,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _pagination_meta(games_list, pages=1, is_partial=False, error=None):
    """Build a pagination_meta dict matching list_games_by_tag's tuple return."""
    return {
        "is_partial": is_partial,
        "pages_fetched": pages,
        "total_pages": pages,
        "total_expected": len(games_list),
        "error": error,
    }


def _with_meta(games_list, **kwargs):
    """Wrap a games list in the (games, pagination_meta) tuple for list_games_by_tag mocks."""
    return (games_list, _pagination_meta(games_list, **kwargs))


# ---------------------------------------------------------------------------
# Test factory (module-level, not pytest fixtures)
# ---------------------------------------------------------------------------

def make_games(n=50, revenue_base=50000, with_tags=True):
    """Generate n synthetic games with all fields required for market analysis."""
    games = []
    for i in range(1, n + 1):
        tags = {"Action": 100, "Indie": 80, "Roguelike": 60} if with_tags else {}
        games.append({
            "appid": i,
            "name": f"Game {i}",
            "revenue": i * revenue_base,
            "price": 4.99 + (i % 6) * 5,
            "review_score": 50 + (i % 50),
            "release_date": f"{2015 + (i % 10)}-{1 + (i % 12):02d}-15",
            "tags": tags,
            "developer": f"Dev{i}",
            "publisher": f"Pub{i}" if i % 3 else f"Dev{i}",
            "owners": i * 1000,
            "ccu": i * 10,
            "median_forever": i * 2.5,
            "followers": i * 500 if i % 2 else None,
            "copies_sold": i * 3000 if i % 2 else None,
            "positive": i * 200,
            "negative": i * 10,
        })
    return games


# ---------------------------------------------------------------------------
# TestSchemas — 8 tests
# ---------------------------------------------------------------------------

class TestSchemas:
    def test_game_profile_defaults(self):
        """GameProfile can be instantiated with minimal required fields."""
        gp = GameProfile(appid=1, name="Test Game")
        assert gp.appid == 1
        assert gp.name == "Test Game"
        assert gp.revenue is None
        assert gp.review_score is None
        assert gp.release_date is None
        assert gp.tags == {}
        assert gp.developer == ""
        assert gp.publisher == ""

    def test_game_profile_full(self):
        """GameProfile stores all optional fields correctly."""
        gp = GameProfile(
            appid=123,
            name="Full Game",
            revenue=500000.0,
            review_score=87.5,
            release_date="2022-05-15",
            owners=50000.0,
            ccu=1200,
            median_playtime=12.5,
            followers=3000,
            copies_sold=10000,
            price=19.99,
            tags={"Action": 100, "Indie": 80},
            developer="DevCo",
            publisher="PubCo",
        )
        assert gp.revenue == 500000.0
        assert gp.price == 19.99
        assert gp.tags == {"Action": 100, "Indie": 80}
        assert gp.median_playtime == 12.5

    def test_analyze_market_result_flat_structure(self):
        """AnalyzeMarketResult instantiates with defaults and all metric fields at top level."""
        result = AnalyzeMarketResult(market_label="Test", total_games=50)
        assert result.market_label == "Test"
        assert result.total_games == 50
        assert result.concentration is None
        assert result.temporal_trends is None
        assert result.price_brackets is None
        assert result.success_rates is None
        assert result.tag_multipliers is None
        assert result.score_revenue is None
        assert result.publisher_analysis is None
        assert result.release_timing is None
        assert result.sub_genres is None
        assert result.competitive_density is None
        assert result.health_scores is None
        assert result.skipped_metrics == []

    def test_skipped_metric(self):
        """SkippedMetric stores metric name and reason."""
        sm = SkippedMetric(metric="tag_multipliers", reason="insufficient data")
        assert sm.metric == "tag_multipliers"
        assert sm.reason == "insufficient data"

    def test_market_health_scores_defaults(self):
        """MarketHealthScores defaults all to neutral 50."""
        scores = MarketHealthScores()
        assert scores.growth == 50
        assert scores.competition == 50
        assert scores.accessibility == 50

    def test_game_percentiles_all_none(self):
        """GamePercentiles can be instantiated with all None values."""
        gp = GamePercentiles()
        assert gp.revenue is None
        assert gp.review_score is None
        assert gp.median_playtime is None
        assert gp.ccu is None
        assert gp.followers is None
        assert gp.copies_sold is None
        assert gp.review_count is None

    def test_evaluate_game_result_defaults(self):
        """EvaluateGameResult has correct default values."""
        r = EvaluateGameResult(appid=1, name="Test")
        assert r.appid == 1
        assert r.name == "Test"
        assert r.genre == ""
        assert r.position_score == 50.0
        assert r.potential_score == 50.0
        assert r.opportunity_score == 50.0
        assert r.strengths == []
        assert r.weaknesses == []
        assert r.competitors == []
        assert r.tag_insights == []

    def test_compute_timing_info(self):
        """ComputeTimingInfo stores timing data correctly."""
        t = ComputeTimingInfo(total_ms=250, per_metric={"concentration": 50}, data_fetch_ms=100)
        assert t.total_ms == 250
        assert t.per_metric["concentration"] == 50
        assert t.data_fetch_ms == 100


# ---------------------------------------------------------------------------
# TestPercentileRank — 6 tests
# ---------------------------------------------------------------------------

class TestPercentileRank:
    def test_basic_middle(self):
        """50 in a 10-100 population: 4 values strictly below -> 40.0%."""
        population = list(range(10, 110, 10))  # [10, 20, 30, 40, 50, 60, 70, 80, 90, 100]
        result = _percentile_rank(50, population)
        assert result == 40.0  # 4 values < 50: [10,20,30,40]

    def test_top_value(self):
        """Top value (100) in 10..100: 9 values strictly below -> 90.0%."""
        population = list(range(10, 110, 10))
        result = _percentile_rank(100, population)
        assert result == 90.0

    def test_bottom_value(self):
        """Minimum value (10) in 10..100: 0 values strictly below -> 0.0%."""
        population = list(range(10, 110, 10))
        result = _percentile_rank(10, population)
        assert result == 0.0

    def test_none_value(self):
        """None value returns None."""
        result = _percentile_rank(None, [10, 20, 30])
        assert result is None

    def test_empty_population(self):
        """Empty population returns None."""
        result = _percentile_rank(50, [])
        assert result is None

    def test_single_value_population(self):
        """Single-element population: value == that element -> 0.0% (none below)."""
        result = _percentile_rank(42, [42])
        assert result == 0.0


# ---------------------------------------------------------------------------
# TestStdDeviations — 6 tests
# ---------------------------------------------------------------------------

class TestStdDeviations:
    def test_above_mean(self):
        """Value above mean returns positive deviation."""
        pop = [10.0, 20.0, 30.0, 40.0, 50.0]
        # mean=30, stdev ~ 15.81
        result = _std_deviations_from_mean(50.0, pop)
        assert result is not None
        assert result > 0

    def test_below_mean(self):
        """Value below mean returns negative deviation."""
        pop = [10.0, 20.0, 30.0, 40.0, 50.0]
        result = _std_deviations_from_mean(10.0, pop)
        assert result is not None
        assert result < 0

    def test_at_mean(self):
        """Value at mean returns ~0.0."""
        pop = [10.0, 20.0, 30.0, 40.0, 50.0]
        result = _std_deviations_from_mean(30.0, pop)
        assert result is not None
        assert abs(result) < 0.01

    def test_none_value(self):
        """None value returns None."""
        result = _std_deviations_from_mean(None, [1.0, 2.0, 3.0])
        assert result is None

    def test_insufficient_data(self):
        """Single-element population returns None (need >= 2)."""
        result = _std_deviations_from_mean(5.0, [5.0])
        assert result is None

    def test_zero_stdev(self):
        """Population with zero stdev (all same values) returns 0.0."""
        pop = [5.0, 5.0, 5.0, 5.0, 5.0]
        result = _std_deviations_from_mean(5.0, pop)
        assert result == 0.0


# ---------------------------------------------------------------------------
# TestDescriptiveStats — 3 tests
# ---------------------------------------------------------------------------

class TestDescriptiveStats:
    def test_basic_stats(self):
        """Computes mean/median/stdev/min/max for all four fields."""
        games = make_games(20)
        stats = _compute_descriptive_stats(games)
        assert "revenue" in stats
        assert "price" in stats
        assert "review_score" in stats
        assert "ccu" in stats
        # Revenue stats should all be non-None with 20 games
        rev = stats["revenue"]
        assert rev["mean"] is not None
        assert rev["median"] is not None
        assert rev["stdev"] is not None
        assert rev["count"] == 20
        assert rev["coverage_pct"] == 100.0

    def test_partial_coverage(self):
        """Fields with some None values compute partial coverage_pct."""
        games = make_games(10)
        # Set ccu to None for half the games
        for g in games[:5]:
            g["ccu"] = None
        stats = _compute_descriptive_stats(games)
        ccu_stats = stats["ccu"]
        assert ccu_stats["count"] == 5
        assert ccu_stats["coverage_pct"] == 50.0

    def test_empty_field(self):
        """Field with all None values returns zeros and None stats."""
        games = make_games(5)
        for g in games:
            g["ccu"] = None
        stats = _compute_descriptive_stats(games)
        ccu_stats = stats["ccu"]
        assert ccu_stats["count"] == 0
        assert ccu_stats["mean"] is None
        assert ccu_stats["coverage_pct"] == 0.0


# ---------------------------------------------------------------------------
# TestTagFrequency — 6 tests
# ---------------------------------------------------------------------------

class TestTagFrequency:
    def test_basic_frequency(self):
        """Tags are counted and returned sorted by count descending."""
        games = make_games(10)
        result = _compute_tag_frequency(games)
        assert len(result) > 0
        # Should be sorted by count descending
        counts = [entry["count"] for entry in result]
        assert counts == sorted(counts, reverse=True)

    def test_excludes_primary_tags(self):
        """Primary tags are excluded from the result."""
        games = make_games(10)
        result = _compute_tag_frequency(games, primary_tags={"Action"})
        tag_names = [entry["tag"] for entry in result]
        assert "Action" not in tag_names

    def test_dict_tags(self):
        """Dict tags (tag->weight) are handled correctly."""
        games = [
            {"appid": 1, "tags": {"Action": 100, "Indie": 80}},
            {"appid": 2, "tags": {"Action": 90, "RPG": 60}},
        ]
        result = _compute_tag_frequency(games)
        action_entry = next((e for e in result if e["tag"] == "Action"), None)
        assert action_entry is not None
        assert action_entry["count"] == 2

    def test_list_tags(self):
        """List tags are handled correctly."""
        games = [
            {"appid": 1, "tags": ["Action", "Indie"]},
            {"appid": 2, "tags": ["Action", "RPG"]},
        ]
        result = _compute_tag_frequency(games)
        action_entry = next((e for e in result if e["tag"] == "Action"), None)
        assert action_entry is not None
        assert action_entry["count"] == 2

    def test_empty_games(self):
        """Empty game list returns empty result."""
        result = _compute_tag_frequency([])
        assert result == []

    def test_top_n_limit(self):
        """top_n parameter limits the result count."""
        games = make_games(20)
        result = _compute_tag_frequency(games, top_n=2)
        assert len(result) <= 2


# ---------------------------------------------------------------------------
# TestAnalysisCache — 5 tests
# ---------------------------------------------------------------------------

class TestAnalysisCache:
    def setup_method(self):
        """Clear cache before each test."""
        clear_analysis_cache()

    def test_set_and_get(self):
        """Cache set then get returns the stored value."""
        key = "test_key_1"
        data = {"market_label": "Test", "total_games": 50}
        _cache_set(key, data)
        result = _cache_get(key)
        assert result == data

    def test_expired_entry(self, monkeypatch):
        """Expired cache entry returns None and is removed."""
        key = "test_key_exp"
        data = {"market_label": "Expired"}
        _cache_set(key, data)
        # Monkey-patch time.monotonic to simulate expiry
        future_time = time.monotonic() + ANALYSIS_CACHE_TTL + 1
        monkeypatch.setattr("src.market_tools.time.monotonic", lambda: future_time)
        result = _cache_get(key)
        assert result is None
        # Should have been deleted from cache
        assert key not in _analysis_cache

    def test_miss(self):
        """Cache miss returns None."""
        result = _cache_get("nonexistent_key_xyz")
        assert result is None

    def test_key_deterministic(self):
        """Same tag and params always produce the same cache key."""
        key1 = _get_analysis_cache_key("action", {"metrics": ["concentration"]})
        key2 = _get_analysis_cache_key("action", {"metrics": ["concentration"]})
        assert key1 == key2

    def test_clear(self):
        """clear_analysis_cache removes all entries."""
        _cache_set("a", {"x": 1})
        _cache_set("b", {"y": 2})
        clear_analysis_cache()
        assert _cache_get("a") is None
        assert _cache_get("b") is None


# ---------------------------------------------------------------------------
# TestGamalyticQuota — 4 tests
# ---------------------------------------------------------------------------

class TestGamalyticQuota:
    def setup_method(self):
        """Reset counter before each test."""
        reset_gamalytic_counter()

    def test_below_threshold(self):
        """Below soft threshold returns None."""
        result = increment_gamalytic_counter(10)
        assert result is None
        assert get_gamalytic_call_count() == 10

    def test_at_soft_threshold(self):
        """At soft threshold returns soft warning string."""
        increment_gamalytic_counter(GAMALYTIC_QUOTA_WARN_THRESHOLD - 1)
        result = increment_gamalytic_counter(1)
        assert result is not None
        assert "Approaching" in result
        assert str(GAMALYTIC_QUOTA_WARN_THRESHOLD) in result

    def test_above_hard_threshold(self):
        """Above hard threshold returns hard warning string."""
        increment_gamalytic_counter(GAMALYTIC_QUOTA_HARD_WARN - 1)
        result = increment_gamalytic_counter(1)
        assert result is not None
        assert "WARNING" in result

    def test_reset(self):
        """reset_gamalytic_counter resets to zero."""
        increment_gamalytic_counter(300)
        assert get_gamalytic_call_count() == 300
        reset_gamalytic_counter()
        assert get_gamalytic_call_count() == 0


# ---------------------------------------------------------------------------
# TestRunMarketAnalysis — 16 tests
# ---------------------------------------------------------------------------

class TestRunMarketAnalysis:
    def test_insufficient_games_returns_error(self):
        """15 games (below threshold of 20) returns an error dict."""
        games = make_games(15)
        result = _run_market_analysis(games, tags=["Action"])
        assert "error" in result
        assert "Insufficient" in result["error"]
        assert result["total_games"] == 15

    def test_minimum_20_games_succeeds(self):
        """Exactly 20 games returns a valid result (no 'error' key)."""
        games = make_games(20)
        result = _run_market_analysis(games, tags=["Action"])
        assert "error" not in result
        assert result["total_games"] == 20

    def test_full_50_games_all_metrics(self):
        """50 games runs all 10 analytics metrics."""
        games = make_games(50)
        result = _run_market_analysis(games, tags=["Action"])
        assert "error" not in result
        assert result["total_games"] == 50
        # With 50 games, concentration should be computed
        assert result["concentration"] is not None

    def test_metrics_filter(self):
        """Specifying metrics=['concentration'] only computes that metric."""
        games = make_games(50)
        result = _run_market_analysis(games, tags=["Action"], metrics=["concentration"])
        assert result["concentration"] is not None
        # Other metrics should be skipped
        # Some will be in skipped_metrics as "not requested"
        skipped_names = [s["metric"] for s in result["skipped_metrics"]]
        assert "temporal_trends" in skipped_names

    def test_exclude_filter(self):
        """Excluding a metric marks it as skipped."""
        games = make_games(50)
        result = _run_market_analysis(games, tags=["Action"], exclude=["concentration"])
        assert result["concentration"] is None
        skipped_names = [s["metric"] for s in result["skipped_metrics"]]
        assert "concentration" in skipped_names

    def test_top_games_count(self):
        """top_n parameter controls number of top games returned."""
        games = make_games(50)
        result = _run_market_analysis(games, tags=["Action"], top_n=5)
        assert len(result["top_games"]) == 5

    def test_top_games_sorted_by_revenue(self):
        """Top games are sorted by revenue descending."""
        games = make_games(50)
        result = _run_market_analysis(games, tags=["Action"], top_n=5)
        revenues = [g["revenue"] for g in result["top_games"] if g.get("revenue") is not None]
        assert revenues == sorted(revenues, reverse=True)

    def test_skipped_metrics_present(self):
        """Skipped metrics list is present in result (empty when all metrics unlock at N>=20)."""
        games = make_games(20)  # N=20 unlocks all metrics -> no skipped metrics
        result = _run_market_analysis(games, tags=["Action"])
        assert "skipped_metrics" in result
        assert isinstance(result["skipped_metrics"], list)
        # With single-tier gate at N>=20, all metrics are available; skipped list is empty
        assert len(result["skipped_metrics"]) == 0

    def test_skipped_has_reason_when_excluded(self):
        """Each skipped metric (via exclude param) has a non-empty reason string."""
        games = make_games(20)
        result = _run_market_analysis(games, tags=["Action"], exclude=["temporal_trends"])
        for skipped in result["skipped_metrics"]:
            assert "reason" in skipped
            assert len(skipped["reason"]) > 0

    def test_coverage_pct(self):
        """coverage_pct reflects the fraction of games with revenue data."""
        games = make_games(20)
        # Remove revenue from half the games
        for g in games[:10]:
            g["revenue"] = None
        result = _run_market_analysis(games, tags=["Action"])
        assert "error" not in result
        assert result["games_with_revenue"] == 10
        assert result["coverage_pct"] == 50.0

    def test_health_scores_present(self):
        """health_scores dict is always present in result."""
        games = make_games(20)
        result = _run_market_analysis(games, tags=["Action"])
        assert "health_scores" in result
        assert result["health_scores"] is not None
        health = result["health_scores"]
        assert "growth" in health
        assert "competition" in health
        assert "accessibility" in health

    def test_include_raw(self):
        """include_raw=True attaches raw game dicts to result."""
        games = make_games(20)
        result = _run_market_analysis(games, tags=["Action"], include_raw=True)
        assert result["raw_data"] is not None
        assert len(result["raw_data"]) == 20

    def test_custom_label(self):
        """market_label parameter sets the label in result."""
        games = make_games(20)
        result = _run_market_analysis(games, tags=["Action"], market_label="My Market")
        assert result["market_label"] == "My Market"

    def test_auto_label_from_tags(self):
        """Without market_label, label is derived from tags."""
        games = make_games(20)
        result = _run_market_analysis(games, tags=["Action", "Roguelike"])
        assert "Action" in result["market_label"]
        assert "Roguelike" in result["market_label"]

    def test_timing_present(self):
        """compute_time is included in the result."""
        games = make_games(20)
        result = _run_market_analysis(games, tags=["Action"])
        assert "compute_time" in result
        ct = result["compute_time"]
        assert ct is not None
        assert "total_ms" in ct
        assert ct["total_ms"] >= 0

    def test_methodology_present(self):
        """methodology dict is present and has key fields."""
        games = make_games(20)
        result = _run_market_analysis(games, tags=["Action"])
        assert "methodology" in result
        m = result["methodology"]
        assert "data_source" in m
        assert "total_games_analyzed" in m
        assert m["total_games_analyzed"] == 20


# ---------------------------------------------------------------------------
# TestHealthScores — 7 tests
# ---------------------------------------------------------------------------

class TestHealthScores:
    def _make_analysis(self, concentration=None, temporal=None, price_brackets=None, success_rates=None):
        """Helper: build minimal analysis dict for _compute_health_scores."""
        return {
            "concentration": concentration,
            "temporal_trends": temporal,
            "price_brackets": price_brackets,
            "success_rates": success_rates,
        }

    def test_monopoly_low_competition(self):
        """High Gini (monopoly) produces low competition score."""
        analysis = self._make_analysis(
            concentration={"gini": 0.9, "top_shares": {}}
        )
        scores = _compute_health_scores(analysis)
        assert scores.competition <= 15  # (1 - 0.9) * 100 = 10

    def test_distributed_high_competition(self):
        """Low Gini (distributed) produces high competition score."""
        analysis = self._make_analysis(
            concentration={"gini": 0.1, "top_shares": {}}
        )
        scores = _compute_health_scores(analysis)
        assert scores.competition >= 85  # (1 - 0.1) * 100 = 90

    def test_growing_market_high_growth(self):
        """Recent years with higher avg_revenue produce growth > 50."""
        yearly = [
            {"year": 2018, "avg_revenue": 10000, "game_count": 10, "partial": False},
            {"year": 2019, "avg_revenue": 12000, "game_count": 11, "partial": False},
            {"year": 2022, "avg_revenue": 50000, "game_count": 20, "partial": False},
            {"year": 2023, "avg_revenue": 60000, "game_count": 25, "partial": False},
        ]
        analysis = self._make_analysis(
            temporal={"yearly": yearly}
        )
        scores = _compute_health_scores(analysis)
        assert scores.growth > 50

    def test_declining_market_low_growth(self):
        """Recent years with lower avg_revenue produce growth < 50."""
        yearly = [
            {"year": 2018, "avg_revenue": 80000, "game_count": 30, "partial": False},
            {"year": 2019, "avg_revenue": 70000, "game_count": 28, "partial": False},
            {"year": 2022, "avg_revenue": 20000, "game_count": 10, "partial": False},
            {"year": 2023, "avg_revenue": 15000, "game_count": 8, "partial": False},
        ]
        analysis = self._make_analysis(
            temporal={"yearly": yearly}
        )
        scores = _compute_health_scores(analysis)
        assert scores.growth < 50

    def test_accessible_market(self):
        """High affordable % + good success rate produces accessibility > 50."""
        analysis = self._make_analysis(
            price_brackets={
                "brackets": [
                    {"label": "f2p", "count": 20},
                    {"label": "0.01_4.99", "count": 20},
                    {"label": "5_9.99", "count": 10},
                    {"label": "25_29.99", "count": 5},
                ]
            },
            success_rates={
                "thresholds": {"100k": 40.0}  # 40% success rate
            }
        )
        scores = _compute_health_scores(analysis)
        assert scores.accessibility > 50

    def test_no_data_neutral(self):
        """No analysis data produces all neutral scores (50)."""
        analysis = self._make_analysis()
        scores = _compute_health_scores(analysis)
        assert scores.growth == 50
        assert scores.competition == 50
        assert scores.accessibility == 50

    def test_clamped_0_100(self):
        """Extreme values are clamped to 0-100 range."""
        # Extremely high success rate
        analysis = self._make_analysis(
            price_brackets={
                "brackets": [
                    {"label": "f2p", "count": 1000},
                ]
            },
            success_rates={"thresholds": {"100k": 100.0}},
            concentration={"gini": 0.0},
        )
        scores = _compute_health_scores(analysis)
        assert 0 <= scores.growth <= 100
        assert 0 <= scores.competition <= 100
        assert 0 <= scores.accessibility <= 100


# ---------------------------------------------------------------------------
# TestBuildGameProfile — 3 tests
# ---------------------------------------------------------------------------

class TestBuildGameProfile:
    def test_full_dict(self):
        """All game dict fields are mapped to GameProfile correctly."""
        game = {
            "appid": 42,
            "name": "Test Game",
            "revenue": 123456.0,
            "review_score": 85.0,
            "release_date": "2021-03-15",
            "owners": 20000.0,
            "ccu": 500,
            "median_forever": 10.5,
            "followers": 1500,
            "copies_sold": 5000,
            "price": 14.99,
            "tags": {"Action": 100, "Indie": 80},
            "developer": "DevCo",
            "publisher": "PubCo",
        }
        profile = _build_game_profile(game)
        assert profile.appid == 42
        assert profile.name == "Test Game"
        assert profile.revenue == 123456.0
        assert profile.review_score == 85.0
        assert profile.release_date == "2021-03-15"
        assert profile.owners == 20000.0
        assert profile.ccu == 500
        assert profile.median_playtime == 10.5  # median_forever -> median_playtime
        assert profile.followers == 1500
        assert profile.copies_sold == 5000
        assert profile.price == 14.99
        assert profile.tags == {"Action": 100, "Indie": 80}
        assert profile.developer == "DevCo"
        assert profile.publisher == "PubCo"

    def test_minimal_dict(self):
        """Minimal dict (only appid) maps safely with None/defaults."""
        game = {"appid": 99}
        profile = _build_game_profile(game)
        assert profile.appid == 99
        assert profile.name == ""
        assert profile.revenue is None
        assert profile.tags == {}

    def test_median_forever_mapping(self):
        """median_forever in game dict maps to median_playtime in GameProfile."""
        game = {"appid": 1, "median_forever": 42.5}
        profile = _build_game_profile(game)
        # The field mapping: game["median_forever"] -> profile.median_playtime
        assert profile.median_playtime == 42.5


# ---------------------------------------------------------------------------
# TestBulkEnrichmentCrossValidation — 4 tests for _enrich_with_gamalytic_bulk
# ---------------------------------------------------------------------------

class TestBulkEnrichmentCrossValidation:
    """Test _enrich_with_gamalytic_bulk cross-validation and two-tier enrichment."""

    @pytest.mark.asyncio
    async def test_bulk_enrichment_high_overlap_uses_bulk(self):
        """When >=60% of top-10 SteamSpy AppIDs match Gamalytic, uses bulk merge."""
        from unittest.mock import AsyncMock, MagicMock
        from src.market_tools import _enrich_with_gamalytic_bulk

        mock_gamalytic = MagicMock()
        # Gamalytic returns games that include appids 1-8 (8/10 = 80% overlap)
        gamalytic_games = [
            {"steamId": i, "name": f"Game{i}", "revenue": 1000000 * i, "copiesSold": 50000 * i}
            for i in range(1, 301)
        ]
        mock_gamalytic.list_games_by_tag = AsyncMock(return_value=_with_meta(gamalytic_games))
        # Tier 2 mocks (get_commercial_batch returns empty for simplicity)
        mock_gamalytic.get_commercial_batch = AsyncMock(return_value=[])

        # SteamSpy games: appids 1-30 (top 10 by owners = appids 1-10)
        steamspy_games = [
            {"appid": i, "name": f"Game{i}", "owners": 10000 * (31 - i), "revenue": None}
            for i in range(1, 31)
        ]

        enriched, warnings, meta = await _enrich_with_gamalytic_bulk(
            steamspy_games, mock_gamalytic, primary_tag="Roguelike"
        )

        assert meta["method"] == "bulk"
        assert meta["match_rate"] >= 0.6
        assert meta["matched"] > 0
        assert meta["tag_used"] == "Roguelike"
        # Check that revenue was merged for matched games
        game1 = next(g for g in enriched if g["appid"] == 1)
        assert game1.get("revenue") is not None

    @pytest.mark.asyncio
    async def test_tier2_enriches_revenue_significant_games(self):
        """Tier 2 fires for games exceeding 0.1% of total genre revenue."""
        from unittest.mock import AsyncMock, MagicMock
        from src.market_tools import _enrich_with_gamalytic_bulk, GAMALYTIC_REVENUE_THRESHOLD_PCT
        from src.schemas import CommercialData
        from datetime import datetime, timezone

        mock_gamalytic = MagicMock()
        # Gamalytic bulk: game 1 has 90% of revenue, games 2-10 have 10% split
        gamalytic_games = [
            {"steamId": 1, "name": "BigGame", "revenue": 90_000_000, "copiesSold": 5_000_000},
        ] + [
            {"steamId": i, "name": f"SmallGame{i}", "revenue": 1_000_000, "copiesSold": 50000}
            for i in range(2, 11)
        ]
        mock_gamalytic.list_games_by_tag = AsyncMock(return_value=_with_meta(gamalytic_games))

        # Tier 2: get_commercial_batch returns full data for qualifying games
        mock_gamalytic.get_commercial_batch = AsyncMock(return_value=[
            CommercialData(
                appid=1, name="BigGame", price=29.99,
                revenue_min=72_000_000, revenue_max=108_000_000,
                confidence="high", source="gamalytic",
                fetched_at=datetime.now(timezone.utc),
                followers=500_000,
            )
        ])

        # Mock SteamSpy for tag weights in Tier 2
        from src.schemas import EngagementData
        mock_steamspy = MagicMock()
        mock_steamspy.get_engagement_data = AsyncMock(return_value=EngagementData(
            appid=1, name="BigGame", ccu=5000, owners_midpoint=5000000.0,
            average_forever=50.0, average_2weeks=10.0,
            positive=50000, negative=2000, price=29.99,
            review_score=96.2, tags={"Roguelike": 1000, "Action": 800},
            owners_min=4000000, owners_max=6000000,
            median_forever=40.0, median_2weeks=8.0,
            developer="Dev", publisher="Pub",
            fetched_at=datetime.now(timezone.utc),
            total_reviews=52000,
        ))

        steamspy_games = [
            {"appid": 1, "name": "BigGame", "owners": 5000000, "revenue": None, "tags": {}},
        ] + [
            {"appid": i, "name": f"SmallGame{i}", "owners": 50000, "revenue": None, "tags": {}}
            for i in range(2, 11)
        ]

        enriched, warnings, meta = await _enrich_with_gamalytic_bulk(
            steamspy_games, mock_gamalytic, steamspy=mock_steamspy, primary_tag="Roguelike"
        )

        assert meta["method"] == "bulk"
        assert meta["tier2_count"] >= 1
        assert meta["total_revenue"] > 0
        # BigGame should have Tier 2 deep fields
        big_game = next(g for g in enriched if g["appid"] == 1)
        assert big_game.get("followers") == 500_000  # from CommercialData
        assert big_game.get("tags") == {"Roguelike": 1000, "Action": 800}  # from SteamSpy

    @pytest.mark.asyncio
    async def test_bulk_enrichment_low_overlap_falls_back(self):
        """When <60% overlap, falls back to per-game enrichment."""
        from unittest.mock import AsyncMock, MagicMock
        from src.market_tools import _enrich_with_gamalytic_bulk
        from src.schemas import CommercialData
        from datetime import datetime, timezone

        mock_gamalytic = MagicMock()
        # Gamalytic returns games with completely different AppIDs (0% overlap)
        gamalytic_games = [
            {"steamId": 9000 + i, "name": f"OtherGame{i}", "revenue": 500000}
            for i in range(100)
        ]
        mock_gamalytic.list_games_by_tag = AsyncMock(return_value=_with_meta(gamalytic_games))
        # Per-game fallback mock
        mock_gamalytic.get_commercial_data = AsyncMock(return_value=CommercialData(
            appid=1, name="Test", price=9.99,
            revenue_min=100000, revenue_max=500000,
            confidence="medium", source="gamalytic",
            fetched_at=datetime.now(timezone.utc),
        ))

        steamspy_games = [
            {"appid": i, "name": f"Game{i}", "owners": 10000 * (31 - i), "revenue": None}
            for i in range(1, 31)
        ]

        enriched, warnings, meta = await _enrich_with_gamalytic_bulk(
            steamspy_games, mock_gamalytic, primary_tag="Rogue-like"
        )

        assert meta["method"] == "per_game_fallback"
        assert meta["match_rate"] < 0.6
        assert any("mismatch" in w.lower() or "fallback" in w.lower() for w in warnings)

    @pytest.mark.asyncio
    async def test_no_tag_uses_per_game(self):
        """When no primary_tag, uses per-game enrichment directly."""
        from unittest.mock import AsyncMock, MagicMock
        from src.market_tools import _enrich_with_gamalytic_bulk
        from src.schemas import CommercialData
        from datetime import datetime, timezone

        mock_gamalytic = MagicMock()
        mock_gamalytic.get_commercial_data = AsyncMock(return_value=CommercialData(
            appid=1, name="Test", price=9.99,
            revenue_min=100000, revenue_max=500000,
            confidence="medium", source="gamalytic",
            fetched_at=datetime.now(timezone.utc),
        ))

        steamspy_games = [
            {"appid": i, "name": f"Game{i}", "owners": 5000, "revenue": None}
            for i in range(1, 6)
        ]

        enriched, warnings, meta = await _enrich_with_gamalytic_bulk(
            steamspy_games, mock_gamalytic, primary_tag=None
        )

        assert meta["method"] == "per_game"
        # get_commercial_data should have been called (per-game path)
        assert mock_gamalytic.get_commercial_data.call_count == 5


# ---------------------------------------------------------------------------
# TestAnalyzeMarketSingle — 2 tests for analyze_market_single
# ---------------------------------------------------------------------------

class TestAnalyzeMarketSingle:
    """Test single-phase analyze_market_single function."""

    @pytest.mark.asyncio
    async def test_enrichment_metadata_in_result(self):
        """analyze_market_single returns enrichment stats with two-tier info."""
        from unittest.mock import AsyncMock, MagicMock
        from src.schemas import GameSummary, SearchResult
        from src.market_tools import analyze_market_single

        mock_steamspy = MagicMock()
        mock_steam_store = MagicMock()
        mock_gamalytic = MagicMock()
        # Must be AsyncMock: Tier 2 calls asyncio.gather(*[steamspy.get_engagement_data(id)...])
        mock_steamspy.get_engagement_data = AsyncMock(return_value=None)

        # SteamSpy returns 30 games
        games = [
            GameSummary(
                appid=i, name=f"Game {i}", developer="Dev", publisher="Pub",
                ccu=100, owners_min=1000*(31-i), owners_max=5000*(31-i),
                owners_midpoint=3000.0*(31-i), average_forever=10.0, average_2weeks=1.0,
                median_forever=8.0, median_2weeks=0.5, positive=200, negative=10, price=9.99,
            )
            for i in range(1, 31)
        ]
        search_result = SearchResult(
            tag="Roguelike", appids=[g.appid for g in games], total_found=30,
            games=games, data_source="steamspy",
            fetched_at="2026-01-01T00:00:00Z", cache_age_seconds=0,
        )
        mock_steamspy.search_by_tag = AsyncMock(return_value=search_result)

        # Gamalytic bulk returns matching games (high overlap)
        gamalytic_games = [
            {"steamId": i, "name": f"Game {i}", "revenue": 1000000 * (31 - i), "copiesSold": 50000}
            for i in range(1, 31)
        ]
        mock_gamalytic.list_games_by_tag = AsyncMock(return_value=_with_meta(gamalytic_games))
        mock_gamalytic.get_commercial_batch = AsyncMock(return_value=[])

        result = await analyze_market_single(
            steamspy=mock_steamspy, steam_store=mock_steam_store,
            gamalytic=mock_gamalytic, tags=["Roguelike"],
        )

        assert "enrichment" in result
        assert result["enrichment"]["method"] == "bulk"
        assert result["enrichment"]["gamalytic_matched"] > 0
        assert "tier2_count" in result["enrichment"]
        assert "total_revenue" in result["enrichment"]
        assert "continuation_token" not in result

    @pytest.mark.asyncio
    async def test_no_continuation_token_in_result(self):
        """Single-phase result never contains continuation_token."""
        from unittest.mock import AsyncMock, MagicMock
        from src.schemas import GameSummary, SearchResult
        from src.market_tools import analyze_market_single

        mock_steamspy = MagicMock()
        mock_steam_store = MagicMock()
        mock_gamalytic = MagicMock()
        # Must be AsyncMock: Tier 2 calls asyncio.gather(*[steamspy.get_engagement_data(id)...])
        mock_steamspy.get_engagement_data = AsyncMock(return_value=None)

        games = [
            GameSummary(
                appid=i, name=f"Game {i}", developer="Dev", publisher="Pub",
                ccu=100, owners_min=1000, owners_max=5000, owners_midpoint=3000.0,
                average_forever=10.0, average_2weeks=1.0, median_forever=8.0,
                median_2weeks=0.5, positive=200, negative=10, price=9.99,
            )
            for i in range(1, 31)
        ]
        mock_steamspy.search_by_tag = AsyncMock(return_value=SearchResult(
            tag="Indie", appids=[g.appid for g in games], total_found=30,
            games=games, data_source="steamspy",
            fetched_at="2026-01-01T00:00:00Z", cache_age_seconds=0,
        ))
        _inline_games = [
            {"steamId": i, "name": f"Game {i}", "revenue": 100000} for i in range(1, 31)
        ]
        mock_gamalytic.list_games_by_tag = AsyncMock(return_value=_with_meta(_inline_games))
        mock_gamalytic.get_commercial_batch = AsyncMock(return_value=[])

        result = await analyze_market_single(
            steamspy=mock_steamspy, steam_store=mock_steam_store,
            gamalytic=mock_gamalytic, tags=["Indie"],
        )

        assert "continuation_token" not in result
        assert "error" not in result


# ---------------------------------------------------------------------------
# TestOpportunityScoreRework
# ---------------------------------------------------------------------------

class TestOpportunityScoreRework:
    """Opportunity score rewards top performers."""

    def test_top_performer_scores_high(self):
        """A game with 90+ position in growing market should get 80+ opportunity."""
        from src.market_tools import _compute_opportunity_score

        percentiles = GamePercentiles(
            revenue=95.0, review_score=90.0, median_playtime=88.0,
            ccu=92.0, followers=None, copies_sold=None, review_count=85.0,
        )
        health = MarketHealthScores(
            competition=60.0, growth=65.0, saturation=40.0,
            accessibility=55.0, overall=60.0,
        )
        position, potential, opportunity = _compute_opportunity_score(
            percentiles, ["top_revenue", "top_reviews", "high_playtime", "strong_ccu"],
            [], health
        )
        assert position >= 85.0
        assert opportunity >= 80.0, f"Top performer got {opportunity}, expected 80+"

    def test_mid_pack_moderate_opportunity(self):
        """A mid-range game in neutral market should get 45-65 opportunity."""
        from src.market_tools import _compute_opportunity_score

        percentiles = GamePercentiles(
            revenue=50.0, review_score=50.0, median_playtime=50.0,
            ccu=50.0, followers=None, copies_sold=None, review_count=50.0,
        )
        health = MarketHealthScores(
            competition=50.0, growth=50.0, saturation=50.0,
            accessibility=50.0, overall=50.0,
        )
        position, potential, opportunity = _compute_opportunity_score(
            percentiles, ["one_strength"], ["one_weakness"], health
        )
        assert 45.0 <= opportunity <= 65.0, f"Mid-pack got {opportunity}, expected 45-65"


# ---------------------------------------------------------------------------
# TestCustomBaselineBypass
# ---------------------------------------------------------------------------

class TestCustomBaselineBypass:
    """Custom baselines bypass MIN_GAMES_THRESHOLD via skip_min_check."""

    def test_skip_min_check_allows_small_list(self):
        """_run_market_analysis with skip_min_check=True accepts <20 games."""
        games = make_games(5)
        result = _run_market_analysis(games, tags=["test"], skip_min_check=True)
        assert "error" not in result
        assert result["total_games"] == 5

    def test_without_skip_min_check_rejects_small_list(self):
        """_run_market_analysis without skip_min_check rejects <20 games."""
        games = make_games(5)
        result = _run_market_analysis(games, tags=["test"])
        assert "error" in result


# ---------------------------------------------------------------------------
# TestGenreMembershipValidation
# ---------------------------------------------------------------------------

class TestGenreMembershipValidation:
    """Test _validate_genre_membership in-memory tag validation."""

    def test_validates_games_with_tag_data(self):
        """Games with SteamSpy tag weights are validated against genre tag."""
        from src.market_tools import _validate_genre_membership

        games = [
            {
                "appid": 1, "name": "RogueHit", "revenue": 50_000_000,
                "tags": {"Rogue-like": 1000, "Action": 800, "Indie": 600},
            },
            {
                "appid": 2, "name": "NotRogue", "revenue": 30_000_000,
                "tags": {"Puzzle": 1000, "Casual": 800, "Match 3": 600},
            },
            {
                "appid": 3, "name": "SmallGame", "revenue": 100,
                "tags": {"RPG": 500},
            },
        ]

        flags = _validate_genre_membership(games, "Rogue-like")

        # Total revenue ~80M + 100. Threshold = 80000100 * 0.001 = ~80000
        # Game 1 (50M) and Game 2 (30M) exceed threshold; Game 3 (100) does not
        assert len(flags) == 2

        rogue_flag = next(f for f in flags if f["appid"] == 1)
        assert rogue_flag["genre_valid"] is True
        assert rogue_flag["tag_rank"] == 1  # "Rogue-like" is highest-weighted tag

        not_rogue_flag = next(f for f in flags if f["appid"] == 2)
        assert not_rogue_flag["genre_valid"] is False
        assert not_rogue_flag["tag_rank"] is None

    def test_skips_games_without_tag_data(self):
        """Games with empty tags dict (no Tier 2 enrichment) are skipped, not flagged."""
        from src.market_tools import _validate_genre_membership

        games = [
            {"appid": 1, "name": "NoTags", "revenue": 50_000_000, "tags": {}},
            {"appid": 2, "name": "NullTags", "revenue": 30_000_000, "tags": None},
            {"appid": 3, "name": "HasTags", "revenue": 20_000_000, "tags": {"Rogue-like": 500}},
        ]

        flags = _validate_genre_membership(games, "Rogue-like")

        # Only game 3 has tag data and exceeds threshold
        assert len(flags) == 1
        assert flags[0]["appid"] == 3
        assert flags[0]["genre_valid"] is True

    def test_case_insensitive_tag_matching(self):
        """Genre tag matching is case-insensitive."""
        from src.market_tools import _validate_genre_membership

        games = [
            {"appid": 1, "name": "Game1", "revenue": 1_000_000,
             "tags": {"rogue-like": 1000, "action": 800}},
        ]

        flags = _validate_genre_membership(games, "Rogue-like")
        assert len(flags) == 1
        assert flags[0]["genre_valid"] is True

    def test_returns_empty_for_no_revenue(self):
        """Returns empty list when total revenue is zero."""
        from src.market_tools import _validate_genre_membership

        games = [
            {"appid": 1, "name": "Free", "revenue": 0, "tags": {"Rogue-like": 500}},
        ]

        flags = _validate_genre_membership(games, "Rogue-like")
        assert flags == []

    def test_top_tags_shows_context(self):
        """Validation flags include top 5 tags for context."""
        from src.market_tools import _validate_genre_membership

        games = [
            {"appid": 1, "name": "Game1", "revenue": 1_000_000,
             "tags": {"Action": 1000, "Adventure": 900, "RPG": 800, "Indie": 700,
                      "Open World": 600, "Rogue-like": 500}},
        ]

        flags = _validate_genre_membership(games, "Rogue-like")
        assert len(flags) == 1
        assert flags[0]["genre_valid"] is True
        assert flags[0]["tag_rank"] == 6  # 6th tag
        assert len(flags[0]["top_tags"]) == 5
        assert flags[0]["top_tags"][0] == "Action"

    def test_normalize_tag_matches_steamspy_format(self):
        """User input 'Roguelike' matches SteamSpy's 'Rogue-like' after normalize_tag."""
        from src.market_tools import _validate_genre_membership
        from src.steam_api import normalize_tag

        games = [
            {"appid": 1, "name": "Game1", "revenue": 1_000_000,
             "tags": {"Rogue-like": 1000, "Action": 800}},
        ]

        # Simulates the caller path: normalize_tag("Roguelike") -> "Rogue-like"
        normalized = normalize_tag("Roguelike")
        assert normalized == "Rogue-like"

        flags = _validate_genre_membership(games, normalized)
        assert len(flags) == 1
        assert flags[0]["genre_valid"] is True
        assert flags[0]["tag_rank"] == 1


# ---------------------------------------------------------------------------
# TestMultiTagErrorSurfacing
# ---------------------------------------------------------------------------

class TestMultiTagErrorSurfacing:
    """Test that invalid tags in multi-tag queries produce descriptive errors."""

    @pytest.mark.asyncio
    async def test_missing_tag_returns_error_with_names(self):
        """Multi-tag query with invalid tag returns error naming the bad tag."""
        from unittest.mock import AsyncMock, MagicMock
        from src.market_tools import analyze_market_single

        mock_steamspy = MagicMock()
        mock_steam_store = MagicMock()
        mock_gamalytic = MagicMock()

        # Steam Store tag map is missing 'Deckbuilder' but has 'Roguelike'
        mock_steam_store.get_tag_map = AsyncMock(return_value={"roguelike": 12345})
        mock_steam_store.search_by_tag_ids_paginated = AsyncMock(return_value=(0, []))

        result = await analyze_market_single(
            steamspy=mock_steamspy,
            steam_store=mock_steam_store,
            gamalytic=mock_gamalytic,
            tags=["Roguelike", "Deckbuilder"],
        )

        assert "error" in result
        assert "Deckbuilder" in result["error"]

    @pytest.mark.asyncio
    async def test_all_tags_valid_proceeds_normally(self):
        """Multi-tag query with all valid tags attempts to find games (no tag error)."""
        from unittest.mock import AsyncMock, MagicMock
        from src.market_tools import analyze_market_single

        mock_steamspy = MagicMock()
        mock_steam_store = MagicMock()
        mock_gamalytic = MagicMock()

        # Both tags exist in tag map
        mock_steam_store.get_tag_map = AsyncMock(return_value={
            "roguelike": 12345,
            "deckbuilder": 67890,
        })
        # search_by_tag_ids_paginated returns 0 results (no games match intersection)
        mock_steam_store.search_by_tag_ids_paginated = AsyncMock(return_value=(0, []))

        result = await analyze_market_single(
            steamspy=mock_steamspy,
            steam_store=mock_steam_store,
            gamalytic=mock_gamalytic,
            tags=["Roguelike", "Deckbuilder"],
        )

        # No tag error — both tags were valid. Error is "no games found" (not tag error)
        assert "error" in result
        assert "not recognized" not in result["error"]  # tag error message absent
        # The error should be about no games, not about bad tags
        assert "No games found" in result["error"] or "no games" in result["error"].lower()


# ---------------------------------------------------------------------------
# TestValidationFlagsInResult
# ---------------------------------------------------------------------------

class TestValidationFlagsInResult:
    """Test that analyze_market_single includes validation_flags when Tier 2 enrichment ran."""

    @pytest.mark.asyncio
    async def test_validation_flags_present_after_tier2(self):
        """Result includes validation_flags for games checked by _validate_genre_membership."""
        from unittest.mock import AsyncMock, MagicMock
        from src.schemas import GameSummary, SearchResult, CommercialData, EngagementData
        from src.market_tools import analyze_market_single
        from datetime import datetime, timezone

        mock_steamspy = MagicMock()
        mock_steam_store = MagicMock()
        mock_gamalytic = MagicMock()

        # 25 games, game 1 has >0.1% of revenue (will get Tier 2)
        # Need 25 games to exceed MIN_GAMES_THRESHOLD (20)
        games = [
            GameSummary(
                appid=1, name="BigGame", developer="Dev", publisher="Pub",
                ccu=5000, owners_min=4000000, owners_max=6000000,
                owners_midpoint=5000000.0, average_forever=50.0, average_2weeks=10.0,
                median_forever=40.0, median_2weeks=8.0, positive=50000, negative=2000, price=29.99,
            ),
        ] + [
            GameSummary(
                appid=i, name=f"SmallGame{i}", developer="Dev", publisher="Pub",
                ccu=100, owners_min=1000, owners_max=5000,
                owners_midpoint=3000.0, average_forever=10.0, average_2weeks=1.0,
                median_forever=8.0, median_2weeks=0.5, positive=200, negative=10, price=9.99,
            )
            for i in range(2, 26)
        ]

        mock_steamspy.search_by_tag = AsyncMock(return_value=SearchResult(
            tag="Roguelike", appids=[g.appid for g in games], total_found=25,
            games=games, data_source="steamspy",
            fetched_at="2026-01-01T00:00:00Z", cache_age_seconds=0,
        ))

        # Gamalytic bulk: game 1 dominates revenue
        gamalytic_games = [
            {"steamId": 1, "name": "BigGame", "revenue": 90_000_000, "copiesSold": 5000000},
        ] + [
            {"steamId": i, "name": f"SmallGame{i}", "revenue": 100_000, "copiesSold": 5000}
            for i in range(2, 26)
        ]
        mock_gamalytic.list_games_by_tag = AsyncMock(return_value=_with_meta(gamalytic_games))

        # Tier 2: full CommercialData for BigGame
        mock_gamalytic.get_commercial_batch = AsyncMock(return_value=[
            CommercialData(
                appid=1, name="BigGame", price=29.99,
                revenue_min=72_000_000, revenue_max=108_000_000,
                confidence="high", source="gamalytic",
                fetched_at=datetime.now(timezone.utc),
                followers=500_000,
            )
        ])

        # Tier 2: SteamSpy tag weights for BigGame
        mock_steamspy.get_engagement_data = AsyncMock(return_value=EngagementData(
            appid=1, name="BigGame", ccu=5000, owners_midpoint=5000000.0,
            average_forever=50.0, average_2weeks=10.0,
            positive=50000, negative=2000, total_reviews=52000, price=29.99,
            review_score=96.2, tags={"Rogue-like": 1000, "Action": 800, "Indie": 600},
            owners_min=4000000, owners_max=6000000,
            median_forever=40.0, median_2weeks=8.0,
            developer="Dev", publisher="Pub",
            fetched_at=datetime.now(timezone.utc),
        ))

        result = await analyze_market_single(
            steamspy=mock_steamspy, steam_store=mock_steam_store,
            gamalytic=mock_gamalytic, tags=["Roguelike"],
        )

        # Should have validation_flags because Tier 2 ran and provided tag data
        assert "validation_flags" in result
        assert len(result["validation_flags"]) >= 1
        flag = result["validation_flags"][0]
        assert flag["appid"] == 1
        assert flag["genre_valid"] is True
        assert flag["tag_rank"] == 1  # "Rogue-like" is top tag


# ---------------------------------------------------------------------------
# TestEnrichmentPipelineFixes — 3 tests for UAT gap-1 fixes
# ---------------------------------------------------------------------------

class TestEnrichmentPipelineFixes:
    """Tests for cross-validation, releaseDate conversion, and fallback release_date."""

    @pytest.mark.asyncio
    async def test_cross_validation_uses_all_appids(self):
        """Cross-validation checks ALL AppIDs so 70% dataset overlap triggers bulk merge.

        Setup:
          - 100 games with owners ascending by appid (appid 100 has most owners).
          - Gamalytic returns appids 1-70.
          - Top-10 by owners = appids 91-100: NONE in Gamalytic (old code: 0% -> fallback).
          - All-appids check: 70/100 = 0.7 (new code: 70% -> bulk).
        """
        from unittest.mock import AsyncMock, MagicMock
        from src.market_tools import _enrich_with_gamalytic_bulk

        mock_gamalytic = MagicMock()
        # Gamalytic returns appids 1-70 only (excludes the high-owner top-10 games)
        gamalytic_games = [
            {"steamId": i, "name": f"Game{i}", "revenue": 500_000, "copiesSold": 10000}
            for i in range(1, 71)
        ]
        mock_gamalytic.list_games_by_tag = AsyncMock(return_value=_with_meta(gamalytic_games))
        mock_gamalytic.get_commercial_batch = AsyncMock(return_value=[])

        # 100 SteamSpy games: owners ascending by appid (appid 100 has most owners)
        # Top-10 by owners = appids 91-100 (NOT in Gamalytic), but 70 total overlap
        steamspy_games = [
            {"appid": i, "name": f"Game{i}", "owners": i * 1000, "revenue": None, "tags": {}}
            for i in range(1, 101)
        ]

        enriched, warnings, meta = await _enrich_with_gamalytic_bulk(
            steamspy_games, mock_gamalytic, primary_tag="Roguelike"
        )

        # With full-dataset cross-validation: 70/100 = 0.7 >= BULK_MATCH_THRESHOLD (0.6)
        assert meta["method"] == "bulk", f"Expected bulk, got {meta['method']}"
        assert meta["match_rate"] == 0.7

    @pytest.mark.asyncio
    async def test_bulk_release_date_converted_to_iso(self):
        """Bulk-merged releaseDate is converted from ms epoch to ISO YYYY-MM-DD string.

        Gamalytic sends releaseDate as Unix ms timestamp (e.g., 1609459200000 = 2021-01-01).
        After bulk merge, enriched game release_date should be "2021-01-01", not the raw int.
        """
        from unittest.mock import AsyncMock, MagicMock
        from src.market_tools import _enrich_with_gamalytic_bulk

        mock_gamalytic = MagicMock()
        # 2021-01-01 UTC in ms epoch
        EPOCH_2021_01_01_MS = 1609459200000
        gamalytic_games = [
            {
                "steamId": i,
                "name": f"Game{i}",
                "revenue": 500_000,
                "copiesSold": 10000,
                "releaseDate": EPOCH_2021_01_01_MS,
            }
            for i in range(1, 71)
        ]
        mock_gamalytic.list_games_by_tag = AsyncMock(return_value=_with_meta(gamalytic_games))
        mock_gamalytic.get_commercial_batch = AsyncMock(return_value=[])

        steamspy_games = [
            {"appid": i, "name": f"Game{i}", "owners": i * 1000, "revenue": None, "tags": {}}
            for i in range(1, 101)
        ]

        enriched, warnings, meta = await _enrich_with_gamalytic_bulk(
            steamspy_games, mock_gamalytic, primary_tag="Roguelike"
        )

        assert meta["method"] == "bulk"
        # Check that games matched by Gamalytic have ISO date, not raw ms integer
        matched_games = [g for g in enriched if g.get("release_date") is not None]
        assert len(matched_games) > 0, "Expected some games to have release_date"
        for game in matched_games:
            assert game["release_date"] == "2021-01-01", (
                f"Expected ISO '2021-01-01' but got {game['release_date']!r} "
                f"(raw epoch was {EPOCH_2021_01_01_MS})"
            )

    @pytest.mark.asyncio
    async def test_per_game_fallback_populates_release_date(self):
        """Per-game fallback path (_enrich_one) populates release_date from CommercialData.

        When CommercialData.release_date is set (ISO string from Gamalytic releaseDate),
        _enrich_one merges it into the enriched game dict.
        """
        from unittest.mock import AsyncMock, MagicMock
        from src.market_tools import _enrich_with_gamalytic_bulk
        from src.schemas import CommercialData
        from datetime import datetime, timezone

        mock_gamalytic = MagicMock()
        # Bulk fetch returns nothing (empty) -> cross-validation fails -> per-game fallback
        mock_gamalytic.list_games_by_tag = AsyncMock(return_value=_with_meta([]))

        # Per-game fallback: CommercialData with release_date set
        mock_gamalytic.get_commercial_data = AsyncMock(return_value=CommercialData(
            appid=1, name="TestGame", price=14.99,
            revenue_min=200_000, revenue_max=800_000,
            confidence="medium", source="gamalytic",
            fetched_at=datetime.now(timezone.utc),
            release_date="2020-06-15",
        ))

        steamspy_games = [
            {"appid": 1, "name": "TestGame", "owners": 50000, "revenue": None, "tags": {}},
        ]

        enriched, warnings, meta = await _enrich_with_gamalytic_bulk(
            steamspy_games, mock_gamalytic, primary_tag=None
        )

        assert meta["method"] == "per_game"
        game = enriched[0]
        assert game.get("release_date") == "2020-06-15", (
            f"Expected '2020-06-15' from CommercialData fallback, got {game.get('release_date')!r}"
        )


# ---------------------------------------------------------------------------
# TestCompareMarketsEnrichment — 3 tests
# ---------------------------------------------------------------------------

class TestCompareMarketsEnrichment:
    """Tests that compare_markets_analysis enriches each market with Gamalytic data."""

    def _make_search_result(self, tag: str, n: int = 30, revenue_base: int = 50000):
        """Return a mock SearchResult with n GameSummary objects."""
        from src.schemas import GameSummary, SearchResult
        games = [
            GameSummary(
                appid=i, name=f"Game {i}", developer="Dev", publisher="Pub",
                ccu=100, owners_min=1000 * (n + 1 - i), owners_max=5000 * (n + 1 - i),
                owners_midpoint=float(3000 * (n + 1 - i)),
                average_forever=10.0, average_2weeks=1.0,
                median_forever=8.0, median_2weeks=0.5,
                positive=200, negative=10, price=9.99,
            )
            for i in range(1, n + 1)
        ]
        return SearchResult(
            tag=tag, appids=[g.appid for g in games], total_found=n,
            games=games, data_source="steamspy",
            fetched_at="2026-01-01T00:00:00Z", cache_age_seconds=0,
        )

    def _make_gamalytic_games(self, n: int = 30, revenue: int = 500_000):
        """Return list of Gamalytic game dicts with revenue data."""
        return [
            {"steamId": i, "name": f"Game {i}", "revenue": revenue * (n + 1 - i), "copiesSold": 10000}
            for i in range(1, n + 1)
        ]

    @pytest.mark.asyncio
    async def test_compare_markets_calls_enrichment(self):
        """Each market in compare_markets_analysis has an 'enrichment' key in its analysis."""
        from unittest.mock import AsyncMock, MagicMock
        from src.market_tools import compare_markets_analysis

        mock_steamspy = MagicMock()
        mock_steam_store = MagicMock()
        mock_gamalytic = MagicMock()

        # Must be AsyncMock: enrichment pipeline calls asyncio.gather with get_engagement_data
        mock_steamspy.get_engagement_data = AsyncMock(return_value=None)

        # Two markets: "Roguelike" and "Strategy"
        mock_steamspy.search_by_tag = AsyncMock(side_effect=[
            self._make_search_result("Roguelike", n=30),
            self._make_search_result("Strategy", n=30),
        ])

        # Gamalytic returns matching game dicts for both markets
        _gam1 = self._make_gamalytic_games(n=30)
        _gam2 = self._make_gamalytic_games(n=30)
        mock_gamalytic.list_games_by_tag = AsyncMock(side_effect=[
            _with_meta(_gam1),
            _with_meta(_gam2),
        ])
        mock_gamalytic.get_commercial_batch = AsyncMock(return_value=[])

        result = await compare_markets_analysis(
            market_inputs=[
                {"tags": ["Roguelike"], "label": "Roguelike"},
                {"tags": ["Strategy"], "label": "Strategy"},
            ],
            steamspy=mock_steamspy,
            steam_store=mock_steam_store,
            gamalytic=mock_gamalytic,
        )

        assert "error" not in result, f"Unexpected error: {result.get('error')}"
        assert "markets" in result
        for market_entry in result["markets"]:
            analysis = market_entry["analysis"]
            assert "enrichment" in analysis, (
                f"Market '{market_entry['market_label']}' missing 'enrichment' key"
            )
            enrichment = analysis["enrichment"]
            assert "method" in enrichment, "enrichment dict missing 'method' key"
            assert enrichment["method"] is not None

    @pytest.mark.asyncio
    async def test_compare_markets_enriched_data_populates_metrics(self):
        """With Gamalytic revenue data, revenue-dependent metrics become non-None."""
        from unittest.mock import AsyncMock, MagicMock
        from src.market_tools import compare_markets_analysis

        mock_steamspy = MagicMock()
        mock_steam_store = MagicMock()
        mock_gamalytic = MagicMock()
        mock_steamspy.get_engagement_data = AsyncMock(return_value=None)

        # Markets with high overlap (all 30 games match Gamalytic)
        mock_steamspy.search_by_tag = AsyncMock(side_effect=[
            self._make_search_result("Action", n=30, revenue_base=0),
            self._make_search_result("Indie", n=30, revenue_base=0),
        ])
        # Gamalytic provides revenue for all games
        _gam_action = self._make_gamalytic_games(n=30, revenue=200_000)
        _gam_indie = self._make_gamalytic_games(n=30, revenue=100_000)
        mock_gamalytic.list_games_by_tag = AsyncMock(side_effect=[
            _with_meta(_gam_action),
            _with_meta(_gam_indie),
        ])
        mock_gamalytic.get_commercial_batch = AsyncMock(return_value=[])

        result = await compare_markets_analysis(
            market_inputs=[
                {"tags": ["Action"], "label": "Action"},
                {"tags": ["Indie"], "label": "Indie"},
            ],
            steamspy=mock_steamspy,
            steam_store=mock_steam_store,
            gamalytic=mock_gamalytic,
        )

        assert "error" not in result
        for market_entry in result["markets"]:
            analysis = market_entry["analysis"]
            label = market_entry["market_label"]
            # concentration should be populated (non-None) when games have revenue
            concentration = analysis.get("concentration")
            assert concentration is not None, (
                f"Market '{label}': concentration is None despite Gamalytic revenue data"
            )
            # data_source in each market's methodology should include "gamalytic"
            market_methodology = analysis.get("methodology") or {}
            ds = market_methodology.get("data_source", "")
            assert "gamalytic" in ds, (
                f"Market '{label}': data_source '{ds}' does not include 'gamalytic'"
            )

    @pytest.mark.asyncio
    async def test_compare_markets_methodology_reflects_enrichment(self):
        """Top-level methodology data_source is 'steamspy+gamalytic_bulk'."""
        from unittest.mock import AsyncMock, MagicMock
        from src.market_tools import compare_markets_analysis

        mock_steamspy = MagicMock()
        mock_steam_store = MagicMock()
        mock_gamalytic = MagicMock()
        mock_steamspy.get_engagement_data = AsyncMock(return_value=None)

        mock_steamspy.search_by_tag = AsyncMock(side_effect=[
            self._make_search_result("Platformer", n=30),
            self._make_search_result("Puzzle", n=30),
        ])
        _gam_plat = self._make_gamalytic_games(n=30)
        _gam_puzz = self._make_gamalytic_games(n=30)
        mock_gamalytic.list_games_by_tag = AsyncMock(side_effect=[
            _with_meta(_gam_plat),
            _with_meta(_gam_puzz),
        ])
        mock_gamalytic.get_commercial_batch = AsyncMock(return_value=[])

        result = await compare_markets_analysis(
            market_inputs=[
                {"tags": ["Platformer"], "label": "Platformer"},
                {"tags": ["Puzzle"], "label": "Puzzle"},
            ],
            steamspy=mock_steamspy,
            steam_store=mock_steam_store,
            gamalytic=mock_gamalytic,
        )

        assert "error" not in result
        methodology = result.get("methodology") or {}
        data_source = methodology.get("data_source", "")
        assert data_source == "steamspy+gamalytic_bulk", (
            f"Expected methodology data_source 'steamspy+gamalytic_bulk', got '{data_source}'"
        )


# ---------------------------------------------------------------------------
# TestReturnGames — 3 tests
# ---------------------------------------------------------------------------

class TestReturnGames:
    """Tests for return_games parameter on analyze_market_single."""

    def _make_search_result(self, tag: str = "Roguelike", n: int = 30):
        from src.schemas import GameSummary, SearchResult
        games = [
            GameSummary(
                appid=i, name=f"Game {i}", developer="Dev", publisher="Pub",
                ccu=100, owners_min=1000 * (n + 1 - i), owners_max=5000 * (n + 1 - i),
                owners_midpoint=float(3000 * (n + 1 - i)),
                average_forever=10.0, average_2weeks=1.0,
                median_forever=8.0, median_2weeks=0.5,
                positive=200, negative=10, price=9.99,
            )
            for i in range(1, n + 1)
        ]
        return SearchResult(
            tag=tag, appids=[g.appid for g in games], total_found=n,
            games=games, data_source="steamspy",
            fetched_at="2026-01-01T00:00:00Z", cache_age_seconds=0,
        )

    @pytest.mark.asyncio
    async def test_analyze_market_single_return_games_false_default(self):
        """analyze_market_single default (return_games not specified) returns a plain dict."""
        from unittest.mock import AsyncMock, MagicMock
        from src.market_tools import analyze_market_single

        mock_steamspy = MagicMock()
        mock_steam_store = MagicMock()
        mock_gamalytic = MagicMock()
        mock_steamspy.get_engagement_data = AsyncMock(return_value=None)
        mock_steamspy.search_by_tag = AsyncMock(return_value=self._make_search_result())
        _inline_games2 = [
            {"steamId": i, "name": f"Game {i}", "revenue": 500_000 * (31 - i), "copiesSold": 10000}
            for i in range(1, 31)
        ]
        mock_gamalytic.list_games_by_tag = AsyncMock(return_value=_with_meta(_inline_games2))
        mock_gamalytic.get_commercial_batch = AsyncMock(return_value=[])

        result = await analyze_market_single(
            steamspy=mock_steamspy, steam_store=mock_steam_store,
            gamalytic=mock_gamalytic, tags=["Roguelike"],
        )

        # Without return_games, result is a plain dict (not a tuple)
        assert isinstance(result, dict), f"Expected dict, got {type(result)}"
        assert not isinstance(result, tuple)
        assert "error" not in result

    @pytest.mark.asyncio
    async def test_analyze_market_single_return_games_true(self):
        """analyze_market_single with return_games=True returns (dict, list) tuple with enriched games."""
        from unittest.mock import AsyncMock, MagicMock
        from src.market_tools import analyze_market_single

        mock_steamspy = MagicMock()
        mock_steam_store = MagicMock()
        mock_gamalytic = MagicMock()
        mock_steamspy.get_engagement_data = AsyncMock(return_value=None)
        mock_steamspy.search_by_tag = AsyncMock(return_value=self._make_search_result())
        _inline_games3 = [
            {"steamId": i, "name": f"Game {i}", "revenue": 500_000 * (31 - i), "copiesSold": 10000}
            for i in range(1, 31)
        ]
        mock_gamalytic.list_games_by_tag = AsyncMock(return_value=_with_meta(_inline_games3))
        mock_gamalytic.get_commercial_batch = AsyncMock(return_value=[])

        result = await analyze_market_single(
            steamspy=mock_steamspy, steam_store=mock_steam_store,
            gamalytic=mock_gamalytic, tags=["Roguelike"],
            return_games=True,
        )

        # With return_games=True, result is a (dict, list) tuple
        assert isinstance(result, tuple), f"Expected tuple, got {type(result)}"
        assert len(result) == 2
        result_dict, games_list = result

        assert isinstance(result_dict, dict)
        assert "error" not in result_dict

        assert isinstance(games_list, list)
        assert len(games_list) == 30

        # Gamalytic enrichment should have provided revenue for games
        games_with_revenue = [g for g in games_list if g.get("revenue") is not None]
        assert len(games_with_revenue) > 0, (
            "Expected some games to have non-None revenue from Gamalytic enrichment"
        )

    @pytest.mark.asyncio
    async def test_analyze_market_single_return_games_error_path(self):
        """analyze_market_single with return_games=True returns (error_dict, []) on no-games error."""
        from unittest.mock import AsyncMock, MagicMock
        from src.schemas import SearchResult
        from src.market_tools import analyze_market_single

        mock_steamspy = MagicMock()
        mock_steam_store = MagicMock()
        mock_gamalytic = MagicMock()

        # SteamSpy returns zero games
        empty_result = SearchResult(
            tag="NonExistentTag99999", appids=[], total_found=0,
            games=[], data_source="steamspy",
            fetched_at="2026-01-01T00:00:00Z", cache_age_seconds=0,
        )
        mock_steamspy.search_by_tag = AsyncMock(return_value=empty_result)

        result = await analyze_market_single(
            steamspy=mock_steamspy, steam_store=mock_steam_store,
            gamalytic=mock_gamalytic, tags=["NonExistentTag99999"],
            return_games=True,
        )

        # Error path returns (error_dict, [])
        assert isinstance(result, tuple), f"Expected tuple on error path, got {type(result)}"
        assert len(result) == 2
        error_dict, games_list = result

        assert isinstance(error_dict, dict)
        assert "error" in error_dict
        assert isinstance(games_list, list)
        assert games_list == [], f"Expected empty list on error path, got {games_list}"


# ---------------------------------------------------------------------------
# TestFetchGameListFallbacks — 6 tests
# ---------------------------------------------------------------------------

class TestFetchGameListFallbacks:
    """Tests for Steam Store and Gamalytic fallback paths in _fetch_game_list_steamspy."""

    @pytest.mark.asyncio
    async def test_single_tag_steam_store_fallback_on_api_error(self):
        """_fetch_game_list_steamspy falls back to Steam Store when SteamSpy tag search returns APIError."""
        from unittest.mock import AsyncMock, MagicMock
        from src.schemas import APIError as APIErrorSchema, EngagementData
        from src.market_tools import _fetch_game_list_steamspy
        from datetime import datetime, timezone

        mock_steamspy = MagicMock()
        mock_steam_store = MagicMock()
        mock_gamalytic = MagicMock()

        # SteamSpy bulk tag fails
        mock_steamspy.search_by_tag = AsyncMock(return_value=APIErrorSchema(
            error_code=500, error_type="api_error", message="SteamSpy unavailable"
        ))

        # Steam Store returns 3 appids
        mock_steam_store.get_tag_map = AsyncMock(return_value={"roguelike": 1234})
        mock_steam_store.search_by_tag_ids_paginated = AsyncMock(return_value=(3, [101, 102, 103]))

        # SteamSpy per-game enrichment succeeds for all 3 appids
        def fallback_engagement(appid):
            return EngagementData(
                appid=appid, name=f"FallbackGame{appid}", ccu=100, owners_min=1000, owners_max=5000,
                owners_midpoint=3000.0, average_forever=10.0, average_2weeks=1.0,
                median_forever=8.0, median_2weeks=0.5, positive=200, negative=10, price=9.99,
                review_score=80.0, tags={"Roguelike": 100},
                developer="Dev", publisher="Pub",
                fetched_at=datetime.now(timezone.utc), total_reviews=210,
            )
        mock_steamspy.get_engagement_data = AsyncMock(side_effect=fallback_engagement)

        games, data_source = await _fetch_game_list_steamspy(
            mock_steamspy, mock_steam_store, mock_gamalytic, ["Roguelike"]
        )

        assert len(games) == 3
        assert data_source == "steam_store_fallback"
        mock_steam_store.get_tag_map.assert_called_once()
        mock_steam_store.search_by_tag_ids_paginated.assert_called_once_with([1234], max_results=2000)

    @pytest.mark.asyncio
    async def test_single_tag_steam_store_fallback_on_empty_result(self):
        """_fetch_game_list_steamspy falls back to Steam Store when SteamSpy returns empty SearchResult."""
        from unittest.mock import AsyncMock, MagicMock
        from src.schemas import SearchResult, EngagementData
        from src.market_tools import _fetch_game_list_steamspy
        from datetime import datetime, timezone

        mock_steamspy = MagicMock()
        mock_steam_store = MagicMock()
        mock_gamalytic = MagicMock()

        # SteamSpy returns empty result (bowling fallback or genuinely empty)
        empty_result = SearchResult(
            tag="ObscureTag", appids=[], total_found=0, games=[],
            data_source="steamspy", fetched_at="2026-01-01T00:00:00Z", cache_age_seconds=0,
        )
        mock_steamspy.search_by_tag = AsyncMock(return_value=empty_result)

        # Steam Store resolves tag and returns appids
        mock_steam_store.get_tag_map = AsyncMock(return_value={"obscuretag": 9999})
        mock_steam_store.search_by_tag_ids_paginated = AsyncMock(return_value=(2, [201, 202]))

        def obscure_engagement(appid):
            return EngagementData(
                appid=appid, name=f"Game{appid}", ccu=50, owners_min=500, owners_max=2000,
                owners_midpoint=1250.0, average_forever=5.0, average_2weeks=0.5,
                median_forever=4.0, median_2weeks=0.2, positive=100, negative=5, price=4.99,
                review_score=95.0, tags={},
                developer="Dev", publisher="Pub",
                fetched_at=datetime.now(timezone.utc), total_reviews=105,
            )
        mock_steamspy.get_engagement_data = AsyncMock(side_effect=obscure_engagement)

        games, data_source = await _fetch_game_list_steamspy(
            mock_steamspy, mock_steam_store, mock_gamalytic, ["ObscureTag"]
        )

        assert len(games) == 2
        assert data_source == "steam_store_fallback"
        mock_steam_store.get_tag_map.assert_called_once()

    @pytest.mark.asyncio
    async def test_single_tag_fallback_tag_not_in_map_returns_empty(self):
        """When tag cannot be resolved in Steam tag map, fallback returns empty gracefully."""
        from unittest.mock import AsyncMock, MagicMock
        from src.schemas import APIError as APIErrorSchema
        from src.market_tools import _fetch_game_list_steamspy

        mock_steamspy = MagicMock()
        mock_steam_store = MagicMock()
        mock_gamalytic = MagicMock()

        mock_steamspy.search_by_tag = AsyncMock(return_value=APIErrorSchema(
            error_code=404, error_type="tag_not_found", message="tag unknown"
        ))
        # Tag map doesn't contain this tag
        mock_steam_store.get_tag_map = AsyncMock(return_value={"roguelike": 1234})

        games, data_source = await _fetch_game_list_steamspy(
            mock_steamspy, mock_steam_store, mock_gamalytic, ["CompletelyUnknownTag"]
        )

        assert games == []
        assert data_source == "steam_store_fallback"
        mock_steam_store.search_by_tag_ids_paginated.assert_not_called()

    @pytest.mark.asyncio
    async def test_appids_gamalytic_fallback_recovers_failed(self):
        """Appids that fail SteamSpy per-game are recovered via Gamalytic commercial data."""
        from unittest.mock import AsyncMock, MagicMock
        from src.schemas import APIError as APIErrorSchema, EngagementData, CommercialData
        from src.market_tools import _fetch_game_list_steamspy
        from datetime import datetime, timezone

        mock_steamspy = MagicMock()
        mock_steam_store = MagicMock()
        mock_gamalytic = MagicMock()

        # Appids 1 and 2 succeed via SteamSpy, appid 3 fails
        def steamspy_side_effect(appid):
            if appid == 3:
                return APIErrorSchema(error_code=502, error_type="api_error", message="502 Bad Gateway")
            return EngagementData(
                appid=appid, name=f"Game{appid}", ccu=100, owners_min=1000, owners_max=5000,
                owners_midpoint=3000.0, average_forever=10.0, average_2weeks=1.0,
                median_forever=8.0, median_2weeks=0.5, positive=200, negative=10, price=9.99,
                review_score=80.0, tags={"Action": 100},
                developer="Dev", publisher="Pub",
                fetched_at=datetime.now(timezone.utc), total_reviews=210,
            )
        mock_steamspy.get_engagement_data = AsyncMock(side_effect=steamspy_side_effect)

        # Gamalytic recovers appid 3
        mock_gamalytic.get_commercial_data = AsyncMock(return_value=CommercialData(
            appid=3, name="RecoveredGame", price=14.99,
            revenue_min=100_000, revenue_max=200_000,
            confidence="low", source="gamalytic",
            fetched_at=datetime.now(timezone.utc),
            followers=500, copies_sold=8000,
            review_score=72.0, release_date="2022-03-15",
            gamalytic_owners=12000,
        ))

        games, data_source = await _fetch_game_list_steamspy(
            mock_steamspy, mock_steam_store, mock_gamalytic,
            [],  # no tags
            appids=[1, 2, 3],
        )

        assert len(games) == 3
        appids_found = {g["appid"] for g in games}
        assert appids_found == {1, 2, 3}

        # Verify recovered game has correct Gamalytic fields
        recovered = next(g for g in games if g["appid"] == 3)
        assert recovered["name"] == "RecoveredGame"
        assert recovered["price"] == 14.99
        assert recovered["revenue"] == 150_000.0  # midpoint of 100k/200k
        assert recovered["followers"] == 500
        assert recovered["copies_sold"] == 8000
        assert recovered["release_date"] == "2022-03-15"
        assert recovered["owners"] == 12000
        # Gamalytic has no ccu/playtime/tags
        assert recovered["ccu"] is None
        assert recovered["median_forever"] is None
        assert recovered["tags"] == {}

        assert data_source == "steamspy_appids"

    @pytest.mark.asyncio
    async def test_appids_double_failure_drops_gracefully(self):
        """When both SteamSpy and Gamalytic fail for an appid, it is silently dropped."""
        from unittest.mock import AsyncMock, MagicMock
        from src.schemas import APIError as APIErrorSchema
        from src.market_tools import _fetch_game_list_steamspy

        mock_steamspy = MagicMock()
        mock_steam_store = MagicMock()
        mock_gamalytic = MagicMock()

        # All appids fail SteamSpy
        mock_steamspy.get_engagement_data = AsyncMock(return_value=APIErrorSchema(
            error_code=500, error_type="api_error", message="steamspy down"
        ))
        # Gamalytic also fails
        mock_gamalytic.get_commercial_data = AsyncMock(return_value=APIErrorSchema(
            error_code=500, error_type="api_error", message="gamalytic down"
        ))

        games, data_source = await _fetch_game_list_steamspy(
            mock_steamspy, mock_steam_store, mock_gamalytic,
            [],
            appids=[10, 20],
        )

        assert games == []
        assert data_source == "steamspy_appids"

    @pytest.mark.asyncio
    async def test_gamalytic_fallback_empty_appids(self):
        """_gamalytic_fallback with empty appids returns empty list without calling API."""
        from unittest.mock import AsyncMock, MagicMock
        from src.market_tools import _gamalytic_fallback

        mock_gamalytic = MagicMock()
        mock_gamalytic.get_commercial_data = AsyncMock()

        result = await _gamalytic_fallback(mock_gamalytic, [])

        assert result == []

    @pytest.mark.asyncio
    async def test_single_tag_small_result_triggers_supplement(self):
        """When SteamSpy returns fewer than STEAMSPY_MIN_EXPECTED games, Steam Store supplement fires."""
        from unittest.mock import AsyncMock, MagicMock
        from src.schemas import SearchResult, GameSummary, EngagementData
        from src.market_tools import _fetch_game_list_steamspy, STEAMSPY_MIN_EXPECTED
        from datetime import datetime, timezone

        mock_steamspy = MagicMock()
        mock_steam_store = MagicMock()
        mock_gamalytic = MagicMock()

        # SteamSpy returns only 30 games (well below STEAMSPY_MIN_EXPECTED=200)
        small_games = [
            GameSummary(appid=i, name=f"Game{i}", owners_midpoint=float(i * 1000))
            for i in range(1, 31)
        ]
        mock_steamspy.search_by_tag = AsyncMock(return_value=SearchResult(
            tag="Life Sim", appids=[g.appid for g in small_games],
            total_found=30, games=small_games,
            data_source="steamspy", fetched_at="2026-01-01T00:00:00Z", cache_age_seconds=0,
        ))

        # Steam Store supplement returns 70 new appids
        new_appids = list(range(1001, 1071))  # 70 new appids
        mock_steam_store.get_tag_map = AsyncMock(return_value={"life sim": 5555})
        mock_steam_store.search_by_tag_ids_paginated = AsyncMock(return_value=(1200, new_appids))

        # SteamSpy per-game enrichment succeeds for new appids
        def engagement_side_effect(appid):
            return EngagementData(
                appid=appid, name=f"SupGame{appid}", ccu=50, owners_min=500, owners_max=2000,
                owners_midpoint=1250.0, average_forever=5.0, average_2weeks=0.5,
                median_forever=4.0, median_2weeks=0.2, positive=100, negative=5, price=9.99,
                review_score=90.0, tags={"Life Sim": 80},
                developer="Dev", publisher="Pub",
                fetched_at=datetime.now(timezone.utc), total_reviews=105,
            )
        mock_steamspy.get_engagement_data = AsyncMock(side_effect=engagement_side_effect)

        games, data_source = await _fetch_game_list_steamspy(
            mock_steamspy, mock_steam_store, mock_gamalytic, ["Life Sim"]
        )

        assert len(games) >= 100  # 30 original + 70 supplement
        assert data_source == "steamspy+steam_store_paginated"
        mock_steam_store.get_tag_map.assert_called_once()
        mock_steam_store.search_by_tag_ids_paginated.assert_called_once_with([5555], max_results=2000)

    @pytest.mark.asyncio
    async def test_single_tag_small_result_supplement_failure_graceful(self):
        """When Steam Store tag map fails during small-result supplement, returns original small result gracefully."""
        from unittest.mock import AsyncMock, MagicMock
        from src.schemas import SearchResult, GameSummary, APIError as APIErrorSchema
        from src.market_tools import _fetch_game_list_steamspy

        mock_steamspy = MagicMock()
        mock_steam_store = MagicMock()
        mock_gamalytic = MagicMock()

        # SteamSpy returns only 10 games
        small_games = [
            GameSummary(appid=i, name=f"TinyGame{i}", owners_midpoint=float(i * 500))
            for i in range(1, 11)
        ]
        mock_steamspy.search_by_tag = AsyncMock(return_value=SearchResult(
            tag="Tiny Niche", appids=[g.appid for g in small_games],
            total_found=10, games=small_games,
            data_source="steamspy", fetched_at="2026-01-01T00:00:00Z", cache_age_seconds=0,
        ))

        # Steam Store tag map fails
        mock_steam_store.get_tag_map = AsyncMock(return_value=APIErrorSchema(
            error_code=500, error_type="api_error", message="Steam Store unavailable"
        ))

        games, data_source = await _fetch_game_list_steamspy(
            mock_steamspy, mock_steam_store, mock_gamalytic, ["Tiny Niche"]
        )

        # Should return original 10 games without crashing
        assert len(games) == 10
        assert data_source == "steamspy"
        # Paginated search should not have been called
        mock_steam_store.search_by_tag_ids_paginated.assert_not_called()

    @pytest.mark.asyncio
    async def test_single_tag_medium_result_no_supplement(self):
        """When SteamSpy returns 200-1899 games, no supplement is triggered."""
        from unittest.mock import AsyncMock, MagicMock
        from src.schemas import SearchResult, GameSummary
        from src.market_tools import _fetch_game_list_steamspy

        mock_steamspy = MagicMock()
        mock_steam_store = MagicMock()
        mock_gamalytic = MagicMock()

        # SteamSpy returns 500 games (medium — no supplement)
        medium_games = [
            GameSummary(appid=i, name=f"MedGame{i}", owners_midpoint=float(i * 2000))
            for i in range(1, 501)
        ]
        mock_steamspy.search_by_tag = AsyncMock(return_value=SearchResult(
            tag="RPG", appids=[g.appid for g in medium_games],
            total_found=500, games=medium_games,
            data_source="steamspy", fetched_at="2026-01-01T00:00:00Z", cache_age_seconds=0,
        ))

        games, data_source = await _fetch_game_list_steamspy(
            mock_steamspy, mock_steam_store, mock_gamalytic, ["RPG"]
        )

        assert len(games) == 500
        assert data_source == "steamspy"
        # Neither supplement path should fire
        mock_steam_store.search_by_tag_ids_paginated.assert_not_called()

    @pytest.mark.asyncio
    async def test_single_tag_large_result_supplement_uses_paginated(self):
        """Large-result supplement (>=1900) uses search_by_tag_ids_paginated (not non-paginated search_by_tag_ids)."""
        from unittest.mock import AsyncMock, MagicMock
        from src.schemas import SearchResult, GameSummary, EngagementData
        from src.market_tools import _fetch_game_list_steamspy, STEAMSPY_SUPPLEMENT_THRESHOLD
        from datetime import datetime, timezone

        mock_steamspy = MagicMock()
        mock_steam_store = MagicMock()
        mock_gamalytic = MagicMock()

        # SteamSpy returns 2000 games (>= STEAMSPY_SUPPLEMENT_THRESHOLD=1900)
        large_games = [
            GameSummary(appid=i, name=f"LargeGame{i}", owners_midpoint=float(i * 5000))
            for i in range(1, 2001)
        ]
        mock_steamspy.search_by_tag = AsyncMock(return_value=SearchResult(
            tag="Action", appids=[g.appid for g in large_games],
            total_found=2000, games=large_games,
            data_source="steamspy", fetched_at="2026-01-01T00:00:00Z", cache_age_seconds=0,
        ))

        # Steam Store supplement returns 50 new appids (non-overlapping)
        supplement_appids = list(range(5001, 5051))  # 50 new appids
        mock_steam_store.get_tag_map = AsyncMock(return_value={"action": 4321})
        mock_steam_store.search_by_tag_ids_paginated = AsyncMock(return_value=(2050, supplement_appids))

        def engagement_side_effect(appid):
            return EngagementData(
                appid=appid, name=f"SupAction{appid}", ccu=200, owners_min=5000, owners_max=20000,
                owners_midpoint=12500.0, average_forever=10.0, average_2weeks=1.0,
                median_forever=8.0, median_2weeks=0.5, positive=500, negative=20, price=14.99,
                review_score=96.0, tags={"Action": 200},
                developer="Dev", publisher="Pub",
                fetched_at=datetime.now(timezone.utc), total_reviews=520,
            )
        mock_steamspy.get_engagement_data = AsyncMock(side_effect=engagement_side_effect)

        games, data_source = await _fetch_game_list_steamspy(
            mock_steamspy, mock_steam_store, mock_gamalytic, ["Action"]
        )

        assert len(games) >= 2050  # 2000 original + 50 supplement
        assert data_source == "steamspy+steam_store_paginated"
        # Verify paginated search was used (not non-paginated)
        mock_steam_store.search_by_tag_ids_paginated.assert_called_once_with([4321], max_results=2000)

    @pytest.mark.asyncio
    async def test_fallback_enrichment_dropout_recovery(self):
        """When SteamSpy per-game fails in fallback path, failed appids are recovered as minimal dicts."""
        from unittest.mock import AsyncMock, MagicMock
        from src.schemas import APIError as APIErrorSchema, EngagementData
        from src.market_tools import _fetch_game_list_steamspy
        from datetime import datetime, timezone

        mock_steamspy = MagicMock()
        mock_steam_store = MagicMock()
        mock_gamalytic = MagicMock()

        # SteamSpy bulk tag fails — triggers fallback
        mock_steamspy.search_by_tag = AsyncMock(return_value=APIErrorSchema(
            error_code=503, error_type="api_error", message="SteamSpy down"
        ))

        # Steam Store paginated returns 100 appids
        store_appids = list(range(101, 201))  # appids 101-200
        mock_steam_store.get_tag_map = AsyncMock(return_value={"life sim": 5555})
        mock_steam_store.search_by_tag_ids_paginated = AsyncMock(return_value=(100, store_appids))

        # SteamSpy per-game succeeds for first 20, fails for remaining 80
        def engagement_side_effect(appid):
            if appid <= 120:  # appids 101-120 succeed (20 games)
                return EngagementData(
                    appid=appid, name=f"GoodGame{appid}", ccu=100, owners_min=1000, owners_max=5000,
                    owners_midpoint=3000.0, average_forever=8.0, average_2weeks=0.8,
                    median_forever=6.0, median_2weeks=0.4, positive=300, negative=15, price=9.99,
                    review_score=95.0, tags={"Life Sim": 90},
                    developer="Dev", publisher="Pub",
                    fetched_at=datetime.now(timezone.utc), total_reviews=315,
                )
            # appids 121-200 fail (80 games)
            return APIErrorSchema(error_code=429, error_type="rate_limit", message="rate limited")
        mock_steamspy.get_engagement_data = AsyncMock(side_effect=engagement_side_effect)

        games, data_source = await _fetch_game_list_steamspy(
            mock_steamspy, mock_steam_store, mock_gamalytic, ["Life Sim"]
        )

        # All 100 appids should survive (20 full dicts + 80 minimal dicts)
        assert len(games) == 100
        assert data_source == "steam_store_fallback"

        # 20 games should have valid enrichment data
        valid_games = [g for g in games if g.get("name") is not None]
        assert len(valid_games) == 20

        # 80 games should be minimal dicts with None values
        minimal_games = [g for g in games if g.get("revenue") is None and g.get("name") is None]
        assert len(minimal_games) == 80

        # All minimal dicts should have appid set
        for g in minimal_games:
            assert g["appid"] in range(121, 201)


# ---------------------------------------------------------------------------
# TestTier15Enrichment — 7 tests for Tier 1.5 per-game enrichment
# ---------------------------------------------------------------------------

class TestTier15Enrichment:
    """Tests for Tier 1.5 per-game enrichment in _enrich_with_gamalytic_bulk.

    Tier 1.5 fills the gap between Tier 1 (bulk merge) and Tier 2 (deep enrichment):
    games not found in Gamalytic's tag taxonomy get per-game CommercialData + SteamSpy tags.
    """

    def _make_bulk_setup(self, gamalytic_appids, steamspy_appids):
        """Return (mock_gamalytic, steamspy_games) for a bulk-merge scenario.

        gamalytic_appids: appids included in Gamalytic bulk response (revenue=1_000_000 each)
        steamspy_appids: all appids in SteamSpy response (each missing revenue initially)
        """
        from unittest.mock import AsyncMock, MagicMock
        mock_gamalytic = MagicMock()
        gamalytic_games = [
            {"steamId": i, "name": f"Game{i}", "revenue": 1_000_000, "copiesSold": 5000}
            for i in gamalytic_appids
        ]
        mock_gamalytic.list_games_by_tag = AsyncMock(return_value=_with_meta(gamalytic_games))
        steamspy_games = [
            {"appid": i, "name": f"Game{i}", "owners": 50_000, "revenue": None, "tags": {}}
            for i in steamspy_appids
        ]
        return mock_gamalytic, steamspy_games

    @pytest.mark.asyncio
    async def test_tier1_5_enriches_unenriched_games(self):
        """Games not in Gamalytic bulk get revenue + tags via Tier 1.5 per-game fetch."""
        from unittest.mock import AsyncMock, MagicMock
        from src.market_tools import _enrich_with_gamalytic_bulk
        from src.schemas import CommercialData, EngagementData
        from datetime import datetime, timezone

        # Gamalytic bulk: appids 1-8 (appids 9 and 10 are NOT in bulk)
        mock_gamalytic, steamspy_games = self._make_bulk_setup(
            gamalytic_appids=list(range(1, 9)),
            steamspy_appids=list(range(1, 11)),
        )
        # Tier 1.5: get_commercial_batch returns CommercialData for appids 9 and 10
        now = datetime.now(timezone.utc)
        mock_gamalytic.get_commercial_batch = AsyncMock(return_value=[
            CommercialData(
                appid=9, name="HiddenGame9", price=14.99,
                revenue_min=900_000, revenue_max=1_100_000,
                confidence="medium", source="gamalytic", fetched_at=now,
            ),
            CommercialData(
                appid=10, name="HiddenGame10", price=24.99,
                revenue_min=1_800_000, revenue_max=2_200_000,
                confidence="high", source="gamalytic", fetched_at=now,
            ),
        ])

        # SteamSpy tags for Tier 1.5 games
        mock_steamspy = MagicMock()
        mock_steamspy.get_engagement_data = AsyncMock(return_value=EngagementData(
            appid=9, name="HiddenGame9", ccu=500, owners_midpoint=50_000.0,
            owners_min=40_000, owners_max=60_000,
            average_forever=20.0, average_2weeks=5.0,
            median_forever=15.0, median_2weeks=3.0,
            positive=5000, negative=100, price=14.99,
            review_score=90.0, tags={"Roguelike": 500, "Indie": 300},
            developer="DevA", publisher="PubA",
            fetched_at=now, total_reviews=5100,
        ))

        enriched, warnings, meta = await _enrich_with_gamalytic_bulk(
            steamspy_games, mock_gamalytic,
            steamspy=mock_steamspy, primary_tag="Roguelike",
        )

        # Both Tier 1.5 games should have revenue populated
        game9 = next(g for g in enriched if g["appid"] == 9)
        game10 = next(g for g in enriched if g["appid"] == 10)
        assert game9["revenue"] is not None, "Game 9 should have revenue from Tier 1.5"
        assert game10["revenue"] is not None, "Game 10 should have revenue from Tier 1.5"

        # Tier 1.5 meta fields populated
        assert meta["tier1_5_count"] == 2   # 2 unenriched appids
        assert meta["tier1_5_commercial_found"] == 2
        assert meta["tier1_5_total"] >= 2   # at least both games were updated

        # SteamSpy tags should be populated (get_engagement_data was called)
        assert mock_steamspy.get_engagement_data.call_count >= 2

    @pytest.mark.asyncio
    async def test_tier1_5_skips_when_all_enriched(self):
        """Tier 1.5 fires no per-game calls when Tier 1 enriches 100% of games."""
        from unittest.mock import AsyncMock, MagicMock
        from src.market_tools import _enrich_with_gamalytic_bulk
        from src.schemas import EngagementData
        from datetime import datetime, timezone

        # Gamalytic bulk: ALL appids 1-10 are in Gamalytic
        mock_gamalytic, steamspy_games = self._make_bulk_setup(
            gamalytic_appids=list(range(1, 11)),
            steamspy_appids=list(range(1, 11)),
        )
        # Tier 2 get_commercial_batch returns empty (skip Tier 2 enrichment)
        mock_gamalytic.get_commercial_batch = AsyncMock(return_value=[])

        now = datetime.now(timezone.utc)
        mock_steamspy = MagicMock()
        mock_steamspy.get_engagement_data = AsyncMock(return_value=EngagementData(
            appid=1, name="Game1", ccu=100, owners_midpoint=50_000.0,
            owners_min=40_000, owners_max=60_000,
            average_forever=10.0, average_2weeks=2.0,
            median_forever=8.0, median_2weeks=1.0,
            positive=200, negative=10, price=9.99,
            review_score=90.0, tags={"Roguelike": 500},
            developer="Dev", publisher="Pub",
            fetched_at=now, total_reviews=210,
        ))

        enriched, warnings, meta = await _enrich_with_gamalytic_bulk(
            steamspy_games, mock_gamalytic,
            steamspy=mock_steamspy, primary_tag="Roguelike",
        )

        # All games enriched in Tier 1 → Tier 1.5 meta fields absent (block not entered)
        assert "tier1_5_count" not in meta, "tier1_5_count should not exist when all enriched"

    @pytest.mark.asyncio
    async def test_tier1_5_warns_for_large_batch(self):
        """Warning is logged (not info) when 1000+ games need Tier 1.5 enrichment."""
        import logging
        from unittest.mock import AsyncMock, MagicMock
        from src.market_tools import _enrich_with_gamalytic_bulk

        # Setup: 3000 SteamSpy games, Gamalytic covers 2000 (66.7% overlap -> passes cross-val)
        # 1000 games remain unenriched -> triggers >= 1000 warning path
        total_games = 3000
        gamalytic_covered = 2000
        mock_gamalytic, steamspy_games = self._make_bulk_setup(
            gamalytic_appids=list(range(1, gamalytic_covered + 1)),
            steamspy_appids=list(range(1, total_games + 1)),
        )
        # get_commercial_batch returns empty (don't care about enrichment results here)
        mock_gamalytic.get_commercial_batch = AsyncMock(return_value=[])

        mock_steamspy = MagicMock()
        mock_steamspy.get_engagement_data = AsyncMock(return_value=None)

        import src.market_tools as mt

        warning_messages = []
        original_warning = mt.logger.warning

        def capture_warning(msg, *args, **kwargs):
            warning_messages.append(msg)
            original_warning(msg, *args, **kwargs)

        mt.logger.warning = capture_warning
        try:
            enriched, warnings_list, meta = await _enrich_with_gamalytic_bulk(
                steamspy_games, mock_gamalytic,
                steamspy=mock_steamspy, primary_tag="Roguelike",
            )
        finally:
            mt.logger.warning = original_warning

        # Should have logged a warning about Tier 1.5 bottleneck
        tier15_warnings = [m for m in warning_messages if "Tier 1.5" in str(m)]
        assert len(tier15_warnings) >= 1, (
            f"Expected warning about large Tier 1.5 batch, got: {warning_messages}"
        )
        assert any("ETA" in str(m) or "bottleneck" in str(m) for m in tier15_warnings), (
            f"Warning should mention ETA/bottleneck: {tier15_warnings}"
        )

    @pytest.mark.asyncio
    async def test_tier1_5_recalculates_tier2_threshold(self):
        """Tier 2 threshold is based on total_revenue AFTER Tier 1.5 enrichment.

        Setup: 8 games enriched by Tier 1 at $1M each. 2 games unenriched.
        Tier 1.5 adds $10M to game 9 and $10M to game 10.
        Without Tier 1.5: total_revenue = 8M, threshold = 8000 (0.1% of 8M).
        With Tier 1.5: total_revenue = 28M, threshold = 28000 (0.1% of 28M).
        Tier 2 should be triggered for more games when threshold is higher/different.
        """
        from unittest.mock import AsyncMock, MagicMock
        from src.market_tools import _enrich_with_gamalytic_bulk, GAMALYTIC_REVENUE_THRESHOLD_PCT
        from src.schemas import CommercialData, EngagementData
        from datetime import datetime, timezone

        mock_gamalytic, steamspy_games = self._make_bulk_setup(
            gamalytic_appids=list(range(1, 9)),
            steamspy_appids=list(range(1, 11)),
        )

        now = datetime.now(timezone.utc)
        # Tier 1.5: appids 9 and 10 each get $10M
        mock_gamalytic.get_commercial_batch = AsyncMock(return_value=[
            CommercialData(
                appid=9, name="BigHidden9", price=19.99,
                revenue_min=9_000_000, revenue_max=11_000_000,
                confidence="high", source="gamalytic", fetched_at=now,
            ),
            CommercialData(
                appid=10, name="BigHidden10", price=19.99,
                revenue_min=9_000_000, revenue_max=11_000_000,
                confidence="high", source="gamalytic", fetched_at=now,
            ),
        ])

        mock_steamspy = MagicMock()
        mock_steamspy.get_engagement_data = AsyncMock(return_value=EngagementData(
            appid=9, name="BigHidden9", ccu=100, owners_midpoint=5000.0,
            owners_min=4000, owners_max=6000,
            average_forever=10.0, average_2weeks=2.0,
            median_forever=8.0, median_2weeks=1.0,
            positive=200, negative=10, price=19.99,
            review_score=90.0, tags={"Roguelike": 500},
            developer="Dev", publisher="Pub",
            fetched_at=now, total_reviews=210,
        ))

        enriched, warnings, meta = await _enrich_with_gamalytic_bulk(
            steamspy_games, mock_gamalytic,
            steamspy=mock_steamspy, primary_tag="Roguelike",
        )

        # Tier 1.5 meta: 2 unenriched games
        assert meta["tier1_5_count"] == 2
        assert meta["tier1_5_commercial_found"] == 2

        # total_revenue should include Tier 1.5 revenue
        # Tier 1: 8 games * $1M = $8M; Tier 1.5: 2 games * ~$10M midpoint = ~$20M
        # Total should be ~$28M
        assert meta["total_revenue"] > 8_000_000, (
            f"total_revenue {meta['total_revenue']} should include Tier 1.5 revenue"
        )

    @pytest.mark.asyncio
    async def test_tier1_5_api_error_handling(self):
        """Games where Tier 1.5 fetch fails retain revenue=None without crashing."""
        from unittest.mock import AsyncMock, MagicMock
        from src.market_tools import _enrich_with_gamalytic_bulk
        from src.schemas import APIError as APIErrorSchema

        # 2 unenriched games (appids 9 and 10)
        mock_gamalytic, steamspy_games = self._make_bulk_setup(
            gamalytic_appids=list(range(1, 9)),
            steamspy_appids=list(range(1, 11)),
        )
        # Tier 1.5 Gamalytic fetch returns all APIErrors
        mock_gamalytic.get_commercial_batch = AsyncMock(return_value=[
            APIErrorSchema(error_code=429, error_type="rate_limit", message="rate limited"),
            APIErrorSchema(error_code=500, error_type="api_error", message="server error"),
        ])

        mock_steamspy = MagicMock()
        mock_steamspy.get_engagement_data = AsyncMock(
            side_effect=Exception("SteamSpy unavailable")
        )

        # Should not raise; unenriched games keep revenue=None
        enriched, warnings, meta = await _enrich_with_gamalytic_bulk(
            steamspy_games, mock_gamalytic,
            steamspy=mock_steamspy, primary_tag="Roguelike",
        )

        # Games 9 and 10 should still have revenue=None (APIError not merged)
        game9 = next(g for g in enriched if g["appid"] == 9)
        game10 = next(g for g in enriched if g["appid"] == 10)
        assert game9.get("revenue") is None, "APIError game should retain revenue=None"
        assert game10.get("revenue") is None, "APIError game should retain revenue=None"

        # Meta still recorded the attempt
        assert meta["tier1_5_count"] == 2
        assert meta["tier1_5_commercial_found"] == 0  # all failed
        assert meta["tier1_5_total"] == 0  # nothing merged

    @pytest.mark.asyncio
    async def test_tier1_5_partial_success(self):
        """3 of 5 unenriched games succeed; 2 fail — partial enrichment works."""
        from unittest.mock import AsyncMock, MagicMock
        from src.market_tools import _enrich_with_gamalytic_bulk
        from src.schemas import CommercialData, APIError as APIErrorSchema
        from datetime import datetime, timezone

        # 8 enriched by Tier 1, appids 9-13 unenriched (5 games)
        mock_gamalytic, steamspy_games = self._make_bulk_setup(
            gamalytic_appids=list(range(1, 9)),
            steamspy_appids=list(range(1, 14)),
        )

        now = datetime.now(timezone.utc)
        # Tier 1.5: 3 succeed, 2 fail
        mock_gamalytic.get_commercial_batch = AsyncMock(return_value=[
            CommercialData(
                appid=9, name="Game9", price=9.99,
                revenue_min=400_000, revenue_max=600_000,
                confidence="medium", source="gamalytic", fetched_at=now,
            ),
            CommercialData(
                appid=10, name="Game10", price=9.99,
                revenue_min=400_000, revenue_max=600_000,
                confidence="medium", source="gamalytic", fetched_at=now,
            ),
            CommercialData(
                appid=11, name="Game11", price=9.99,
                revenue_min=400_000, revenue_max=600_000,
                confidence="medium", source="gamalytic", fetched_at=now,
            ),
            APIErrorSchema(error_code=404, error_type="not_found", message="not found"),
            APIErrorSchema(error_code=404, error_type="not_found", message="not found"),
        ])

        mock_steamspy = MagicMock()
        mock_steamspy.get_engagement_data = AsyncMock(return_value=None)

        enriched, warnings, meta = await _enrich_with_gamalytic_bulk(
            steamspy_games, mock_gamalytic,
            steamspy=mock_steamspy, primary_tag="Roguelike",
        )

        # Exactly 3 succeeded
        assert meta["tier1_5_commercial_found"] == 3, (
            f"Expected 3 successful Gamalytic results, got {meta['tier1_5_commercial_found']}"
        )
        assert meta["tier1_5_count"] == 5  # 5 unenriched

        # Games 9-11 have revenue; games 12-13 keep None
        for appid in [9, 10, 11]:
            game = next(g for g in enriched if g["appid"] == appid)
            assert game["revenue"] is not None, f"Game {appid} should have revenue"
        for appid in [12, 13]:
            game = next(g for g in enriched if g["appid"] == appid)
            assert game["revenue"] is None, f"Game {appid} should keep revenue=None (failed)"

    @pytest.mark.asyncio
    async def test_tier1_5_steamspy_none_still_merges_revenue(self):
        """When steamspy=None, Tier 1.5 still merges revenue from Gamalytic; tags stay empty."""
        from unittest.mock import AsyncMock, MagicMock
        from src.market_tools import _enrich_with_gamalytic_bulk
        from src.schemas import CommercialData
        from datetime import datetime, timezone

        # 2 unenriched games (appids 9, 10)
        mock_gamalytic, steamspy_games = self._make_bulk_setup(
            gamalytic_appids=list(range(1, 9)),
            steamspy_appids=list(range(1, 11)),
        )

        now = datetime.now(timezone.utc)
        mock_gamalytic.get_commercial_batch = AsyncMock(return_value=[
            CommercialData(
                appid=9, name="Game9", price=9.99,
                revenue_min=450_000, revenue_max=550_000,
                confidence="medium", source="gamalytic", fetched_at=now,
            ),
            CommercialData(
                appid=10, name="Game10", price=14.99,
                revenue_min=900_000, revenue_max=1_100_000,
                confidence="high", source="gamalytic", fetched_at=now,
            ),
        ])

        # steamspy=None — no SteamSpy client
        enriched, warnings, meta = await _enrich_with_gamalytic_bulk(
            steamspy_games, mock_gamalytic,
            steamspy=None, primary_tag="Roguelike",
        )

        # Revenue should be populated from Gamalytic even without SteamSpy
        game9 = next(g for g in enriched if g["appid"] == 9)
        game10 = next(g for g in enriched if g["appid"] == 10)
        assert game9["revenue"] is not None, "Game9 should have revenue from Gamalytic"
        assert game10["revenue"] is not None, "Game10 should have revenue from Gamalytic"

        # Tags should remain empty (no SteamSpy to populate them)
        assert game9.get("tags") == {}, f"Game9 tags should be empty dict, got {game9.get('tags')}"
        assert game10.get("tags") == {}, f"Game10 tags should be empty dict, got {game10.get('tags')}"

        # Meta: tags_found = 0 (no SteamSpy)
        assert meta["tier1_5_tags_found"] == 0
        assert meta["tier1_5_commercial_found"] == 2
        mock_gamalytic.get_commercial_data.assert_not_called()


class TestReleasedCountMethodology:
    """Tests for total_released_on_steam in methodology output.

    Verifies that analyze_market_single fires a parallel Released_DESC count
    request and reports it in methodology alongside the review-ranked analysis set.
    """

    def _make_search_result(self, tag: str = "Roguelike", n: int = 30):
        from src.schemas import GameSummary, SearchResult
        games = [
            GameSummary(
                appid=i, name=f"Game {i}", developer="Dev", publisher="Pub",
                ccu=100, owners_min=1000 * (n + 1 - i), owners_max=5000 * (n + 1 - i),
                owners_midpoint=float(3000 * (n + 1 - i)),
                average_forever=10.0, average_2weeks=1.0,
                median_forever=8.0, median_2weeks=0.5,
                positive=200, negative=10, price=9.99,
            )
            for i in range(1, n + 1)
        ]
        return SearchResult(
            tag=tag, appids=[g.appid for g in games], total_found=n,
            games=games, data_source="steamspy",
            fetched_at="2026-01-01T00:00:00Z", cache_age_seconds=0,
        )

    def _setup_single_tag_mocks(self, n_games=30, released_count=4563):
        """Set up mocks for single-tag path: SteamSpy succeeds, released count available."""
        from unittest.mock import AsyncMock, MagicMock

        mock_steamspy = MagicMock()
        mock_steam_store = MagicMock()
        mock_gamalytic = MagicMock()

        mock_steamspy.search_by_tag = AsyncMock(return_value=self._make_search_result(n=n_games))
        mock_steamspy.get_engagement_data = AsyncMock(return_value=None)

        _inline_games = [
            {"steamId": i, "name": f"Game {i}", "revenue": 500_000 * (n_games + 1 - i), "copiesSold": 10000}
            for i in range(1, n_games + 1)
        ]
        mock_gamalytic.list_games_by_tag = AsyncMock(return_value=_with_meta(_inline_games))
        mock_gamalytic.get_commercial_batch = AsyncMock(return_value=[])

        # Released count: get_tag_map resolves tag, get_total_released_count returns count
        mock_steam_store.get_tag_map = AsyncMock(return_value={"roguelike": 1234})
        mock_steam_store.get_total_released_count = AsyncMock(return_value=released_count)

        return mock_steamspy, mock_steam_store, mock_gamalytic

    def _setup_multi_tag_mocks(self, n_appids=50, released_count=3855):
        """Set up mocks for multi-tag path: Steam Store search + per-game enrichment."""
        from unittest.mock import AsyncMock, MagicMock
        from src.schemas import EngagementData
        from datetime import datetime, timezone

        mock_steamspy = MagicMock()
        mock_steam_store = MagicMock()
        mock_gamalytic = MagicMock()

        appids = list(range(101, 101 + n_appids))

        # Multi-tag path: get_tag_map + search_by_tag_ids_paginated
        mock_steam_store.get_tag_map = AsyncMock(return_value={
            "visual novel": 3799, "choices matter": 6426,
        })
        mock_steam_store.search_by_tag_ids_paginated = AsyncMock(
            return_value=(n_appids, appids)
        )
        mock_steam_store.get_total_released_count = AsyncMock(return_value=released_count)

        # SteamSpy per-game enrichment for multi-tag path (side_effect for correct per-appid dicts)
        def multi_tag_engagement(appid):
            return EngagementData(
                appid=appid, name=f"MultiTagGame{appid}", ccu=50, owners_min=500, owners_max=2000,
                owners_midpoint=1250.0, average_forever=5.0, average_2weeks=0.5,
                median_forever=4.0, median_2weeks=0.3, positive=100, negative=5, price=14.99,
                review_score=85.0, tags={"Visual Novel": 400, "Choices Matter": 300},
                developer="Dev", publisher="Pub",
                fetched_at=datetime.now(timezone.utc), total_reviews=105,
            )
        mock_steamspy.get_engagement_data = AsyncMock(side_effect=multi_tag_engagement)

        # Gamalytic per-game enrichment (multi-tag uses per-game, not bulk)
        mock_gamalytic.list_games_by_tag = AsyncMock(return_value=_with_meta([]))
        mock_gamalytic.get_commercial_batch = AsyncMock(return_value=[
            {"steamId": aid, "name": f"Game {aid}", "revenue": 100_000 * (i + 1), "copiesSold": 5000}
            for i, aid in enumerate(appids)
        ])

        return mock_steamspy, mock_steam_store, mock_gamalytic

    @pytest.mark.asyncio
    async def test_single_tag_reports_released_count(self):
        """Single-tag analysis includes total_released_on_steam in methodology."""
        from src.market_tools import analyze_market_single

        mock_spy, mock_store, mock_gam = self._setup_single_tag_mocks(
            n_games=30, released_count=4563,
        )

        result = await analyze_market_single(
            steamspy=mock_spy, steam_store=mock_store, gamalytic=mock_gam,
            tags=["Roguelike"], metrics=["concentration"], top_n=1,
        )

        assert "error" not in result
        meth = result["methodology"]
        assert meth["total_released_on_steam"] == 4563
        assert result["total_games"] == 30
        # Verify the note explains the difference
        notes = meth["notes"]
        assert len(notes) == 1
        assert "30 games" in notes[0]
        assert "4563 total released titles" in notes[0]
        # Verify correct tag IDs were used
        mock_store.get_total_released_count.assert_called_once_with([1234])

    @pytest.mark.asyncio
    async def test_single_tag_game_list_source_label(self):
        """Single-tag SteamSpy path shows 'SteamSpy tag search' in game_list_source."""
        from src.market_tools import analyze_market_single

        mock_spy, mock_store, mock_gam = self._setup_single_tag_mocks()

        result = await analyze_market_single(
            steamspy=mock_spy, steam_store=mock_store, gamalytic=mock_gam,
            tags=["Roguelike"], metrics=["concentration"], top_n=1,
        )

        assert "error" not in result
        assert "SteamSpy tag search" in result["methodology"]["game_list_source"]

    @pytest.mark.asyncio
    async def test_multi_tag_reports_released_count(self):
        """Multi-tag analysis includes total_released_on_steam in methodology."""
        from src.market_tools import analyze_market_single

        mock_spy, mock_store, mock_gam = self._setup_multi_tag_mocks(
            n_appids=50, released_count=3855,
        )

        result = await analyze_market_single(
            steamspy=mock_spy, steam_store=mock_store, gamalytic=mock_gam,
            tags=["Visual Novel", "Choices Matter"],
            metrics=["concentration"], top_n=1,
        )

        assert "error" not in result
        meth = result["methodology"]
        assert meth["total_released_on_steam"] == 3855
        assert result["total_games"] == 50
        # Note should reference both counts
        notes = meth["notes"]
        assert len(notes) == 1
        assert "50 games" in notes[0]
        assert "3855 total released titles" in notes[0]
        # Released count should use both tag IDs
        mock_store.get_total_released_count.assert_called_once_with([3799, 6426])

    @pytest.mark.asyncio
    async def test_multi_tag_game_list_source_label(self):
        """Multi-tag path shows 'Steam Store multi-tag search' in game_list_source."""
        from src.market_tools import analyze_market_single

        mock_spy, mock_store, mock_gam = self._setup_multi_tag_mocks()

        result = await analyze_market_single(
            steamspy=mock_spy, steam_store=mock_store, gamalytic=mock_gam,
            tags=["Visual Novel", "Choices Matter"],
            metrics=["concentration"], top_n=1,
        )

        assert "error" not in result
        source = result["methodology"]["game_list_source"]
        assert "Steam Store multi-tag search" in source
        assert "Visual Novel" in source
        assert "Choices Matter" in source

    @pytest.mark.asyncio
    async def test_released_count_graceful_on_tag_map_failure(self):
        """Released count is None (no crash, no note) when get_tag_map fails."""
        from unittest.mock import AsyncMock
        from src.schemas import APIError as APIErrorSchema
        from src.market_tools import analyze_market_single

        mock_spy, mock_store, mock_gam = self._setup_single_tag_mocks()
        # Override: tag map fails for the released-count call
        # get_tag_map is called TWICE: once by _fetch_game_list_steamspy (not needed for
        # single-tag SteamSpy success path), and once by _get_released_count.
        # We want _fetch_game_list_steamspy to succeed (SteamSpy path, no tag map needed),
        # but _get_released_count to fail.
        mock_store.get_tag_map = AsyncMock(return_value=APIErrorSchema(
            error_code=502, error_type="api_error", message="Steam down"
        ))

        result = await analyze_market_single(
            steamspy=mock_spy, steam_store=mock_store, gamalytic=mock_gam,
            tags=["Roguelike"], metrics=["concentration"], top_n=1,
        )

        assert "error" not in result
        meth = result["methodology"]
        # No released count, no note about it
        assert "total_released_on_steam" not in meth
        assert len(meth["notes"]) == 0

    @pytest.mark.asyncio
    async def test_released_count_graceful_on_count_failure(self):
        """Released count is None when get_total_released_count returns None."""
        from unittest.mock import AsyncMock
        from src.market_tools import analyze_market_single

        mock_spy, mock_store, mock_gam = self._setup_single_tag_mocks()
        mock_store.get_total_released_count = AsyncMock(return_value=None)

        result = await analyze_market_single(
            steamspy=mock_spy, steam_store=mock_store, gamalytic=mock_gam,
            tags=["Roguelike"], metrics=["concentration"], top_n=1,
        )

        assert "error" not in result
        meth = result["methodology"]
        assert "total_released_on_steam" not in meth
        assert len(meth["notes"]) == 0

    @pytest.mark.asyncio
    async def test_appids_path_skips_released_count(self):
        """Custom AppID list path does not fire released count request."""
        from unittest.mock import AsyncMock, MagicMock
        from src.schemas import EngagementData
        from src.market_tools import analyze_market_single
        from datetime import datetime, timezone

        mock_steamspy = MagicMock()
        mock_steam_store = MagicMock()
        mock_gamalytic = MagicMock()

        appids = [101, 102, 103]
        mock_steamspy.get_engagement_data = AsyncMock(return_value=EngagementData(
            appid=101, name="Game", ccu=50, owners_min=500, owners_max=2000,
            owners_midpoint=1250.0, average_forever=5.0, average_2weeks=0.5,
            median_forever=4.0, median_2weeks=0.3, positive=100, negative=5, price=9.99,
            review_score=85.0, tags={"Action": 100},
            developer="Dev", publisher="Pub",
            fetched_at=datetime.now(timezone.utc), total_reviews=105,
        ))
        mock_gamalytic.get_commercial_batch = AsyncMock(return_value=[
            {"steamId": aid, "name": f"Game {aid}", "revenue": 500_000, "copiesSold": 10000}
            for aid in appids
        ])
        mock_steam_store.get_total_released_count = AsyncMock()

        result = await analyze_market_single(
            steamspy=mock_steamspy, steam_store=mock_steam_store, gamalytic=mock_gamalytic,
            tags=[], appids=appids,
            metrics=["concentration"], top_n=1, skip_min_check=True,
        )

        assert "error" not in result
        meth = result["methodology"]
        # No released count for custom appid lists
        assert "total_released_on_steam" not in meth
        assert len(meth["notes"]) == 0
        # get_total_released_count should never be called
        mock_steam_store.get_total_released_count.assert_not_called()

    @pytest.mark.asyncio
    async def test_released_count_higher_than_analyzed(self):
        """Released count is always >= analyzed count — methodology note reflects the gap."""
        from src.market_tools import analyze_market_single

        mock_spy, mock_store, mock_gam = self._setup_single_tag_mocks(
            n_games=30, released_count=6876,
        )

        result = await analyze_market_single(
            steamspy=mock_spy, steam_store=mock_store, gamalytic=mock_gam,
            tags=["Roguelike"], metrics=["concentration"], top_n=1,
        )

        assert "error" not in result
        meth = result["methodology"]
        assert meth["total_released_on_steam"] == 6876
        assert result["total_games"] == 30
        # The note should clearly show the gap
        note = meth["notes"][0]
        assert "30 games" in note
        assert "6876 total released titles" in note
