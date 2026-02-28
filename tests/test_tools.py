"""Tests for MCP tool input validation."""

import asyncio
import json
import pytest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch, MagicMock

from mcp.server.fastmcp import Image
from src.schemas import SearchResult, GameMetadata, APIError, CommercialData, VisualProfile
from src.config import Config
from src.tools import _empty_to_none


def _make_search_genre(steamspy_mock):
    """Create a standalone search_genre function with mocked clients."""
    async def search_genre(genre: str, limit: int = 10) -> str:
        if not genre or not genre.strip():
            return json.dumps({"error": "genre is required", "error_type": "validation"})
        return_all = (limit == 0)
        if not return_all and not 1 <= limit <= 10000:
            return json.dumps({"error": "limit must be between 1 and 10000, or 0 for all results", "error_type": "validation"})

        result = await steamspy_mock.search_by_tag(genre.strip(), None if return_all else limit)
        if isinstance(result, APIError):
            return json.dumps(result.model_dump())
        # Use model_dump with mode='json' to serialize datetime
        dumped = result.model_dump(mode='json')
        return json.dumps({"appids": dumped["appids"], "tag": dumped["tag"], "total_found": dumped["total_found"]})

    return search_genre


def _make_fetch_metadata(store_mock):
    """Create a standalone fetch_metadata function with mocked clients."""
    async def fetch_metadata(appid: int) -> str:
        if appid <= 0:
            return json.dumps({"error": "appid must be positive", "error_type": "validation"})

        result = await store_mock.get_app_details(appid)
        if isinstance(result, APIError):
            return json.dumps(result.model_dump())
        return result.model_dump_json()

    return fetch_metadata


class TestSearchGenreValidation:
    @pytest.mark.asyncio
    async def test_empty_genre_returns_error(self):
        fn = _make_search_genre(AsyncMock())
        result = json.loads(await fn(""))
        assert result["error_type"] == "validation"
        assert "genre is required" in result["error"]

    @pytest.mark.asyncio
    async def test_whitespace_genre_returns_error(self):
        fn = _make_search_genre(AsyncMock())
        result = json.loads(await fn("   "))
        assert result["error_type"] == "validation"

    @pytest.mark.asyncio
    async def test_limit_zero_returns_all_results(self):
        """limit=0 means 'return all' in production — should succeed, not error."""
        mock = AsyncMock()
        mock.search_by_tag.return_value = SearchResult(
            appids=[1, 2, 3],
            tag="Action",
            total_found=3,
            fetched_at=datetime.now(timezone.utc),
            games=[]
        )
        fn = _make_search_genre(mock)
        result = json.loads(await fn("Action", limit=0))
        assert "error_type" not in result
        assert result["tag"] == "Action"
        # Verify None passed as limit (means return all)
        mock.search_by_tag.assert_called_once_with("Action", None)

    @pytest.mark.asyncio
    async def test_limit_over_10000_returns_error(self):
        fn = _make_search_genre(AsyncMock())
        result = json.loads(await fn("Action", limit=10001))
        assert result["error_type"] == "validation"

    @pytest.mark.asyncio
    async def test_limit_negative_returns_error(self):
        fn = _make_search_genre(AsyncMock())
        result = json.loads(await fn("Action", limit=-5))
        assert result["error_type"] == "validation"

    @pytest.mark.asyncio
    async def test_valid_request_returns_results(self):
        mock = AsyncMock()
        mock.search_by_tag.return_value = SearchResult(
            appids=[1, 2],
            tag="RPG",
            total_found=2,
            fetched_at=datetime.now(timezone.utc),
            games=[]  # Phase 5: Include games field (can be empty for tool tests)
        )
        fn = _make_search_genre(mock)
        result = json.loads(await fn("RPG", limit=10))

        assert result["appids"] == [1, 2]
        assert result["tag"] == "RPG"
        assert result["total_found"] == 2

    @pytest.mark.asyncio
    async def test_genre_is_stripped(self):
        mock = AsyncMock()
        mock.search_by_tag.return_value = SearchResult(
            appids=[],
            tag="RPG",
            total_found=0,
            fetched_at=datetime.now(timezone.utc),
            games=[]  # Phase 5: Include games field
        )
        fn = _make_search_genre(mock)
        await fn("  RPG  ", limit=10)

        mock.search_by_tag.assert_called_once_with("RPG", 10)

    @pytest.mark.asyncio
    async def test_api_error_forwarded(self):
        mock = AsyncMock()
        mock.search_by_tag.return_value = APIError(
            error_code=429, error_type="rate_limit", message="Too fast"
        )
        fn = _make_search_genre(mock)
        result = json.loads(await fn("Action"))

        assert result["error_code"] == 429
        assert result["error_type"] == "rate_limit"


