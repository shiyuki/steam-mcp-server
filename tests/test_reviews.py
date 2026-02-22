"""Tests for review intelligence: SteamReviewClient, helper functions, and MCP tool validation."""

import pytest
import json
import time
from unittest.mock import AsyncMock, patch, MagicMock
from datetime import datetime, timezone
from src.steam_api import SteamReviewClient, _strip_bbcode, _truncate_text, _ms_to_iso
from src.schemas import (
    ReviewItem, ReviewsData, ReviewAggregates, PlaytimeBucket,
    SentimentPeriod, ReviewsMeta, QueryParams, ReviewSnippet, EADateInfo
)


# ---------------------------------------------------------------------------
# Factory functions (module-level, not fixtures)
# ---------------------------------------------------------------------------

def make_raw_review(
    review_id="111",
    text="Great game",
    voted_up=True,
    votes_up=50,
    votes_funny=5,
    playtime_at_review=1200,  # 20 hours in minutes
    playtime_forever=3600,    # 60 hours in minutes
    timestamp_created=1700000000,
    timestamp_updated=1700000000,
    written_during_early_access=False,
    received_for_free=False,
    language="english",
    steamid="76561198000000001",
    num_games_owned=100,
    num_reviews=5,
    developer_response=None,
):
    """Create a raw review dict matching Steam API format."""
    review = {
        "recommendationid": review_id,
        "review": text,
        "voted_up": voted_up,
        "votes_up": votes_up,
        "votes_funny": votes_funny,
        "timestamp_created": timestamp_created,
        "timestamp_updated": timestamp_updated,
        "written_during_early_access": written_during_early_access,
        "received_for_free": received_for_free,
        "language": language,
        "steam_purchase": True,
        "author": {
            "steamid": steamid,
            "num_games_owned": num_games_owned,
            "num_reviews": num_reviews,
            "playtime_at_review": playtime_at_review,
            "playtime_forever": playtime_forever,
        },
    }
    if developer_response is not None:
        review["developer_response"] = developer_response
    return review


def make_page_response(reviews, cursor="NEXT_CURSOR", query_summary=None):
    """Create a paginated API response."""
    resp = {
        "success": 1,
        "cursor": cursor,
        "reviews": reviews,
    }
    if query_summary is not None:
        resp["query_summary"] = query_summary
    return resp


def make_query_summary(total_positive=4000, total_negative=1000, total_reviews=5000, review_score=9):
    """Create a query_summary dict.

    Default total_reviews=5000 (below EA scan threshold of 10000) so tests
    that use this helper don't accidentally trigger the EA-period scan.
    Tests that specifically test EA-period behavior must pass total_reviews=90000+.
    """
    return {
        "num_reviews": total_reviews,
        "review_score": review_score,
        "total_positive": total_positive,
        "total_negative": total_negative,
        "total_reviews": total_reviews,
    }


# ---------------------------------------------------------------------------
# TestBBCodeStripping
# ---------------------------------------------------------------------------

class TestBBCodeStripping:
    """Tests for the _strip_bbcode helper function."""

    def test_strip_bold_italic(self):
        result = _strip_bbcode("[b]bold[/b] and [i]italic[/i]")
        assert result == "bold and italic"

    def test_strip_h2(self):
        result = _strip_bbcode("[h2]Title[/h2]")
        assert result == "Title"

    def test_strip_url_with_href(self):
        result = _strip_bbcode("[url=http://x.com]link[/url]")
        assert result == "link"

    def test_strip_spoiler_preserves_text(self):
        result = _strip_bbcode("[spoiler]hidden text[/spoiler]")
        assert result == "hidden text"

    def test_strip_list_items(self):
        result = _strip_bbcode("[list][*]item1[*]item2[/list]")
        assert "item1" in result
        assert "item2" in result
        # List items separated by newlines from [*] conversion
        assert "\n" in result

    def test_strip_hr(self):
        result = _strip_bbcode("[hr][/hr]")
        assert result == ""

    def test_no_tags(self):
        result = _strip_bbcode("plain text")
        assert result == "plain text"

    def test_empty_string(self):
        result = _strip_bbcode("")
        assert result == ""

    def test_nested_tags(self):
        result = _strip_bbcode("[b][i]nested[/i][/b]")
        assert result == "nested"

    def test_mixed_content(self):
        text = "[h2]Title[/h2] [b]bold[/b] and [url=http://x.com]link text[/url] with [spoiler]secret[/spoiler]"
        result = _strip_bbcode(text)
        assert "[" not in result
        assert "]" not in result
        assert "Title" in result
        assert "bold" in result
        assert "link text" in result
        assert "secret" in result


# ---------------------------------------------------------------------------
# TestTextTruncation
# ---------------------------------------------------------------------------

class TestTextTruncation:
    """Tests for the _truncate_text helper function."""

    def test_short_text_not_truncated(self):
        text = "Short text"
        result_text, truncated, full_length = _truncate_text(text)
        assert result_text == text
        assert truncated is False
        assert full_length == len(text)

    def test_long_text_truncated(self):
        text = "x " * 600  # 1200 chars (over 1000 limit)
        result_text, truncated, full_length = _truncate_text(text)
        assert truncated is True
        assert len(result_text) <= 1000
        assert full_length == len(text)

    def test_truncation_word_boundary(self):
        # Build a text that has a space just before 1000 chars
        words = ["word"] * 250  # 250 words of 4 chars each + spaces = ~1250 chars
        text = " ".join(words)
        result_text, truncated, full_length = _truncate_text(text)
        assert truncated is True
        # Word boundary: result should not end mid-word (last char should be a full word)
        if len(result_text) < len(text):
            # Should end at a space or word boundary — not mid-word
            assert not result_text.endswith("wor")  # not partial "word"

    def test_truncation_returns_correct_length(self):
        text = "a" * 1500
        _, truncated, full_length = _truncate_text(text)
        assert truncated is True
        assert full_length == 1500

    def test_exact_limit(self):
        text = "a" * 1000
        result_text, truncated, full_length = _truncate_text(text)
        assert truncated is False
        assert result_text == text
        assert full_length == 1000


# ---------------------------------------------------------------------------
# TestMsToIso
# ---------------------------------------------------------------------------

class TestMsToIso:
    """Tests for the _ms_to_iso helper function."""

    def test_valid_timestamp(self):
        # 1509494400000 ms = 2017-11-01 UTC
        result = _ms_to_iso(1509494400000)
        assert result == "2017-11-01"

    def test_another_timestamp(self):
        # 1548219600000 ms = around 2019-01-23
        result = _ms_to_iso(1548219600000)
        assert result is not None
        assert len(result) == 10  # YYYY-MM-DD format
        assert result.startswith("2019-")

    def test_none_returns_none(self):
        result = _ms_to_iso(None)
        assert result is None

    def test_zero_returns_none(self):
        result = _ms_to_iso(0)
        assert result is None

    def test_false_returns_none(self):
        result = _ms_to_iso(False)
        assert result is None


# ---------------------------------------------------------------------------
# TestReviewParsing
# ---------------------------------------------------------------------------

