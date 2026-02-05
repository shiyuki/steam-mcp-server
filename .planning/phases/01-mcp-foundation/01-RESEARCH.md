# Phase 1: MCP Foundation & Steam API Integration - Research

**Researched:** 2026-02-05
**Domain:** Python MCP server development with Steam Web API integration
**Confidence:** HIGH

## Summary

Phase 1 requires building a Model Context Protocol (MCP) server in Python that connects to Claude/Cursor via stdio transport and exposes Steam game search/metadata tools. The research identified the official Python MCP SDK (v1.26.0) as the stable foundation, with FastMCP being the dominant pattern but not required for basic implementations. The Steam API landscape presents a critical challenge: the official Steam Web API lacks tag-based search, requiring either the deprecated SteamSpy API or multi-step workflows (GetAppList → appdetails filtering). Conservative rate limiting (1.5s delays) is essential due to undocumented Steam API throttling.

**Key findings:**
- Python MCP SDK v1.26.0 is stable; v2 coming Q1 2026 but stay on v1.x for now
- STDIO transport has critical requirement: NEVER write to stdout (use logging to stderr only)
- Pydantic validation works but has known issues with some MCP clients serializing to JSON strings
- Steam's official API doesn't support tag/genre search directly—requires workarounds
- Rate limiting is undocumented but ~100k/day with 1 req/sec recommended pace

**Primary recommendation:** Use official `mcp` package with stdio transport, implement conservative 1.5s rate limiting with exponential backoff, and accept that tag-based search requires either SteamSpy (1 req/60s limit) or multi-step workflow with manual filtering.

## Standard Stack

The established libraries/tools for Python MCP server development:

### Core
| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| mcp | 1.26.0 | Official Python MCP SDK | Only official SDK from Anthropic, powers all Python MCP servers |
| httpx | 0.28.1 | Async HTTP client | Industry standard for async HTTP in Python, better than requests for async |
| pydantic | 2.x | Schema validation and type safety | Native integration with MCP SDK, automatic schema generation from type hints |
| python-dotenv | latest | Environment variable management | Standard for .env file loading, security best practice |

### Supporting
| Library | Version | Purpose | When to Use |
|---------|---------|---------|-------------|
| tenacity | latest | Retry logic with exponential backoff | Cleaner than manual retry loops, works with async/await |
| logging | stdlib | Structured logging to stderr | Required for STDIO servers (cannot use print()) |
| uv | latest | Fast package manager | 10-100x faster than pip, recommended by official MCP docs |

### Alternatives Considered
| Instead of | Could Use | Tradeoff |
|------------|-----------|----------|
| mcp | FastMCP (jlowin/fastmcp) | FastMCP adds convenience (70% market share) but adds dependency; official SDK sufficient for this phase |
| httpx | aiohttp | httpx has cleaner API and better timeout handling; aiohttp more mature but verbose |
| tenacity | backoff library | Both work; tenacity more actively maintained and better async support |

**Installation:**
```bash
# Using uv (recommended)
uv init steam-mcp-server
cd steam-mcp-server
uv venv
source .venv/bin/activate  # or .venv\Scripts\activate on Windows
uv add "mcp[cli]" httpx pydantic python-dotenv tenacity

# Using pip (fallback)
pip install "mcp[cli]" httpx pydantic python-dotenv tenacity
```

## Architecture Patterns

### Recommended Project Structure
```
steam-mcp-server/
├── src/
│   ├── __init__.py
│   ├── server.py           # MCP server initialization and tool registration
│   ├── steam_api.py        # Steam API client with rate limiting
│   ├── schemas.py          # Pydantic models for tool inputs/outputs
│   └── config.py           # Environment variable loading
├── tests/
│   ├── test_tools.py
│   └── test_steam_api.py
├── .env                     # API keys (gitignored)
├── .env.example            # Template without secrets
├── pyproject.toml          # uv/pip configuration
└── README.md
```

### Pattern 1: STDIO Server with Logging to Stderr
**What:** MCP server using stdio transport with all logging routed to stderr
**When to use:** Always for Claude Desktop / Cursor integration (stdio is the standard)
**Example:**
```python
# Source: https://modelcontextprotocol.io/docs/develop/build-server
import logging
from mcp.server.mcpserver import MCPServer

# Configure logging to stderr BEFORE any print() calls
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler()]  # Writes to stderr by default
)
logger = logging.getLogger(__name__)

# NEVER use print() - it writes to stdout and corrupts JSON-RPC
logger.info("Server starting")  # ✅ Good
# print("Server starting")      # ❌ Bad - breaks stdio transport
```