class TestFetchMetadataValidation:
    @pytest.mark.asyncio
    async def test_negative_appid_returns_error(self):
        fn = _make_fetch_metadata(AsyncMock())
        result = json.loads(await fn(-1))
        assert result["error_type"] == "validation"
        assert "appid must be positive" in result["error"]

    @pytest.mark.asyncio
    async def test_zero_appid_returns_error(self):
        fn = _make_fetch_metadata(AsyncMock())
        result = json.loads(await fn(0))
        assert result["error_type"] == "validation"

    @pytest.mark.asyncio
    async def test_valid_appid_returns_metadata(self):
        mock = AsyncMock()
        mock.get_app_details.return_value = GameMetadata(
            appid=646570,
            name="Slay the Spire",
            tags=["Indie"],
            developer="Mega Crit",
            description="A deckbuilder",
            fetched_at=datetime.now(timezone.utc)
        )
        fn = _make_fetch_metadata(mock)
        result = json.loads(await fn(646570))

        assert result["appid"] == 646570
        assert result["name"] == "Slay the Spire"

    @pytest.mark.asyncio
    async def test_api_error_forwarded(self):
        mock = AsyncMock()
        mock.get_app_details.return_value = APIError(
            error_code=404, error_type="not_found", message="Not found"
        )
        fn = _make_fetch_metadata(mock)
        result = json.loads(await fn(99999999))

        assert result["error_code"] == 404
        assert result["error_type"] == "not_found"


# ---------------------------------------------------------------------------
# Phase 8: fetch_commercial detail_level and fetch_commercial_batch tests
# ---------------------------------------------------------------------------

def _make_fetch_commercial_extended(gamalytic_mock):
    """Create standalone fetch_commercial with detail_level support and mocked GamalyticClient."""
    async def fetch_commercial(appid: int, detail_level: str = "full") -> str:
        if appid <= 0:
            return json.dumps({"error": "appid must be positive", "error_type": "validation"})
        if detail_level not in ("full", "summary"):
            return json.dumps({"error": "detail_level must be 'full' or 'summary'", "error_type": "validation"})
        result = await gamalytic_mock.get_commercial_data(appid, detail_level=detail_level)
        if isinstance(result, APIError):
            return json.dumps(result.model_dump())
        return json.dumps(result.model_dump())
    return fetch_commercial


def _make_fetch_commercial_batch(gamalytic_mock):
    """Create standalone fetch_commercial_batch with mocked GamalyticClient."""
    async def fetch_commercial_batch(appids: str, detail_level: str = "summary") -> str:
        appids_str = appids.strip()
        if not appids_str:
            return json.dumps({"error": "appids is required", "error_type": "validation"})
        if detail_level not in ("full", "summary"):
            return json.dumps({"error": "detail_level must be 'full' or 'summary'", "error_type": "validation"})
        try:
            appid_list = [int(a.strip()) for a in appids_str.split(",") if a.strip()]
            if not appid_list:
                return json.dumps({"error": "appids list is empty", "error_type": "validation"})
            if any(a <= 0 for a in appid_list):
                return json.dumps({"error": "All appids must be positive", "error_type": "validation"})
        except ValueError:
            return json.dumps({"error": "appids must be comma-separated integers", "error_type": "validation"})
        results = await gamalytic_mock.get_commercial_batch(appid_list, detail_level=detail_level)
        return json.dumps([r.model_dump() for r in results])
    return fetch_commercial_batch