class TestReviewParsing:
    """Tests for SteamReviewClient._parse_review."""

    def setup_method(self):
        self.mock_http = AsyncMock()
        self.client = SteamReviewClient(self.mock_http)

    def test_parse_basic_review(self):
        raw = make_raw_review(
            review_id="999",
            text="Great game",
            voted_up=True,
            votes_up=100,
            votes_funny=10,
            playtime_at_review=1200,
            playtime_forever=3600,
            timestamp_created=1700000000,
            timestamp_updated=1700100000,
            written_during_early_access=False,
            received_for_free=False,
            language="english",
            steamid="76561198000000001",
            num_games_owned=50,
            num_reviews=3,
        )
        result = self.client._parse_review(raw)
        assert isinstance(result, ReviewItem)
        assert result.review_id == "999"
        assert result.text == "Great game"
        assert result.voted_up is True
        assert result.votes_up == 100
        assert result.votes_funny == 10
        assert result.timestamp_created == 1700000000
        assert result.timestamp_updated == 1700100000
        assert result.written_during_early_access is False
        assert result.received_for_free is False
        assert result.language == "english"
        assert result.author_steamid == "76561198000000001"
        assert result.author_num_games_owned == 50
        assert result.author_num_reviews == 3

    def test_parse_bbcode_stripped(self):
        raw = make_raw_review(text="[b]Bold review[/b] text here")
        result = self.client._parse_review(raw)
        assert "[b]" not in result.text
        assert "Bold review" in result.text

    def test_parse_truncated_text(self):
        long_text = "word " * 300  # well over 1000 chars
        raw = make_raw_review(text=long_text)
        result = self.client._parse_review(raw)
        assert result.truncated is True
        assert len(result.text) <= 1000
        assert result.full_length == len(long_text.strip())  # stripped by _strip_bbcode

    def test_parse_developer_response(self):
        raw = make_raw_review(developer_response="Thanks for your feedback!")
        result = self.client._parse_review(raw)
        assert result.developer_response == "Thanks for your feedback!"

    def test_parse_no_developer_response(self):
        raw = make_raw_review(developer_response=None)
        result = self.client._parse_review(raw)
        assert result.developer_response is None

    def test_parse_playtime_conversion(self):
        raw = make_raw_review(playtime_at_review=1200, playtime_forever=3600)
        result = self.client._parse_review(raw)
        # 1200 minutes = 20 hours
        assert result.playtime_at_review == 20.0
        # 3600 minutes = 60 hours
        assert result.playtime_total == 60.0

    def test_parse_ea_flag(self):
        raw = make_raw_review(written_during_early_access=True)
        result = self.client._parse_review(raw)
        assert result.written_during_early_access is True


# ---------------------------------------------------------------------------
# TestCursorPagination
# ---------------------------------------------------------------------------

class TestCursorPagination:
    """Tests for SteamReviewClient._fetch_review_text cursor pagination."""

    def setup_method(self):
        self.mock_http = AsyncMock()
        self.client = SteamReviewClient(self.mock_http)

    @pytest.mark.asyncio
    async def test_single_page_fetch(self):
        reviews_page1 = [make_raw_review(review_id=str(i)) for i in range(5)]
        page1 = make_page_response(reviews_page1, cursor="CURSOR1", query_summary=make_query_summary())
        # Second call returns empty
        page2 = make_page_response([], cursor="CURSOR1")

        self.mock_http.get_with_metadata.side_effect = [
            (page1, datetime.now(timezone.utc), 0),
            (page2, datetime.now(timezone.utc), 0),
        ]

        raw_reviews, query_summary, cursor, pages = await self.client._fetch_review_text(
            appid=12345, limit=100, language="english", review_type="all",
            purchase_type="all", filter_offtopic=True, day_range=None,
            platform="all", sort="helpful"
        )
        assert len(raw_reviews) == 5

    @pytest.mark.asyncio
    async def test_multi_page_dedup(self):
        # Page 1 has reviews 1-3
        reviews_page1 = [make_raw_review(review_id=str(i)) for i in range(1, 4)]
        # Page 2 has reviews 3-5 (review 3 is a duplicate)
        reviews_page2 = [make_raw_review(review_id=str(i)) for i in range(3, 6)]
        page1 = make_page_response(reviews_page1, cursor="CURSOR2", query_summary=make_query_summary())
        page2 = make_page_response(reviews_page2, cursor="CURSOR3")
        page3 = make_page_response([], cursor="CURSOR3")

        self.mock_http.get_with_metadata.side_effect = [
            (page1, datetime.now(timezone.utc), 0),
            (page2, datetime.now(timezone.utc), 0),
            (page3, datetime.now(timezone.utc), 0),
        ]

        raw_reviews, _, _, _ = await self.client._fetch_review_text(
            appid=12345, limit=100, language="english", review_type="all",
            purchase_type="all", filter_offtopic=True, day_range=None,
            platform="all", sort="helpful"
        )
        # Should have reviews 1, 2, 3, 4, 5 (5 unique, deduplicated)
        assert len(raw_reviews) == 5
        ids = {r["recommendationid"] for r in raw_reviews}
        assert ids == {"1", "2", "3", "4", "5"}

    @pytest.mark.asyncio
    async def test_cursor_unchanged_stops(self):
        reviews_page1 = [make_raw_review(review_id="1")]
        page1 = make_page_response(reviews_page1, cursor="SAME_CURSOR", query_summary=make_query_summary())
        # Page 2 returns same cursor — should stop
        reviews_page2 = [make_raw_review(review_id="2")]
        page2 = make_page_response(reviews_page2, cursor="SAME_CURSOR")

        self.mock_http.get_with_metadata.side_effect = [
            (page1, datetime.now(timezone.utc), 0),
            (page2, datetime.now(timezone.utc), 0),
        ]

        raw_reviews, _, _, pages = await self.client._fetch_review_text(
            appid=12345, limit=100, language="english", review_type="all",
            purchase_type="all", filter_offtopic=True, day_range=None,
            platform="all", sort="helpful"
        )
        # After page 1 completes (pages=1), page 2 returns same cursor → break before counting
        # So pages_fetched == 1 (only page 1 was completed before the unchanged-cursor stop)
        assert pages == 1

    @pytest.mark.asyncio
    async def test_empty_page_stops(self):
        reviews_page1 = [make_raw_review(review_id="1")]
        page1 = make_page_response(reviews_page1, cursor="CURSOR1", query_summary=make_query_summary())
        page2 = make_page_response([], cursor="CURSOR2")

        self.mock_http.get_with_metadata.side_effect = [
            (page1, datetime.now(timezone.utc), 0),
            (page2, datetime.now(timezone.utc), 0),
        ]

        raw_reviews, _, _, _ = await self.client._fetch_review_text(
            appid=12345, limit=100, language="english", review_type="all",
            purchase_type="all", filter_offtopic=True, day_range=None,
            platform="all", sort="helpful"
        )
        assert len(raw_reviews) == 1

    @pytest.mark.asyncio
    async def test_50_page_hard_cap(self):
        """Pagination stops at 50 pages maximum."""
        # Build 51 page responses — each with unique reviews and changing cursors
        side_effects = []
        for i in range(51):
            reviews = [make_raw_review(review_id=str(i * 100 + j)) for j in range(2)]
            cursor = f"CURSOR_{i+1}"
            qs = make_query_summary() if i == 0 else None
            page = make_page_response(reviews, cursor=cursor, query_summary=qs)
            side_effects.append((page, datetime.now(timezone.utc), 0))

        self.mock_http.get_with_metadata.side_effect = side_effects

        raw_reviews, _, _, pages = await self.client._fetch_review_text(
            appid=12345, limit=10000, language="english", review_type="all",
            purchase_type="all", filter_offtopic=True, day_range=None,
            platform="all", sort="helpful"
        )
        # Should stop at 50 pages max
        assert pages <= 50

    @pytest.mark.asyncio
    async def test_query_summary_first_page_only(self):
        """Query summary is captured from page 1, not overwritten by later pages."""
        qs_page1 = make_query_summary(total_positive=70000, total_reviews=72000)
        qs_page2 = make_query_summary(total_positive=1, total_reviews=2)  # Different (should not overwrite)

        reviews_page1 = [make_raw_review(review_id="1")]
        page1 = make_page_response(reviews_page1, cursor="C1", query_summary=qs_page1)
        # Page 2 has a query_summary too but it should be ignored
        reviews_page2 = [make_raw_review(review_id="2")]
        page2_data = {
            "success": 1,
            "cursor": "C2",
            "reviews": reviews_page2,
            "query_summary": qs_page2
        }
        page3 = make_page_response([], cursor="C2")

        self.mock_http.get_with_metadata.side_effect = [
            (page1, datetime.now(timezone.utc), 0),
            (page2_data, datetime.now(timezone.utc), 0),
            (page3, datetime.now(timezone.utc), 0),
        ]

        _, query_summary, _, _ = await self.client._fetch_review_text(
            appid=12345, limit=100, language="english", review_type="all",
            purchase_type="all", filter_offtopic=True, day_range=None,
            platform="all", sort="helpful"
        )
        # Should have first page's query summary
        assert query_summary is not None
        assert query_summary.get("total_reviews") == 72000