### Pattern 2: Tool Registration with Pydantic Schemas
**What:** Define tools using @mcp.tool() decorator with type-annotated functions
**When to use:** All MCP tool definitions
**Example:**
```python
# Source: https://github.com/modelcontextprotocol/python-sdk
from mcp.server.mcpserver import MCPServer
from pydantic import BaseModel, Field

mcp = MCPServer(name="steam-server")

class SearchGenreInput(BaseModel):
    genre: str = Field(description="Steam tag/genre to search (e.g. 'Action', 'RPG')")
    limit: int = Field(default=10, ge=1, le=100, description="Max results to return")

@mcp.tool()
async def search_genre(input: SearchGenreInput) -> list[int]:
    """Search Steam games by genre/tag and return AppIDs."""
    # SDK automatically validates input against SearchGenreInput schema
    # Returns list of AppIDs
    pass
```

### Pattern 3: Async HTTP Client with Rate Limiting
**What:** Shared httpx AsyncClient with retry logic and rate limiting
**When to use:** All external API calls
**Example:**
```python
# Source: https://scrapeops.io/python-web-scraping-playbook/python-httpx-retry-failed-requests/
import httpx
import asyncio
from tenacity import (
    retry, stop_after_attempt, wait_exponential,
    retry_if_exception_type
)

class SteamAPIClient:
    def __init__(self, api_key: str, rate_limit_delay: float = 1.5):
        self.api_key = api_key
        self.rate_limit_delay = rate_limit_delay
        self.last_request_time = 0
        self.client = httpx.AsyncClient(timeout=30.0)

    async def _rate_limit(self):
        """Enforce minimum delay between requests."""
        elapsed = asyncio.get_event_loop().time() - self.last_request_time
        if elapsed < self.rate_limit_delay:
            await asyncio.sleep(self.rate_limit_delay - elapsed)
        self.last_request_time = asyncio.get_event_loop().time()

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1.5, min=1, max=10),
        retry=retry_if_exception_type((httpx.HTTPError, asyncio.TimeoutError))
    )
    async def get(self, url: str) -> dict:
        await self._rate_limit()
        response = await self.client.get(url)
        response.raise_for_status()
        return response.json()
```

### Pattern 4: Structured Error Responses
**What:** Return MCP-compliant errors with isError flag and classification
**When to use:** All tool error handling
**Example:**
```python
# Source: https://mcpcat.io/guides/error-handling-custom-mcp-servers/
from mcp.types import CallToolResult, TextContent

async def search_genre(input: SearchGenreInput) -> CallToolResult:
    try:
        results = await steam_client.search_by_tag(input.genre)
        return CallToolResult(
            content=[TextContent(type="text", text=str(results))]
        )
    except httpx.HTTPStatusError as e:
        error_code = e.response.status_code
        if error_code == 429:
            classification = "rate_limit"
            message = "Steam API rate limit exceeded. Please try again in 60 seconds."
        elif error_code == 404:
            classification = "not_found"
            message = f"Genre '{input.genre}' not found."
        else:
            classification = "api_error"
            message = f"Steam API returned {error_code}"

        return CallToolResult(
            content=[TextContent(type="text", text=message)],
            isError=True
        )
```

### Anti-Patterns to Avoid
- **Writing to stdout in STDIO servers:** Use logging library, not print()
- **Hardcoding API keys:** Always use environment variables with python-dotenv
- **Synchronous HTTP in async tools:** Use httpx.AsyncClient, not requests
- **No rate limiting:** Steam has undocumented limits; always implement conservative delays
- **1:1 API mapping:** Don't expose raw Steam API; combine search + metadata fetch into single tool

## Don't Hand-Roll