def _make_commercial_data(appid=646570):
    """Create a minimal CommercialData for testing."""
    return CommercialData(
        appid=appid,
        name="Test Game",
        price=24.99,
        revenue_min=8000000,
        revenue_max=12000000,
        confidence="high",
        source="gamalytic",
        fetched_at=datetime.now(timezone.utc),
    )


class TestFetchCommercialExtended:
    """Tests for fetch_commercial tool with detail_level support."""

    @pytest.mark.asyncio
    async def test_fetch_commercial_detail_level_summary(self):
        """detail_level='summary' passes through to get_commercial_data."""
        mock = AsyncMock()
        mock.get_commercial_data.return_value = _make_commercial_data()
        fn = _make_fetch_commercial_extended(mock)

        result = json.loads(await fn(646570, detail_level="summary"))

        assert "error_type" not in result
        mock.get_commercial_data.assert_called_once_with(646570, detail_level="summary")

    @pytest.mark.asyncio
    async def test_fetch_commercial_detail_level_invalid(self):
        """detail_level='invalid' returns validation error."""
        fn = _make_fetch_commercial_extended(AsyncMock())
        result = json.loads(await fn(646570, detail_level="invalid"))

        assert result["error_type"] == "validation"
        assert "detail_level" in result["error"]

    @pytest.mark.asyncio
    async def test_fetch_commercial_default_detail_level(self):
        """No detail_level param defaults to 'full'."""
        mock = AsyncMock()
        mock.get_commercial_data.return_value = _make_commercial_data()
        fn = _make_fetch_commercial_extended(mock)

        await fn(646570)

        mock.get_commercial_data.assert_called_once_with(646570, detail_level="full")


class TestFetchCommercialBatch:
    """Tests for fetch_commercial_batch tool."""

    @pytest.mark.asyncio
    async def test_batch_valid_appids(self):
        """Valid comma-separated AppIDs returns array of 2 results."""
        mock = AsyncMock()
        mock.get_commercial_batch.return_value = [
            _make_commercial_data(646570),
            _make_commercial_data(2379780),
        ]
        fn = _make_fetch_commercial_batch(mock)

        result = json.loads(await fn("646570,2379780"))

        assert isinstance(result, list)
        assert len(result) == 2
        assert result[0]["appid"] == 646570
        assert result[1]["appid"] == 2379780

    @pytest.mark.asyncio
    async def test_batch_empty_string(self):
        """Empty appids string returns validation error."""
        fn = _make_fetch_commercial_batch(AsyncMock())
        result = json.loads(await fn(""))

        assert result["error_type"] == "validation"
        assert "appids" in result["error"]

    @pytest.mark.asyncio
    async def test_batch_invalid_format(self):
        """Non-integer appids returns validation error."""
        fn = _make_fetch_commercial_batch(AsyncMock())
        result = json.loads(await fn("abc,def"))

        assert result["error_type"] == "validation"

    @pytest.mark.asyncio
    async def test_batch_negative_appid(self):
        """Negative appid in batch returns validation error."""
        fn = _make_fetch_commercial_batch(AsyncMock())
        result = json.loads(await fn("-1,646570"))

        assert result["error_type"] == "validation"
        assert "positive" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_batch_detail_level_default_summary(self):
        """Batch default detail_level is 'summary'."""
        mock = AsyncMock()
        mock.get_commercial_batch.return_value = [_make_commercial_data()]
        fn = _make_fetch_commercial_batch(mock)

        await fn("646570")

        mock.get_commercial_batch.assert_called_once_with([646570], detail_level="summary")

    @pytest.mark.asyncio
    async def test_batch_error_forwarded(self):
        """APIError in batch results is serialized properly."""
        mock = AsyncMock()
        mock.get_commercial_batch.return_value = [
            _make_commercial_data(646570),
            APIError(error_code=404, error_type="not_found", message="Not found"),
        ]
        fn = _make_fetch_commercial_batch(mock)

        result = json.loads(await fn("646570,999"))

        assert len(result) == 2
        assert result[1]["error_code"] == 404
        assert result[1]["error_type"] == "not_found"

    @pytest.mark.asyncio
    async def test_batch_whitespace_appids(self):
        """AppIDs with spaces around commas are handled correctly."""
        mock = AsyncMock()
        mock.get_commercial_batch.return_value = [_make_commercial_data()]
        fn = _make_fetch_commercial_batch(mock)

        await fn("  646570  ")

        mock.get_commercial_batch.assert_called_once_with([646570], detail_level="summary")

    @pytest.mark.asyncio
    async def test_batch_detail_level_full(self):
        """Explicit detail_level='full' passes through to batch."""
        mock = AsyncMock()
        mock.get_commercial_batch.return_value = [_make_commercial_data()]
        fn = _make_fetch_commercial_batch(mock)

        await fn("646570", detail_level="full")

        mock.get_commercial_batch.assert_called_once_with([646570], detail_level="full")