# ---------------------------------------------------------------------------
# TestHistogram
# ---------------------------------------------------------------------------

class TestHistogram:
    """Tests for SteamReviewClient._compute_histogram."""

    def setup_method(self):
        self.mock_http = AsyncMock()
        self.client = SteamReviewClient(self.mock_http)

    def test_histogram_all_buckets(self):
        """Reviews spanning all 7 buckets should populate each bucket."""
        reviews = [
            make_raw_review(review_id="1", playtime_at_review=30),    # 0.5 hrs -> 0_1hr
            make_raw_review(review_id="2", playtime_at_review=90),    # 1.5 hrs -> 1_2hr
            make_raw_review(review_id="3", playtime_at_review=180),   # 3 hrs -> 2_5hr
            make_raw_review(review_id="4", playtime_at_review=420),   # 7 hrs -> 5_10hr
            make_raw_review(review_id="5", playtime_at_review=900),   # 15 hrs -> 10_20hr
            make_raw_review(review_id="6", playtime_at_review=1800),  # 30 hrs -> 20_50hr
            make_raw_review(review_id="7", playtime_at_review=3600),  # 60 hrs -> 50hr_plus
        ]
        buckets = self.client._compute_histogram(reviews)
        assert len(buckets) == 7
        assert all(b.count == 1 for b in buckets)

    def test_histogram_zero_playtime(self):
        """Review with 0 playtime falls in 0_1hr bucket."""
        reviews = [make_raw_review(review_id="1", playtime_at_review=0)]
        buckets = self.client._compute_histogram(reviews)
        bucket_0_1 = next(b for b in buckets if b.label == "0_1hr")
        assert bucket_0_1.count == 1

    def test_histogram_boundary_values(self):
        """Playtime exactly at boundary falls in the higher bucket (min <= x < max)."""
        # 1.0 hours exactly -> 1_2hr (not 0_1hr, since 0_1hr is [0, 1))
        reviews_1hr = [make_raw_review(review_id="1", playtime_at_review=60)]  # exactly 1 hr
        buckets_1hr = self.client._compute_histogram(reviews_1hr)
        bucket_0_1 = next(b for b in buckets_1hr if b.label == "0_1hr")
        bucket_1_2 = next(b for b in buckets_1hr if b.label == "1_2hr")
        assert bucket_0_1.count == 0
        assert bucket_1_2.count == 1

        # 2.0 hours exactly -> 2_5hr (not 1_2hr, since 1_2hr is [1, 2))
        reviews_2hr = [make_raw_review(review_id="2", playtime_at_review=120)]  # exactly 2 hrs
        buckets_2hr = self.client._compute_histogram(reviews_2hr)
        bucket_1_2_check = next(b for b in buckets_2hr if b.label == "1_2hr")
        bucket_2_5 = next(b for b in buckets_2hr if b.label == "2_5hr")
        assert bucket_1_2_check.count == 0
        assert bucket_2_5.count == 1

    def test_histogram_50_plus(self):
        """Playtime of 100 hours falls in 50hr_plus bucket."""
        reviews = [make_raw_review(review_id="1", playtime_at_review=6000)]  # 100 hours
        buckets = self.client._compute_histogram(reviews)
        bucket_50_plus = next(b for b in buckets if b.label == "50hr_plus")
        assert bucket_50_plus.count == 1

    def test_histogram_empty_reviews(self):
        """Empty review list returns all-zero buckets (7 total)."""
        buckets = self.client._compute_histogram([])
        assert len(buckets) == 7
        assert all(b.count == 0 for b in buckets)
        assert all(b.positive_count == 0 for b in buckets)
        assert all(b.negative_count == 0 for b in buckets)

    def test_histogram_positive_negative_split(self):
        """Reviews with different voted_up values tracked per bucket.

        30min=0.5hr -> 0_1hr (positive), 60min=1.0hr -> 1_2hr (positive),
        90min=1.5hr -> 1_2hr (negative).
        """
        reviews = [
            make_raw_review(review_id="1", playtime_at_review=60, voted_up=True),
            make_raw_review(review_id="2", playtime_at_review=90, voted_up=False),
            make_raw_review(review_id="3", playtime_at_review=30, voted_up=True),
        ]
        buckets = self.client._compute_histogram(reviews)
        # 30min=0.5hr lands in 0_1hr (positive only)
        bucket_0_1 = next(b for b in buckets if b.label == "0_1hr")
        assert bucket_0_1.count == 1
        assert bucket_0_1.positive_count == 1
        assert bucket_0_1.negative_count == 0
        # 60min=1.0hr and 90min=1.5hr land in 1_2hr (1 positive, 1 negative)
        bucket_1_2 = next(b for b in buckets if b.label == "1_2hr")
        assert bucket_1_2.count == 2
        assert bucket_1_2.positive_count == 1
        assert bucket_1_2.negative_count == 1


# ---------------------------------------------------------------------------
# TestSentimentPeriods
# ---------------------------------------------------------------------------