Problems that look simple but have existing solutions:

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Retry logic with exponential backoff | Custom sleep loops with doubling delays | tenacity library with @retry decorator | Handles jitter, async/await, multiple retry conditions, max attempts |
| HTTP client with timeouts | Raw urllib or manual socket code | httpx.AsyncClient | Built-in timeout management, connection pooling, async support |
| Environment variable loading | Manual os.getenv() with defaults scattered in code | python-dotenv with .env files | Centralized config, .env.example templates, type coercion |
| Input validation | Manual type checks and if/else chains | Pydantic BaseModel with Field validators | Automatic error messages, JSON schema generation, IDE support |
| MCP schema generation | Manual JSON schema dictionaries | Type hints with Pydantic models | SDK auto-generates schemas from types |
| Async rate limiting | Global counters with locks | Class-based rate limiter with asyncio timing | Handles concurrent requests correctly, no race conditions |

**Key insight:** The Python MCP ecosystem expects type hints + Pydantic for everything. Fighting this pattern means rebuilding what the SDK provides for free.

## Common Pitfalls

### Pitfall 1: Print Statements Breaking STDIO Transport
**What goes wrong:** Server starts but Claude/Cursor can't communicate; shows connection errors
**Why it happens:** print() writes to stdout, corrupting JSON-RPC messages that MCP uses for communication
**How to avoid:**
- Configure logging BEFORE any imports that might print
- Use logging.info()/debug()/error() instead of print()
- Search codebase for print() before testing
**Warning signs:** "Transport error", "Invalid JSON-RPC", server appears dead but process is running

### Pitfall 2: Steam API Has No Official Tag Search
**What goes wrong:** Expecting ISteamApps/GetAppList to accept tag parameters, getting all 100k+ games instead
**Why it happens:** Official Steam Web API only provides full app list; tags require storefront scraping or SteamSpy
**How to avoid:**
- Option A: Use SteamSpy API (steamspy.com/api.php?request=tag&tag=Action) but respect 1 req/60s limit
- Option B: Fetch IStoreService/GetAppList (paginated), cache locally, filter by tag via appdetails
- Option C: Hybrid—use SteamSpy for search, official API for metadata
**Warning signs:** Fetching 100k games takes minutes, hitting rate limits, Claude times out

### Pitfall 3: Pydantic Models as JSON Strings
**What goes wrong:** Tool receives parameters as JSON string instead of dict, Pydantic validation fails
**Why it happens:** Some MCP clients (including Claude Code) serialize Pydantic models to strings before sending
**How to avoid:**
- Use individual parameters instead of nested Pydantic models for now
- If using models, test with actual client (Claude Desktop) not just SDK test harness
- Consider manual JSON parsing in tool if SDK validation fails
**Warning signs:** ValidationError about expecting dict but receiving string, tools work in tests but fail in Claude

### Pitfall 4: Ignoring Rate Limits Until Production
**What goes wrong:** Development works fine, production gets 429 errors and IP bans
**Why it happens:** Steam's rate limits aren't documented; conservative delays feel slow but are necessary
**How to avoid:**
- Implement 1.5s delay from day one, even in development
- Add exponential backoff for retries (1.5s → 3s → 6s)
- Log all rate limit errors to understand patterns
**Warning signs:** 403/429 status codes, "strict rate limits for connecting IP" messages

### Pitfall 5: No Environment Variable Validation
**What goes wrong:** Server starts but fails on first API call with "API key not found"
**Why it happens:** Missing .env file or typo in variable name; error happens too late
**How to avoid:**
```python
# In config.py
import os
from dotenv import load_dotenv

load_dotenv()

STEAM_API_KEY = os.getenv("STEAM_API_KEY")
if not STEAM_API_KEY:
    raise ValueError(
        "STEAM_API_KEY not found. Copy .env.example to .env and add your key."
    )
```
**Warning signs:** Server starts but fails mysteriously on first tool call

## Code Examples

Verified patterns from official sources:

### Complete STDIO Server Initialization
```python
# Source: https://github.com/modelcontextprotocol/python-sdk
import logging
import asyncio
from mcp.server.mcpserver import MCPServer
from mcp.server.stdio import stdio_server

# Configure logging to stderr
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

# Initialize MCP server
mcp = MCPServer(name="steam-server")

@mcp.tool()
async def search_genre(genre: str, limit: int = 10) -> str:
    """Search Steam games by genre/tag."""
    logger.info(f"Searching for genre: {genre}")
    # Implementation here
    return f"Found {limit} games"

async def main():
    """Run MCP server with stdio transport."""
    async with stdio_server() as (read_stream, write_stream):
        await mcp.run(
            read_stream,
            write_stream,
            mcp.create_initialization_options()
        )

if __name__ == "__main__":
    asyncio.run(main())
```

