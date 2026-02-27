"""Steam API clients for SteamSpy and Steam Store."""

import asyncio
import re
import httpx
from datetime import datetime, timezone
from statistics import mean, median, quantiles
from html.parser import HTMLParser
from html import unescape
from src.config import Config
from src.http_client import CachedAPIClient
from src.schemas import (
    GameMetadata, SearchResult, CommercialData, EngagementData, APIError,
    AggregateEngagementData, MetricStats, GameEngagementSummary,
    ReviewItem, ReviewsData, ReviewAggregates, PlaytimeBucket, SentimentPeriod,
    ReviewsMeta, QueryParams, ReviewSnippet, EADateInfo,
    GamalyticHistoryEntry, CountryDataEntry, CompetitorEntry, EstimateModelEntry, DLCEntry,
)
from src.logging_config import get_logger
from src.timeout_utils import gather_with_timeout, compute_gather_timeout, GAMALYTIC_RATE

logger = get_logger(__name__)


class _HTMLTextExtractor(HTMLParser):
    """HTML parser for extracting plain text from HTML."""
    def __init__(self):
        super().__init__()
        self.text_parts = []

    def handle_data(self, data):
        self.text_parts.append(data)

    def get_text(self):
        return unescape(''.join(self.text_parts)).strip()


def strip_html(html: str) -> str:
    """Convert HTML to plain text, stripping tags and unescaping entities.

    Args:
        html: HTML string to convert

    Returns:
        Plain text with HTML tags removed and entities unescaped
    """
    if not html:
        return ""
    parser = _HTMLTextExtractor()
    parser.feed(html)
    return parser.get_text()


def parse_supported_languages(raw: str) -> list[dict]:
    """Parse Steam supported_languages HTML into structured list.

    Input format: "English<strong>*</strong>, French<strong>*</strong>, German, ..."
    with trailing note: "<br><strong>*</strong>languages with full audio support"

    Returns:
        List of {"language": str, "full_audio": bool} dicts
    """
    if not raw:
        return []
    # Remove the trailing note line
    clean = re.sub(r'<br\s*/?>\s*<strong>\*</strong>[^,]*$', '', raw, flags=re.IGNORECASE | re.DOTALL)
    parts = [p.strip() for p in clean.split(",") if p.strip()]
    result = []
    for part in parts:
        has_audio = bool(re.search(r'<strong>\*</strong>', part, re.IGNORECASE))
        name = strip_html(part).strip().rstrip("*").strip()
        if name:
            result.append({"language": name, "full_audio": has_audio})
    return result


def parse_steam_date(date_str: str) -> str | None:
    """Parse Steam date string to ISO format YYYY-MM-DD.

    Handles common Steam date formats:
    - "23 Jan, 2019"
    - "Jan 23, 2019"
    - "January 23, 2019"
    - "23 January, 2019"

    Args:
        date_str: Steam date string

    Returns:
        ISO date string "YYYY-MM-DD" or None if unparseable/placeholder
    """
    if not date_str or date_str.lower() in ("coming soon", "tba", "to be announced"):
        return None

    for fmt in ["%d %b, %Y", "%b %d, %Y", "%B %d, %Y", "%d %B, %Y"]:
        try:
            dt = datetime.strptime(date_str.strip(), fmt)
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            continue

    return None


# Tag normalization: Map common user-friendly tag names to SteamSpy's exact format
TAG_ALIASES: dict[str, str] = {
    "roguelike": "Rogue-like",
    "roguelite": "Rogue-lite",
    "roguevania": "Rogue-like",
    "metroidvania": "Metroidvania",
    "hack and slash": "Hack and Slash",
    "beat em up": "Beat 'em up",
    "shoot em up": "Shoot 'Em Up",
    "4x": "4X",
    "turn based": "Turn-Based",
    "turn based strategy": "Turn-Based Strategy",
    "turn based tactics": "Turn-Based Tactics",
    "real time strategy": "Real-Time Strategy",
    "real time tactics": "Real-Time Tactics",
    "co op": "Co-op",
    "local co op": "Local Co-Op",
    "sci fi": "Sci-fi",
    "choose your own adventure": "Choose Your Own Adventure",
    "city builder": "City Builder",
    "base building": "Base Building",
    "tower defense": "Tower Defense",
    "deck building": "Deckbuilder",
    "deckbuilding": "Deckbuilder",
    "bullet hell": "Bullet Hell",
    "souls like": "Souls-like",
    "soulslike": "Souls-like",
    "rogue like": "Rogue-like",
    "rogue lite": "Rogue-lite",
}


def normalize_tag(tag: str) -> str:
    """Normalize tag name to SteamSpy's expected format.

    Args:
        tag: User-provided tag name

    Returns:
        Normalized tag name (from alias map if found, otherwise original with whitespace stripped)
    """
    tag = tag.strip()
    normalized = TAG_ALIASES.get(tag.lower())
    return normalized if normalized else tag


# Bowling fallback detection: Known AppIDs in SteamSpy's default fallback set
# These are empirically verified AppIDs from SteamSpy's actual bowling fallback response
BOWLING_FALLBACK_APPIDS = {901583, 12210, 2990, 891040, 22230}

# Tags with fewer games than this threshold get cross-validated via appdetails;
# broad tags (>=5000) use bulk data as-is for coverage
TAG_CROSSVAL_THRESHOLD = 5000


# ---------------------------------------------------------------------------
# Review Intelligence helpers
# ---------------------------------------------------------------------------

def _strip_bbcode(text: str) -> str:
    """Strip BBCode markup tags, preserving text content including spoiler text.

    Handles: [b], [i], [u], [h2], [spoiler], [hr], [list], [*], [url=...], etc.
    Order matters — URL tags require two-pass removal before generic tag pass.

    Args:
        text: BBCode-formatted string

    Returns:
        Plain text with all BBCode tags removed and whitespace trimmed
    """
    # Remove [url=...] opening tags (with attribute)
    text = re.sub(r'\[url=[^\]]*\]', '', text)
    # Remove [/url] closing tags
    text = re.sub(r'\[/url\]', '', text, flags=re.IGNORECASE)
    # Convert list item markers to newlines
    text = re.sub(r'\[\*\]', '\n', text)
    # Remove all remaining BBCode tags (opening, closing, self-closing with attributes)
    text = re.sub(r'\[/?[a-zA-Z][a-zA-Z0-9]*[^\]]*\]', '', text)
    return text.strip()


def _truncate_text(text: str, max_chars: int = 1000) -> tuple[str, bool, int]:
    """Truncate text to max_chars with word-boundary awareness.

    Attempts to break at a word boundary (last space before limit) rather than
    mid-word, falling back to hard truncation if the nearest space is too far back.

    Args:
        text: Input string to truncate
        max_chars: Maximum character count (default 1000)

    Returns:
        Tuple of (truncated_text, is_truncated, full_length)
    """
    full_length = len(text)
    if full_length <= max_chars:
        return (text, False, full_length)
    # Try to break at a word boundary
    last_space = text.rfind(' ', 0, max_chars)
    if last_space > max_chars * 0.8:
        candidate = text[:last_space]
    else:
        candidate = text[:max_chars]
    return (candidate, True, full_length)