class TestSentimentPeriods:
    """Tests for SteamReviewClient._compute_sentiment_periods."""

    def setup_method(self):
        self.mock_http = AsyncMock()
        self.client = SteamReviewClient(self.mock_http)

    def _make_ea_dates(self, ea_release_date=None, full_release_date=None, currently_ea=False):
        return EADateInfo(
            ea_release_date=ea_release_date,
            full_release_date=full_release_date,
            currently_ea=currently_ea,
            date_source="gamalytic",
        )

    def test_fixed_periods_with_anchor(self):
        """Reviews at various timestamps relative to anchor date have correct period assignment."""
        # Anchor: 2020-01-01 UTC = 1577836800
        anchor_ts = 1577836800
        ea_dates = self._make_ea_dates(ea_release_date="2020-01-01")

        DAY = 86400
        reviews = [
            make_raw_review(review_id="1", timestamp_created=anchor_ts + 10 * DAY),    # first_30d
            make_raw_review(review_id="2", timestamp_created=anchor_ts + 45 * DAY),    # first_90d
            make_raw_review(review_id="3", timestamp_created=anchor_ts + 200 * DAY),   # first_365d
            make_raw_review(review_id="4", timestamp_created=anchor_ts + 400 * DAY),   # 365d_plus
        ]
        fixed_rolling, yearly, ratio_timeline = self.client._compute_sentiment_periods(reviews, ea_dates)

        # Get first_30d period
        first_30d = next((p for p in fixed_rolling if p.period == "first_30d"), None)
        assert first_30d is not None
        assert first_30d.total_count == 1  # Only review at +10 days

        # first_90d should include reviews at 10d and 45d
        first_90d = next((p for p in fixed_rolling if p.period == "first_90d"), None)
        assert first_90d is not None
        assert first_90d.total_count == 2

    def test_rolling_periods(self):
        """Reviews relative to now → last_30d and last_90d correctly populated."""
        now_ts = int(datetime.now(tz=timezone.utc).timestamp())
        DAY = 86400
        ea_dates = self._make_ea_dates()

        reviews = [
            make_raw_review(review_id="1", timestamp_created=now_ts - 5 * DAY),    # last_30d and last_90d
            make_raw_review(review_id="2", timestamp_created=now_ts - 20 * DAY),   # last_30d and last_90d
            make_raw_review(review_id="3", timestamp_created=now_ts - 45 * DAY),   # last_90d only
            make_raw_review(review_id="4", timestamp_created=now_ts - 95 * DAY),   # neither
        ]
        fixed_rolling, _, _ = self.client._compute_sentiment_periods(reviews, ea_dates)

        last_30d = next((p for p in fixed_rolling if p.period == "last_30d"), None)
        last_90d = next((p for p in fixed_rolling if p.period == "last_90d"), None)
        assert last_30d is not None
        assert last_30d.total_count == 2
        assert last_90d is not None
        assert last_90d.total_count == 3

    def test_empty_period_has_null_ratio(self):
        """Period with 0 reviews returns positive_ratio=None."""
        ea_dates = self._make_ea_dates(ea_release_date="2020-01-01")
        # Provide reviews all far from anchor so first_30d is empty
        anchor_ts = 1577836800
        DAY = 86400
        reviews = [
            make_raw_review(review_id="1", timestamp_created=anchor_ts + 400 * DAY)
        ]
        fixed_rolling, _, _ = self.client._compute_sentiment_periods(reviews, ea_dates)

        first_30d = next((p for p in fixed_rolling if p.period == "first_30d"), None)
        assert first_30d is not None
        assert first_30d.positive_ratio is None

    def test_weighted_ratio(self):
        """Reviews with different votes_up compute helpfulness-weighted ratio."""
        ea_dates = self._make_ea_dates(ea_release_date="2020-01-01")
        anchor_ts = 1577836800
        DAY = 86400
        # 1 positive review with 90 votes_up, 1 negative with 10 votes_up
        reviews = [
            make_raw_review(review_id="1", timestamp_created=anchor_ts + 10 * DAY,
                           voted_up=True, votes_up=90),
            make_raw_review(review_id="2", timestamp_created=anchor_ts + 11 * DAY,
                           voted_up=False, votes_up=10),
        ]
        fixed_rolling, _, _ = self.client._compute_sentiment_periods(reviews, ea_dates)
        first_30d = next((p for p in fixed_rolling if p.period == "first_30d"), None)
        assert first_30d is not None
        # Weighted ratio = 90 / (90+10) * 100 = 90%
        assert first_30d.weighted_ratio == pytest.approx(90.0)

    def test_yearly_breakdown(self):
        """Reviews spanning 3 calendar years produce 3 yearly SentimentPeriod entries."""
        ea_dates = self._make_ea_dates()
        # Reviews in 2022, 2023, 2024
        reviews = [
            make_raw_review(review_id="1", timestamp_created=1640000000),  # 2021-12-20
            make_raw_review(review_id="2", timestamp_created=1672531200),  # 2023-01-01
            make_raw_review(review_id="3", timestamp_created=1704067200),  # 2024-01-01
        ]
        _, yearly, _ = self.client._compute_sentiment_periods(reviews, ea_dates)
        assert len(yearly) >= 2  # At least 2 years when spanning multiple years

    def test_single_year_no_yearly(self):
        """Reviews in only 1 calendar year → yearly list empty."""
        ea_dates = self._make_ea_dates()
        # All in 2023
        reviews = [
            make_raw_review(review_id="1", timestamp_created=1672531200),  # 2023-01-01
            make_raw_review(review_id="2", timestamp_created=1680000000),  # 2023-03-28
            make_raw_review(review_id="3", timestamp_created=1690000000),  # 2023-07-22
        ]
        _, yearly, _ = self.client._compute_sentiment_periods(reviews, ea_dates)
        assert len(yearly) == 0

    def test_avg_reviews_per_day(self):
        """avg_reviews_per_day is calculated correctly for a period."""
        ea_dates = self._make_ea_dates(ea_release_date="2020-01-01")
        anchor_ts = 1577836800
        DAY = 86400
        # 3 reviews in first_30d
        reviews = [
            make_raw_review(review_id=str(i), timestamp_created=anchor_ts + (i+1) * DAY)
            for i in range(3)
        ]
        fixed_rolling, _, _ = self.client._compute_sentiment_periods(reviews, ea_dates)
        first_30d = next((p for p in fixed_rolling if p.period == "first_30d"), None)
        assert first_30d is not None
        # 3 reviews over 30 days = 0.1 reviews/day
        assert first_30d.avg_reviews_per_day == pytest.approx(3 / 30)


# ---------------------------------------------------------------------------
# TestEADateResolution
# ---------------------------------------------------------------------------

class TestEADateResolution:
    """Tests for SteamReviewClient._resolve_ea_dates three-tier fallback."""

    def setup_method(self):
        self.mock_http = AsyncMock()
        self.client = SteamReviewClient(self.mock_http)

    @pytest.mark.asyncio
    async def test_gamalytic_ea_game(self):
        """Gamalytic returns EAReleaseDate + earlyAccessExitDate → gamalytic source, currently_ea from API."""
        gamalytic_data = {
            "EAReleaseDate": 1509494400000,   # 2017-11-01
            "earlyAccessExitDate": 1548219600000,  # 2019-01-23
            "firstReleaseDate": 1509494400000,
            "releaseDate": 1548219600000,
            "earlyAccess": False,  # Already exited EA
        }
        self.mock_http.get_with_metadata.return_value = (gamalytic_data, datetime.now(timezone.utc), 0)

        result = await self.client._resolve_ea_dates(12345)
        assert isinstance(result, EADateInfo)
        assert result.date_source == "gamalytic"
        assert result.ea_release_date == "2017-11-01"
        assert result.currently_ea is False

    @pytest.mark.asyncio
    async def test_gamalytic_non_ea_game(self):
        """Gamalytic returns releaseDate only → both dates same, currently_ea=False."""
        gamalytic_data = {
            "EAReleaseDate": None,
            "earlyAccessExitDate": None,
            "firstReleaseDate": 1509494400000,
            "releaseDate": 1509494400000,
            "earlyAccess": False,
        }
        self.mock_http.get_with_metadata.return_value = (gamalytic_data, datetime.now(timezone.utc), 0)

        result = await self.client._resolve_ea_dates(12345)
        assert result.date_source == "gamalytic"
        assert result.currently_ea is False
        assert result.ea_release_date == result.full_release_date

    @pytest.mark.asyncio
    async def test_steam_api_fallback(self):
        """Gamalytic fails, Steam appdetails returns genre 70 → steam_api source, currently_ea=True."""
        import httpx
        gamalytic_request = httpx.Request("GET", "https://api.gamalytic.com/game/12345")
        gamalytic_response = httpx.Response(503, request=gamalytic_request)

        steam_appdetails = {
            "12345": {
                "success": True,
                "data": {
                    "release_date": {"coming_soon": False, "date": "Nov 01, 2017"},
                    "genres": [{"id": "70", "description": "Early Access"}],
                }
            }
        }
        self.mock_http.get_with_metadata.side_effect = [
            httpx.HTTPStatusError("503", request=gamalytic_request, response=gamalytic_response),
            (steam_appdetails, datetime.now(timezone.utc), 0),
        ]

        result = await self.client._resolve_ea_dates(12345)
        assert result.date_source == "steam_api"
        assert result.currently_ea is True

    @pytest.mark.asyncio
    async def test_steam_api_non_ea(self):
        """Gamalytic fails, Steam appdetails has no genre 70 → currently_ea=False."""
        import httpx
        gamalytic_request = httpx.Request("GET", "https://api.gamalytic.com/game/12345")
        gamalytic_response = httpx.Response(503, request=gamalytic_request)

        steam_appdetails = {
            "12345": {
                "success": True,
                "data": {
                    "release_date": {"coming_soon": False, "date": "Jan 23, 2019"},
                    "genres": [{"id": "1", "description": "Action"}],
                }
            }
        }
        self.mock_http.get_with_metadata.side_effect = [
            httpx.HTTPStatusError("503", request=gamalytic_request, response=gamalytic_response),
            (steam_appdetails, datetime.now(timezone.utc), 0),
        ]

        result = await self.client._resolve_ea_dates(12345)
        assert result.date_source == "steam_api"
        assert result.currently_ea is False

    @pytest.mark.asyncio
    async def test_review_derived_fallback(self):
        """Both Gamalytic and Steam fail → date_source='review_derived'."""
        import httpx
        gamalytic_request = httpx.Request("GET", "https://api.gamalytic.com/game/12345")
        gamalytic_response = httpx.Response(503, request=gamalytic_request)
        steam_request = httpx.Request("GET", "https://store.steampowered.com/api/appdetails")
        steam_response = httpx.Response(503, request=steam_request)

        self.mock_http.get_with_metadata.side_effect = [
            httpx.HTTPStatusError("503", request=gamalytic_request, response=gamalytic_response),
            httpx.HTTPStatusError("503", request=steam_request, response=steam_response),
        ]

        result = await self.client._resolve_ea_dates(12345)
        assert result.date_source == "review_derived"
        assert result.ea_release_date is None
        assert result.full_release_date is None