# ---------------------------------------------------------------------------
# fetch_game_art tests
# ---------------------------------------------------------------------------

def _make_game_metadata(**overrides):
    """Create a minimal GameMetadata for testing fetch_game_art."""
    defaults = dict(
        appid=646570,
        name="Slay the Spire",
        tags=["Roguelike", "Deckbuilder", "Card Game"],
        developer="Mega Crit",
        publisher="Mega Crit",
        genres=["Indie", "Strategy"],
        release_date="2019-01-23",
        header_image="https://shared.akamai.steamstatic.com/store_item_assets/steam/apps/646570/header.jpg",
        screenshots=[
            f"https://shared.akamai.steamstatic.com/store_item_assets/steam/apps/646570/ss_{i}.1920x1080.jpg"
            for i in range(6)
        ],
        screenshots_count=6,
        fetched_at=datetime.now(timezone.utc),
    )
    defaults.update(overrides)
    return GameMetadata(**defaults)


def _make_fetch_game_art(store_mock, http_mock):
    """Create a standalone fetch_game_art function with mocked clients."""
    IMAGE_CACHE_TTL = 86400

    async def fetch_game_art(appid: int, include_screenshots: bool = True, max_screenshots: int = 4) -> list:
        if appid <= 0:
            return [json.dumps({"error": "appid must be positive", "error_type": "validation"})]
        if not 1 <= max_screenshots <= 10:
            return [json.dumps({"error": "max_screenshots must be between 1 and 10", "error_type": "validation"})]

        result = await store_mock.get_app_details(appid)
        if isinstance(result, APIError):
            return [json.dumps(result.model_dump())]

        context = {
            "appid": result.appid,
            "name": result.name,
            "developer": result.developer,
            "publisher": result.publisher,
            "tags": result.tags[:10],
            "genres": result.genres,
            "release_date": result.release_date,
            "screenshots_available": result.screenshots_count,
        }

        content_blocks = []
        images_failed = []

        header_url = result.header_image
        if header_url:
            try:
                header_bytes = await http_mock.get_bytes(header_url, cache_ttl=IMAGE_CACHE_TTL)
                content_blocks.append(Image(data=header_bytes, format="jpeg"))
            except Exception as e:
                images_failed.append(f"header: {e}")

        if include_screenshots and result.screenshots:
            screenshot_urls = result.screenshots[:max_screenshots]
            thumbnail_urls = [
                url.replace(".1920x1080.", ".600x338.") for url in screenshot_urls
            ]

            async def _download_screenshot(url, idx):
                try:
                    data = await http_mock.get_bytes(url, cache_ttl=IMAGE_CACHE_TTL)
                    return (idx, Image(data=data, format="jpeg"), None)
                except Exception as e:
                    return (idx, None, f"screenshot_{idx}: {e}")

            tasks = [_download_screenshot(url, i) for i, url in enumerate(thumbnail_urls)]
            results = await asyncio.gather(*tasks)
            for idx, img, err in sorted(results, key=lambda r: r[0]):
                if img is not None:
                    content_blocks.append(img)
                if err is not None:
                    images_failed.append(err)

        context["images_returned"] = len(content_blocks)
        if images_failed:
            context["images_failed"] = images_failed
        return [json.dumps(context)] + content_blocks

    return fetch_game_art


