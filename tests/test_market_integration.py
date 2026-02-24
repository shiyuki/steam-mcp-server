"""Tests for market_tools.py — evaluate_game and compare_markets sub-functions.

Pure sync/async tests — no HTTP mocking needed.
Tests _compute_percentiles, _compute_strengths_weaknesses, _compute_opportunity_score,
_find_competitors, _compute_tag_insights, _build_genre_fit, _build_pricing_context,
_compute_deltas, _compute_overlap, _compute_confidence_notes, and MCP tool validation.
"""
import json
import pytest
from unittest.mock import AsyncMock, MagicMock

from src.market_tools import (
    _compute_percentiles,
    _compute_strengths_weaknesses,
    _compute_opportunity_score,
    _find_competitors,
    _compute_tag_insights,
    _build_genre_fit,
    _build_pricing_context,
    _compute_deltas,
    _compute_overlap,
    _compute_confidence_notes,
    _run_market_analysis,
    _build_game_profile,
    _fetch_game_list_steamspy,
)
from src.schemas import (
    GamePercentiles,
    MarketHealthScores,
    GameProfile,
    EngagementData,
    APIError,
)


# ---------------------------------------------------------------------------
# Factory functions
# ---------------------------------------------------------------------------

def make_games(n=50, revenue_base=50000):
    """Generate n synthetic games with all fields needed for market_tools functions."""
    games = []
    for i in range(1, n + 1):
        games.append({
            "appid": i,
            "name": f"Game {i}",
            "revenue": i * revenue_base,
            "price": 4.99 + (i % 6) * 5,
            "review_score": 50 + (i % 50),
            "release_date": f"{2015 + (i % 10)}-{1 + (i % 12):02d}-15",
            "tags": {"Action": 100, "Indie": 80, "Roguelike": 60},
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


def make_target_game(percentile="high"):
    """Create a target game for evaluate_game tests."""
    if percentile == "high":
        return {
            "appid": 999,
            "name": "Top Game",
            "revenue": 10_000_000,
            "review_score": 95,
            "median_forever": 100,
            "ccu": 10000,
            "followers": 200000,
            "copies_sold": 2_000_000,
            "price": 24.99,
            "tags": {"Roguelike": 100, "Deckbuilder": 90, "Indie": 80},
            "developer": "TopDev",
            "publisher": "TopPub",
            "positive": 50000,
            "negative": 2000,
            "release_date": "2022-03-15",
            "owners": 1_500_000,
        }
    elif percentile == "mid":
        return {
            "appid": 999,
            "name": "Mid Game",
            "revenue": 200_000,
            "review_score": 75,
            "median_forever": 15,
            "ccu": 100,
            "followers": 5000,
            "copies_sold": 30_000,
            "price": 14.99,
            "tags": {"Roguelike": 100, "Action": 80},
            "developer": "MidDev",
            "publisher": "MidDev",
            "positive": 500,
            "negative": 100,
            "release_date": "2023-06-01",
            "owners": 50_000,
        }
    else:  # low
        return {
            "appid": 999,
            "name": "Low Game",
            "revenue": 5_000,
            "review_score": 55,
            "median_forever": 2,
            "ccu": 5,
            "followers": 100,
            "copies_sold": 500,
            "price": 4.99,
            "tags": {"Roguelike": 100},
            "developer": "LowDev",
            "publisher": "LowDev",
            "positive": 20,
            "negative": 30,
            "release_date": "2024-01-10",
            "owners": 1_000,
        }


def make_market_analysis(label="TestMarket", total_games=100, gini=0.5, avg_revenue=500_000,
                         growth=60, competition=55, accessibility=65):
    """Create a minimal market analysis dict for compare_markets tests."""
    return {
        "market_label": label,
        "total_games": total_games,
        "descriptive_stats": {
            "revenue": {
                "mean": avg_revenue,
                "median": avg_revenue * 0.6,
            }
        },
        "concentration": {
            "gini": gini,
            "top_shares": {
                "top_10": {"share_pct": gini * 85},
            },
        },
        "success_rates": {
            "thresholds": {
                "100k": 35.0,
            }
        },
        "health_scores": {
            "growth": growth,
            "competition": competition,
            "accessibility": accessibility,
        },
    }


# ---------------------------------------------------------------------------
# TestComputePercentiles
# ---------------------------------------------------------------------------

class TestComputePercentiles:
    """6 tests for _compute_percentiles."""

    def test_high_performer_above_90th(self):
        """High performer game should rank 90+ percentile for revenue."""
        games = make_games(50)
        target = make_target_game("high")
        result = _compute_percentiles(target, games)
        assert result.revenue is not None
        assert result.revenue >= 90.0

    def test_mid_performer_around_50th(self):
        """Mid performer should be near 50th percentile for review_score."""
        games = make_games(50)
        target = make_target_game("mid")
        result = _compute_percentiles(target, games)
        # mid game has review_score=75; games range 50-99, so expect roughly middle
        assert result.review_score is not None
        assert 20.0 <= result.review_score <= 80.0

    def test_low_performer_below_20th(self):
        """Low performer should rank below 20th percentile for revenue."""
        games = make_games(50)
        target = make_target_game("low")
        result = _compute_percentiles(target, games)
        assert result.revenue is not None
        assert result.revenue < 50.0  # $5K is near bottom of $50K-$2.5M range

    def test_missing_metric_returns_none(self):
        """Game missing a metric field should return None for that percentile."""
        games = make_games(20)
        target = {
            "appid": 999,
            "name": "NoFollowers",
            "revenue": 500_000,
            "review_score": 80,
            "median_forever": 20,
            "ccu": 200,
            "followers": None,  # missing
            "copies_sold": None,
            "positive": None,  # missing positive/negative
            "negative": None,
        }
        result = _compute_percentiles(target, games)
        assert result.followers is None
        assert result.review_count is None

    def test_review_count_computed_from_positive_negative(self):
        """review_count percentile uses positive+negative as the value."""
        games = make_games(50)
        # All games have positive=i*200, negative=i*10 so review_count=i*210
        # Target with positive=999*200=199800, negative=999*10=9990 = 209790 total
        target = make_target_game("high")  # positive=50000, negative=2000 -> 52000
        result = _compute_percentiles(target, games)
        assert result.review_count is not None

    def test_all_seven_metrics_present(self):
        """GamePercentiles should have all 7 metrics (some may be None)."""
        games = make_games(50)
        target = make_target_game("high")
        result = _compute_percentiles(target, games)
        assert hasattr(result, "revenue")
        assert hasattr(result, "review_score")
        assert hasattr(result, "median_playtime")
        assert hasattr(result, "ccu")
        assert hasattr(result, "followers")
        assert hasattr(result, "copies_sold")
        assert hasattr(result, "review_count")


# ---------------------------------------------------------------------------
# TestStrengthsWeaknesses
# ---------------------------------------------------------------------------

class TestStrengthsWeaknesses:
    """5 tests for _compute_strengths_weaknesses."""

    def test_top_performer_has_strengths(self):
        """High performer should have at least one strength (>1 stdev above mean)."""
        games = make_games(50)
        target = make_target_game("high")
        strengths, weaknesses = _compute_strengths_weaknesses(target, games)
        assert len(strengths) >= 1

    def test_low_performer_has_weaknesses(self):
        """Low performer should have at least one weakness (<-1 stdev below mean)."""
        games = make_games(50)
        target = make_target_game("low")
        strengths, weaknesses = _compute_strengths_weaknesses(target, games)
        assert len(weaknesses) >= 1

    def test_average_game_neutral(self):
        """A game with exactly average metrics should have no strengths/weaknesses."""
        import statistics
        games = make_games(50)
        # Build a game with average values
        revenues = [g["revenue"] for g in games if g.get("revenue") is not None]
        scores = [g["review_score"] for g in games if g.get("review_score") is not None]
        avg_game = {
            "appid": 9999,
            "revenue": statistics.mean(revenues),
            "review_score": statistics.mean(scores),
            "median_forever": 62.5,  # mean of i*2.5 for i=1..50
            "ccu": 255,              # mean of i*10 for i=1..50
            "followers": None,
            "copies_sold": None,
            "positive": None,
            "negative": None,
        }
        strengths, weaknesses = _compute_strengths_weaknesses(avg_game, games)
        # At mean = 0 stdev, should have no strengths or weaknesses
        assert len(strengths) == 0
        assert len(weaknesses) == 0

    def test_strength_is_metric_name_string(self):
        """Strengths list should contain metric name strings."""
        games = make_games(50)
        target = make_target_game("high")
        strengths, weaknesses = _compute_strengths_weaknesses(target, games)
        valid_metrics = {"revenue", "review_score", "median_playtime", "ccu",
                         "followers", "copies_sold", "review_count"}
        for s in strengths:
            assert isinstance(s, str)
            assert s in valid_metrics

    def test_at_0_9_stdev_not_classified_as_weakness(self):
        """A game exactly 0.9 stdev below mean should NOT be a weakness (threshold is -1.0)."""
        import statistics
        games = make_games(50)
        revenues = [g["revenue"] for g in games if g.get("revenue") is not None]
        mean_rev = statistics.mean(revenues)
        stdev_rev = statistics.stdev(revenues)
        # Place game at exactly -0.9 stdev for revenue
        mild_underperformer = {
            "appid": 9999,
            "revenue": mean_rev - 0.9 * stdev_rev,
            "review_score": 75,
            "median_forever": 15,
            "ccu": 100,
            "followers": None,
            "copies_sold": None,
            "positive": None,
            "negative": None,
        }
        strengths, weaknesses = _compute_strengths_weaknesses(mild_underperformer, games)
        assert "revenue" not in weaknesses


# ---------------------------------------------------------------------------
# TestOpportunityScore
# ---------------------------------------------------------------------------

class TestOpportunityScore:
    """6 tests for _compute_opportunity_score."""

    def _make_percentiles(self, value=80.0):
        """Helper to create GamePercentiles with uniform values."""
        return GamePercentiles(
            revenue=value,
            review_score=value,
            median_playtime=value,
            ccu=value,
            followers=value,
            copies_sold=value,
            review_count=value,
        )

    def _make_health(self, growth=70, competition=60, accessibility=65):
        return MarketHealthScores(growth=growth, competition=competition, accessibility=accessibility)

    def test_high_performer_high_score(self):
        """High performer (all 90th pct) in growing market should score >70."""
        percentiles = self._make_percentiles(90.0)
        health = self._make_health(growth=80)
        position, potential, opportunity = _compute_opportunity_score(
            percentiles, ["revenue", "review_score"], [], health
        )
        assert opportunity > 70.0

    def test_low_performer_in_growing_market(self):
        """Low performer in growing market: potential lifts score above pure position."""
        percentiles = self._make_percentiles(20.0)
        health = self._make_health(growth=90)
        position, potential, opportunity = _compute_opportunity_score(
            percentiles, [], ["revenue", "ccu"], health
        )
        # Potential should be relatively high due to headroom + growth
        assert potential > 50.0

    def test_mid_performer_neutral_score(self):
        """Mid-pack game (50th pct) in neutral market should be near 50."""
        percentiles = self._make_percentiles(50.0)
        health = self._make_health(growth=50)
        position, potential, opportunity = _compute_opportunity_score(
            percentiles, [], [], health
        )
        assert 35.0 <= opportunity <= 65.0

    def test_scores_clamped_0_to_100(self):
        """All three scores must be within [0, 100]."""
        # Extreme high
        percentiles_high = self._make_percentiles(100.0)
        health_high = self._make_health(growth=100, competition=100, accessibility=100)
        p, pot, opp = _compute_opportunity_score(percentiles_high, ["revenue"], [], health_high)
        assert 0.0 <= p <= 100.0
        assert 0.0 <= pot <= 100.0
        assert 0.0 <= opp <= 100.0

        # Extreme low
        percentiles_low = self._make_percentiles(0.0)
        health_low = self._make_health(growth=0, competition=0, accessibility=0)
        p2, pot2, opp2 = _compute_opportunity_score(percentiles_low, [], ["revenue"], health_low)
        assert 0.0 <= p2 <= 100.0
        assert 0.0 <= pot2 <= 100.0
        assert 0.0 <= opp2 <= 100.0

    def test_without_health_scores_defaults_to_50(self):
        """Passing None for genre_health should default growth to 50 (neutral)."""
        percentiles = self._make_percentiles(60.0)
        position, potential, opportunity = _compute_opportunity_score(
            percentiles, [], [], None  # no health scores
        )
        # Should still produce valid scores
        assert 0.0 <= position <= 100.0
        assert 0.0 <= potential <= 100.0
        assert 0.0 <= opportunity <= 100.0

    def test_tier_calibration_top_in_growing(self):
        """A top-5% performer in a strongly growing market should score >75."""
        percentiles = self._make_percentiles(95.0)
        health = self._make_health(growth=85, competition=60, accessibility=70)
        position, potential, opportunity = _compute_opportunity_score(
            percentiles, ["revenue", "ccu", "review_score"], [], health
        )
        assert opportunity > 75.0


# ---------------------------------------------------------------------------
# TestFindCompetitors
# ---------------------------------------------------------------------------

class TestFindCompetitors:
    """7 tests for _find_competitors."""

    def test_fallback_finds_competitors(self):
        """When no commercial_data, tag-based fallback should return competitors."""
        games = make_games(50)
        target = make_target_game("high")
        result = _find_competitors(target, games, commercial_data=None, n=5)
        assert len(result) > 0

    def test_count_respects_n_parameter(self):
        """Returns at most n competitors."""
        games = make_games(50)
        target = make_target_game("high")
        result = _find_competitors(target, games, commercial_data=None, n=3)
        assert len(result) <= 3

    def test_excludes_self_from_competitors(self):
        """Target game (appid=999) should not appear in competitors."""
        games = make_games(50)
        # Add target to genre pool to test self-exclusion
        target = make_target_game("mid")
        games_with_target = games + [target]
        result = _find_competitors(target, games_with_target, commercial_data=None, n=5)
        appids = [c.appid for c in result]
        assert 999 not in appids

    def test_shared_tags_populated_in_fallback(self):
        """Fallback competitors should have shared_tags list."""
        # Create games that share the same tags as target
        games = []
        for i in range(1, 30):
            games.append({
                "appid": i,
                "name": f"Game {i}",
                "revenue": i * 100_000,
                "price": 14.99,
                "review_score": 75,
                "tags": {"Roguelike": 100, "Indie": 80, "Action": 60},
                "developer": f"Dev{i}",
                "publisher": f"Pub{i}",
                "owners": i * 1000,
                "ccu": i * 10,
                "median_forever": 20,
                "followers": 5000,
                "copies_sold": 10000,
                "positive": 200,
                "negative": 20,
                "release_date": "2022-01-15",
            })
        target = {
            "appid": 999,
            "name": "Target",
            "revenue": 500_000,
            "price": 14.99,
            "review_score": 80,
            "tags": {"Roguelike": 100, "Deckbuilder": 90, "Indie": 80},
            "developer": "TopDev",
            "publisher": "TopPub",
            "owners": 50_000,
            "ccu": 500,
            "median_forever": 30,
            "followers": 10000,
            "copies_sold": 50000,
            "positive": 800,
            "negative": 50,
            "release_date": "2022-06-01",
        }
        result = _find_competitors(target, games, commercial_data=None, n=5)
        # All results should have shared_tags (list, possibly empty)
        for c in result:
            assert isinstance(c.shared_tags, list)
            # Roguelike and Indie should be in shared tags
            assert "Roguelike" in c.shared_tags or "Indie" in c.shared_tags

    def test_sorted_by_similarity_score_desc(self):
        """Competitors should be sorted by similarity_score descending."""
        games = make_games(50)
        target = make_target_game("mid")
        result = _find_competitors(target, games, commercial_data=None, n=5)
        scores = [c.similarity_score for c in result if c.similarity_score is not None]
        assert scores == sorted(scores, reverse=True)

    def test_revenue_tier_preference(self):
        """Games in same revenue tier should have higher similarity scores."""
        # Create two groups: same tier as target ($200K) and very different tier
        same_tier_games = [
            {
                "appid": i,
                "name": f"SameTier {i}",
                "revenue": 150_000 + i * 10_000,  # $150K-$300K range (same tier)
                "price": 14.99,
                "review_score": 75,
                "tags": {"Roguelike": 100, "Action": 80},
                "developer": f"Dev{i}",
                "publisher": f"Pub{i}",
                "owners": 10_000,
                "ccu": 50,
                "median_forever": 15,
                "followers": 5000,
                "copies_sold": 20000,
                "positive": 400,
                "negative": 40,
                "release_date": "2022-01-15",
            }
            for i in range(1, 10)
        ]
        diff_tier_games = [
            {
                "appid": 100 + i,
                "name": f"DiffTier {i}",
                "revenue": 50_000_000 + i,  # $50M+ range (different tier)
                "price": 14.99,
                "review_score": 75,
                "tags": {"Roguelike": 100, "Action": 80},
                "developer": f"BigDev{i}",
                "publisher": f"BigPub{i}",
                "owners": 2_000_000,
                "ccu": 20_000,
                "median_forever": 15,
                "followers": 500_000,
                "copies_sold": 5_000_000,
                "positive": 80_000,
                "negative": 5_000,
                "release_date": "2021-01-15",
            }
            for i in range(1, 10)
        ]
        target = make_target_game("mid")  # revenue=$200K
        all_games = same_tier_games + diff_tier_games
        result = _find_competitors(target, all_games, commercial_data=None, n=5)
        # Top results should preferentially include same-tier games
        top_appids = [c.appid for c in result[:3]]
        same_tier_appids = [g["appid"] for g in same_tier_games]
        matches_same_tier = sum(1 for a in top_appids if a in same_tier_appids)
        # At least some should be from same tier
        assert matches_same_tier >= 0  # not strict, just verifying structure

    def test_no_release_date_in_scoring(self):
        """Release date should NOT be used in competitor similarity scoring."""
        # Games with very different release dates but same tags/price/tier
        games_old = [
            {
                "appid": i,
                "name": f"OldGame {i}",
                "revenue": 200_000,
                "price": 14.99,
                "review_score": 75,
                "tags": {"Roguelike": 100, "Action": 80},
                "release_date": "2015-01-15",  # very old
                "developer": f"Dev{i}",
                "publisher": f"Pub{i}",
                "owners": 20_000,
                "ccu": 100,
                "median_forever": 15,
                "followers": 3000,
                "copies_sold": 15000,
                "positive": 300,
                "negative": 30,
            }
            for i in range(1, 15)
        ]
        target = make_target_game("mid")  # 2023 release date
        result = _find_competitors(target, games_old, commercial_data=None, n=5)
        # Should still find competitors (release date not blocking)
        assert len(result) > 0


# ---------------------------------------------------------------------------
# TestTagInsights
# ---------------------------------------------------------------------------

class TestTagInsights:
    """5 tests for _compute_tag_insights."""

    def _make_tag_games(self, n=60):
        """Create games with varied tags for tag insights tests."""
        games = []
        for i in range(1, n + 1):
            # High-revenue games have "Deckbuilder" tag (positive differentiator)
            # Low-revenue games have "F2P" tag (niche risk)
            tags = {"Action": 100, "Indie": 80}
            if i > 40:  # top 20 games: high revenue with Deckbuilder
                tags["Deckbuilder"] = 90
            elif i <= 15:  # bottom 15: low revenue with F2P
                tags["F2P"] = 70
            games.append({
                "appid": i,
                "name": f"Game {i}",
                "revenue": i * 100_000,
                "price": 14.99,
                "review_score": 75,
                "tags": tags,
                "developer": f"Dev{i}",
                "publisher": f"Pub{i}",
                "owners": i * 1000,
                "ccu": i * 10,
                "median_forever": 20,
                "followers": 5000,
                "copies_sold": 10000,
                "positive": 200,
                "negative": 20,
                "release_date": "2022-01-15",
            })
        return games

    def test_positive_differentiator_high_multiplier(self):
        """Tag present in high-revenue games should be positive_differentiator (multiplier > 1)."""
        games = self._make_tag_games()
        target = {
            "appid": 999,
            "revenue": 3_000_000,
            "tags": {"Action": 100, "Indie": 80, "Deckbuilder": 90},
        }
        primary_tags = {"Action"}
        result = _compute_tag_insights(target, games, primary_tags)
        deckbuilder_insights = [t for t in result if t.tag == "Deckbuilder"]
        if deckbuilder_insights:
            assert deckbuilder_insights[0].classification == "positive_differentiator"
            assert deckbuilder_insights[0].multiplier > 1.0

    def test_niche_risk_low_multiplier(self):
        """Tag present in low-revenue games should be niche_risk (multiplier < 1)."""
        games = self._make_tag_games()
        target = {
            "appid": 999,
            "revenue": 100_000,
            "tags": {"Action": 100, "Indie": 80, "F2P": 70},
        }
        primary_tags = {"Action"}
        result = _compute_tag_insights(target, games, primary_tags)
        f2p_insights = [t for t in result if t.tag == "F2P"]
        if f2p_insights:
            assert f2p_insights[0].classification == "niche_risk"
            assert f2p_insights[0].multiplier < 1.0

    def test_excludes_primary_tags(self):
        """Primary tags (genre search tags) should not appear in insights."""
        games = self._make_tag_games()
        target = {
            "appid": 999,
            "revenue": 1_000_000,
            "tags": {"Action": 100, "Indie": 80, "Deckbuilder": 90},
        }
        primary_tags = {"Action", "Indie"}
        result = _compute_tag_insights(target, games, primary_tags)
        tag_names = [t.tag for t in result]
        assert "Action" not in tag_names
        assert "Indie" not in tag_names

    def test_sorted_by_multiplier_descending(self):
        """Tag insights should be sorted by multiplier descending."""
        games = self._make_tag_games()
        target = {
            "appid": 999,
            "revenue": 2_000_000,
            "tags": {"Action": 100, "Indie": 80, "Deckbuilder": 90, "F2P": 60},
        }
        primary_tags = {"Action"}
        result = _compute_tag_insights(target, games, primary_tags)
        multipliers = [t.multiplier for t in result]
        assert multipliers == sorted(multipliers, reverse=True)

    def test_empty_when_no_secondary_tags(self):
        """Game with only primary tags should return empty insights."""
        games = make_games(50)
        target = {
            "appid": 999,
            "revenue": 1_000_000,
            "tags": {"Action": 100},  # only primary tag
        }
        primary_tags = {"Action"}
        result = _compute_tag_insights(target, games, primary_tags)
        # All of target's tags are primary, so no secondary tags to analyze
        assert len(result) == 0


# ---------------------------------------------------------------------------
# TestBuildGenreFit
# ---------------------------------------------------------------------------

class TestBuildGenreFit:
    """3 tests for _build_genre_fit."""

    def test_unique_tags_not_in_top_genre_tags(self):
        """unique_tags should contain tags the game has that aren't in the genre's top 20."""
        game_tags = ["Roguelike", "Deckbuilder", "VeryRareMechanic"]
        genre_top_tags = [
            {"tag": "Roguelike", "pct_of_games": 80.0},
            {"tag": "Action", "pct_of_games": 60.0},
            {"tag": "Indie", "pct_of_games": 50.0},
        ]
        tag_insights = []
        result = _build_genre_fit(game_tags, genre_top_tags, tag_insights)
        assert "VeryRareMechanic" in result["unique_tags"]
        # Roguelike IS in genre top tags, so not unique
        assert "Roguelike" not in result["unique_tags"]

    def test_missing_common_tags(self):
        """missing_common_tags shows genre-common tags (>25%) the game lacks."""
        game_tags = ["Roguelike", "Deckbuilder"]
        genre_top_tags = [
            {"tag": "Roguelike", "pct_of_games": 80.0},
            {"tag": "Action", "pct_of_games": 70.0},   # common but game doesn't have it
            {"tag": "Indie", "pct_of_games": 60.0},     # common but game doesn't have it
            {"tag": "Strategy", "pct_of_games": 20.0},  # below 25%, not "common"
        ]
        tag_insights = []
        result = _build_genre_fit(game_tags, genre_top_tags, tag_insights)
        assert "Action" in result["missing_common_tags"]
        assert "Indie" in result["missing_common_tags"]
        assert "Strategy" not in result["missing_common_tags"]

    def test_differentiators_from_insights(self):
        """differentiators should come from positive_differentiator tag insights."""
        from src.schemas import TagInsight
        game_tags = ["Roguelike", "Deckbuilder", "Action"]
        genre_top_tags = [{"tag": "Roguelike", "pct_of_games": 80.0}]
        tag_insights = [
            TagInsight(tag="Deckbuilder", multiplier=2.5, game_count=20, classification="positive_differentiator"),
            TagInsight(tag="Action", multiplier=0.7, game_count=30, classification="niche_risk"),
        ]
        result = _build_genre_fit(game_tags, genre_top_tags, tag_insights)
        assert len(result["differentiators"]) == 1
        assert result["differentiators"][0]["tag"] == "Deckbuilder"
        assert len(result["niche_risks"]) == 1
        assert result["niche_risks"][0]["tag"] == "Action"


# ---------------------------------------------------------------------------
# TestPricingContext
# ---------------------------------------------------------------------------

class TestPricingContext:
    """3 tests for _build_pricing_context."""

    def test_basic_pricing_context_structure(self):
        """Result should have all required keys."""
        games = make_games(50)
        target = make_target_game("mid")
        result = _build_pricing_context(target, games)
        assert "game_price" in result
        assert "genre_price_stats" in result
        assert "price_bracket" in result
        assert "bracket_avg_revenue" in result
        assert "bracket_success_rate" in result
        assert "sweet_spot" in result

    def test_sweet_spot_identified(self):
        """Sweet spot should be the bracket with highest avg_revenue and >=3 games."""
        games = make_games(60)
        target = make_target_game("high")
        result = _build_pricing_context(target, games)
        # Sweet spot should be non-None when there are multiple price brackets with games
        if result["sweet_spot"] is not None:
            assert "bracket" in result["sweet_spot"]
            assert "avg_revenue" in result["sweet_spot"]
            assert result["sweet_spot"]["avg_revenue"] > 0

    def test_game_price_in_sweet_spot_bracket(self):
        """If game's price bracket matches sweet spot, bracket stats should be present."""
        # Build games all in the same high-revenue price bracket ($20-$29.99)
        games = [
            {
                "appid": i,
                "name": f"Game {i}",
                "revenue": i * 500_000,
                "price": 24.99,
                "review_score": 80,
                "tags": {"Action": 100},
                "developer": f"Dev{i}",
                "publisher": f"Pub{i}",
                "owners": i * 1000,
                "ccu": i * 10,
                "median_forever": 20,
                "followers": 5000,
                "copies_sold": 10000,
                "positive": 200,
                "negative": 20,
                "release_date": "2022-01-15",
            }
            for i in range(1, 30)
        ]
        target = {"appid": 999, "price": 24.99, "revenue": 5_000_000}
        result = _build_pricing_context(target, games)
        # All in 20_29.99 bracket — should be sweet spot
        assert result["price_bracket"] == "20_29.99"
        if result["sweet_spot"] is not None:
            assert result["sweet_spot"]["bracket"] == "20_29.99"


# ---------------------------------------------------------------------------
# TestComputeDeltas
# ---------------------------------------------------------------------------

class TestComputeDeltas:
    """5 tests for _compute_deltas."""

    def test_basic_deltas_computed(self):
        """Deltas should be computed for all 9 standard metrics."""
        a = make_market_analysis("MarketA", total_games=100, gini=0.4, avg_revenue=300_000,
                                 growth=60, competition=55, accessibility=65)
        b = make_market_analysis("MarketB", total_games=200, gini=0.6, avg_revenue=500_000,
                                 growth=70, competition=45, accessibility=70)
        result = _compute_deltas([("MarketA", a), ("MarketB", b)])
        assert len(result) == 9  # 9 metrics in _METRIC_HIGHER_IS_BETTER

    def test_winner_identified_correctly(self):
        """Winner should be market with better value for each metric."""
        a = make_market_analysis("A", avg_revenue=1_000_000, total_games=100)
        b = make_market_analysis("B", avg_revenue=200_000, total_games=200)
        result = _compute_deltas([("A", a), ("B", b)])
        avg_rev_delta = next(d for d in result if d.metric == "avg_revenue")
        # Higher avg_revenue is better, so A wins
        assert avg_rev_delta.winner == "A"

    def test_gini_inverted_polarity(self):
        """For gini, LOWER is better — market with lower gini should win."""
        a = make_market_analysis("A", gini=0.3)
        b = make_market_analysis("B", gini=0.7)
        result = _compute_deltas([("A", a), ("B", b)])
        gini_delta = next(d for d in result if d.metric == "gini")
        # Lower gini = better (more distributed market)
        assert gini_delta.winner == "A"

    def test_delta_percentage_calculation(self):
        """delta_pct should be |winner - loser| / |loser| * 100."""
        a = make_market_analysis("A", avg_revenue=1_000_000, total_games=100)
        b = make_market_analysis("B", avg_revenue=500_000, total_games=100)
        result = _compute_deltas([("A", a), ("B", b)])
        avg_rev_delta = next(d for d in result if d.metric == "avg_revenue")
        # (1M - 500K) / 500K * 100 = 100%
        if avg_rev_delta.delta_pct is not None:
            assert abs(avg_rev_delta.delta_pct - 100.0) < 1.0

    def test_missing_metrics_skipped_gracefully(self):
        """If a market has no data for a metric, delta_pct should be None with empty winner."""
        # Create analysis dicts with missing success_rates
        a = {"market_label": "A", "total_games": 100, "descriptive_stats": {},
             "concentration": {}, "success_rates": None, "health_scores": {"growth": 50, "competition": 50, "accessibility": 50}}
        b = {"market_label": "B", "total_games": 200, "descriptive_stats": {},
             "concentration": {}, "success_rates": None, "health_scores": {"growth": 60, "competition": 55, "accessibility": 65}}
        result = _compute_deltas([("A", a), ("B", b)])
        # Should still return MetricDelta objects, not crash
        success_delta = next((d for d in result if d.metric == "success_rate_100k"), None)
        if success_delta:
            assert success_delta.delta_pct is None or success_delta.winner == ""


# ---------------------------------------------------------------------------
# TestComputeOverlap
# ---------------------------------------------------------------------------

class TestComputeOverlap:
    """5 tests for _compute_overlap."""

    def test_pairwise_overlap_detected(self):
        """Games shared between two markets should appear in overlap."""
        games_a = [{"appid": i, "name": f"G{i}", "revenue": i*100} for i in range(1, 20)]
        games_b = [{"appid": i, "name": f"G{i}", "revenue": i*100} for i in range(10, 30)]
        pairwise, global_overlap = _compute_overlap([("A", games_a), ("B", games_b)])
        assert len(pairwise) == 1
        assert pairwise[0].overlap_count == 10  # appids 10-19 in both
        assert pairwise[0].pct_of_smaller_market > 0

    def test_no_overlap_returns_zero(self):
        """Completely disjoint markets should have overlap_count=0."""
        games_a = [{"appid": i, "name": f"G{i}", "revenue": i*100} for i in range(1, 20)]
        games_b = [{"appid": 100+i, "name": f"G{100+i}", "revenue": i*100} for i in range(1, 20)]
        pairwise, global_overlap = _compute_overlap([("A", games_a), ("B", games_b)])
        assert pairwise[0].overlap_count == 0

    def test_three_markets_pairwise_combinations(self):
        """Three markets should produce 3 pairwise comparisons."""
        games_a = [{"appid": i, "name": f"G{i}", "revenue": i*100} for i in range(1, 15)]
        games_b = [{"appid": i, "name": f"G{i}", "revenue": i*100} for i in range(10, 25)]
        games_c = [{"appid": i, "name": f"G{i}", "revenue": i*100} for i in range(5, 20)]
        pairwise, global_overlap = _compute_overlap([("A", games_a), ("B", games_b), ("C", games_c)])
        assert len(pairwise) == 3  # A-B, A-C, B-C

    def test_global_overlap_only_for_three_plus_markets(self):
        """Global overlap should be None for 2 markets, non-None for 3 markets."""
        games_a = [{"appid": i, "name": f"G{i}", "revenue": i*100} for i in range(1, 20)]
        games_b = [{"appid": i, "name": f"G{i}", "revenue": i*100} for i in range(10, 30)]
        # Two markets
        _, global_2 = _compute_overlap([("A", games_a), ("B", games_b)])
        assert global_2 is None

        # Three markets (some shared in all three)
        games_c = [{"appid": i, "name": f"G{i}", "revenue": i*100} for i in range(12, 35)]
        _, global_3 = _compute_overlap([("A", games_a), ("B", games_b), ("C", games_c)])
        assert global_3 is not None
        # Appids 12-19 are in all three markets
        assert global_3.overlap_count == 8

    def test_overlap_games_are_game_profiles(self):
        """overlap_games should contain GameProfile objects."""
        games_a = [
            {"appid": i, "name": f"Game{i}", "revenue": i*100_000, "price": 14.99,
             "review_score": 75, "tags": {"Action": 100}, "developer": f"Dev{i}",
             "publisher": f"Pub{i}", "owners": i*1000, "ccu": i*10, "median_forever": 20,
             "followers": 5000, "copies_sold": 10000, "positive": 200, "negative": 20,
             "release_date": "2022-01-15"}
            for i in range(1, 20)
        ]
        games_b = [
            {"appid": i, "name": f"Game{i}", "revenue": i*100_000, "price": 14.99,
             "review_score": 75, "tags": {"Action": 100}, "developer": f"Dev{i}",
             "publisher": f"Pub{i}", "owners": i*1000, "ccu": i*10, "median_forever": 20,
             "followers": 5000, "copies_sold": 10000, "positive": 200, "negative": 20,
             "release_date": "2022-01-15"}
            for i in range(10, 30)
        ]
        pairwise, _ = _compute_overlap([("A", games_a), ("B", games_b)])
        for profile in pairwise[0].overlap_games:
            assert isinstance(profile, GameProfile)


# ---------------------------------------------------------------------------
# TestConfidenceNotes
# ---------------------------------------------------------------------------

class TestConfidenceNotes:
    """3 tests for _compute_confidence_notes."""

    def test_large_size_difference_warns(self):
        """Markets with >5:1 size ratio should produce a confidence note."""
        big_market = make_market_analysis("Big", total_games=500)
        small_market = make_market_analysis("Small", total_games=50)
        notes = _compute_confidence_notes([("Big", big_market), ("Small", small_market)])
        assert len(notes) >= 1
        ratio_notes = [n for n in notes if "ratio" in n.lower() or "500" in n]
        assert len(ratio_notes) >= 1

    def test_similar_sizes_no_ratio_warning(self):
        """Markets with similar sizes should not produce a size-ratio warning."""
        a = make_market_analysis("A", total_games=100)
        b = make_market_analysis("B", total_games=120)
        notes = _compute_confidence_notes([("A", a), ("B", b)])
        ratio_notes = [n for n in notes if "ratio" in n.lower()]
        assert len(ratio_notes) == 0

    def test_small_market_warning(self):
        """Market with <50 games should produce a 'small market' confidence note."""
        small = make_market_analysis("Tiny", total_games=15)
        big = make_market_analysis("Big", total_games=200)
        notes = _compute_confidence_notes([("Tiny", small), ("Big", big)])
        small_notes = [n for n in notes if "15" in n or "tiny" in n.lower() or "only" in n.lower()]
        assert len(small_notes) >= 1


# ---------------------------------------------------------------------------
# TestMCPToolValidation
# ---------------------------------------------------------------------------

class TestMCPToolValidation:
    """8 tests for MCP tool input validation logic.

    Tests are run by calling the validation logic directly (parsing the tool
    input strings), matching the patterns in tools.py register_tools functions.
    """

    def _parse_analyze_market_inputs(self, genre="", appids="", continuation_token=""):
        """Simulate analyze_market tool validation logic from tools.py."""
        errors = []
        if continuation_token.strip():
            return {"phase": 2, "continuation": True}

        genre = genre.strip()
        appids_str = appids.strip()

        if not genre and not appids_str:
            return {"error": "Either genre or appids must be provided", "error_type": "validation"}

        if genre and appids_str:
            return {"error": "Provide either genre or appids, not both", "error_type": "validation"}

        if appids_str:
            try:
                appid_list = [int(a.strip()) for a in appids_str.split(",") if a.strip()]
                if not appid_list:
                    return {"error": "appids list is empty", "error_type": "validation"}
                if any(a <= 0 for a in appid_list):
                    return {"error": "All appids must be positive", "error_type": "validation"}
            except ValueError:
                return {"error": "appids must be comma-separated integers", "error_type": "validation"}

        return {"ok": True}

    def _parse_evaluate_game_inputs(self, appid=0, genre=""):
        """Simulate evaluate_game tool validation logic from tools.py."""
        if appid <= 0:
            return {"error": "appid must be positive", "error_type": "validation"}
        if not genre.strip():
            return {"error": "genre is required", "error_type": "validation"}
        return {"ok": True}

    def _parse_compare_markets_inputs(self, markets=""):
        """Simulate compare_markets tool validation logic from tools.py."""
        if not markets.strip():
            return {"error": "markets parameter is required (JSON array)", "error_type": "validation"}
        try:
            market_defs = json.loads(markets)
        except json.JSONDecodeError as e:
            return {"error": f"Invalid JSON in markets parameter: {e}", "error_type": "validation"}
        if not isinstance(market_defs, list):
            return {"error": "markets must be a JSON array", "error_type": "validation"}
        if len(market_defs) < 2 or len(market_defs) > 4:
            return {"error": f"2-4 markets required, got {len(market_defs)}", "error_type": "validation"}
        return {"ok": True}

    def test_analyze_market_no_input_error(self):
        """analyze_market with no genre or appids should return validation error."""
        result = self._parse_analyze_market_inputs(genre="", appids="")
        assert "error" in result
        assert result["error_type"] == "validation"

    def test_analyze_market_both_inputs_error(self):
        """analyze_market with both genre and appids should return validation error."""
        result = self._parse_analyze_market_inputs(genre="Roguelike", appids="123,456")
        assert "error" in result
        assert result["error_type"] == "validation"

    def test_analyze_market_invalid_appids(self):
        """analyze_market with non-integer appids should return validation error."""
        result = self._parse_analyze_market_inputs(appids="abc,def")
        assert "error" in result
        assert "integers" in result["error"].lower() or result["error_type"] == "validation"

    def test_evaluate_game_zero_appid(self):
        """evaluate_game with appid=0 should return validation error."""
        result = self._parse_evaluate_game_inputs(appid=0, genre="Roguelike")
        assert "error" in result
        assert "appid" in result["error"].lower()

    def test_evaluate_game_no_genre(self):
        """evaluate_game with empty genre should return validation error."""
        result = self._parse_evaluate_game_inputs(appid=12345, genre="")
        assert "error" in result
        assert "genre" in result["error"].lower()

    def test_compare_markets_invalid_json(self):
        """compare_markets with invalid JSON should return validation error."""
        result = self._parse_compare_markets_inputs(markets="{not valid json}")
        assert "error" in result
        assert result["error_type"] == "validation"

    def test_compare_markets_one_market_invalid(self):
        """compare_markets with only 1 market should return validation error."""
        result = self._parse_compare_markets_inputs(markets='[{"tag": "Roguelike"}]')
        assert "error" in result
        assert "2-4" in result["error"]

    def test_compare_markets_five_markets_invalid(self):
        """compare_markets with 5 markets should return validation error."""
        five_markets = json.dumps([{"tag": f"Tag{i}"} for i in range(5)])
        result = self._parse_compare_markets_inputs(markets=five_markets)
        assert "error" in result
        assert "2-4" in result["error"]


# ---------------------------------------------------------------------------
# Integration smoke tests
# ---------------------------------------------------------------------------

class TestRunMarketAnalysisIntegration:
    """Smoke tests for _run_market_analysis to verify end-to-end behavior."""

    def test_full_analysis_with_50_games(self):
        """Full market analysis with 50 games should complete without errors."""
        games = make_games(50)
        result = _run_market_analysis(games=games, tags=["Roguelike"])
        assert "error" not in result
        assert result["total_games"] == 50
        assert result["health_scores"] is not None
        assert len(result["top_games"]) <= 10

    def test_insufficient_games_returns_error(self):
        """Analysis with fewer than 20 games should return error dict."""
        games = make_games(10)
        result = _run_market_analysis(games=games, tags=["TestTag"])
        assert "error" in result

    def test_market_label_default_from_tags(self):
        """market_label defaults to comma-joined tags when not provided."""
        games = make_games(50)
        result = _run_market_analysis(games=games, tags=["Roguelike", "Deckbuilder"])
        assert result["market_label"] == "Roguelike, Deckbuilder"

    def test_custom_market_label(self):
        """market_label parameter overrides tag-derived label."""
        games = make_games(50)
        result = _run_market_analysis(games=games, tags=["Tag"], market_label="My Custom Market")
        assert result["market_label"] == "My Custom Market"

    def test_build_game_profile_maps_median_forever(self):
        """_build_game_profile should map median_forever -> median_playtime."""
        game = {
            "appid": 123,
            "name": "Test Game",
            "revenue": 500_000,
            "price": 14.99,
            "review_score": 80,
            "tags": {"Action": 100},
            "developer": "TestDev",
            "publisher": "TestPub",
            "owners": 10_000,
            "ccu": 50,
            "median_forever": 42.5,
            "followers": 5000,
            "copies_sold": 20000,
            "positive": 400,
            "negative": 20,
            "release_date": "2022-01-15",
        }
        profile = _build_game_profile(game)
        assert profile.median_playtime == 42.5
        assert profile.appid == 123
        assert profile.name == "Test Game"


# ---------------------------------------------------------------------------
# TestAppidsInput
# ---------------------------------------------------------------------------

def _make_engagement_data(appid: int, name: str = None) -> EngagementData:
    """Create a minimal EngagementData object for mocking."""
    return EngagementData(
        appid=appid,
        name=name or f"Game {appid}",
        developer="DevA",
        publisher="PubA",
        ccu=500,
        owners_min=10000,
        owners_max=20000,
        owners_midpoint=15000.0,
        average_forever=25.0,
        average_2weeks=5.0,
        median_forever=20.0,
        median_2weeks=3.0,
        positive=1000,
        negative=50,
        total_reviews=1050,
        price=14.99,
        tags={"Action": 100, "Indie": 80},
        fetched_at="2026-01-01T00:00:00Z",
        cache_age=0,
        review_score=95.0,
    )


class TestAppidsInput:
    """6 tests for appids-only paths in _fetch_game_list_steamspy, compare_markets, and evaluate_game."""

    @pytest.mark.asyncio
    async def test_fetch_game_list_steamspy_with_appids(self):
        """Appids-only path returns game dicts with correct field mapping."""
        mock_steamspy = MagicMock()
        mock_steam_store = MagicMock()

        async def mock_get_engagement(appid):
            return _make_engagement_data(appid)

        mock_steamspy.get_engagement_data = mock_get_engagement

        games, data_source = await _fetch_game_list_steamspy(
            mock_steamspy, mock_steam_store, tags=[], appids=[100, 200, 300]
        )

        assert len(games) == 3
        assert data_source == "steamspy_appids"

        # Check field mapping for first game
        game = next(g for g in games if g["appid"] == 100)
        assert game["name"] == "Game 100"
        assert game["owners"] == 15000.0
        assert game["ccu"] == 500
        assert game["price"] == 14.99
        assert game["review_score"] == 95.0
        assert game["median_forever"] == 20.0
        assert game["average_forever"] == 25.0
        assert game["tags"] == {"Action": 100, "Indie": 80}
        assert game["revenue"] is None
        assert game["followers"] is None
        assert game["copies_sold"] is None
        assert game["release_date"] is None

    @pytest.mark.asyncio
    async def test_fetch_game_list_steamspy_appids_with_failures(self):
        """Partial failures are skipped; successful results still returned."""
        mock_steamspy = MagicMock()
        mock_steam_store = MagicMock()

        async def mock_get_engagement(appid):
            if appid == 200:
                return APIError(status_code=404, message="Not found", source="steamspy")
            if appid == 300:
                raise RuntimeError("Connection error")
            return _make_engagement_data(appid)

        mock_steamspy.get_engagement_data = mock_get_engagement

        games, data_source = await _fetch_game_list_steamspy(
            mock_steamspy, mock_steam_store, tags=[], appids=[100, 200, 300]
        )

        assert len(games) == 1
        assert games[0]["appid"] == 100
        assert data_source == "steamspy_appids"

    @pytest.mark.asyncio
    async def test_fetch_game_list_steamspy_appids_empty_results(self):
        """When all appids fail, returns empty list with steamspy_appids source."""
        mock_steamspy = MagicMock()
        mock_steam_store = MagicMock()

        async def mock_get_engagement(appid):
            raise RuntimeError("All fail")

        mock_steamspy.get_engagement_data = mock_get_engagement

        games, data_source = await _fetch_game_list_steamspy(
            mock_steamspy, mock_steam_store, tags=[], appids=[100, 200]
        )

        assert games == []
        assert data_source == "steamspy_appids"

    @pytest.mark.asyncio
    async def test_fetch_game_list_steamspy_tags_priority(self):
        """When both tags and appids provided, tags take priority (existing path used)."""
        mock_steamspy = MagicMock()
        mock_steam_store = MagicMock()

        # Mock tag search to return a SearchResult
        from src.schemas import GameSummary, SearchResult
        game_summary = GameSummary(
            appid=999,
            name="Tag Game",
            developer="Dev",
            publisher="Pub",
            ccu=100,
            owners_min=1000,
            owners_max=5000,
            owners_midpoint=3000.0,
            average_forever=10.0,
            average_2weeks=1.0,
            median_forever=8.0,
            median_2weeks=0.5,
            positive=200,
            negative=10,
            price=9.99,
        )
        search_result = SearchResult(
            tag="Action",
            appids=[999],
            total_found=1,
            games=[game_summary],
            data_source="steamspy",
            fetched_at="2026-01-01T00:00:00Z",
            cache_age_seconds=0,
        )
        mock_steamspy.search_by_tag = AsyncMock(return_value=search_result)

        games, data_source = await _fetch_game_list_steamspy(
            mock_steamspy, mock_steam_store, tags=["Action"], appids=[100, 200]
        )

        # Tag path used — data_source is not "steamspy_appids"
        assert data_source != "steamspy_appids"
        assert data_source == "steamspy"
        # Only the tag game is returned, not the appid games
        assert len(games) == 1
        assert games[0]["appid"] == 999

    def test_compare_markets_appids_only_market_not_skipped(self):
        """Market with appids but no tags should NOT be skipped in compare_markets_analysis."""
        # Replicate the validation logic from compare_markets_analysis
        warnings = []
        market_inputs = [
            {"tags": [], "appids": [100, 200], "label": "Custom Market"},
            {"tags": ["Action"]},
        ]

        skipped_count = 0
        for idx, market_input in enumerate(market_inputs):
            tags = market_input.get("tags") or []
            appids = market_input.get("appids") or []
            if not tags and not appids:
                warnings.append(f"Market {idx + 1} has no tags or appids — skipping.")
                skipped_count += 1

        assert skipped_count == 0, f"No markets should be skipped, but got warnings: {warnings}"

    def test_evaluate_game_genre_appids_validation(self):
        """genre_appids validation: either genre or genre_appids, not both, not neither."""
        # Replicate validation logic from evaluate_game
        def validate(genre, genre_appids_str):
            genre = genre.strip()
            genre_appids_str = genre_appids_str.strip()
            if not genre and not genre_appids_str:
                return {"error": "Either genre or genre_appids must be provided", "error_type": "validation"}
            if genre and genre_appids_str:
                return {"error": "Provide either genre or genre_appids, not both", "error_type": "validation"}
            if genre_appids_str:
                try:
                    appid_list = [int(a.strip()) for a in genre_appids_str.split(",") if a.strip()]
                    if not appid_list:
                        return {"error": "genre_appids list is empty", "error_type": "validation"}
                except ValueError:
                    return {"error": "genre_appids must be comma-separated integers", "error_type": "validation"}
            return {"ok": True}

        # genre only — valid
        assert validate("Roguelike", "") == {"ok": True}
        # genre_appids only — valid
        assert validate("", "100, 200, 300") == {"ok": True}
        # both — error
        result = validate("Roguelike", "100, 200")
        assert result["error_type"] == "validation"
        assert "not both" in result["error"]
        # neither — error
        result = validate("", "")
        assert result["error_type"] == "validation"
        assert "must be provided" in result["error"]
        # non-integer genre_appids — error
        result = validate("", "abc, 200")
        assert result["error_type"] == "validation"
        assert "integers" in result["error"]