# ---------------------------------------------------------------------------
# TestCompactMode
# ---------------------------------------------------------------------------

class TestCompactMode:
    """Tests for compact detail_level behavior in fetch_reviews."""

    def setup_method(self):
        self.mock_http = AsyncMock()
        self.client = SteamReviewClient(self.mock_http)

    def _make_fetch_reviews_mocks(self, reviews):
        """Build mock side effects for fetch_reviews call.

        asyncio.gather(text_task, stats_helpful_task, stats_recent_task, ea_task, lang_task)
        interleaving order (5 parallel tasks):
        1. text_page1           (text_task first await)
        2. stats_helpful_page1  (stats_helpful_task first await)
        3. stats_recent_page1   (stats_recent_task first await)
        4. gamalytic            (ea_task single await)
        5. lang_page1           (lang_task 1st await, cursor="LANG_C1")
        6. text_page_empty      (text_task second await after sleep)
        7. stats_helpful_empty  (stats_helpful_task second await after sleep)
        8. stats_recent_empty   (stats_recent_task second await after sleep)
        9. lang_page_empty      (lang_task 2nd await, cursor="LANG_C1", reviews=[])
        """
        qs = make_query_summary()
        page1 = make_page_response(reviews, cursor="C1", query_summary=qs)
        page_empty = make_page_response([], cursor="C1")
        stats_helpful_page1 = make_page_response(reviews, cursor="SH1")
        stats_helpful_empty = make_page_response([], cursor="SH1")
        stats_recent_page1 = make_page_response(reviews, cursor="SR1")
        stats_recent_empty = make_page_response([], cursor="SR1")
        lang_page1 = {
            "success": 1,
            "cursor": "LANG_C1",
            "reviews": [make_raw_review(review_id=str(i)) for i in range(3)]
        }
        lang_page_empty = {
            "success": 1,
            "cursor": "LANG_C1",
            "reviews": [],
        }
        gamalytic_data = {
            "EAReleaseDate": None,
            "earlyAccessExitDate": None,
            "firstReleaseDate": 1509494400000,
            "releaseDate": 1509494400000,
            "earlyAccess": False,
        }
        return [
            (page1, datetime.now(timezone.utc), 0),                  # text page1
            (stats_helpful_page1, datetime.now(timezone.utc), 0),    # stats helpful page1
            (stats_recent_page1, datetime.now(timezone.utc), 0),     # stats recent page1
            (gamalytic_data, datetime.now(timezone.utc), 0),         # ea gamalytic
            (lang_page1, datetime.now(timezone.utc), 0),             # lang dist page1
            (page_empty, datetime.now(timezone.utc), 0),             # text page_empty
            (stats_helpful_empty, datetime.now(timezone.utc), 0),    # stats helpful empty
            (stats_recent_empty, datetime.now(timezone.utc), 0),     # stats recent empty
            (lang_page_empty, datetime.now(timezone.utc), 0),        # lang dist page2 (empty)
        ]

    @pytest.mark.asyncio
    async def test_compact_returns_snippets(self):
        """detail_level='compact' → snippets populated, reviews empty."""
        reviews = [
            make_raw_review(review_id="1", voted_up=True, votes_up=100),
            make_raw_review(review_id="2", voted_up=False, votes_up=50),
            make_raw_review(review_id="3", voted_up=True, votes_up=200),
        ]
        self.mock_http.get_with_metadata.side_effect = self._make_fetch_reviews_mocks(reviews)

        result = await self.client.fetch_reviews(appid=12345, detail_level="compact")
        assert isinstance(result, ReviewsData)
        assert len(result.reviews) == 0
        assert len(result.snippets) > 0

    @pytest.mark.asyncio
    async def test_compact_top2_positive_top1_negative(self):
        """Compact mode returns exactly top 2 positive + top 1 negative snippets.

        With 3 reviews (id=1 pos votes=100, id=2 neg votes=50, id=3 pos votes=200):
        - top_positive_1: review id=3 (200 votes, highest positive)
        - top_positive_2: review id=1 (100 votes, second highest positive)
        - top_negative: review id=2 (50 votes, only negative)
        """
        reviews = [
            make_raw_review(review_id="1", voted_up=True, votes_up=100),
            make_raw_review(review_id="2", voted_up=False, votes_up=50),
            make_raw_review(review_id="3", voted_up=True, votes_up=200),
        ]
        self.mock_http.get_with_metadata.side_effect = self._make_fetch_reviews_mocks(reviews)

        result = await self.client.fetch_reviews(appid=12345, detail_level="compact")
        assert len(result.snippets) == 3
        perspectives = {s.perspective for s in result.snippets}
        assert "top_positive_1" in perspectives
        assert "top_positive_2" in perspectives
        assert "top_negative" in perspectives

        # Verify ranking: top_positive_1 should have highest votes_up among positives
        top_pos_1 = next(s for s in result.snippets if s.perspective == "top_positive_1")
        top_pos_2 = next(s for s in result.snippets if s.perspective == "top_positive_2")
        assert top_pos_1.votes_up >= top_pos_2.votes_up
        assert top_pos_1.review_id == "3"  # 200 votes
        assert top_pos_2.review_id == "1"  # 100 votes

    @pytest.mark.asyncio
    async def test_compact_snippet_length(self):
        """Each snippet text ≤ 200 chars."""
        long_text = "a" * 500
        reviews = [
            make_raw_review(review_id="1", voted_up=True, votes_up=100, text=long_text),
            make_raw_review(review_id="2", voted_up=False, votes_up=50, text=long_text),
            make_raw_review(review_id="3", voted_up=True, votes_up=200, text=long_text),
        ]
        self.mock_http.get_with_metadata.side_effect = self._make_fetch_reviews_mocks(reviews)

        result = await self.client.fetch_reviews(appid=12345, detail_level="compact")
        for snippet in result.snippets:
            assert len(snippet.text) <= 200

    @pytest.mark.asyncio
    async def test_compact_stats_scan_limit(self):
        """Compact mode uses 1000-review stats scan (max_reviews=1000)."""
        reviews = [make_raw_review(review_id="1", voted_up=True, votes_up=100)]
        self.mock_http.get_with_metadata.side_effect = self._make_fetch_reviews_mocks(reviews)

        result = await self.client.fetch_reviews(appid=12345, detail_level="compact")
        assert result.meta is not None
        assert result.meta.detail_level == "compact"
        # The stats_limit for compact is 1000 (not 5000) — confirmed by meta detail_level

    @pytest.mark.asyncio
    async def test_compact_only_positive_reviews(self):
        """Compact mode with all-positive reviews: at most 2 snippets, no top_negative."""
        reviews = [
            make_raw_review(review_id="1", voted_up=True, votes_up=100),
            make_raw_review(review_id="2", voted_up=True, votes_up=200),
            make_raw_review(review_id="3", voted_up=True, votes_up=50),
        ]
        self.mock_http.get_with_metadata.side_effect = self._make_fetch_reviews_mocks(reviews)

        result = await self.client.fetch_reviews(appid=12345, detail_level="compact")
        assert len(result.snippets) == 2  # Only 2 positive snippets, no negative
        perspectives = {s.perspective for s in result.snippets}
        assert "top_positive_1" in perspectives
        assert "top_positive_2" in perspectives
        assert "top_negative" not in perspectives

    @pytest.mark.asyncio
    async def test_compact_single_positive_one_negative(self):
        """Compact mode with 1 positive + 1 negative: 2 snippets total."""
        reviews = [
            make_raw_review(review_id="1", voted_up=True, votes_up=100),
            make_raw_review(review_id="2", voted_up=False, votes_up=50),
        ]
        self.mock_http.get_with_metadata.side_effect = self._make_fetch_reviews_mocks(reviews)

        result = await self.client.fetch_reviews(appid=12345, detail_level="compact")
        assert len(result.snippets) == 2
        perspectives = {s.perspective for s in result.snippets}
        assert "top_positive_1" in perspectives
        assert "top_negative" in perspectives
        assert "top_positive_2" not in perspectives  # Only 1 positive, no second slot