class TestFetchGameArt:
    """Tests for fetch_game_art tool."""

    @pytest.mark.asyncio
    async def test_negative_appid_returns_error(self):
        fn = _make_fetch_game_art(AsyncMock(), AsyncMock())
        result = await fn(-1)
        assert len(result) == 1
        parsed = json.loads(result[0])
        assert parsed["error_type"] == "validation"
        assert "appid must be positive" in parsed["error"]

    @pytest.mark.asyncio
    async def test_zero_appid_returns_error(self):
        fn = _make_fetch_game_art(AsyncMock(), AsyncMock())
        result = await fn(0)
        parsed = json.loads(result[0])
        assert parsed["error_type"] == "validation"

    @pytest.mark.asyncio
    async def test_max_screenshots_too_low(self):
        fn = _make_fetch_game_art(AsyncMock(), AsyncMock())
        result = await fn(646570, max_screenshots=0)
        parsed = json.loads(result[0])
        assert parsed["error_type"] == "validation"
        assert "max_screenshots" in parsed["error"]

    @pytest.mark.asyncio
    async def test_max_screenshots_too_high(self):
        fn = _make_fetch_game_art(AsyncMock(), AsyncMock())
        result = await fn(646570, max_screenshots=11)
        parsed = json.loads(result[0])
        assert parsed["error_type"] == "validation"

    @pytest.mark.asyncio
    async def test_api_error_forwarded(self):
        store_mock = AsyncMock()
        store_mock.get_app_details.return_value = APIError(
            error_code=404, error_type="not_found", message="Not found"
        )
        fn = _make_fetch_game_art(store_mock, AsyncMock())
        result = await fn(99999999)
        parsed = json.loads(result[0])
        assert parsed["error_type"] == "not_found"

    @pytest.mark.asyncio
    async def test_happy_path_header_and_screenshots(self):
        """Returns header + screenshots as Image objects."""
        store_mock = AsyncMock()
        store_mock.get_app_details.return_value = _make_game_metadata()

        http_mock = AsyncMock()
        http_mock.get_bytes.return_value = b"\xff\xd8\xff\xe0fake-jpeg"

        fn = _make_fetch_game_art(store_mock, http_mock)
        result = await fn(646570)

        # First element is JSON context
        context = json.loads(result[0])
        assert context["appid"] == 646570
        assert context["name"] == "Slay the Spire"
        assert context["images_returned"] == 5  # 1 header + 4 screenshots
        assert "images_failed" not in context

        # Rest are Image objects
        assert len(result) == 6  # context + 5 images
        for img in result[1:]:
            assert isinstance(img, Image)

    @pytest.mark.asyncio
    async def test_screenshots_disabled(self):
        """include_screenshots=False returns only header."""
        store_mock = AsyncMock()
        store_mock.get_app_details.return_value = _make_game_metadata()

        http_mock = AsyncMock()
        http_mock.get_bytes.return_value = b"\xff\xd8\xff\xe0fake-jpeg"

        fn = _make_fetch_game_art(store_mock, http_mock)
        result = await fn(646570, include_screenshots=False)

        context = json.loads(result[0])
        assert context["images_returned"] == 1  # header only
        assert len(result) == 2  # context + header

    @pytest.mark.asyncio
    async def test_thumbnail_url_conversion(self):
        """Screenshot URLs are converted from 1920x1080 to 600x338 thumbnails."""
        store_mock = AsyncMock()
        store_mock.get_app_details.return_value = _make_game_metadata()

        http_mock = AsyncMock()
        http_mock.get_bytes.return_value = b"img"

        fn = _make_fetch_game_art(store_mock, http_mock)
        await fn(646570, max_screenshots=1)

        # Check that get_bytes was called with thumbnail URL
        calls = [str(c) for c in http_mock.get_bytes.call_args_list]
        # At least one call should have .600x338.
        thumbnail_calls = [c for c in calls if ".600x338." in c]
        assert len(thumbnail_calls) >= 1
        # No call should have .1920x1080. (except header which doesn't have it)
        fullsize_calls = [c for c in calls if ".1920x1080." in c]
        assert len(fullsize_calls) == 0

    @pytest.mark.asyncio
    async def test_max_screenshots_limits_downloads(self):
        """Only max_screenshots images are downloaded."""
        store_mock = AsyncMock()
        store_mock.get_app_details.return_value = _make_game_metadata(screenshots_count=6)

        http_mock = AsyncMock()
        http_mock.get_bytes.return_value = b"img"

        fn = _make_fetch_game_art(store_mock, http_mock)
        result = await fn(646570, max_screenshots=2)

        context = json.loads(result[0])
        # 1 header + 2 screenshots
        assert context["images_returned"] == 3
        assert len(result) == 4

    @pytest.mark.asyncio
    async def test_partial_screenshot_failure(self):
        """Individual screenshot failures don't crash the tool."""
        store_mock = AsyncMock()
        store_mock.get_app_details.return_value = _make_game_metadata()

        call_count = 0

        async def flaky_get_bytes(url, cache_ttl=None):
            nonlocal call_count
            call_count += 1
            # Fail on the 3rd call (2nd screenshot)
            if call_count == 3:
                raise ConnectionError("CDN timeout")
            return b"img"

        http_mock = AsyncMock()
        http_mock.get_bytes = flaky_get_bytes

        fn = _make_fetch_game_art(store_mock, http_mock)
        result = await fn(646570, max_screenshots=4)

        context = json.loads(result[0])
        # 1 header + 3 successful screenshots (1 failed)
        assert context["images_returned"] == 4
        assert len(context["images_failed"]) == 1
        assert "screenshot_1" in context["images_failed"][0]

    @pytest.mark.asyncio
    async def test_header_failure_still_returns_screenshots(self):
        """Header download failure doesn't prevent screenshot downloads."""
        store_mock = AsyncMock()
        store_mock.get_app_details.return_value = _make_game_metadata()

        call_count = 0

        async def header_fails(url, cache_ttl=None):
            nonlocal call_count
            call_count += 1
            if call_count == 1:  # First call is header
                raise ConnectionError("CDN error")
            return b"img"

        http_mock = AsyncMock()
        http_mock.get_bytes = header_fails

        fn = _make_fetch_game_art(store_mock, http_mock)
        result = await fn(646570, max_screenshots=2)

        context = json.loads(result[0])
        assert context["images_returned"] == 2  # 0 header + 2 screenshots
        assert "images_failed" in context
        assert any("header" in f for f in context["images_failed"])

    @pytest.mark.asyncio
    async def test_no_header_image(self):
        """Game with empty header_image URL still returns screenshots."""
        store_mock = AsyncMock()
        store_mock.get_app_details.return_value = _make_game_metadata(header_image="")

        http_mock = AsyncMock()
        http_mock.get_bytes.return_value = b"img"

        fn = _make_fetch_game_art(store_mock, http_mock)
        result = await fn(646570, max_screenshots=2)

        context = json.loads(result[0])
        assert context["images_returned"] == 2  # screenshots only