def _ms_to_iso(ms_ts) -> str | None:
    """Convert a Gamalytic Unix millisecond timestamp to an ISO date string.

    Args:
        ms_ts: Millisecond Unix timestamp (int or float). Falsy values (0, None) return None.

    Returns:
        ISO date string "YYYY-MM-DD" in UTC, or None if timestamp is absent/zero
    """
    if not ms_ts:
        return None
    return datetime.fromtimestamp(ms_ts / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


class SteamSpyClient:
    """Client for SteamSpy API (tag-based game search)."""

    BASE_URL = "https://steamspy.com/api.php"

    def __init__(self, http_client: CachedAPIClient):
        """Initialize SteamSpy client with cached HTTP client.

        Args:
            http_client: CachedAPIClient instance for API requests
        """
        self.http = http_client

    def _detect_steamspy_fallback(self, data: dict, tag: str) -> bool:
        """Detect if SteamSpy returned its default bowling game fallback instead of actual tag results.

        SteamSpy returns a default ~70 bowling games dataset for unrecognized tags.
        Detection: Small result set (<100) containing known bowling game AppIDs.

        Args:
            data: SteamSpy API response dict (appid keys)
            tag: Tag that was queried

        Returns:
            True if bowling fallback detected, False otherwise
        """
        # Only check for small result sets (fallback is ~70 games)
        if len(data) >= 100:
            return False

        # Convert string AppID keys to integers
        appids_in_response = {int(appid) for appid in data.keys()}

        # Check if at least 2 known bowling game AppIDs are present
        bowling_matches = len(BOWLING_FALLBACK_APPIDS & appids_in_response)
        return bowling_matches >= 2

    async def search_by_tag(self, tag: str, limit: int | None = 10, sort_by: str = "owners") -> SearchResult | APIError:
        """Search for games by tag using SteamSpy API with enriched per-game summaries.

        Returns per-game summary data (name, owners, CCU, price, reviews, playtime) from
        SteamSpy bulk endpoint. Uses bulk data directly for all tag sizes.

        Args:
            tag: Tag to search for (e.g., "roguelike", "indie")
            limit: Maximum number of games to return (default: 10). Use None to return all results.
            sort_by: Sort field - "owners" (default), "ccu", "price", "review_score"

        Returns:
            SearchResult with enriched game summaries, or APIError on failure
        """
        original_tag = tag
        tag = normalize_tag(tag)

        try:
            data, fetched_at, cache_age = await self.http.get_with_metadata(
                self.BASE_URL,
                params={"request": "tag", "tag": tag},
                cache_ttl=3600
            )

            if self._detect_steamspy_fallback(data, tag):
                logger.warning(f"SteamSpy returned bowling fallback for tag '{tag}' - tag may not exist")
                return APIError(
                    error_code=404,
                    error_type="tag_not_found",
                    message=f"Tag '{tag}' not recognized by SteamSpy. The API returned default results instead of matching games. Try a different tag name or check Steam's tag list."
                )

            from src.schemas import GameSummary

            games_list = []

            # Parse bulk data directly — no per-game cross-validation
            # SteamSpy bulk endpoint is the source of truth for tag membership
            for game_data in data.values():
                parsed = self._parse_game_data(game_data)
                games_list.append(GameSummary(**parsed))

            if len(data) < TAG_CROSSVAL_THRESHOLD:  # Used for logging only (cross-validation removed)
                logger.info(f"Niche tag '{tag}': {len(data)} games from bulk endpoint (no cross-validation)")
            else:
                logger.info(f"Broad tag '{tag}': {len(data)} games from bulk endpoint")

            # Sort by specified field
            sort_key_map = {
                "owners": lambda g: g.owners_midpoint,
                "ccu": lambda g: g.ccu,
                "price": lambda g: g.price,
                "review_score": lambda g: g.review_score or 0
            }
            sort_key = sort_key_map.get(sort_by, sort_key_map["owners"])
            games_list.sort(key=sort_key, reverse=True)

            # Apply limit AFTER sorting (limit gets top-N by sort criteria)
            limited_games = games_list if limit is None else games_list[:limit]

            # Warn on large responses (>5000 games)
            warning = None
            if len(data) > 5000:
                warning = f"Large result: {len(data)} games. Consider using a more specific tag or a lower limit."

            return SearchResult(
                appids=[g.appid for g in limited_games],  # Backward compat
                games=limited_games,  # New enriched field
                tag=tag,
                total_found=len(data),
                sort_by=sort_by,
                result_size_warning=warning,
                normalized_tag=tag if tag != original_tag else None,
                fetched_at=fetched_at,
                cache_age_seconds=cache_age
            )
        except httpx.HTTPStatusError as e:
            status = e.response.status_code
            error_type = "rate_limit" if status == 429 else "api_error"
            return APIError(
                error_code=status,
                error_type=error_type,
                message=f"SteamSpy API error: {status}"
            )
        except Exception as e:
            logger.error(f"SteamSpy error: {e}")
            return APIError(
                error_code=502,
                error_type="api_error",
                message=str(e)
            )

    async def get_engagement_data(self, appid: int) -> EngagementData | APIError:
        """Fetch player engagement metrics for a specific game from SteamSpy.

        Args:
            appid: Steam AppID to fetch engagement data for

        Returns:
            EngagementData with CCU, owners, playtime, reviews, tags, and derived metrics, or APIError on failure
        """
        try:
            # Use get_with_metadata for freshness tracking (1 hour cache TTL)
            data, fetched_at, cache_age = await self.http.get_with_metadata(
                self.BASE_URL,
                params={"request": "appdetails", "appid": str(appid)},
                cache_ttl=3600
            )

            # Parse owner range: "20,000 .. 50,000" -> owners_min, owners_max, owners_midpoint
            owners_str = data.get("owners", "0 .. 0")
            owners_str = owners_str.replace(",", "")
            parts = owners_str.split(" .. ")
            if len(parts) == 2:
                try:
                    owners_min = int(parts[0])
                    owners_max = int(parts[1])
                except ValueError:
                    owners_min = 0
                    owners_max = 0
            else:
                owners_min = 0
                owners_max = 0
            owners_midpoint = (owners_min + owners_max) / 2

            # Get raw values
            ccu = data.get("ccu", 0)
            positive = data.get("positive", 0)
            negative = data.get("negative", 0)
            total_reviews = positive + negative

            # Convert playtime from minutes to hours
            average_forever = data.get("average_forever", 0) / 60.0
            average_2weeks = data.get("average_2weeks", 0) / 60.0
            median_forever = data.get("median_forever", 0) / 60.0
            median_2weeks = data.get("median_2weeks", 0) / 60.0

            # Convert price from cents to dollars
            price_str = data.get("price", "0")
            try:
                price = int(price_str) / 100.0
            except (ValueError, TypeError):
                price = 0.0

            # Parse tags dict (SteamSpy returns {"TagName": weight_int, ...})
            tags = data.get("tags", {})
            if not isinstance(tags, dict):
                tags = {}

            # Compute derived metrics with division-by-zero guards
            ccu_ratio = round(ccu / owners_midpoint * 100, 2) if owners_midpoint > 0 else None
            review_score = round(positive / total_reviews * 100, 2) if total_reviews > 0 else None
            playtime_engagement = round(median_forever / average_forever, 2) if average_forever > 0 else None
            activity_ratio = round(average_2weeks / average_forever * 100, 2) if average_forever > 0 else None

            # Apply context-aware quality flags
            # Pattern: if zero AND other data exists = "available" (real zero)
            #          if zero AND no other data = "zero_uncertain" (likely missing)
            has_other_data = owners_midpoint > 0 or total_reviews > 0

            if ccu == 0:
                ccu_quality = "available" if has_other_data else "zero_uncertain"
            else:
                ccu_quality = "available"

            if owners_midpoint == 0:
                owners_quality = "available" if (ccu > 0 or total_reviews > 0) else "zero_uncertain"
            else:
                owners_quality = "available"

            if average_forever == 0 and median_forever == 0:
                playtime_quality = "available" if has_other_data else "zero_uncertain"
            else:
                playtime_quality = "available"

            if total_reviews == 0:
                reviews_quality = "available" if (ccu > 0 or owners_midpoint > 0) else "zero_uncertain"
            else:
                reviews_quality = "available"

            return EngagementData(
                appid=appid,
                name=data.get("name", ""),
                developer=data.get("developer", ""),
                publisher=data.get("publisher", ""),
                ccu=ccu,
                owners_min=owners_min,
                owners_max=owners_max,
                owners_midpoint=owners_midpoint,
                average_forever=average_forever,
                average_2weeks=average_2weeks,
                median_forever=median_forever,
                median_2weeks=median_2weeks,
                positive=positive,
                negative=negative,
                total_reviews=total_reviews,
                price=price,
                score_rank=str(data.get("score_rank", "")),
                genre=data.get("genre", "") or "",
                languages=data.get("languages", "") or "",
                tags=tags,
                ccu_ratio=ccu_ratio,
                review_score=review_score,
                playtime_engagement=playtime_engagement,
                activity_ratio=activity_ratio,
                ccu_quality=ccu_quality,
                owners_quality=owners_quality,
                playtime_quality=playtime_quality,
                reviews_quality=reviews_quality,
                fetched_at=fetched_at,
                cache_age_seconds=cache_age
            )
        except httpx.HTTPStatusError as e:
            status = e.response.status_code
            error_type = "rate_limit" if status == 429 else "api_error"
            return APIError(
                error_code=status,
                error_type=error_type,
                message=f"SteamSpy API error: {status}"
            )
        except Exception as e:
            logger.error(f"SteamSpy engagement data error for AppID {appid}: {e}")
            return APIError(
                error_code=502,
                error_type="api_error",
                message=str(e)
            )

    def _parse_game_data(self, data: dict) -> dict:
        """Parse common fields from SteamSpy game data into standardized format.

        Args:
            data: Raw SteamSpy game data dict

        Returns:
            Parsed dict with standardized field names and types
        """
        # Parse owner range
        owners_str = data.get("owners", "0 .. 0")
        owners_str = owners_str.replace(",", "")
        parts = owners_str.split(" .. ")
        if len(parts) == 2:
            try:
                owners_min = int(parts[0])
                owners_max = int(parts[1])
            except ValueError:
                owners_min = 0
                owners_max = 0
        else:
            owners_min = 0
            owners_max = 0
        owners_midpoint = (owners_min + owners_max) / 2

        # Get raw values
        ccu = data.get("ccu", 0)
        positive = data.get("positive", 0)
        negative = data.get("negative", 0)

        # Convert playtime from minutes to hours (all 4 playtime fields)
        average_forever = data.get("average_forever", 0) / 60.0
        average_2weeks = data.get("average_2weeks", 0) / 60.0
        median_forever = data.get("median_forever", 0) / 60.0
        median_2weeks = data.get("median_2weeks", 0) / 60.0

        # Convert price from cents to dollars
        price_str = data.get("price", "0")
        try:
            price = int(price_str) / 100.0
        except (ValueError, TypeError):
            price = 0.0

        # Compute review score if reviews exist
        total_reviews = positive + negative
        review_score = round(positive / total_reviews * 100, 2) if total_reviews > 0 else None

        return {
            "appid": int(data.get("appid", 0)),
            "name": data.get("name", ""),
            "developer": data.get("developer", ""),
            "publisher": data.get("publisher", ""),
            "ccu": ccu,
            "owners_min": owners_min,
            "owners_max": owners_max,
            "owners_midpoint": owners_midpoint,
            "average_forever": average_forever,
            "median_forever": median_forever,
            "average_2weeks": average_2weeks,
            "median_2weeks": median_2weeks,
            "positive": positive,
            "negative": negative,
            "review_score": review_score,
            "price": price,
            "score_rank": str(data.get("score_rank", "")),
            "userscore": data.get("userscore", 0)
        }

    async def aggregate_engagement(
        self,
        tag: str | None = None,
        appids: list[int] | None = None,
        sort_by: str = "owners"
    ) -> AggregateEngagementData | APIError:
        """Aggregate engagement metrics across multiple games.

        Supports two input modes:
        1. Tag-based: Fetch all games in a tag (bulk endpoint, 1 API call)
        2. AppID-list: Fetch specific games (concurrent individual calls)

        Args:
            tag: Steam tag name for bulk query
            appids: List of AppIDs for custom selection
            sort_by: Sort field for per-game list ("owners", "ccu", "reviews", "playtime")

        Returns:
            AggregateEngagementData with per-metric stats, dual metric approaches, per-game breakdown, or APIError
        """
        games_list = []
        failures = []
        fetched_at = datetime.now(timezone.utc)
        cache_age = 0

        # Branch 1: Tag-based (bulk endpoint)
        if tag:
            # Normalize tag before API call
            tag = normalize_tag(tag)

            try:
                data, fetched_at, cache_age = await self.http.get_with_metadata(
                    self.BASE_URL,
                    params={"request": "tag", "tag": tag},
                    cache_ttl=3600
                )

                if not data:
                    return APIError(
                        error_code=404,
                        error_type="not_found",
                        message=f"No games found for tag: {tag}"
                    )

                # Check for bowling fallback before processing
                if self._detect_steamspy_fallback(data, tag):
                    logger.warning(f"SteamSpy returned bowling fallback for tag '{tag}' in aggregation")
                    return APIError(
                        error_code=404,
                        error_type="tag_not_found",
                        message=f"Tag '{tag}' not recognized by SteamSpy. The API returned default results instead of matching games. Try a different tag name or check Steam's tag list."
                    )

                # Parse bulk data directly — no per-game cross-validation
                # SteamSpy bulk endpoint is the source of truth for tag membership
                for game_data in data.values():
                    parsed = self._parse_game_data(game_data)
                    games_list.append(GameEngagementSummary(**parsed))

                if len(data) < TAG_CROSSVAL_THRESHOLD:  # Used for logging only (cross-validation removed)
                    logger.info(f"Niche tag '{tag}': {len(data)} games from bulk endpoint (no cross-validation)")
                else:
                    logger.info(f"Broad tag '{tag}': {len(data)} games from bulk endpoint")

            except httpx.HTTPStatusError as e:
                status = e.response.status_code
                error_type = "rate_limit" if status == 429 else "api_error"
                return APIError(
                    error_code=status,
                    error_type=error_type,
                    message=f"SteamSpy API error: {status}"
                )
            except Exception as e:
                logger.error(f"Tag aggregation error: {e}")
                return APIError(
                    error_code=502,
                    error_type="api_error",
                    message=str(e)
                )

        # Branch 2: AppID-list (concurrent individual calls)
        elif appids:
            tasks = [self.get_engagement_data(appid) for appid in appids]
            from src.timeout_utils import STEAMSPY_RATE as _SS_RATE
            gr = await gather_with_timeout(
                tasks,
                timeout=compute_gather_timeout(len(appids), _SS_RATE),
                label=f"aggregate_engagement({len(appids)})",
            )
            results = gr.results

            for appid, result in zip(appids, results):
                if isinstance(result, Exception):
                    failures.append({"appid": appid, "reason": "exception"})
                elif isinstance(result, APIError):
                    failures.append({"appid": appid, "reason": result.error_type})
                elif isinstance(result, EngagementData):
                    # Convert EngagementData to GameEngagementSummary
                    games_list.append(GameEngagementSummary(
                        appid=result.appid,
                        name=result.name,
                        ccu=result.ccu,
                        owners_midpoint=result.owners_midpoint,
                        average_forever=result.average_forever,
                        average_2weeks=result.average_2weeks,
                        positive=result.positive,
                        negative=result.negative,
                        review_score=result.review_score,
                        price=result.price,
                        tags=result.tags
                    ))

            if not games_list:
                return APIError(
                    error_code=404,
                    error_type="not_found",
                    message="No valid games retrieved from AppID list"
                )

        else:
            return APIError(
                error_code=400,
                error_type="validation",
                message="Either tag or appids must be provided"
            )

        # Compute statistics for each metric
        # For tag branch: games_total = validation scope (candidates checked)
        # For appid branch: games_total = all requested games (successes + failures)
        if tag:
            games_total = len(games_list) + len(failures)  # Total candidates validated
        else:
            games_total = len(games_list) + len(failures)
        games_analyzed = len(games_list)

        # Helper to compute stats for a metric
        def compute_metric_stats(values: list[float]) -> MetricStats | None:
            if len(values) < 2:
                return None
            return MetricStats(
                mean=round(mean(values), 2),
                median=round(median(values), 2),
                p25=round(quantiles(values, n=4)[0], 2),
                p75=round(quantiles(values, n=4)[2], 2),
                min=round(min(values), 2),
                max=round(max(values), 2),
                games_with_data=len(values)
            )

        # CCU stats (include zeros - real measurement)
        ccu_values = [g.ccu for g in games_list]
        ccu_stats = compute_metric_stats(ccu_values)

        # Owners stats (exclude zeros)
        owners_values = [g.owners_midpoint for g in games_list if g.owners_midpoint > 0]
        owners_stats = compute_metric_stats(owners_values)

        # Playtime stats (exclude zeros)
        playtime_values = [g.average_forever for g in games_list if g.average_forever > 0]
        playtime_stats = compute_metric_stats(playtime_values)

        # Review score stats (exclude None)
        review_score_values = [g.review_score for g in games_list if g.review_score is not None]
        review_score_stats = compute_metric_stats(review_score_values)

        # Compute dual metrics: market-weighted vs per-game average
        market_weighted = {}
        per_game_average = {}

        # CCU ratio
        total_ccu = sum(g.ccu for g in games_list)
        total_owners = sum(g.owners_midpoint for g in games_list)
        if total_owners > 0:
            market_weighted["ccu_ratio"] = round(total_ccu / total_owners * 100, 2)
        else:
            market_weighted["ccu_ratio"] = None

        # Per-game CCU ratio
        ccu_ratios = [
            g.ccu / g.owners_midpoint * 100
            for g in games_list
            if g.owners_midpoint > 0
        ]
        if ccu_ratios:
            per_game_average["ccu_ratio"] = round(mean(ccu_ratios), 2)
        else:
            per_game_average["ccu_ratio"] = None

        # Review score (market-weighted)
        total_positive = sum(g.positive for g in games_list)
        total_negative = sum(g.negative for g in games_list)
        total_reviews = total_positive + total_negative
        if total_reviews > 0:
            market_weighted["review_score"] = round(total_positive / total_reviews * 100, 2)
        else:
            market_weighted["review_score"] = None

        # Review score (per-game average)
        review_scores = [g.review_score for g in games_list if g.review_score is not None]
        if review_scores:
            per_game_average["review_score"] = round(mean(review_scores), 2)
        else:
            per_game_average["review_score"] = None

        # Activity ratio (market-weighted)
        total_avg_2weeks = sum(g.average_2weeks for g in games_list)
        total_avg_forever = sum(g.average_forever for g in games_list)
        if total_avg_forever > 0:
            market_weighted["activity_ratio"] = round(total_avg_2weeks / total_avg_forever * 100, 2)
        else:
            market_weighted["activity_ratio"] = None

        # Activity ratio (per-game average)
        activity_ratios = [
            g.average_2weeks / g.average_forever * 100
            for g in games_list
            if g.average_forever > 0
        ]
        if activity_ratios:
            per_game_average["activity_ratio"] = round(mean(activity_ratios), 2)
        else:
            per_game_average["activity_ratio"] = None

        # Sort per-game list
        sort_key_map = {
            "owners": lambda g: g.owners_midpoint,
            "ccu": lambda g: g.ccu,
            "reviews": lambda g: g.positive + g.negative,
            "playtime": lambda g: g.average_forever
        }
        sort_key = sort_key_map.get(sort_by, sort_key_map["owners"])
        games_list.sort(key=sort_key, reverse=True)

        return AggregateEngagementData(
            tag=tag,
            appids_requested=appids,
            games_total=games_total,
            games_analyzed=games_analyzed,
            ccu_stats=ccu_stats,
            owners_stats=owners_stats,
            playtime_stats=playtime_stats,
            review_score_stats=review_score_stats,
            market_weighted=market_weighted,
            per_game_average=per_game_average,
            games=games_list,
            failures=failures,
            sort_by=sort_by,
            fetched_at=fetched_at,
            cache_age_seconds=cache_age
        )


class SteamStoreClient:
    """Client for Steam Store API (game metadata fetching)."""

    BASE_URL = "https://store.steampowered.com/api"

    def __init__(self, http_client: CachedAPIClient):
        """Initialize Steam Store client with cached HTTP client.

        Args:
            http_client: CachedAPIClient instance for API requests
        """
        self.http = http_client

    async def get_app_details(self, appid: int) -> GameMetadata | APIError:
        """Fetch metadata for a specific AppID using Steam Store API.

        Args:
            appid: Steam AppID to fetch metadata for

        Returns:
            GameMetadata with game details, or APIError on failure
        """
        try:
            # Use get_with_metadata for freshness tracking (7 day cache TTL)
            data, fetched_at, cache_age = await self.http.get_with_metadata(
                f"{self.BASE_URL}/appdetails",
                params={"appids": str(appid)},
                cache_ttl=604800  # 7 days — platforms/ratings/achievements rarely change
            )
            # Response: {"12345": {"success": true, "data": {...}}}
            app_data = data.get(str(appid), {})
            if not app_data.get("success"):
                return APIError(
                    error_code=404,
                    error_type="not_found",
                    message=f"AppID {appid} not found or unavailable"
                )

            details = app_data.get("data", {})

            # Existing fields (unchanged for backward compat)
            tags = [g.get("description", "") for g in details.get("genres", [])]
            developers = details.get("developers", [])
            developer = developers[0] if developers else ""

            # Initialize new fields with defaults in case parsing fails
            new_fields = {
                "publisher": "",
                "short_description": "",
                "detailed_description": "",
                "header_image": "",
                "price": None,
                "is_free_to_play": False,
                "platforms": {},
                "release_date": None,
                "release_date_raw": "",
                "metacritic_score": None,
                "metacritic_url": "",
                "categories": [],
                "genres": [],
                "recommendations": 0,
                "screenshots": [],
                "screenshots_count": 0,
                "movies": [],
                "movies_count": 0,
                "dlc": [],
                "dlc_count": 0,
                "content_descriptors": [],
                "supported_languages_count": 0,
                "supported_languages_raw": "",
                "achievement_count": None,
                "ratings": {},
                "supported_languages": [],
                "developer_website": None,
                "pc_requirements_min": "",
                "pc_requirements_rec": "",
                "controller_support": None,
            }

            # Pre-initialize languages_raw so Phase 8 block can reference it even if the main try/except exits early
            languages_raw = ""

            # Parse new fields with defensive try/except to prevent individual field failures from crashing
            try:
                # Publisher
                publishers = details.get("publishers", [])
                new_fields["publisher"] = publishers[0] if publishers else ""

                # Descriptions
                new_fields["short_description"] = details.get("short_description", "")
                new_fields["detailed_description"] = strip_html(details.get("detailed_description", ""))

                # Visual assets
                new_fields["header_image"] = details.get("header_image", "")

                # Price
                new_fields["is_free_to_play"] = details.get("is_free", False)
                price_overview = details.get("price_overview")
                if price_overview:
                    try:
                        new_fields["price"] = price_overview.get("final", 0) / 100.0
                    except (TypeError, ValueError):
                        new_fields["price"] = None
                elif new_fields["is_free_to_play"]:
                    new_fields["price"] = 0.0

                # Platforms
                platforms_data = details.get("platforms", {})
                new_fields["platforms"] = {
                    "windows": platforms_data.get("windows", False),
                    "mac": platforms_data.get("mac", False),
                    "linux": platforms_data.get("linux", False)
                }

                # Release date
                release_date_data = details.get("release_date", {})
                new_fields["release_date_raw"] = release_date_data.get("date", "")
                new_fields["release_date"] = parse_steam_date(new_fields["release_date_raw"])

                # Metacritic
                metacritic_data = details.get("metacritic")
                if metacritic_data:
                    new_fields["metacritic_score"] = metacritic_data.get("score")
                    new_fields["metacritic_url"] = metacritic_data.get("url", "")

                # Categories & Genres
                new_fields["categories"] = [c.get("description", "") for c in details.get("categories", [])]
                new_fields["genres"] = [g.get("description", "") for g in details.get("genres", [])]

                # Recommendations
                recommendations_data = details.get("recommendations", {})
                new_fields["recommendations"] = recommendations_data.get("total", 0) if recommendations_data else 0

                # Screenshots
                screenshots_data = details.get("screenshots", [])
                new_fields["screenshots"] = [s.get("path_full", "") for s in screenshots_data if s.get("path_full")]
                new_fields["screenshots_count"] = len(new_fields["screenshots"])

                # Movies (trailers)
                movies_data = details.get("movies", [])
                movies = []
                for m in movies_data:
                    webm_data = m.get("webm", {}) or {}
                    mp4_data = m.get("mp4", {}) or {}
                    movie_entry = {
                        "name": m.get("name", ""),
                        "thumbnail": m.get("thumbnail", ""),
                        "webm_url": webm_data.get("max", ""),
                        "mp4_url": mp4_data.get("max", "")
                    }
                    movies.append(movie_entry)
                new_fields["movies"] = movies
                new_fields["movies_count"] = len(movies)

                # DLC
                dlc_data = details.get("dlc", [])
                new_fields["dlc"] = dlc_data  # List of AppIDs
                new_fields["dlc_count"] = len(dlc_data)

                # Content descriptors
                content_descriptors_data = details.get("content_descriptors", {})
                notes = content_descriptors_data.get("notes", "") if content_descriptors_data else ""
                if notes:
                    new_fields["content_descriptors"] = [cd.strip() for cd in notes.split("\n") if cd.strip()]

                # Languages
                languages_raw = details.get("supported_languages", "")
                new_fields["supported_languages_raw"] = languages_raw
                if languages_raw:
                    languages_clean = strip_html(languages_raw)
                    new_fields["supported_languages_count"] = len([l.strip() for l in languages_clean.split(",") if l.strip()]) if languages_clean else 0

            except Exception as e:
                # Log the error but continue with defaults - old fields must still be returned
                logger.warning(f"Failed to parse extended fields for AppID {appid}: {e}. Returning with defaults.")

            # Phase 8 extended fields — each has its own try/except to remain isolated
            try:
                achievements_data = details.get("achievements", {})
                if isinstance(achievements_data, dict):
                    new_fields["achievement_count"] = achievements_data.get("total")
            except Exception as e:
                logger.warning(f"AppID {appid}: achievement_count parse error: {e}")

            try:
                ratings_raw = details.get("ratings", {})
                if isinstance(ratings_raw, dict):
                    parsed_ratings = {}
                    for authority, rating_data in ratings_raw.items():
                        if isinstance(rating_data, dict):
                            parsed_ratings[authority] = {
                                "rating": rating_data.get("rating", ""),
                                "descriptors": rating_data.get("descriptors", ""),
                            }
                    new_fields["ratings"] = parsed_ratings
            except Exception as e:
                logger.warning(f"AppID {appid}: ratings parse error: {e}")

            try:
                if languages_raw:
                    new_fields["supported_languages"] = parse_supported_languages(languages_raw)
            except Exception as e:
                logger.warning(f"AppID {appid}: supported_languages parse error: {e}")

            try:
                new_fields["developer_website"] = details.get("website") or None
            except Exception as e:
                logger.warning(f"AppID {appid}: developer_website parse error: {e}")

            try:
                pc_req_data = details.get("pc_requirements", {})
                if isinstance(pc_req_data, dict):
                    new_fields["pc_requirements_min"] = strip_html(pc_req_data.get("minimum", ""))
                    new_fields["pc_requirements_rec"] = strip_html(pc_req_data.get("recommended", ""))
                elif isinstance(pc_req_data, list):
                    # Some games return empty list instead of dict
                    pass  # Keep defaults (empty strings)
            except Exception as e:
                logger.warning(f"AppID {appid}: pc_requirements parse error: {e}")

            try:
                new_fields["controller_support"] = details.get("controller_support") or None
                # Validate: should be "full" or "partial" or None
                if new_fields["controller_support"] not in (None, "full", "partial"):
                    logger.warning(f"AppID {appid}: unexpected controller_support value: {new_fields['controller_support']}")
            except Exception as e:
                logger.warning(f"AppID {appid}: controller_support parse error: {e}")

            return GameMetadata(
                appid=appid,
                name=details.get("name", "Unknown"),
                tags=tags,
                developer=developer,
                description=details.get("short_description", ""),  # Backward compat
                fetched_at=fetched_at,
                cache_age_seconds=cache_age,
                **new_fields
            )
        except httpx.HTTPStatusError as e:
            status = e.response.status_code
            error_type = "rate_limit" if status == 429 else "api_error"
            return APIError(
                error_code=status,
                error_type=error_type,
                message=f"Steam Store API error: {status}"
            )
        except Exception as e:
            logger.error(f"Steam Store error: {e}")
            return APIError(
                error_code=502,
                error_type="api_error",
                message=str(e)
            )

    async def get_tag_map(self) -> dict[str, int] | APIError:
        """Fetch Steam's tag registry and return a case-insensitive name→ID mapping.

        Uses Steam Store's populartags endpoint which returns all ~446 tags.
        Cached for 24 hours since tags rarely change.

        Returns:
            Dict mapping lowercase tag names to tag IDs, or APIError on failure
        """
        try:
            data, fetched_at, cache_age = await self.http.get_with_metadata(
                "https://store.steampowered.com/tagdata/populartags/english",
                cache_ttl=86400  # 24 hour cache — tags rarely change
            )
            # Response: [{"tagid": 492, "name": "Indie"}, ...]
            if not isinstance(data, list):
                return APIError(
                    error_code=502,
                    error_type="api_error",
                    message="Unexpected tag registry response format"
                )
            tag_map = {}
            for entry in data:
                name = entry.get("name", "")
                tag_id = entry.get("tagid")
                if name and tag_id is not None:
                    tag_map[name.lower()] = int(tag_id)
            return tag_map
        except Exception as e:
            logger.error(f"Failed to fetch Steam tag registry: {e}")
            return APIError(
                error_code=502,
                error_type="api_error",
                message=f"Failed to fetch Steam tag registry: {e}"
            )

    async def search_by_tag_ids(
        self, tag_ids: list[int], start: int = 0, count: int = 50
    ) -> tuple[int, list[int]] | APIError:
        """Search Steam Store for games matching tag IDs.

        Uses Steam Store's AJAX search endpoint. Supports multi-tag intersection
        (games must have ALL specified tags). Returns total count and AppIDs.

        Args:
            tag_ids: List of Steam tag IDs to search (intersection if multiple)
            start: Pagination offset (default 0)
            count: Number of results per page (default 50, max ~50 per page)

        Returns:
            Tuple of (total_count, appid_list), or APIError on failure
        """
        try:
            tags_param = ",".join(str(tid) for tid in tag_ids)
            data, fetched_at, cache_age = await self.http.get_with_metadata(
                "https://store.steampowered.com/search/results/",
                params={
                    "query": "",
                    "start": str(start),
                    "count": str(count),
                    "tags": tags_param,
                    "sort_by": "Reviews_DESC",
                    "infinite": "1",
                },
                cache_ttl=3600,  # 1 hour cache
                headers={"X-Requested-With": "XMLHttpRequest"},
            )

            total_count = data.get("total_count", 0)
            results_html = data.get("results_html", "")

            # Extract AppIDs from HTML: data-ds-appid="12345"
            appids = [int(aid) for aid in re.findall(r'data-ds-appid="(\d+)"', results_html)]

            return total_count, appids
        except Exception as e:
            logger.error(f"Steam Store search error: {e}")
            return APIError(
                error_code=502,
                error_type="api_error",
                message=f"Steam Store search error: {e}"
            )

    async def get_total_released_count(self, tag_ids: list[int]) -> int | None:
        """Get total number of released games matching tag IDs on Steam Store.

        Uses Released_DESC sort which includes all released games (not just
        review-ranked ones). Single lightweight request (count=1) to get
        total_count only. Used for methodology reporting alongside the
        Reviews_DESC-filtered analysis set.

        Returns:
            Total count of released games, or None on failure
        """
        try:
            tags_param = ",".join(str(tid) for tid in tag_ids)
            data, _, _ = await self.http.get_with_metadata(
                "https://store.steampowered.com/search/results/",
                params={
                    "query": "",
                    "start": "0",
                    "count": "1",
                    "tags": tags_param,
                    "sort_by": "Released_DESC",
                    "infinite": "1",
                },
                cache_ttl=3600,
                headers={"X-Requested-With": "XMLHttpRequest"},
            )
            return data.get("total_count", 0)
        except Exception as e:
            logger.warning(f"Failed to get released count for tags {tag_ids}: {e}")
            return None

    async def search_by_tag_ids_paginated(
        self, tag_ids: list[int], max_results: int = 2000, page_size: int = 50
    ) -> tuple[int, list[int]] | APIError:
        """Search Steam Store with automatic pagination up to max_results."""
        all_appids: list[int] = []
        seen: set[int] = set()
        total_count = 0
        start = 0
        pages_fetched = 0

        while start < max_results:
            result = await self.search_by_tag_ids(tag_ids, start=start, count=page_size)
            if isinstance(result, APIError):
                if not all_appids:
                    return result
                logger.warning(
                    f"Paginated search: page at start={start} failed, "
                    f"returning {len(all_appids)} appids collected so far"
                )
                break

            pages_fetched += 1
            page_total, page_appids = result
            total_count = page_total

            new_appids = [a for a in page_appids if a not in seen]
            if not new_appids:
                break

            for a in new_appids:
                seen.add(a)
                all_appids.append(a)

            if len(all_appids) >= max_results:
                all_appids = all_appids[:max_results]
                break

            start += page_size

            if start >= total_count:
                break

        logger.info(
            f"Paginated search: collected {len(all_appids)} appids "
            f"(total_count={total_count}, pages={pages_fetched}, "
            f"cap={max_results})"
        )
        return total_count, all_appids


class GamalyticClient:
    """Client for Gamalytic API (revenue estimation) with review-based fallback."""

    BASE_URL = "https://api.gamalytic.com"

    def __init__(self, http_client: CachedAPIClient):
        """Initialize Gamalytic client with cached HTTP client.

        Args:
            http_client: CachedAPIClient instance for API requests
        """
        self.http = http_client

    async def get_revenue_estimate(self, appid: int) -> CommercialData | APIError:
        """Backward-compatible alias for get_commercial_data."""
        return await self.get_commercial_data(appid)

    async def get_commercial_data(self, appid: int, detail_level: str = "full") -> CommercialData | APIError:
        """Get commercial data for a Steam game with full field parsing and fallback.

        Primary path: Gamalytic API (no auth required) — parses 40+ fields
        Fallback path 1: Steam Web API player count (when Gamalytic fails)
        Fallback path 2: SteamSpy CCU from appdetails (third tier, stale data)
        Fallback path 3: SteamSpy review-based estimation (reviews * 30-50)
        Triangulation: Compare Gamalytic with SteamSpy owner data when available

        Args:
            appid: Steam AppID to get commercial data for
            detail_level: "full" (default) or "summary" (strips heavy arrays)

        Returns:
            CommercialData with revenue range and extended metadata, or APIError on failure
        """
        # Primary path: Try Gamalytic API first
        gamalytic_headers: dict[str, str] = {}
        if Config.GAMALYTIC_API_KEY:
            gamalytic_headers["api-key"] = Config.GAMALYTIC_API_KEY
        gamalytic_failure_reason: str | None = None
        try:
            data, fetched_at, cache_age = await self.http.get_with_metadata(
                f"{self.BASE_URL}/game/{appid}",
                cache_ttl=21600,  # 6 hours
                headers=gamalytic_headers or None,
            )

            # Gamalytic response: {"steamId": "504230", "name": "Celeste", "price": 19.99, "revenue": 17990584, ...}
            revenue = data.get("revenue", 0)
            price = data.get("price", 0.0)
            name = data.get("name", "")

            # Convert single revenue integer to +/-20% range
            revenue_min = revenue * 0.80
            revenue_max = revenue * 1.20

            # --- Extended field parsing (all defensive: each field in its own try/except) ---

            copies_sold = None
            try:
                raw = data.get("copiesSold")
                if raw is not None:
                    val = int(raw)
                    copies_sold = val if val >= 0 else None
            except Exception:
                pass

            followers = None
            try:
                raw = data.get("followers")
                if raw is not None:
                    val = int(raw)
                    followers = val if val >= 0 else None
            except Exception:
                pass

            accuracy = None
            try:
                raw = data.get("accuracy")
                if raw is not None:
                    val = float(raw)
                    accuracy = val if 0.0 <= val <= 1.0 else None
            except Exception:
                pass

            total_revenue_flag = None
            try:
                raw = data.get("totalRevenue")
                if raw is not None:
                    total_revenue_flag = bool(raw)
            except Exception:
                pass

            review_score = None
            try:
                raw = data.get("reviewScore")
                if raw is not None:
                    review_score = float(raw)
            except Exception:
                pass

            gamalytic_owners = None
            try:
                raw = data.get("owners")
                if raw is not None:
                    val = int(raw)
                    gamalytic_owners = val if val >= 0 else None
            except Exception:
                pass

            gamalytic_players = None
            try:
                raw = data.get("players")
                if raw is not None:
                    val = int(raw)
                    gamalytic_players = val if val >= 0 else None
            except Exception:
                pass

            gamalytic_reviews = None
            try:
                raw = data.get("reviews")
                if raw is not None:
                    val = int(raw)
                    gamalytic_reviews = val if val >= 0 else None
            except Exception:
                pass

            gamalytic_is_early_access = None
            try:
                raw = data.get("isEarlyAccess")
                if raw is not None:
                    gamalytic_is_early_access = bool(raw)
            except Exception:
                pass

            gamalytic_ea_date = None
            try:
                raw = data.get("EAReleaseDate") or data.get("earlyAccessDate")
                if raw is not None:
                    gamalytic_ea_date = _ms_to_iso(raw)
            except Exception:
                pass

            release_date_iso = None
            try:
                raw = data.get("releaseDate")
                if raw is not None:
                    release_date_iso = _ms_to_iso(raw)
            except Exception:
                pass

            # --- History array parsing ---
            history_entries = []
            try:
                raw_history = data.get("history", [])
                for i, entry in enumerate(raw_history):
                    entry_type = "launch" if i == 0 else "daily"
                    ts = _ms_to_iso(entry.get("timeStamp"))
                    # Light validation: reject timestamps before 2003
                    if ts and ts < "2003":
                        ts = None
                    history_entries.append(GamalyticHistoryEntry(
                        entry_type=entry_type,
                        timestamp=ts,
                        reviews=entry.get("reviews"),
                        price=entry.get("price"),
                        score=entry.get("score"),
                        rank=entry.get("rank"),
                        followers=entry.get("followers"),
                        gamalytic_players=entry.get("players"),
                        avg_playtime=entry.get("avgPlaytime"),
                        sales=entry.get("sales"),
                        revenue=entry.get("revenue"),
                    ))
            except Exception:
                history_entries = []

            # --- Country data (cap at top 10 by share_pct) ---
            country_entries = []
            try:
                raw_countries = data.get("countryData", [])
                if isinstance(raw_countries, dict):
                    # Pro API returns {country_code: share_pct, ...}
                    for code, share in raw_countries.items():
                        country_entries.append(CountryDataEntry(
                            country_code=str(code),
                            share_pct=float(share) if share is not None else None,
                        ))
                elif isinstance(raw_countries, list):
                    for c in raw_countries:
                        if isinstance(c, dict):
                            share_raw = c.get("share")
                            country_entries.append(CountryDataEntry(
                                country_code=str(c.get("country", "")),
                                share_pct=float(share_raw) if share_raw is not None else None,
                            ))
                country_entries.sort(key=lambda x: x.share_pct or 0, reverse=True)
                country_entries = country_entries[:10]
            except Exception:
                country_entries = []

            # --- Audience overlap (up to 10 entries) ---
            overlap_entries = []
            try:
                raw_overlap = data.get("audienceOverlap", [])
                if isinstance(raw_overlap, list):
                    for item in raw_overlap[:10]:
                        if isinstance(item, dict):
                            overlap_entries.append(CompetitorEntry(
                                appid=item.get("appId") or item.get("steamId"),
                                name=str(item.get("name", "")),
                                overlap_pct=item.get("link") or item.get("overlap") or item.get("overlapPercent"),
                                revenue=item.get("revenue"),
                                followers=item.get("followers"),
                                score=item.get("score"),
                            ))
            except Exception:
                overlap_entries = []

            # --- Also played (up to 10 entries) ---
            also_played_entries = []
            try:
                raw_also = data.get("alsoPlayed", [])
                if isinstance(raw_also, list):
                    for item in raw_also[:10]:
                        if isinstance(item, dict):
                            also_played_entries.append(CompetitorEntry(
                                appid=item.get("appId") or item.get("steamId"),
                                name=str(item.get("name", "")),
                                overlap_pct=item.get("link") or item.get("overlap") or item.get("overlapPercent"),
                                revenue=item.get("revenue"),
                                followers=item.get("followers"),
                                score=item.get("score"),
                            ))
            except Exception:
                also_played_entries = []

            # --- Estimate details ---
            estimate_entries = []
            try:
                raw_estimates = data.get("estimateDetails", [])
                if isinstance(raw_estimates, dict):
                    # Pro API returns {model_name: estimate_value, ...}
                    for model_name, estimate_val in raw_estimates.items():
                        estimate_entries.append(EstimateModelEntry(
                            model_name=str(model_name),
                            estimate=float(estimate_val) if estimate_val is not None else None,
                        ))
                elif isinstance(raw_estimates, list):
                    for est in raw_estimates:
                        if isinstance(est, dict):
                            estimate_entries.append(EstimateModelEntry(
                                model_name=str(est.get("model", est.get("name", ""))),
                                estimate=est.get("estimate") or est.get("value"),
                            ))
            except Exception:
                estimate_entries = []

            # --- DLC array ---
            dlc_entries = []
            try:
                raw_dlc = data.get("dlc", [])
                if isinstance(raw_dlc, list):
                    for d in raw_dlc:
                        if isinstance(d, dict):
                            dlc_entries.append(DLCEntry(
                                name=str(d.get("name", "")),
                                price=d.get("price"),
                                release_date=_ms_to_iso(d.get("releaseDate")),
                                revenue=d.get("revenue"),
                            ))
            except Exception:
                dlc_entries = []

            # --- Player count: extract from history (first tier) ---
            current_player_count = None
            player_count_source = None
            player_count_note = None
            if history_entries and len(history_entries) > 1:
                latest = history_entries[-1]
                if latest.gamalytic_players is not None:
                    current_player_count = latest.gamalytic_players
                    player_count_source = "gamalytic_history"

            # --- Prominent warning if expected extended fields are absent ---
            expected_extended = ["copiesSold", "history", "countryData"]
            extended_present = any(data.get(f) for f in expected_extended)
            if not extended_present and data.get("revenue"):
                logger.error(
                    "=" * 60 + "\n"
                    "GAMALYTIC EXTENDED FIELDS UNAVAILABLE\n"
                    f"AppID {appid}: expected fields (copiesSold, history, countryData) absent.\n"
                    "API response may have changed structure. Core revenue data returned.\n"
                    "Fix needed: verify Gamalytic /game/{appid} response shape.\n"
                    + "=" * 60
                )

            # --- detail_level filtering ---
            if detail_level == "summary":
                history_entries = []
                overlap_entries = []
                also_played_entries = []
                dlc_entries = []
                estimate_entries = []
                country_entries = country_entries[:3]

            # Create CommercialData with all fields
            commercial_data = CommercialData(
                appid=appid,
                name=name,
                price=price if price > 0 else None,
                revenue_min=revenue_min,
                revenue_max=revenue_max,
                currency="USD",
                confidence="high",
                source="gamalytic",
                fetched_at=fetched_at,
                cache_age_seconds=cache_age,
                # Extended fields
                copies_sold=copies_sold,
                followers=followers,
                accuracy=accuracy,
                total_revenue=total_revenue_flag,
                review_score=review_score,
                gamalytic_owners=gamalytic_owners,
                gamalytic_players=gamalytic_players,
                gamalytic_reviews=gamalytic_reviews,
                gamalytic_is_early_access=gamalytic_is_early_access,
                gamalytic_ea_date=gamalytic_ea_date,
                release_date=release_date_iso,
                history=history_entries,
                country_data=country_entries,
                audience_overlap=overlap_entries,
                also_played=also_played_entries,
                estimate_details=estimate_entries,
                gamalytic_dlc=dlc_entries,
                current_player_count=current_player_count,
                player_count_source=player_count_source,
                player_count_note=player_count_note,
                # Phase 10: Explicit data quality (success path)
                data_quality="gamalytic",
            )

            # Triangulation: Compare with SteamSpy owner data
            try:
                await self._add_triangulation_warning(commercial_data, appid, revenue)
            except Exception as e:
                logger.error(f"Triangulation check failed for AppID {appid}: {e}")
                # Triangulation is informational only - continue without warning

            return commercial_data

        except httpx.HTTPStatusError as e:
            status = e.response.status_code
            gamalytic_failure_reason = f"Gamalytic returned HTTP {status}"
            if status == 429:
                gamalytic_failure_reason += " (rate limit exceeded)"
            logger.warning(f"Gamalytic API error {status} for AppID {appid}, trying Steam Web API fallback")
            # Fall through to fallback
        except Exception as e:
            gamalytic_failure_reason = f"Gamalytic unavailable: {type(e).__name__}"
            logger.warning(f"Gamalytic API error for AppID {appid}: {e}, trying Steam Web API fallback")
            # Fall through to fallback

        # Fallback path: Try Steam Web API for player count, then review-based estimate
        fallback_result = await self._get_review_based_estimate(appid)
        # Phase 10: Inject warning fields before player count enhancement
        if isinstance(fallback_result, CommercialData) and gamalytic_failure_reason:
            fallback_result.api_warnings = [
                f"{gamalytic_failure_reason}. Falling back to review-based estimate (+-300-500% accuracy)."
            ]
            fallback_result.data_quality = "review_estimate"
        if isinstance(fallback_result, CommercialData):
            # Try to get player count from Steam Web API (second-tier fallback)
            steam_ccu = await self._get_steam_player_count(appid)
            if steam_ccu is not None:
                # Steam Web API succeeded — override any SteamSpy CCU from _get_review_based_estimate
                fallback_result.current_player_count = steam_ccu
                fallback_result.player_count_source = "steam_web_api"
                fallback_result.player_count_note = (
                    "Gamalytic unavailable. Player count from Steam Web API "
                    "(real-time snapshot, may need rerun for updated data)."
                )
            # If Steam Web API also failed, _get_review_based_estimate already set SteamSpy CCU as third fallback
        return fallback_result

    async def _get_review_based_estimate(self, appid: int) -> CommercialData | APIError:
        """Fallback revenue estimation using SteamSpy review count.

        Args:
            appid: Steam AppID to estimate revenue for

        Returns:
            CommercialData with review-based estimate, or APIError on failure
        """
        try:
            data, fetched_at, cache_age = await self.http.get_with_metadata(
                "https://steamspy.com/api.php",
                params={"request": "appdetails", "appid": str(appid)},
                cache_ttl=3600  # 1 hour
            )

            # SteamSpy response: {"positive": 82000, "negative": 304, "price": "1999", ...}
            positive = data.get("positive", 0)
            negative = data.get("negative", 0)
            price_str = data.get("price", "0")

            total_reviews = positive + negative
            if total_reviews == 0:
                return APIError(
                    error_code=404,
                    error_type="not_found",
                    message=f"Cannot estimate revenue: no reviews for AppID {appid}"
                )

            # Revenue estimation: reviews * 30-50 (conservative to optimistic)
            revenue_min = total_reviews * 30
            revenue_max = total_reviews * 50

            # Convert SteamSpy price from cents to dollars
            price_dollars = int(price_str) / 100.0 if price_str and price_str != "0" else None

            # Third-tier fallback: extract CCU from SteamSpy appdetails response
            # (this data is already fetched — just not extracted yet)
            current_player_count = None
            player_count_source = None
            player_count_note = None
            steamspy_ccu = data.get("ccu", 0)
            if steamspy_ccu and int(steamspy_ccu) > 0:
                current_player_count = int(steamspy_ccu)
                player_count_source = "steamspy"
                player_count_note = (
                    "Gamalytic and Steam Web API unavailable. "
                    "Player count from SteamSpy (may be stale — SteamSpy updates infrequently)."
                )

            return CommercialData(
                appid=appid,
                name="",  # SteamSpy doesn't provide name in appdetails
                price=price_dollars,
                revenue_min=revenue_min,
                revenue_max=revenue_max,
                currency="USD",
                confidence="low",
                source="review_estimate",
                method="review_multiplier",
                fetched_at=fetched_at,
                cache_age_seconds=cache_age,
                current_player_count=current_player_count,
                player_count_source=player_count_source,
                player_count_note=player_count_note,
            )

        except httpx.HTTPStatusError as e:
            status = e.response.status_code
            error_type = "rate_limit" if status == 429 else "api_error"
            return APIError(
                error_code=status,
                error_type=error_type,
                message=f"SteamSpy API error: {status}"
            )
        except Exception as e:
            logger.error(f"SteamSpy fallback error for AppID {appid}: {e}")
            return APIError(
                error_code=502,
                error_type="api_error",
                message=f"Both Gamalytic and SteamSpy unavailable for AppID {appid}"
            )

    async def _add_triangulation_warning(
        self, commercial_data: CommercialData, appid: int, gamalytic_revenue: float
    ) -> None:
        """Add triangulation warning if SteamSpy owner data diverges from Gamalytic.

        Modifies commercial_data in place to add warning fields if divergence > 20%.

        Args:
            commercial_data: CommercialData object to potentially modify
            appid: Steam AppID
            gamalytic_revenue: Original Gamalytic revenue value
        """
        # Fetch SteamSpy data for owner count
        data, _, _ = await self.http.get_with_metadata(
            "https://steamspy.com/api.php",
            params={"request": "appdetails", "appid": str(appid)},
            cache_ttl=3600
        )

        # Parse owners string: "1,000,000 .. 2,000,000" -> low=1000000, high=2000000
        owners_str = data.get("owners", "0 .. 0")
        owners_str = owners_str.replace(",", "")
        parts = owners_str.split(" .. ")
        if len(parts) != 2:
            return  # Can't parse, skip triangulation

        low = int(parts[0])
        high = int(parts[1])
        owners_midpoint = (low + high) / 2

        # Get price from SteamSpy (in cents)
        price_str = data.get("price", "0")
        price_dollars = int(price_str) / 100.0 if price_str and price_str != "0" else 0

        # Skip triangulation for free games
        if price_dollars == 0:
            return

        # Calculate SteamSpy-implied revenue (owners * price * 70% Steam cut)
        steamspy_implied_revenue = owners_midpoint * price_dollars * 0.70

        # Calculate divergence from Gamalytic
        gamalytic_midpoint = gamalytic_revenue  # Single revenue value from API
        divergence = abs(steamspy_implied_revenue - gamalytic_midpoint) / max(gamalytic_midpoint, 1)

        # Add warning if divergence > 20%
        if divergence > 0.20:
            commercial_data.triangulation_warning = (
                f"SteamSpy owner-based estimate (${steamspy_implied_revenue:,.0f}) "
                f"diverges {divergence:.0%} from Gamalytic (${gamalytic_midpoint:,.0f}). "
                f"Cross-reference recommended."
            )
            commercial_data.steamspy_implied_revenue = steamspy_implied_revenue
            commercial_data.gamalytic_revenue = gamalytic_midpoint

    async def _get_steam_player_count(self, appid: int) -> int | None:
        """Fallback: get current player count from Steam Web API.

        Uses GetNumberOfCurrentPlayers endpoint (public, no API key required).
        Cache TTL: 300 seconds (5 minutes) for real-time data.

        Args:
            appid: Steam AppID

        Returns:
            Current player count or None if unavailable
        """
        try:
            data, _, _ = await self.http.get_with_metadata(
                "https://api.steampowered.com/ISteamUserStats/GetNumberOfCurrentPlayers/v1/",
                params={"appid": str(appid)},
                cache_ttl=300  # 5-minute TTL for real-time data
            )
            response = data.get("response", {})
            result = response.get("result")
            count = response.get("player_count")
            # result=1 means success per Steam API docs
            if result == 1 and count is not None:
                return int(count)
            return None
        except Exception as e:
            logger.warning(f"Steam Web API player count unavailable for AppID {appid}: {e}")
            return None

    async def get_commercial_batch(
        self, appids: list[int], detail_level: str = "full"
    ) -> list[CommercialData | APIError]:
        """Fetch commercial data for multiple AppIDs concurrently.

        Rate limiting handled by HostRateLimiter (5 req/s for gamalytic.com).
        Gather is wrapped in a scaled timeout to prevent indefinite hangs.

        Args:
            appids: List of Steam AppIDs
            detail_level: "full" or "summary"

        Returns:
            List of CommercialData or APIError for each AppID (same order as input)
        """
        tasks = [self.get_commercial_data(appid, detail_level=detail_level) for appid in appids]
        gather_result = await gather_with_timeout(
            tasks,
            timeout=compute_gather_timeout(len(appids), GAMALYTIC_RATE),
            label=f"commercial_batch({len(appids)})",
            return_exceptions=True,
        )
        return [
            r if isinstance(r, (CommercialData, APIError))
            else APIError(error_code=502, error_type="api_error", message=str(r))
            for r in gather_result.results
        ]

    async def list_games_by_tag(
        self,
        tag: str,
        fields: str = "steamId,name,revenue,copiesSold,reviewScore,price,releaseDate,genres",
        limit_per_page: int = 200,
        max_pages: int | None = None,
    ) -> tuple[list[dict], dict] | APIError:
        """Fetch games by tag from Gamalytic bulk list endpoint with pagination.

        Uses GET /steam-games/list?tags={tag}&limit={limit}&page={N}&fields={fields}&sort=revenue&sort_mode=desc
        Paginates through ALL pages (or up to max_pages) at the HostRateLimiter rate (5 req/s).

        Args:
            tag: Steam tag name (e.g., "Roguelike", "Roguelike Deckbuilder"). Multi-word OK.
                Hyphenated forms (e.g., "Rogue-like") are NOT recognized by Gamalytic.
            fields: Comma-separated field names for response. Default includes all useful fields.
            limit_per_page: Games per page (max 200). Default 200 for efficiency.
            max_pages: Optional cap on pages fetched. None = fetch all pages.

        Returns:
            Tuple of (games_list, pagination_meta) or APIError on total failure.
            games_list: List of game dicts with requested fields (steamId as int).
            pagination_meta: Dict with keys:
                - is_partial (bool): True if pagination was interrupted by error.
                - pages_fetched (int): Number of pages successfully fetched.
                - total_pages (int | None): Total pages reported by API.
                - total_expected (int | None): Total games reported by API.
                - error (str | None): Error message if partial, else None.
        """
        all_games: list[dict] = []
        page = 1
        total_pages_reported: int | None = None
        total_games_reported: int | None = None

        def _make_meta(*, is_partial: bool = False, error: str | None = None) -> dict:
            return {
                "is_partial": is_partial,
                "pages_fetched": page - 1 if is_partial else page,
                "total_pages": total_pages_reported,
                "total_expected": total_games_reported,
                "error": error,
            }

        try:
            while True:
                page_params: dict[str, str] = {
                    "tags": tag,
                    "limit": str(limit_per_page),
                    "page": str(page),
                    "fields": fields,
                    "sort": "revenue",
                    "sort_mode": "desc",
                }
                gamalytic_headers: dict[str, str] = {}
                if Config.GAMALYTIC_API_KEY:
                    gamalytic_headers["api-key"] = Config.GAMALYTIC_API_KEY
                data, _, _ = await self.http.get_with_metadata(
                    f"{self.BASE_URL}/steam-games/list",
                    params=page_params,
                    cache_ttl=3600,  # 1 hour cache — bulk data is stable
                    headers=gamalytic_headers or None,
                )

                # Parse response: {"pages": N, "total": M, "result": [...], "next": {...}}
                results = data.get("result", [])
                total_pages_reported = data.get("pages", 0)
                total_games_reported = data.get("total", 0)

                if page == 1:
                    logger.info(
                        f"Gamalytic list_games_by_tag '{tag}': {total_games_reported} total games, "
                        f"{total_pages_reported} pages at {limit_per_page}/page"
                    )

                if not results:
                    break

                for game in results:
                    # Normalize steamId to int (API returns it as int already, but be safe)
                    steam_id = game.get("steamId")
                    if steam_id is not None:
                        game["steamId"] = int(steam_id)
                    all_games.append(game)

                # Check pagination bounds
                if page >= total_pages_reported:
                    break
                if max_pages is not None and page >= max_pages:
                    logger.info(f"Gamalytic list: reached max_pages={max_pages}, stopping at {len(all_games)} games")
                    break

                page += 1

        except httpx.HTTPStatusError as e:
            status = e.response.status_code
            error_msg = f"Gamalytic list API error {status} for tag '{tag}' on page {page}"
            logger.warning(error_msg)
            if all_games:
                logger.warning(
                    f"Returning {len(all_games)} PARTIAL results from {page - 1} pages "
                    f"(expected {total_games_reported} from {total_pages_reported} pages)"
                )
                return all_games, _make_meta(is_partial=True, error=error_msg)
            return APIError(
                error_code=status,
                error_type="rate_limit" if status == 429 else "api_error",
                message=error_msg,
            )
        except Exception as e:
            error_msg = f"Gamalytic list_games_by_tag error for tag '{tag}' on page {page}: {e}"
            logger.error(error_msg)
            if all_games:
                logger.warning(
                    f"Returning {len(all_games)} PARTIAL results from {page - 1} pages "
                    f"(expected {total_games_reported} from {total_pages_reported} pages)"
                )
                return all_games, _make_meta(is_partial=True, error=error_msg)
            return APIError(
                error_code=502,
                error_type="api_error",
                message=f"Gamalytic list endpoint failed for tag '{tag}': {e}",
            )

        logger.info(f"Gamalytic list_games_by_tag '{tag}': fetched {len(all_games)} games across {page} pages")
        return all_games, _make_meta()


DIMENSION_KEYWORDS: dict[str, list[str]] = {
    "art_visuals": [
        "beautiful", "gorgeous", "stunning", "art style", "artstyle",
        "art direction", "visuals", "aesthetic", "graphics", "artwork",
        "hand-drawn", "hand drawn", "animation", "pixel art", "colorful",
        "colours", "colors",
    ],
    "music_sound": [
        "soundtrack", "music", "sound design", "audio", "voice acting",
        "voice actor", "ost", "sound effect", "sfx", "score",
        "composer", "singing",
    ],
    "story_narrative": [
        "story", "narrative", "plot", "writing", "characters", "dialogue",
        "lore", "storyline", "storytelling", "cutscene", "ending",
        "protagonist", "villain",
    ],
    "gameplay_mechanics": [
        "gameplay", "mechanics", "combat", "controls", "satisfying",
        "game loop", "progression", "build", "crafting", "movement",
        "abilities", "skills",
    ],
    "difficulty": [
        "difficult", "challenging", "hard", "punishing", "unfair",
        "easy", "casual", "frustrating", "rage", "souls-like",
        "hardcore", "forgiving",
    ],
    "ui_ux": [
        "ui", "ux", "interface", "menu", "inventory", "hud",
        "controller", "keybind", "accessibility", "font",
        "readable", "navigate", "tooltip",
    ],
    "performance": [
        "fps", "optimization", "optimized", "lag", "crash", "bug",
        "stuttering", "frame rate", "framerate", "loading",
        "performance", "stable", "unstable",
    ],
    "content_value": [
        "hours of", "content", "replayability", "replay value",
        "worth", "value", "bang for", "money", "dlc",
        "update", "endgame", "end game", "longevity",
    ],
}

_DIMENSION_PATTERNS: dict[str, list[tuple[str, re.Pattern]]] = {}
for _dim, _kws in DIMENSION_KEYWORDS.items():
    _DIMENSION_PATTERNS[_dim] = [
        (kw, re.compile(r'\b' + re.escape(kw) + r'\b', re.IGNORECASE))
        for kw in _kws
    ]


def _compute_dimension_profile(reviews: list) -> "DimensionProfile":
    """Scan review texts for keyword mentions across 8 dimensions.

    Args:
        reviews: list of ReviewItem objects (must have .text attribute)

    Returns:
        DimensionProfile with mention rates and top keywords per dimension.
    """
    from .schemas import DimensionProfile, DimensionKeywordStats

    n = len(reviews)
    if n == 0:
        return DimensionProfile(reviews_analyzed=0)

    dim_results = {}
    for dim, patterns in _DIMENSION_PATTERNS.items():
        keyword_counts: dict[str, int] = {}
        reviews_with_match = 0

        for review in reviews:
            text = review.text or ""
            found_any = False
            for kw, pattern in patterns:
                if pattern.search(text):
                    keyword_counts[kw] = keyword_counts.get(kw, 0) + 1
                    found_any = True
            if found_any:
                reviews_with_match += 1

        top_kws = dict(sorted(keyword_counts.items(), key=lambda x: -x[1])[:5])

        dim_results[dim] = DimensionKeywordStats(
            mention_rate=round(reviews_with_match / n, 3),
            mention_count=reviews_with_match,
            top_keywords=top_kws,
        )

    return DimensionProfile(reviews_analyzed=n, **dim_results)


class SteamReviewClient:
    """Client for Steam Review API with cursor pagination and stats computation."""

    REVIEW_URL = "https://store.steampowered.com/appreviews/{appid}"
    GAMALYTIC_URL = "https://api.gamalytic.com/game/{appid}"
    APPDETAILS_URL = "https://store.steampowered.com/api/appdetails"

    # Histogram bucket definitions: (label, min_hours, max_hours_exclusive_or_None)
    HISTOGRAM_BUCKETS = [
        ("0_1hr", 0, 1),
        ("1_2hr", 1, 2),
        ("2_5hr", 2, 5),
        ("5_10hr", 5, 10),
        ("10_20hr", 10, 20),
        ("20_50hr", 20, 50),
        ("50hr_plus", 50, None),
    ]

    # Steam review score descriptions (score 1-9)
    SCORE_DESCRIPTIONS = {
        1: "Overwhelmingly Negative",
        2: "Very Negative",
        3: "Negative",
        4: "Mostly Negative",
        5: "Mixed",
        6: "Mostly Positive",
        7: "Positive",
        8: "Very Positive",
        9: "Overwhelmingly Positive",
    }

    def __init__(self, http_client: CachedAPIClient):
        self.http = http_client

    # ---------------------------------------------------------------------------
    # Low-level page fetching
    # ---------------------------------------------------------------------------

    async def _fetch_review_page(
        self, appid: int, cursor: str, params: dict, *, cache_ttl: int = 0
    ) -> tuple[dict, str]:
        """Fetch a single page of reviews from the Steam Review API.

        Args:
            appid: Steam AppID
            cursor: Pagination cursor ("*" for first page)
            params: Base filter params to include
            cache_ttl: Cache TTL in seconds (default 0 = no cache for cursor pagination)

        Returns:
            Tuple of (response_data_dict, new_cursor_string)
        """
        full_params = {
            "json": "1",
            "num_per_page": "100",
            "cursor": cursor,
            **params,
        }
        data, _, _ = await self.http.get_with_metadata(
            self.REVIEW_URL.format(appid=appid),
            params=full_params,
            cache_ttl=cache_ttl,
        )
        new_cursor = data.get("cursor", "")
        return (data, new_cursor)

    async def _fetch_review_text(
        self,
        appid: int,
        limit: int,
        language: str,
        review_type: str,
        purchase_type: str,
        filter_offtopic: bool,
        day_range: int | None,
        platform: str,
        sort: str,
        start_cursor: str = "*",
    ) -> tuple[list[dict], dict | None, str, int]:
        """Paginate the review text sequence (helpful-sort or recent-sort).

        Args:
            appid: Steam AppID
            limit: Maximum number of reviews to return
            language: Language filter (e.g. "english")
            review_type: "all", "positive", or "negative"
            purchase_type: "all", "steam", or "non_steam_purchase"
            filter_offtopic: Whether to filter offtopic activity
            day_range: Day range for recent reviews (Steam API param, None to omit)
            platform: Platform filter ("all", "win", "mac", "linux") — local filter only
            sort: "helpful" or "recent"

        Returns:
            Tuple of (raw_reviews_list, query_summary_or_None, last_cursor, pages_fetched)
        """
        base_params: dict = {
            "filter": "all" if sort != "recent" else "recent",
            "language": language,
            "review_type": review_type,
            "purchase_type": purchase_type,
            "filter_offtopic_activity": "1" if filter_offtopic else "0",
        }
        if day_range is not None:
            base_params["day_range"] = str(day_range)

        cursor = start_cursor
        seen_ids: set[str] = set()
        all_reviews: list[dict] = []
        pages_fetched = 0
        max_pages = 50
        query_summary: dict | None = None
        partial = False

        while pages_fetched < max_pages:
            try:
                data, cursor_new = await self._fetch_review_page(appid, cursor, base_params)
            except Exception as e:
                logger.warning(
                    f"Review text pagination error for AppID {appid} at page {pages_fetched}: {e}"
                )
                partial = True
                break

            # Capture query_summary only on first page (cursor="*")
            if pages_fetched == 0:
                query_summary = data.get("query_summary", {})

            page_reviews = data.get("reviews", [])

            # Empty page signals exhaustion
            if not page_reviews:
                break

            # Unchanged cursor signals exhaustion
            if cursor_new == cursor and pages_fetched > 0:
                break

            # Dedup by recommendationid
            for review in page_reviews:
                rid = str(review.get("recommendationid", ""))
                if rid and rid not in seen_ids:
                    seen_ids.add(rid)
                    all_reviews.append(review)

            cursor = cursor_new
            pages_fetched += 1

            if len(all_reviews) >= limit:
                break

            await asyncio.sleep(0.1)  # 100ms inter-page delay

        return (all_reviews[:limit], query_summary, cursor, pages_fetched)

    # ---------------------------------------------------------------------------
    # Review parsing and aggregation
    # ---------------------------------------------------------------------------

    def _parse_review(self, raw: dict) -> ReviewItem:
        """Map a raw Steam review API dict to a ReviewItem schema object.

        Args:
            raw: Raw review dict from Steam API

        Returns:
            ReviewItem with all fields populated
        """
        author = raw.get("author", {})
        raw_text = raw.get("review", "")
        stripped = _strip_bbcode(raw_text)
        text, truncated, full_length = _truncate_text(stripped, max_chars=1000)

        # Convert playtime from minutes to hours
        playtime_at_review = author.get("playtime_at_review", 0) / 60
        playtime_total = author.get("playtime_forever", 0) / 60

        dev_response = raw.get("developer_response")

        return ReviewItem(
            review_id=str(raw.get("recommendationid", "")),
            text=text,
            truncated=truncated,
            full_length=full_length,
            voted_up=raw.get("voted_up", False),
            votes_up=raw.get("votes_up", 0),
            votes_funny=raw.get("votes_funny", 0),
            playtime_at_review=playtime_at_review,
            playtime_total=playtime_total,
            timestamp_created=raw.get("timestamp_created", 0),
            timestamp_updated=raw.get("timestamp_updated", 0),
            written_during_early_access=raw.get("written_during_early_access", False),
            received_for_free=raw.get("received_for_free", False),
            language=raw.get("language", "english"),
            author_steamid=author.get("steamid", ""),
            author_num_games_owned=author.get("num_games_owned", 0),
            author_num_reviews=author.get("num_reviews", 0),
            developer_response=dev_response if dev_response else None,
        )

    def _build_aggregates(
        self, query_summary: dict, reviews: list[ReviewItem]
    ) -> ReviewAggregates:
        """Build ReviewAggregates from query_summary (covers ALL reviews, not just fetched).

        Args:
            query_summary: query_summary dict from Steam API first page
            reviews: Fetched ReviewItem list (used for weighted ratio computation)

        Returns:
            ReviewAggregates with all fields populated
        """
        total_positive = query_summary.get("total_positive", 0)
        total_negative = query_summary.get("total_negative", 0)
        total_reviews = query_summary.get("total_reviews", 0)
        review_score = query_summary.get("review_score", 0)
        review_score_desc = self.SCORE_DESCRIPTIONS.get(review_score, "")
        positive_ratio = (
            (total_positive / total_reviews * 100) if total_reviews > 0 else None
        )

        # Helpfulness-weighted positive ratio from fetched reviews
        total_votes_up_positive = sum(
            r.votes_up for r in reviews if r.voted_up
        )
        total_votes_up_all = sum(r.votes_up for r in reviews)
        weighted_positive_ratio = (
            (total_votes_up_positive / total_votes_up_all * 100)
            if total_votes_up_all > 0
            else None
        )

        return ReviewAggregates(
            total_positive=total_positive,
            total_negative=total_negative,
            total_reviews=total_reviews,
            review_score=review_score,
            review_score_desc=review_score_desc,
            positive_ratio=positive_ratio,
            weighted_positive_ratio=weighted_positive_ratio,
        )

    # ---------------------------------------------------------------------------
    # Stats scan
    # ---------------------------------------------------------------------------

    async def _fetch_stats_scan(
        self, appid: int, language: str, max_reviews: int,
        filter_mode: str = "recent",
    ) -> list[dict]:
        """Fetch reviews for stats computation (histogram, sentiment periods).

        Uses review_type=all and purchase_type=all regardless of user filters.
        Only the language filter carries over.

        Args:
            appid: Steam AppID
            language: Language filter
            max_reviews: Maximum reviews to collect
            filter_mode: Steam API filter — "recent" for newest reviews,
                "all" for helpfulness-sorted (time-diverse coverage)

        Returns:
            List of raw review dicts (not parsed)
        """
        base_params: dict = {
            "filter": filter_mode,
            "language": language,
            "review_type": "all",
            "purchase_type": "all",
            "filter_offtopic_activity": "1",
        }

        cursor = "*"
        seen_ids: set[str] = set()
        all_reviews: list[dict] = []
        pages_fetched = 0
        max_pages = 50

        while pages_fetched < max_pages:
            try:
                data, cursor_new = await self._fetch_review_page(appid, cursor, base_params)
            except Exception as e:
                logger.warning(
                    f"Stats scan pagination error for AppID {appid} at page {pages_fetched}: {e}"
                )
                break

            page_reviews = data.get("reviews", [])

            if not page_reviews:
                break

            if cursor_new == cursor and pages_fetched > 0:
                break

            # Dedup by recommendationid
            for review in page_reviews:
                rid = str(review.get("recommendationid", ""))
                if rid and rid not in seen_ids:
                    seen_ids.add(rid)
                    all_reviews.append(review)

            cursor = cursor_new
            pages_fetched += 1

            if len(all_reviews) >= max_reviews:
                break

            await asyncio.sleep(0.1)  # 100ms inter-page delay

        return all_reviews[:max_reviews]

    # ---------------------------------------------------------------------------
    # EA date resolution
    # ---------------------------------------------------------------------------

    async def _resolve_ea_dates(self, appid: int) -> EADateInfo:
        """Resolve Early Access date info via three-tier fallback.

        Tier 1: Gamalytic API (most detailed EA data)
        Tier 2: Steam appdetails API (release date + genre ID 70 check)
        Tier 3: Review-derived fallback (date_source="review_derived")

        Args:
            appid: Steam AppID

        Returns:
            EADateInfo with best-available date data
        """
        # Tier 1 — Gamalytic
        gamalytic_headers: dict[str, str] = {}
        if Config.GAMALYTIC_API_KEY:
            gamalytic_headers["api-key"] = Config.GAMALYTIC_API_KEY
        try:
            data, _, _ = await self.http.get_with_metadata(
                self.GAMALYTIC_URL.format(appid=appid),
                cache_ttl=21600,  # 6 hours — shares cache with fetch_commercial
                headers=gamalytic_headers or None,
            )
            ea_date = _ms_to_iso(data.get("EAReleaseDate"))
            exit_date = _ms_to_iso(data.get("earlyAccessExitDate"))
            first_release = _ms_to_iso(data.get("firstReleaseDate"))
            release_date = _ms_to_iso(data.get("releaseDate"))
            currently_ea = data.get("earlyAccess", False)

            if ea_date or currently_ea:
                return EADateInfo(
                    ea_release_date=ea_date or first_release,
                    full_release_date=exit_date or release_date,
                    currently_ea=currently_ea,
                    date_source="gamalytic",
                )
            # Non-EA game: both dates same
            return EADateInfo(
                ea_release_date=release_date or first_release,
                full_release_date=release_date or first_release,
                currently_ea=False,
                date_source="gamalytic",
            )
        except Exception:
            pass  # Fall through to Tier 2

        # Tier 2 — Steam appdetails
        try:
            data, _, _ = await self.http.get_with_metadata(
                self.APPDETAILS_URL,
                params={"appids": str(appid)},
                cache_ttl=86400,  # 24 hours
            )
            app_data = data.get(str(appid), {}).get("data", {})
            if app_data:
                release_info = app_data.get("release_date", {})
                release_str = release_info.get("date", "")
                release_iso = None
                if release_str:
                    for fmt in ["%b %d, %Y", "%d %b, %Y"]:
                        try:
                            dt = datetime.strptime(release_str, fmt)
                            release_iso = dt.strftime("%Y-%m-%d")
                            break
                        except ValueError:
                            continue
                # Genre ID 70 = Early Access
                genres = app_data.get("genres", [])
                is_ea = any(str(g.get("id")) == "70" for g in genres)
                return EADateInfo(
                    ea_release_date=release_iso,
                    full_release_date=release_iso,
                    currently_ea=is_ea,
                    date_source="steam_api",
                )
        except Exception:
            pass  # Fall through to Tier 3

        # Tier 3 — Review-derived fallback
        return EADateInfo(
            ea_release_date=None,
            full_release_date=None,
            currently_ea=False,
            date_source="review_derived",
        )

    # ---------------------------------------------------------------------------
    # Histogram computation
    # ---------------------------------------------------------------------------

    def _compute_histogram(self, raw_reviews: list[dict]) -> list[PlaytimeBucket]:
        """Compute playtime histogram from stats scan raw reviews.

        Args:
            raw_reviews: List of raw review dicts from stats scan

        Returns:
            List of PlaytimeBucket objects covering 6 time ranges
        """
        # Initialize bucket counters
        buckets: list[dict] = [
            {"label": label, "count": 0, "positive_count": 0, "negative_count": 0}
            for label, _min, _max in self.HISTOGRAM_BUCKETS
        ]

        for review in raw_reviews:
            author = review.get("author", {})
            pt_minutes = author.get("playtime_at_review", 0)
            pt_hours = pt_minutes / 60
            voted_up = review.get("voted_up", False)

            for i, (label, min_h, max_h) in enumerate(self.HISTOGRAM_BUCKETS):
                if max_h is None:
                    in_bucket = pt_hours >= min_h
                else:
                    in_bucket = min_h <= pt_hours < max_h
                if in_bucket:
                    buckets[i]["count"] += 1
                    if voted_up:
                        buckets[i]["positive_count"] += 1
                    else:
                        buckets[i]["negative_count"] += 1
                    break  # Each review goes in exactly one bucket

        return [
            PlaytimeBucket(
                label=b["label"],
                count=b["count"],
                positive_count=b["positive_count"],
                negative_count=b["negative_count"],
            )
            for b in buckets
        ]

    # ---------------------------------------------------------------------------
    # Sentiment period computation
    # ---------------------------------------------------------------------------

    def _compute_sentiment_periods(
        self, raw_reviews: list[dict], ea_dates: EADateInfo
    ) -> tuple[list[SentimentPeriod], list[SentimentPeriod], list[list]]:
        """Compute time-period sentiment metrics from raw stats scan reviews.

        Args:
            raw_reviews: Raw review dicts from stats scan (recent-sort order)
            ea_dates: EA date info for anchor determination

        Returns:
            Tuple of (fixed_and_rolling_periods, yearly_periods, ratio_timeline)
        """
        now_ts = int(datetime.now(tz=timezone.utc).timestamp())

        # Determine anchor timestamp from EA dates
        anchor_ts: int | None = None
        for date_str in (ea_dates.ea_release_date, ea_dates.full_release_date):
            if date_str:
                try:
                    dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                    anchor_ts = int(dt.timestamp())
                    break
                except ValueError:
                    continue

        # Fallback to earliest review timestamp
        if anchor_ts is None and raw_reviews:
            timestamps = [r.get("timestamp_created", 0) for r in raw_reviews if r.get("timestamp_created")]
            if timestamps:
                anchor_ts = min(timestamps)

        # Period definitions: (name, start_ts, end_ts, days_in_period)
        DAY = 86400
        period_defs: list[tuple[str, int | None, int | None, float]] = []

        if anchor_ts is not None:
            period_defs.extend([
                ("first_30d", anchor_ts, anchor_ts + 30 * DAY, 30.0),
                ("first_90d", anchor_ts, anchor_ts + 90 * DAY, 90.0),
                ("first_365d", anchor_ts, anchor_ts + 365 * DAY, 365.0),
                ("365d_plus", anchor_ts + 365 * DAY, None, None),  # open-ended
            ])
        period_defs.extend([
            ("last_30d", now_ts - 30 * DAY, now_ts, 30.0),
            ("last_90d", now_ts - 90 * DAY, now_ts, 90.0),
        ])

        def _build_sentiment_period(period_name: str, reviews_in: list[dict], days: float | None) -> SentimentPeriod:
            pos_count = sum(1 for r in reviews_in if r.get("voted_up", False))
            neg_count = len(reviews_in) - pos_count
            total = len(reviews_in)
            positive_ratio = (pos_count / total * 100) if total > 0 else None

            # Helpfulness-weighted ratio
            votes_up_pos = sum(r.get("votes_up", 0) for r in reviews_in if r.get("voted_up", False))
            votes_up_all = sum(r.get("votes_up", 0) for r in reviews_in)
            weighted_ratio = (votes_up_pos / votes_up_all * 100) if votes_up_all > 0 else None

            # Avg playtime at review (hours)
            playtimes = [r.get("author", {}).get("playtime_at_review", 0) / 60 for r in reviews_in]
            avg_playtime = (sum(playtimes) / len(playtimes)) if playtimes else None

            # Avg helpfulness
            votes_up_list = [r.get("votes_up", 0) for r in reviews_in]
            avg_helpfulness = (sum(votes_up_list) / len(votes_up_list)) if votes_up_list else None

            # Avg reviews per day
            if days is not None and days > 0:
                avg_reviews_per_day = total / days
            elif reviews_in:
                # Compute actual span for open-ended periods
                timestamps = [r.get("timestamp_created", 0) for r in reviews_in if r.get("timestamp_created")]
                if len(timestamps) >= 2:
                    span_days = (max(timestamps) - min(timestamps)) / DAY
                    avg_reviews_per_day = total / span_days if span_days > 0 else None
                else:
                    avg_reviews_per_day = None
            else:
                avg_reviews_per_day = None

            return SentimentPeriod(
                period=period_name,
                positive_count=pos_count,
                negative_count=neg_count,
                total_count=total,
                positive_ratio=positive_ratio,
                weighted_ratio=weighted_ratio,
                avg_playtime_at_review=avg_playtime,
                avg_helpfulness=avg_helpfulness,
                avg_reviews_per_day=avg_reviews_per_day,
                review_count=total,
            )

        # Build fixed + rolling periods
        fixed_rolling: list[SentimentPeriod] = []
        for period_name, start_ts, end_ts, days in period_defs:
            reviews_in = []
            for r in raw_reviews:
                ts = r.get("timestamp_created", 0)
                if start_ts is not None and ts < start_ts:
                    continue
                if end_ts is not None and ts >= end_ts:
                    continue
                reviews_in.append(r)
            fixed_rolling.append(_build_sentiment_period(period_name, reviews_in, days))

        # Yearly breakdown — only if reviews span 2+ calendar years
        yearly: list[SentimentPeriod] = []
        if raw_reviews:
            by_year: dict[int, list[dict]] = {}
            for r in raw_reviews:
                ts = r.get("timestamp_created", 0)
                if ts:
                    year = datetime.fromtimestamp(ts, tz=timezone.utc).year
                    by_year.setdefault(year, []).append(r)

            if len(by_year) >= 2:
                for year in sorted(by_year.keys()):
                    yearly.append(
                        _build_sentiment_period(str(year), by_year[year], 365.0)
                    )

        # Ratio timeline — fixed periods only
        ratio_timeline: list[list] = []
        fixed_period_names = [p[0] for p in period_defs if not p[0].startswith("last_")]
        for sp in fixed_rolling:
            if sp.period in fixed_period_names:
                ratio_timeline.append([sp.period, sp.positive_ratio])

        return (fixed_rolling, yearly, ratio_timeline)

    # ---------------------------------------------------------------------------
    # Language distribution
    # ---------------------------------------------------------------------------

    async def _fetch_language_distribution(self, appid: int) -> dict[str, int]:
        """Fetch paginated language distribution sample.

        Fetches up to 500 reviews (5 pages of 100) with language=all
        to build a meaningful language distribution.

        Args:
            appid: Steam AppID

        Returns:
            Dict of {language: count} from the sample pages
        """
        try:
            lang_counts: dict[str, int] = {}
            cursor = "*"
            lang_max_pages = 5
            pages_fetched = 0

            while pages_fetched < lang_max_pages:
                params = {
                    "json": "1",
                    "num_per_page": "100",
                    "cursor": cursor,
                    "filter": "all",
                    "language": "all",
                }
                data, _, _ = await self.http.get_with_metadata(
                    self.REVIEW_URL.format(appid=appid),
                    params=params,
                    cache_ttl=900,
                )
                reviews = data.get("reviews", [])
                if not reviews:
                    break

                new_cursor = data.get("cursor", "")
                if new_cursor == cursor and pages_fetched > 0:
                    break

                for r in reviews:
                    lang = r.get("language", "unknown")
                    lang_counts[lang] = lang_counts.get(lang, 0) + 1

                cursor = new_cursor
                pages_fetched += 1
                await asyncio.sleep(0.1)

            return lang_counts
        except Exception as e:
            logger.warning(f"Language distribution fetch failed for AppID {appid}: {e}")
            return {}

    # ---------------------------------------------------------------------------
    # Main public method
    # ---------------------------------------------------------------------------

    async def fetch_reviews(
        self,
        appid: int,
        limit: int = 200,
        language: str = "english",
        review_type: str = "all",
        purchase_type: str = "all",
        detail_level: str = "full",
        filter_offtopic: bool = True,
        min_playtime: float | None = None,
        date_range: str | None = None,
        platform: str = "all",
        sort: str = "helpful",
        cursor: str = "",
        include_dimensions: bool = True,
    ) -> ReviewsData:
        """Fetch Steam reviews for a game with pagination, stats, and sentiment.

        Orchestrates review text sequence + stats scan in parallel, then computes
        histogram, sentiment periods, EA dates, and assembles ReviewsData.

        Args:
            appid: Steam AppID
            limit: Maximum reviews in text sequence (default 200)
            language: Language filter (default "english")
            review_type: "all", "positive", or "negative"
            purchase_type: "all", "steam", or "non_steam_purchase"
            detail_level: "full" (5000-review stats scan) or "compact" (1000-review scan)
            filter_offtopic: Filter offtopic activity (default True)
            min_playtime: Minimum playtime_at_review in hours (local filter)
            date_range: Relative ("30d", "6m", "1y") or absolute ISO ("2024-01-01,2024-06-30")
            platform: Platform filter (local-only; Steam API doesn't expose this directly)
            sort: "helpful" (default) or "recent"
            cursor: Pagination cursor from previous call (empty = first call)

        Returns:
            ReviewsData with reviews/snippets, aggregates, histogram, sentiment, and meta
        """
        is_continuation = bool(cursor)
        # Build QueryParams echo
        query_params = QueryParams(
            appid=appid,
            language=language,
            review_type=review_type,
            purchase_type=purchase_type,
            limit=limit,
            detail_level=detail_level,
            filter_offtopic=filter_offtopic,
            min_playtime=min_playtime,
            date_range=date_range,
            platform=platform,
            sort=sort,
        )

        # Resolve date_range to day_range param and/or ISO filter range
        day_range_param: int | None = None
        iso_date_start: int | None = None  # Unix timestamp
        iso_date_end: int | None = None    # Unix timestamp

        if date_range:
            # Absolute ISO range: "2024-01-01,2024-06-30"
            if "," in date_range:
                parts = date_range.split(",", 1)
                try:
                    dt_start = datetime.strptime(parts[0].strip(), "%Y-%m-%d").replace(tzinfo=timezone.utc)
                    dt_end = datetime.strptime(parts[1].strip(), "%Y-%m-%d").replace(tzinfo=timezone.utc)
                    iso_date_start = int(dt_start.timestamp())
                    iso_date_end = int(dt_end.timestamp()) + 86400  # inclusive end
                except ValueError:
                    pass
            else:
                # Relative: "30d", "6m", "1y"
                import re as _re
                m = _re.match(r"^(\d+)(d|m|y)$", date_range.strip().lower())
                if m:
                    num = int(m.group(1))
                    unit = m.group(2)
                    if unit == "d":
                        days = num
                    elif unit == "m":
                        days = num * 30
                    else:  # y
                        days = num * 365
                    day_range_param = min(days, 365)  # Steam caps at 365

        # Dual stats scan: helpful (time-diverse) + recent (recency coverage)
        # Configurable split — adjust limits to shift balance between coverage types
        if detail_level == "compact":
            helpful_limit, recent_limit = 500, 500
        else:
            helpful_limit, recent_limit = 3000, 3000

        # Run review text fetch (with cursor support for continuation)
        start_cursor = cursor if is_continuation else "*"
        text_task = self._fetch_review_text(
            appid, limit, language, review_type, purchase_type,
            filter_offtopic, day_range_param, platform, sort,
            start_cursor=start_cursor,
        )

        if is_continuation:
            # Continuation: skip expensive stats/EA/lang scans — return reviews only
            raw_text, query_summary, last_cursor, pages_text = await text_task
            stats_reviews: list[dict] = []
            ea_dates = None
            lang_dist = None
        else:
            # First call: run stats scans + EA dates + language distribution in parallel
            stats_helpful_task = self._fetch_stats_scan(appid, language, helpful_limit, filter_mode="all")
            stats_recent_task = self._fetch_stats_scan(appid, language, recent_limit, filter_mode="recent")
            ea_task = self._resolve_ea_dates(appid)
            lang_task = self._fetch_language_distribution(appid)

            (
                (raw_text, query_summary, last_cursor, pages_text),
                stats_helpful,
                stats_recent,
                ea_dates,
                lang_dist,
            ) = await asyncio.gather(text_task, stats_helpful_task, stats_recent_task, ea_task, lang_task)

            # Merge helpful + recent stats scans (dedup by recommendationid)
            stats_reviews: list[dict] = []
            seen_ids: set[str] = set()
            for r in stats_helpful + stats_recent:
                rid = str(r.get("recommendationid", ""))
                if rid and rid not in seen_ids:
                    seen_ids.add(rid)
                    stats_reviews.append(r)

        # Determine partial status (set during _fetch_review_text if mid-pagination failure)
        # We can't directly get it from the tuple, but partial is implicitly detected by
        # checking if pages_text was limited before limit reached — treat as ok for now.

        # Handle no-reviews case
        fetched_at = datetime.now(tz=timezone.utc)
        if query_summary is not None and query_summary.get("total_reviews", 0) == 0:
            meta = ReviewsMeta(
                appid=appid,
                total_fetched=0,
                total_available=0,
                pages_fetched=pages_text,
                partial=False,
                cursor=last_cursor or None,
                ea_dates=ea_dates,
                detail_level=detail_level,
                status="no_reviews",
                fetched_at=fetched_at,
            )
            return ReviewsData(
                meta=meta,
                query_params=query_params,
            )

        # Parse raw review text to ReviewItem list
        parsed_reviews: list[ReviewItem] = [self._parse_review(r) for r in raw_text]

        # Apply local filters
        if min_playtime is not None:
            parsed_reviews = [r for r in parsed_reviews if r.playtime_at_review >= min_playtime]

        if iso_date_start is not None:
            parsed_reviews = [
                r for r in parsed_reviews
                if iso_date_start <= r.timestamp_created <= (iso_date_end or 9999999999)
            ]

        # Build aggregates from query_summary
        aggregates: ReviewAggregates | None = None
        if query_summary:
            aggregates = self._build_aggregates(query_summary, parsed_reviews)

        # Compute histogram and sentiment from stats scan (skip on continuation)
        histogram = self._compute_histogram(stats_reviews) if stats_reviews else None
        if stats_reviews and ea_dates is not None:
            fixed_rolling, yearly, ratio_timeline = self._compute_sentiment_periods(stats_reviews, ea_dates)
        else:
            fixed_rolling, yearly, ratio_timeline = None, None, None

        # Build meta
        total_available = query_summary.get("total_reviews", 0) if query_summary else 0
        meta = ReviewsMeta(
            appid=appid,
            total_fetched=len(parsed_reviews),
            total_available=total_available,
            pages_fetched=pages_text,
            partial=False,
            cursor=last_cursor if last_cursor and last_cursor != "*" else None,
            ea_dates=ea_dates,
            detail_level=detail_level,
            status="ok",
            fetched_at=fetched_at,
        )

        # Compact mode: top 2 positive + top 1 negative snippets
        snippets: list[ReviewSnippet] = []
        if detail_level == "compact":
            positive_reviews = [r for r in parsed_reviews if r.voted_up]
            negative_reviews = [r for r in parsed_reviews if not r.voted_up]

            # Sort by votes_up descending, take top N
            positive_reviews.sort(key=lambda r: r.votes_up, reverse=True)
            negative_reviews.sort(key=lambda r: r.votes_up, reverse=True)

            for i, review in enumerate(positive_reviews[:2]):
                snippets.append(ReviewSnippet(
                    review_id=review.review_id,
                    text=review.text[:200],
                    voted_up=review.voted_up,
                    votes_up=review.votes_up,
                    perspective=f"top_positive_{i+1}",
                ))

            for review in negative_reviews[:1]:
                snippets.append(ReviewSnippet(
                    review_id=review.review_id,
                    text=review.text[:200],
                    voted_up=review.voted_up,
                    votes_up=review.votes_up,
                    perspective="top_negative",
                ))

        # Dimension profiling
        dimension_profile = None
        if include_dimensions:
            # In compact mode, use parsed_reviews (full set fetched for stats),
            # not just the 3 snippets
            dimension_profile = _compute_dimension_profile(parsed_reviews)

        # Assemble ReviewsData
        if detail_level == "compact":
            return ReviewsData(
                snippets=snippets,
                aggregates=aggregates,
                histogram=histogram,
                sentiment=fixed_rolling,
                yearly=yearly,
                ratio_timeline=ratio_timeline,
                language_distribution=lang_dist,
                dimension_profile=dimension_profile,
                meta=meta,
                query_params=query_params,
            )
        else:
            return ReviewsData(
                reviews=parsed_reviews,
                aggregates=aggregates,
                histogram=histogram,
                sentiment=fixed_rolling,
                yearly=yearly,
                ratio_timeline=ratio_timeline,
                language_distribution=lang_dist,
                dimension_profile=dimension_profile,
                meta=meta,
                query_params=query_params,
            )