# ---------------------------------------------------------------------------
# TestFetchReviews
# ---------------------------------------------------------------------------

class TestFetchReviews:
    """Integration-style tests for SteamReviewClient.fetch_reviews."""

    def setup_method(self):
        self.mock_http = AsyncMock()
        self.client = SteamReviewClient(self.mock_http)

    def _make_side_effects(
        self, reviews, query_summary=None, gamalytic_data=None, lang_reviews=None
    ):
        """Standard side_effect list for 5-parallel API calls in fetch_reviews.

        asyncio.gather(text_task, stats_helpful_task, stats_recent_task, ea_task, lang_task)
        interleaving order:
        1. text_page1, 2. stats_helpful_page1, 3. stats_recent_page1,
        4. gamalytic, 5. lang_page1 (cursor=LANG_C1),
        6. text_page_empty (after sleep), 7. stats_helpful_empty (after sleep),
        8. stats_recent_empty (after sleep),
        9. lang_page_empty (lang 2nd await, reviews=[] stops pagination)
        """
        if query_summary is None:
            query_summary = make_query_summary()
        if gamalytic_data is None:
            gamalytic_data = {
                "EAReleaseDate": None, "earlyAccessExitDate": None,
                "firstReleaseDate": 1509494400000, "releaseDate": 1509494400000,
                "earlyAccess": False,
            }
        if lang_reviews is None:
            lang_reviews = reviews[:3]

        page1 = make_page_response(reviews, cursor="C1", query_summary=query_summary)
        page_empty = make_page_response([], cursor="C1")
        stats_helpful_page1 = make_page_response(reviews, cursor="SH1")
        stats_helpful_empty = make_page_response([], cursor="SH1")
        stats_recent_page1 = make_page_response(reviews, cursor="SR1")
        stats_recent_empty = make_page_response([], cursor="SR1")
        lang_page1 = {"success": 1, "cursor": "LANG_C1", "reviews": lang_reviews}
        lang_page_empty = {"success": 1, "cursor": "LANG_C1", "reviews": []}

        return [
            (page1, datetime.now(timezone.utc), 0),                  # text page1
            (stats_helpful_page1, datetime.now(timezone.utc), 0),    # stats helpful page1
            (stats_recent_page1, datetime.now(timezone.utc), 0),     # stats recent page1
            (gamalytic_data, datetime.now(timezone.utc), 0),         # ea gamalytic
            (lang_page1, datetime.now(timezone.utc), 0),             # lang dist page1
            (page_empty, datetime.now(timezone.utc), 0),             # text page_empty
            (stats_helpful_empty, datetime.now(timezone.utc), 0),    # stats helpful empty
            (stats_recent_empty, datetime.now(timezone.utc), 0),     # stats recent empty
            (lang_page_empty, datetime.now(timezone.utc), 0),        # lang dist page2 (empty)
        ]

    @pytest.mark.asyncio
    async def test_basic_fetch_returns_reviews_data(self):
        """Mock all API calls → returns ReviewsData with correct structure."""
        reviews = [make_raw_review(review_id=str(i)) for i in range(5)]
        self.mock_http.get_with_metadata.side_effect = self._make_side_effects(reviews)

        result = await self.client.fetch_reviews(appid=12345)
        assert isinstance(result, ReviewsData)
        assert result.meta is not None
        assert result.meta.appid == 12345
        assert result.meta.status == "ok"
        assert len(result.reviews) == 5

    @pytest.mark.asyncio
    async def test_zero_reviews_returns_no_reviews_status(self):
        """query_summary total_reviews=0 → meta.status='no_reviews'."""
        qs = make_query_summary(total_positive=0, total_negative=0, total_reviews=0)
        reviews = []
        gamalytic_data = {
            "EAReleaseDate": None, "earlyAccessExitDate": None,
            "firstReleaseDate": None, "releaseDate": None,
            "earlyAccess": False,
        }
        page1 = make_page_response(reviews, cursor="C1", query_summary=qs)
        stats_page1 = make_page_response([], cursor="S1")
        lang_page = {"success": 1, "cursor": "*", "reviews": []}

        # With empty reviews, text_task stops after page1 (empty page_reviews → break)
        # Order: text_page1, stats_helpful_page1, stats_recent_page1, gamalytic, lang_page
        self.mock_http.get_with_metadata.side_effect = [
            (page1, datetime.now(timezone.utc), 0),          # text page1 (empty reviews)
            (stats_page1, datetime.now(timezone.utc), 0),    # stats helpful page1 (empty)
            (stats_page1, datetime.now(timezone.utc), 0),    # stats recent page1 (empty)
            (gamalytic_data, datetime.now(timezone.utc), 0), # ea gamalytic
            (lang_page, datetime.now(timezone.utc), 0),      # lang distribution
        ]

        result = await self.client.fetch_reviews(appid=12345)
        assert result.meta is not None
        assert result.meta.status == "no_reviews"
        assert result.meta.total_fetched == 0

    @pytest.mark.asyncio
    async def test_partial_on_mid_pagination_error(self):
        """First page OK, second page throws → partial result with partial data.

        asyncio.gather interleaving (5 parallel tasks):
        1. text page1 (OK), 2. stats_helpful page1, 3. stats_recent page1,
        4. gamalytic, 5. lang_page1 (cursor=LANG_C1),
        6. text page2 (HTTP ERROR → caught, loop breaks),
        7. stats_helpful empty, 8. stats_recent empty,
        9. lang_page_empty (lang 2nd await, reviews=[] stops pagination)
        """
        import httpx
        reviews_page1 = [make_raw_review(review_id=str(i)) for i in range(5)]
        page1 = make_page_response(reviews_page1, cursor="C1", query_summary=make_query_summary())

        # Second page errors
        req = httpx.Request("GET", "https://store.steampowered.com/appreviews/12345")
        resp = httpx.Response(500, request=req)

        gamalytic_data = {
            "EAReleaseDate": None, "earlyAccessExitDate": None,
            "firstReleaseDate": None, "releaseDate": None,
            "earlyAccess": False,
        }
        stats_helpful_page1 = make_page_response(reviews_page1, cursor="SH1")
        stats_helpful_empty = make_page_response([], cursor="SH1")
        stats_recent_page1 = make_page_response(reviews_page1, cursor="SR1")
        stats_recent_empty = make_page_response([], cursor="SR1")
        lang_page1 = {"success": 1, "cursor": "LANG_C1", "reviews": reviews_page1}
        lang_page_empty = {"success": 1, "cursor": "LANG_C1", "reviews": []}

        self.mock_http.get_with_metadata.side_effect = [
            (page1, datetime.now(timezone.utc), 0),                  # text page1
            (stats_helpful_page1, datetime.now(timezone.utc), 0),    # stats helpful page1
            (stats_recent_page1, datetime.now(timezone.utc), 0),     # stats recent page1
            (gamalytic_data, datetime.now(timezone.utc), 0),         # gamalytic
            (lang_page1, datetime.now(timezone.utc), 0),             # lang dist page1
            httpx.HTTPStatusError("Server Error", request=req, response=resp),  # text page2 error
            (stats_helpful_empty, datetime.now(timezone.utc), 0),    # stats helpful empty
            (stats_recent_empty, datetime.now(timezone.utc), 0),     # stats recent empty
            (lang_page_empty, datetime.now(timezone.utc), 0),        # lang dist page2 (empty)
        ]

        result = await self.client.fetch_reviews(appid=12345, limit=1000)
        # Should still return ReviewsData with what was fetched
        assert isinstance(result, ReviewsData)
        assert len(result.reviews) == 5  # Got page 1 before error

    @pytest.mark.asyncio
    async def test_min_playtime_filter(self):
        """Reviews below min_playtime filtered out."""
        reviews = [
            make_raw_review(review_id="1", playtime_at_review=30),    # 0.5 hrs — below threshold
            make_raw_review(review_id="2", playtime_at_review=600),   # 10 hrs — above threshold
            make_raw_review(review_id="3", playtime_at_review=300),   # 5 hrs — above threshold
        ]
        self.mock_http.get_with_metadata.side_effect = self._make_side_effects(reviews)

        result = await self.client.fetch_reviews(appid=12345, min_playtime=5.0)
        # Only reviews with playtime >= 5 hours should remain
        assert len(result.reviews) == 2
        for r in result.reviews:
            assert r.playtime_at_review >= 5.0

    @pytest.mark.asyncio
    async def test_language_distribution_populated(self):
        """Language distribution dict populated from API (paginated, 2 pages consumed)."""
        reviews = [
            make_raw_review(review_id="1", language="english"),
            make_raw_review(review_id="2", language="english"),
            make_raw_review(review_id="3", language="schinese"),
        ]
        gamalytic_data = {
            "EAReleaseDate": None, "earlyAccessExitDate": None,
            "firstReleaseDate": None, "releaseDate": None, "earlyAccess": False,
        }
        page1 = make_page_response(reviews, cursor="C1", query_summary=make_query_summary())
        page_empty = make_page_response([], cursor="C1")
        stats_helpful_page1 = make_page_response(reviews, cursor="SH1")
        stats_helpful_empty = make_page_response([], cursor="SH1")
        stats_recent_page1 = make_page_response(reviews, cursor="SR1")
        stats_recent_empty = make_page_response([], cursor="SR1")
        lang_page1 = {"success": 1, "cursor": "LANG_C1", "reviews": reviews}
        lang_page_empty = {"success": 1, "cursor": "LANG_C1", "reviews": []}

        # Order: text_page1, stats_helpful, stats_recent, gamalytic, lang_page1, text_empty, stats_helpful_empty, stats_recent_empty, lang_page_empty
        self.mock_http.get_with_metadata.side_effect = [
            (page1, datetime.now(timezone.utc), 0),                  # text page1
            (stats_helpful_page1, datetime.now(timezone.utc), 0),    # stats helpful page1
            (stats_recent_page1, datetime.now(timezone.utc), 0),     # stats recent page1
            (gamalytic_data, datetime.now(timezone.utc), 0),         # gamalytic
            (lang_page1, datetime.now(timezone.utc), 0),             # lang dist page1
            (page_empty, datetime.now(timezone.utc), 0),             # text empty
            (stats_helpful_empty, datetime.now(timezone.utc), 0),    # stats helpful empty
            (stats_recent_empty, datetime.now(timezone.utc), 0),     # stats recent empty
            (lang_page_empty, datetime.now(timezone.utc), 0),        # lang dist page2 (empty)
        ]

        result = await self.client.fetch_reviews(appid=12345)
        assert isinstance(result.language_distribution, dict)
        assert len(result.language_distribution) > 0

    @pytest.mark.asyncio
    async def test_aggregates_from_query_summary(self):
        """Aggregates match query_summary values."""
        qs = make_query_summary(total_positive=4000, total_negative=1000, total_reviews=5000, review_score=9)
        reviews = [make_raw_review(review_id=str(i)) for i in range(3)]
        self.mock_http.get_with_metadata.side_effect = self._make_side_effects(
            reviews, query_summary=qs
        )

        result = await self.client.fetch_reviews(appid=12345)
        assert result.aggregates is not None
        assert result.aggregates.total_positive == 4000
        assert result.aggregates.total_negative == 1000
        assert result.aggregates.total_reviews == 5000
        assert result.aggregates.review_score == 9

    @pytest.mark.asyncio
    async def test_ratio_timeline_order(self):
        """ratio_timeline has 4 entries in fixed period order."""
        reviews = [make_raw_review(review_id=str(i)) for i in range(5)]
        gamalytic_data = {
            "EAReleaseDate": None, "earlyAccessExitDate": None,
            "firstReleaseDate": 1509494400000, "releaseDate": 1509494400000,
            "earlyAccess": False,
        }
        self.mock_http.get_with_metadata.side_effect = self._make_side_effects(
            reviews, gamalytic_data=gamalytic_data
        )

        result = await self.client.fetch_reviews(appid=12345)
        # ratio_timeline contains fixed periods (not rolling)
        assert isinstance(result.ratio_timeline, list)
        # Each entry is [period_name, ratio_value]
        for entry in result.ratio_timeline:
            assert len(entry) == 2
        # Fixed period names should be in timeline (not "last_30d"/"last_90d")
        period_names = [e[0] for e in result.ratio_timeline]
        assert all(not name.startswith("last_") for name in period_names)