# ---------------------------------------------------------------------------
# Phase 14: Visual Intelligence tool tests
# ---------------------------------------------------------------------------

async def _save_visual_profile(reports_dir: str, **overrides) -> dict:
    """Test helper that replicates save_visual_profile tool logic."""
    import os as _os
    import pathlib as _pathlib
    from pydantic import ValidationError as _VE

    defaults = dict(
        appid=1,
        name="",
        character_style_primary="pixel",
        environment_style_primary="pixel",
        production_fidelity="low",
        mood_primary="dark",
        palette="dark",
        ui_density="minimal",
        perspective="top_down",
        header_composition="logo_abstract",
        character_style_secondary=None,
        environment_style_secondary=None,
        mood_secondary=None,
        ui_density_confidence="inferred",
        genre_tags="",
        notes="",
    )
    defaults.update(overrides)
    p = defaults

    if not reports_dir:
        return {"error": "REPORTS_DIR is not configured. Set REPORTS_DIR environment variable.", "error_type": "configuration"}
    if p["appid"] <= 0:
        return {"error": "appid must be positive", "error_type": "validation"}

    char_sec = _empty_to_none(p["character_style_secondary"])
    env_sec = _empty_to_none(p["environment_style_secondary"])
    mood_sec = _empty_to_none(p["mood_secondary"])
    uid_conf = _empty_to_none(p["ui_density_confidence"]) or "inferred"
    tags_list = [t.strip() for t in p["genre_tags"].split(",") if t.strip()] if p["genre_tags"] else []

    try:
        profile = VisualProfile(
            appid=p["appid"], name=p["name"],
            classified_at=datetime.now(timezone.utc).isoformat(),
            character_style_primary=p["character_style_primary"],
            character_style_secondary=char_sec,
            environment_style_primary=p["environment_style_primary"],
            environment_style_secondary=env_sec,
            production_fidelity=p["production_fidelity"],
            mood_primary=p["mood_primary"], mood_secondary=mood_sec,
            palette=p["palette"], ui_density=p["ui_density"],
            ui_density_confidence=uid_conf,
            perspective=p["perspective"], header_composition=p["header_composition"],
            genre_tags=tags_list, notes=p["notes"],
        )
    except _VE as e:
        return {"error": str(e), "error_type": "validation"}

    cache_path = _pathlib.Path(reports_dir) / "visual-profiles.json"
    if cache_path.exists():
        with open(cache_path, 'r', encoding='utf-8') as f:
            cache = json.load(f)
    else:
        cache = {"_meta": {"tool": "save_visual_profile", "created_at": datetime.now(timezone.utc).isoformat(), "last_updated": "", "profile_count": 0}, "profiles": {}}

    cache["profiles"][str(p["appid"])] = profile.model_dump()
    cache["_meta"]["profile_count"] = len(cache["profiles"])
    cache["_meta"]["last_updated"] = datetime.now(timezone.utc).isoformat()

    tmp_path = cache_path.with_suffix('.tmp')
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    with open(tmp_path, 'w', encoding='utf-8') as f:
        json.dump(cache, f, indent=2, ensure_ascii=False)
    _os.replace(tmp_path, cache_path)

    return {"saved": True, "appid": p["appid"], "name": profile.name, "profile_count": cache["_meta"]["profile_count"]}