### Environment-Based Configuration
```python
# Source: https://medium.com/@alwinraju/storing-environment-variables-and-api-keys-in-python
# config.py
import os
from dotenv import load_dotenv

load_dotenv()

class Config:
    STEAM_API_KEY: str = os.getenv("STEAM_API_KEY", "")
    RATE_LIMIT_DELAY: float = float(os.getenv("RATE_LIMIT_DELAY", "1.5"))
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")

    @classmethod
    def validate(cls):
        """Validate required config on startup."""
        if not cls.STEAM_API_KEY:
            raise ValueError(
                "STEAM_API_KEY is required. "
                "Copy .env.example to .env and add your Steam API key."
            )
        if cls.RATE_LIMIT_DELAY < 1.0:
            raise ValueError("RATE_LIMIT_DELAY must be >= 1.0 seconds")

# .env.example (committed to git)
"""
STEAM_API_KEY=your_key_here
RATE_LIMIT_DELAY=1.5
LOG_LEVEL=INFO
"""

# .env (gitignored)
"""
STEAM_API_KEY=ABC123XYZ789
RATE_LIMIT_DELAY=1.5
LOG_LEVEL=DEBUG
"""
```

### Input Validation with Pydantic
```python
# Source: https://ai.pydantic.dev/mcp/server/
# schemas.py
from pydantic import BaseModel, Field, validator

class SearchGenreInput(BaseModel):
    """Input schema for search_genre tool."""
    genre: str = Field(
        description="Steam tag/genre to search (e.g., 'Action', 'RPG', 'Indie')",
        min_length=1,
        max_length=50
    )
    limit: int = Field(
        default=10,
        ge=1,
        le=100,
        description="Maximum number of results to return"
    )

    @validator('genre')
    def normalize_genre(cls, v):
        """Normalize genre to title case."""
        return v.strip().title()

class FetchMetadataInput(BaseModel):
    """Input schema for fetch_metadata tool."""
    appid: int = Field(
        description="Steam AppID to fetch metadata for",
        gt=0
    )

class GameMetadata(BaseModel):
    """Output schema for game metadata."""
    appid: int
    name: str
    tags: list[str]
    developer: str
    description: str

    class Config:
        # Allow extra fields from Steam API without errors
        extra = "ignore"
```

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| ISteamApps/GetAppList/v1 | IStoreService/GetAppList/v1 | 2023-2024 | Old endpoint deprecated due to scale issues; new one paginated with 50k max results |
| SSE transport for MCP | STDIO transport | 2026 | SSE already deprecated in 2026; STDIO is standard |
| requests library | httpx for async | 2020-2022 | Modern async/await support; requests still sync-only |
| Manual JSON schemas | Pydantic + type hints | 2023+ | SDK auto-generates schemas; manual JSON is legacy |
| MCP Python SDK v1.x | v2.x (Q1 2026) | Coming soon | v1.x stable now; v2 alpha available but not recommended for production |

**Deprecated/outdated:**
- **ISteamApps/GetAppList/v0001**: Replaced by IStoreService/GetAppList/v1 due to scale limitations
- **SSE transport in MCP**: Deprecated by 2026; use STDIO or upcoming Streamable HTTP
- **FastMCP as separate package**: May be merged into official SDK in v2; use official SDK unless needing FastMCP-specific features
- **SteamSpy accuracy**: Data quality declining over time; still useful for tags but owner counts are estimates

## Open Questions

Things that couldn't be fully resolved:

1. **Best approach for tag-based search**
   - What we know: Official Steam API has no tag search; SteamSpy has 1 req/60s limit; appdetails doesn't expose tags
   - What's unclear: Whether to cache full app list locally or use SteamSpy despite rate limits
   - Recommendation: Prototype with SteamSpy for Phase 1 (simpler), accept 60s delay between searches. Document limitation for Phase 2 optimization (local cache + appdetails filtering)