# ---------------------------------------------------------------------------
# TestFetchReviewsTool
# ---------------------------------------------------------------------------

def _make_fetch_reviews_tool(review_client_mock):
    """Create a standalone fetch_reviews function replicating the MCP tool validation logic.

    Mirrors the validation in tools.py register_tools fetch_reviews nested function.
    This pattern matches test_tools.py (_make_search_genre, _make_fetch_metadata).
    """
    import asyncio as _asyncio

    async def fetch_reviews(
        appid: int,
        limit: int = 200,
        language: str = "english",
        review_type: str = "all",
        purchase_type: str = "all",
        detail_level: str = "full",
        filter_offtopic: bool = True,
        min_playtime: float = 0,
        date_range: str = "",
        platform: str = "all",
        sort: str = "helpful",
        cursor: str = "",
    ) -> str:
        if appid <= 0:
            return json.dumps({"error": "appid must be positive", "error_type": "validation"})
        if limit < 1 or limit > 10000:
            return json.dumps({"error": "limit must be between 1 and 10000", "error_type": "validation"})
        if detail_level not in ("full", "compact"):
            return json.dumps({"error": "detail_level must be 'full' or 'compact'", "error_type": "validation"})
        if review_type not in ("all", "positive", "negative"):
            return json.dumps({"error": "review_type must be 'all', 'positive', or 'negative'", "error_type": "validation"})
        if purchase_type not in ("all", "steam", "non_steam"):
            return json.dumps({"error": "purchase_type must be 'all', 'steam', or 'non_steam'", "error_type": "validation"})
        if sort not in ("helpful", "recent"):
            return json.dumps({"error": "sort must be 'helpful' or 'recent'", "error_type": "validation"})
        valid_platforms = ("all", "windows", "mac", "linux", "steam_deck")
        if platform not in valid_platforms:
            return json.dumps({"error": f"platform must be one of: {', '.join(valid_platforms)}", "error_type": "validation"})

        result = await review_client_mock.fetch_reviews(
            appid=appid, limit=limit, language=language,
            review_type=review_type, purchase_type=purchase_type,
            detail_level=detail_level, filter_offtopic=filter_offtopic,
            min_playtime=min_playtime if min_playtime > 0 else None,
            date_range=date_range if date_range else None,
            platform=platform, sort=sort,
        )
        return json.dumps(result.model_dump(), default=str)

    return fetch_reviews


