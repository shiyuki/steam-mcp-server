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
        mock_gamalytic.list_games_by_tag = AsyncMock(return_value=gamalytic_games)
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
        mock_gamalytic.list_games_by_tag = AsyncMock(return_value=gamalytic_games)

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
        mock_gamalytic.list_games_by_tag = AsyncMock(return_value=gamalytic_games)
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
        mock_gamalytic.list_games_by_tag = AsyncMock(return_value=gamalytic_games)
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
        mock_gamalytic.list_games_by_tag = AsyncMock(return_value=[
            {"steamId": i, "name": f"Game {i}", "revenue": 100000} for i in range(1, 31)
        ])
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
                "tags": {"Roguelike": 1000, "Action": 800, "Indie": 600},
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

        flags = _validate_genre_membership(games, "Roguelike")

        # Total revenue ~80M + 100. Threshold = 80000100 * 0.001 = ~80000
        # Game 1 (50M) and Game 2 (30M) exceed threshold; Game 3 (100) does not
        assert len(flags) == 2

        rogue_flag = next(f for f in flags if f["appid"] == 1)
        assert rogue_flag["genre_valid"] is True
        assert rogue_flag["tag_rank"] == 1  # "Roguelike" is highest-weighted tag

        not_rogue_flag = next(f for f in flags if f["appid"] == 2)
        assert not_rogue_flag["genre_valid"] is False
        assert not_rogue_flag["tag_rank"] is None

    def test_skips_games_without_tag_data(self):
        """Games with empty tags dict (no Tier 2 enrichment) are skipped, not flagged."""
        from src.market_tools import _validate_genre_membership

        games = [
            {"appid": 1, "name": "NoTags", "revenue": 50_000_000, "tags": {}},
            {"appid": 2, "name": "NullTags", "revenue": 30_000_000, "tags": None},
            {"appid": 3, "name": "HasTags", "revenue": 20_000_000, "tags": {"Roguelike": 500}},
        ]

        flags = _validate_genre_membership(games, "Roguelike")

        # Only game 3 has tag data and exceeds threshold
        assert len(flags) == 1
        assert flags[0]["appid"] == 3
        assert flags[0]["genre_valid"] is True

    def test_case_insensitive_tag_matching(self):
        """Genre tag matching is case-insensitive."""
        from src.market_tools import _validate_genre_membership

        games = [
            {"appid": 1, "name": "Game1", "revenue": 1_000_000,
             "tags": {"roguelike": 1000, "action": 800}},
        ]

        flags = _validate_genre_membership(games, "Roguelike")
        assert len(flags) == 1
        assert flags[0]["genre_valid"] is True

    def test_returns_empty_for_no_revenue(self):
        """Returns empty list when total revenue is zero."""
        from src.market_tools import _validate_genre_membership

        games = [
            {"appid": 1, "name": "Free", "revenue": 0, "tags": {"Roguelike": 500}},
        ]

        flags = _validate_genre_membership(games, "Roguelike")
        assert flags == []

    def test_top_tags_shows_context(self):
        """Validation flags include top 5 tags for context."""
        from src.market_tools import _validate_genre_membership

        games = [
            {"appid": 1, "name": "Game1", "revenue": 1_000_000,
             "tags": {"Action": 1000, "Adventure": 900, "RPG": 800, "Indie": 700,
                      "Open World": 600, "Roguelike": 500}},
        ]

        flags = _validate_genre_membership(games, "Roguelike")
        assert len(flags) == 1
        assert flags[0]["genre_valid"] is True
        assert flags[0]["tag_rank"] == 6  # 6th tag
        assert len(flags[0]["top_tags"]) == 5
        assert flags[0]["top_tags"][0] == "Action"


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
        mock_steam_store.search_by_tag_ids = AsyncMock(return_value=(0, []))

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
        # search_by_tag_ids returns 0 results (no games match intersection)
        mock_steam_store.search_by_tag_ids = AsyncMock(return_value=(0, []))

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
        mock_gamalytic.list_games_by_tag = AsyncMock(return_value=gamalytic_games)

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
            review_score=96.2, tags={"Roguelike": 1000, "Action": 800, "Indie": 600},
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
        assert flag["tag_rank"] == 1  # "Roguelike" is top tag


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
        mock_gamalytic.list_games_by_tag = AsyncMock(return_value=gamalytic_games)
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
        mock_gamalytic.list_games_by_tag = AsyncMock(return_value=gamalytic_games)
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
        mock_gamalytic.list_games_by_tag = AsyncMock(return_value=[])

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