async def _load_visual_profiles(reports_dir: str, appids: str = "", genre_tags: str = "") -> dict:
    """Test helper that replicates load_visual_profiles tool logic."""
    import pathlib as _pathlib

    if not reports_dir:
        return {"error": "REPORTS_DIR is not configured. Set REPORTS_DIR environment variable.", "error_type": "configuration"}

    cache_path = _pathlib.Path(reports_dir) / "visual-profiles.json"
    if not cache_path.exists():
        return {"profiles": [], "count": 0, "cache_missing": True}

    with open(cache_path, 'r', encoding='utf-8') as f:
        cache = json.load(f)

    profiles = list(cache.get("profiles", {}).values())

    if appids.strip():
        try:
            appid_set = {int(a.strip()) for a in appids.split(",") if a.strip()}
            profiles = [p for p in profiles if p.get("appid") in appid_set]
        except ValueError:
            return {"error": "appids must be comma-separated integers", "error_type": "validation"}

    if genre_tags.strip():
        tag_set = {t.strip().lower() for t in genre_tags.split(",") if t.strip()}
        profiles = [p for p in profiles if any(t.lower() in tag_set for t in p.get("genre_tags", []))]

    return {"profiles": profiles, "count": len(profiles), "cache_meta": cache.get("_meta", {})}