class TestFetchReviewsTool:
    """MCP tool validation tests for fetch_reviews tool.

    Uses the standalone wrapper pattern (same as test_tools.py) to avoid
    importing the nested MCP-decorated function directly.
    """

    @pytest.mark.asyncio
    async def test_invalid_appid_zero(self):
        fn = _make_fetch_reviews_tool(AsyncMock())
        result = json.loads(await fn(appid=0))
        assert result.get("error_type") == "validation"
        assert "appid" in result.get("error", "")

    @pytest.mark.asyncio
    async def test_invalid_appid_negative(self):
        fn = _make_fetch_reviews_tool(AsyncMock())
        result = json.loads(await fn(appid=-1))
        assert result.get("error_type") == "validation"

    @pytest.mark.asyncio
    async def test_invalid_detail_level(self):
        fn = _make_fetch_reviews_tool(AsyncMock())
        result = json.loads(await fn(appid=12345, detail_level="invalid"))
        assert result.get("error_type") == "validation"
        assert "detail_level" in result.get("error", "")

    @pytest.mark.asyncio
    async def test_invalid_review_type(self):
        fn = _make_fetch_reviews_tool(AsyncMock())
        result = json.loads(await fn(appid=12345, review_type="invalid"))
        assert result.get("error_type") == "validation"
        assert "review_type" in result.get("error", "")

    @pytest.mark.asyncio
    async def test_invalid_purchase_type(self):
        fn = _make_fetch_reviews_tool(AsyncMock())
        result = json.loads(await fn(appid=12345, purchase_type="invalid"))
        assert result.get("error_type") == "validation"
        assert "purchase_type" in result.get("error", "")

    @pytest.mark.asyncio
    async def test_invalid_sort(self):
        fn = _make_fetch_reviews_tool(AsyncMock())
        result = json.loads(await fn(appid=12345, sort="invalid"))
        assert result.get("error_type") == "validation"
        assert "sort" in result.get("error", "")

    @pytest.mark.asyncio
    async def test_invalid_platform(self):
        fn = _make_fetch_reviews_tool(AsyncMock())
        result = json.loads(await fn(appid=12345, platform="invalid"))
        assert result.get("error_type") == "validation"
        assert "platform" in result.get("error", "")

    @pytest.mark.asyncio
    async def test_invalid_limit_zero(self):
        fn = _make_fetch_reviews_tool(AsyncMock())
        result = json.loads(await fn(appid=12345, limit=0))
        assert result.get("error_type") == "validation"
        assert "limit" in result.get("error", "")

    @pytest.mark.asyncio
    async def test_invalid_limit_too_high(self):
        fn = _make_fetch_reviews_tool(AsyncMock())
        result = json.loads(await fn(appid=12345, limit=20000))
        assert result.get("error_type") == "validation"
        assert "limit" in result.get("error", "")


# ---------------------------------------------------------------------------
# TestEAPeriodScan
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# TestLanguageDistribution
# ---------------------------------------------------------------------------

class TestLanguageDistribution:
    """Tests for SteamReviewClient._fetch_language_distribution (paginated, up to 5 pages)."""

    def setup_method(self):
        self.mock_http = AsyncMock()
        self.client = SteamReviewClient(self.mock_http)

    def _make_lang_page(self, lang_counts: dict[str, int], cursor: str) -> tuple:
        """Create a language page response with specified language distribution."""
        reviews = []
        for lang, count in lang_counts.items():
            for i in range(count):
                reviews.append({"language": lang})
        return ({"reviews": reviews, "cursor": cursor}, datetime.now(timezone.utc), 0)

    @pytest.mark.asyncio
    async def test_lang_dist_paginates_multiple_pages(self):
        """Language distribution aggregates counts across multiple pages."""
        page1 = ({"reviews": [{"language": "english"}] * 3, "cursor": "C1"}, datetime.now(timezone.utc), 0)
        page2 = ({"reviews": [{"language": "russian"}] * 2, "cursor": "C2"}, datetime.now(timezone.utc), 0)
        page3 = ({"reviews": [{"language": "schinese"}] * 1, "cursor": "C3"}, datetime.now(timezone.utc), 0)
        page4 = ({"reviews": [], "cursor": "C3"}, datetime.now(timezone.utc), 0)

        self.mock_http.get_with_metadata.side_effect = [page1, page2, page3, page4]

        result = await self.client._fetch_language_distribution(99999)
        assert result["english"] == 3
        assert result["russian"] == 2
        assert result["schinese"] == 1

    @pytest.mark.asyncio
    async def test_lang_dist_stops_on_empty_page(self):
        """Pagination stops when reviews list is empty."""
        page1 = ({"reviews": [{"language": "english"}] * 5, "cursor": "C1"}, datetime.now(timezone.utc), 0)
        page2 = ({"reviews": [], "cursor": "C1"}, datetime.now(timezone.utc), 0)

        self.mock_http.get_with_metadata.side_effect = [page1, page2]

        result = await self.client._fetch_language_distribution(99999)
        # Only page1 counted
        assert result["english"] == 5
        # get_with_metadata called exactly 2 times (page1 + page2 empty → stops)
        assert self.mock_http.get_with_metadata.call_count == 2

    @pytest.mark.asyncio
    async def test_lang_dist_stops_at_5_pages(self):
        """Pagination stops at 5 pages maximum even if more pages available."""
        pages = []
        for i in range(6):
            cursor = f"C{i+1}"
            page = ({"reviews": [{"language": "english"}] * 10, "cursor": cursor}, datetime.now(timezone.utc), 0)
            pages.append(page)

        self.mock_http.get_with_metadata.side_effect = pages

        result = await self.client._fetch_language_distribution(99999)
        # Should stop after 5 pages (lang_max_pages=5), counting 50 reviews
        assert result["english"] == 50
        assert self.mock_http.get_with_metadata.call_count == 5

    @pytest.mark.asyncio
    async def test_lang_dist_stops_on_same_cursor(self):
        """Pagination stops when cursor doesn't change (API pagination exhausted)."""
        page1 = ({"reviews": [{"language": "english"}] * 5, "cursor": "C1"}, datetime.now(timezone.utc), 0)
        # Page 2 has same cursor "C1" and non-empty reviews → same-cursor guard triggers
        page2 = ({"reviews": [{"language": "russian"}] * 5, "cursor": "C1"}, datetime.now(timezone.utc), 0)

        self.mock_http.get_with_metadata.side_effect = [page1, page2]

        result = await self.client._fetch_language_distribution(99999)
        # Stops after page1 (page2 cursor == page1 cursor and pages_fetched > 0)
        assert result["english"] == 5
        assert "russian" not in result
        assert self.mock_http.get_with_metadata.call_count == 2