2. **Steam API authentication requirements**
   - What we know: Some endpoints require API key, some don't; partner.steam-api.com requires publisher keys
   - What's unclear: Does IStoreService/GetAppList require key? Does store.steampowered.com/api/appdetails require key?
   - Recommendation: Implement environment variable pattern for API key; test endpoints to determine which need authentication. Fallback to unauthenticated endpoints if possible.

3. **Pydantic model parameter serialization issue**
   - What we know: Claude Code serializes Pydantic models to JSON strings, causing validation errors
   - What's unclear: Whether this affects Claude Desktop, Cursor, or just Claude Code; whether SDK fix is coming in v2
   - Recommendation: Use individual parameters instead of nested Pydantic models for Phase 1. Re-evaluate when v2 ships (Q1 2026).

4. **Optimal rate limiting strategy**
   - What we know: Steam enforces ~100k req/day, recommends 1 req/sec, store.steampowered.com limited to ~200 req/5min
   - What's unclear: Exact per-second/per-minute limits; whether limits differ by endpoint
   - Recommendation: Implement 1.5s delay (conservative), log all 429/403 errors, adjust based on observed patterns. Consider separate rate limiters per domain (api.steampowered.com vs store.steampowered.com).

## Sources

### Primary (HIGH confidence)
- [Official MCP Python SDK GitHub](https://github.com/modelcontextprotocol/python-sdk) - v1.26.0 stable, v2 coming Q1 2026
- [MCP Build Server Documentation](https://modelcontextprotocol.io/docs/develop/build-server) - STDIO logging requirements, tool patterns
- [MCP Python SDK PyPI](https://pypi.org/project/mcp/) - v1.26.0 released 2026-01-24, requires Python >=3.10
- [Steamworks Web API Overview](https://partner.steamgames.com/doc/webapi_overview) - Official rate limits (100k/day), authentication
- [ISteamApps Interface Documentation](https://partner.steamgames.com/doc/webapi/isteamapps) - GetAppList deprecated, recommends IStoreService
- [IStoreService.json GitHub](https://github.com/SteamDatabase/SteamTracking/blob/master/API/IStoreService.json) - GetAppList parameters, pagination

### Secondary (MEDIUM confidence)
- [Python HTTPX Retry Guide](https://scrapeops.io/python-web-scraping-playbook/python-httpx-retry-failed-requests/) - Tenacity + httpx patterns
- [MCP Error Handling Guide](https://mcpcat.io/guides/error-handling-custom-mcp-servers/) - isError flag usage, error classification
- [Pydantic AI MCP Server](https://ai.pydantic.dev/mcp/server/) - Pydantic validation patterns with MCP
- [Steam Web API Xpaw Tester](https://steamapi.xpaw.me/) - Unofficial but accurate API documentation
- [SteamSpy API Documentation](https://steamspy.com/api.php) - Tag search alternative, rate limits
- [Python Environment Variables Security](https://medium.com/@alwinraju/storing-environment-variables-and-api-keys-in-python) - python-dotenv best practices

### Tertiary (LOW confidence - needs validation)
- [FastMCP GitHub](https://github.com/jlowin/fastmcp) - Claims 70% MCP server market share; not verified independently
- [MCP Server Best Practices 2026](https://www.cdata.com/blog/mcp-server-best-practices-2026) - Community article, not official
- [How Not to Write an MCP Server](https://towardsdatascience.com/how-not-to-write-an-mcp-server/) - Anti-patterns; some opinions may be subjective
- [Steam Store API Community Docs](https://steamcommunity.com/discussions/forum/1/1735468061777761727/) - Community-documented appdetails endpoint, not official

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH - Official SDK docs and PyPI versions verified
- Architecture: HIGH - Patterns from official MCP docs and working examples
- Steam API: MEDIUM - Official docs for general limits, community docs for specific endpoints
- Pitfalls: HIGH - Verified from official docs (stdout logging) and community consensus (Pydantic serialization bug)

**Research date:** 2026-02-05
**Valid until:** 2026-04-05 (60 days - MCP SDK v2 expected Q1 2026 may change recommendations)

**Key risks:**
- Steam API tag search requires workaround; may need architecture change if SteamSpy becomes unavailable
- Pydantic serialization bug affects some clients; workaround required until SDK v2
- Rate limits undocumented; conservative approach may be overly cautious but safer than IP bans