class TestSaveVisualProfile:
    @pytest.mark.asyncio
    async def test_saves_profile_to_file(self, tmp_path):
        result = await _save_visual_profile(str(tmp_path), appid=646570, name="Slay the Spire")
        assert result["saved"] is True
        assert result["appid"] == 646570
        cache_file = tmp_path / "visual-profiles.json"
        assert cache_file.exists()
        cache = json.loads(cache_file.read_text())
        assert "646570" in cache["profiles"]
        assert cache["profiles"]["646570"]["character_style_primary"] == "pixel"

    @pytest.mark.asyncio
    async def test_invalid_enum_returns_validation_error(self, tmp_path):
        result = await _save_visual_profile(str(tmp_path), character_style_primary="watercolor")
        assert result["error_type"] == "validation"

    @pytest.mark.asyncio
    async def test_overwrite_existing_profile(self, tmp_path):
        await _save_visual_profile(str(tmp_path), appid=1, character_style_primary="pixel")
        result = await _save_visual_profile(str(tmp_path), appid=1, character_style_primary="illustrated")
        assert result["profile_count"] == 1
        cache = json.loads((tmp_path / "visual-profiles.json").read_text())
        assert cache["profiles"]["1"]["character_style_primary"] == "illustrated"

    @pytest.mark.asyncio
    async def test_multiple_profiles_increment_count(self, tmp_path):
        await _save_visual_profile(str(tmp_path), appid=1)
        result = await _save_visual_profile(str(tmp_path), appid=2)
        assert result["profile_count"] == 2

    @pytest.mark.asyncio
    async def test_missing_reports_dir_returns_error(self):
        result = await _save_visual_profile("", appid=1)
        assert result["error_type"] == "configuration"

    @pytest.mark.asyncio
    async def test_negative_appid_returns_error(self, tmp_path):
        result = await _save_visual_profile(str(tmp_path), appid=-1)
        assert result["error_type"] == "validation"

    @pytest.mark.asyncio
    async def test_empty_string_secondary_treated_as_none(self, tmp_path):
        result = await _save_visual_profile(str(tmp_path), appid=1, character_style_secondary="")
        assert result["saved"] is True
        cache = json.loads((tmp_path / "visual-profiles.json").read_text())
        assert cache["profiles"]["1"]["character_style_secondary"] is None

    @pytest.mark.asyncio
    async def test_genre_tags_comma_parsing(self, tmp_path):
        result = await _save_visual_profile(str(tmp_path), appid=1, genre_tags="roguelike, deckbuilder")
        assert result["saved"] is True
        cache = json.loads((tmp_path / "visual-profiles.json").read_text())
        assert cache["profiles"]["1"]["genre_tags"] == ["roguelike", "deckbuilder"]

    @pytest.mark.asyncio
    async def test_meta_envelope_present(self, tmp_path):
        await _save_visual_profile(str(tmp_path), appid=1)
        cache = json.loads((tmp_path / "visual-profiles.json").read_text())
        meta = cache["_meta"]
        assert meta["tool"] == "save_visual_profile"
        assert "created_at" in meta
        assert "last_updated" in meta
        assert meta["profile_count"] == 1


class TestLoadVisualProfiles:
    @pytest.mark.asyncio
    async def test_load_from_empty_cache(self, tmp_path):
        result = await _load_visual_profiles(str(tmp_path))
        assert result["cache_missing"] is True
        assert result["count"] == 0

    @pytest.mark.asyncio
    async def test_load_all_profiles(self, tmp_path):
        for i in range(3):
            await _save_visual_profile(str(tmp_path), appid=i + 1)
        result = await _load_visual_profiles(str(tmp_path))
        assert result["count"] == 3

    @pytest.mark.asyncio
    async def test_filter_by_appids(self, tmp_path):
        for i in range(3):
            await _save_visual_profile(str(tmp_path), appid=i + 1)
        result = await _load_visual_profiles(str(tmp_path), appids="1,2")
        assert result["count"] == 2

    @pytest.mark.asyncio
    async def test_filter_by_genre_tags(self, tmp_path):
        await _save_visual_profile(str(tmp_path), appid=1, genre_tags="roguelike")
        await _save_visual_profile(str(tmp_path), appid=2, genre_tags="roguelike")
        await _save_visual_profile(str(tmp_path), appid=3, genre_tags="strategy")
        result = await _load_visual_profiles(str(tmp_path), genre_tags="roguelike")
        assert result["count"] == 2

    @pytest.mark.asyncio
    async def test_genre_tag_filter_case_insensitive(self, tmp_path):
        await _save_visual_profile(str(tmp_path), appid=1, genre_tags="Roguelike")
        result = await _load_visual_profiles(str(tmp_path), genre_tags="roguelike")
        assert result["count"] == 1

    @pytest.mark.asyncio
    async def test_invalid_appid_string_returns_error(self, tmp_path):
        await _save_visual_profile(str(tmp_path), appid=1)
        result = await _load_visual_profiles(str(tmp_path), appids="abc")
        assert result["error_type"] == "validation"

    @pytest.mark.asyncio
    async def test_missing_reports_dir_returns_error(self):
        result = await _load_visual_profiles("")
        assert result["error_type"] == "configuration"
