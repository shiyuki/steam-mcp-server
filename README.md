# Steam MCP Server

An MCP (Model Context Protocol) server that enables Claude and Cursor to search Steam games by genre and fetch game metadata.

## Prerequisites

- Python 3.10 or higher
- [uv](https://docs.astral.sh/uv/) package manager
- Steam Web API key ([get one here](https://steamcommunity.com/dev/apikey))

## Installation

1. Navigate to the project directory:
   ```bash
   cd steam-mcp-server
   ```

2. Copy the environment template and add your API key:
   ```bash
   cp .env.example .env
   # Edit .env and add your STEAM_API_KEY
   ```

3. Install dependencies:
   ```bash
   uv sync
   ```

## Claude Desktop Configuration

Add this server to your Claude Desktop config:

**Windows:** `%APPDATA%\Claude\claude_desktop_config.json`
**macOS:** `~/Library/Application Support/Claude/claude_desktop_config.json`

```json
{
  "mcpServers": {
    "steam": {
      "command": "uv",
      "args": ["--directory", "C:\\Users\\shiyu\\steam-mcp-server", "run", "python", "-m", "src.server"],
      "env": {
        "STEAM_API_KEY": "your_key_here"
      }
    }
  }
}
```

**Note:** Replace the path with your actual installation directory. On Windows, use double backslashes.

After editing, restart Claude Desktop completely.

## Cursor Configuration

Cursor uses the same MCP configuration format. Add to your Cursor settings or workspace `.cursor/mcp.json`.

## Available Tools

### search_genre

Search Steam games by genre/tag.

**Parameters:**
- `genre` (string, required): Steam tag to search (e.g., "Roguelike", "Action", "RPG")
- `limit` (integer, optional): Maximum results, 1-100, default 10

**Example prompt:** "Search for roguelike games on Steam"

### fetch_metadata

Get detailed metadata for a specific game from the Steam Store.

**Parameters:**
- `appid` (integer, required): Steam AppID (e.g., 646570 for Slay the Spire)

**Returns:** Comprehensive game details including:
- Identification: name, developer, publisher, header image
- Pricing: price (USD), is_free_to_play
- Platform support: windows/mac/linux flags
- Release info: release_date (ISO format), release_date_raw
- Reviews: recommendations count, metacritic_score, metacritic_url
- Categories, genres, tags
- Media: screenshots (with count), movies/trailers (with count)
- DLC: list of DLC AppIDs and count
- Content descriptors
- Languages: supported_languages_count, supported_languages_raw (HTML string)
- Extended fields (Phase 8):
  - `achievement_count` — total achievements (integer or null)
  - `ratings` — regional ratings dict (e.g., `{"esrb": {"rating": "M", "descriptors": "..."}}`); authorities include ESRB, PEGI, USK, DEJUS, OFLC
  - `supported_languages` — structured list: `[{"language": "English", "full_audio": true}, ...]`
  - `developer_website` — studio website URL or null
  - `pc_requirements_min` — minimum PC requirements as plain text (HTML stripped)
  - `pc_requirements_rec` — recommended PC requirements as plain text (HTML stripped)
  - `controller_support` — "full", "partial", or null

**Cache:** 7-day TTL (platforms, ratings, and achievements rarely change)

**Example prompt:** "Get metadata for Steam AppID 646570"

### fetch_commercial

Fetch commercial data (pricing, revenue estimates, and extended analytics) for a Steam game.

**Parameters:**
- `appid` (integer, required): Steam AppID (e.g., 646570 for Slay the Spire)
- `detail_level` (string, optional): "full" (default) or "summary"
  - `full` — returns all fields including history array, competitor arrays, DLC, and estimation breakdown
  - `summary` — returns core fields only: revenue, copies_sold, price, accuracy, review_score, followers, and top-3 countries

**Returns:** Revenue range (min-max) with confidence level and data source, plus extended Gamalytic fields:
- `copies_sold` — estimated units sold
- `followers` — Steam wishlist/follower count
- `accuracy` — Gamalytic data confidence score
- `total_revenue` — whether revenue figure covers lifetime (true) or partial period (false)
- `history` — daily snapshots array with revenue/reviews/players trends (full only)
- `country_data` — top 10 revenue markets by country (full only)
- `audience_overlap` — 10 games with overlapping audiences (full only)
- `also_played` — 10 competitor games frequently played together (full only)
- `estimate_details` — 3 independent revenue model breakdowns (full only)
- `gamalytic_dlc` — DLC revenue data from Gamalytic (full only)
- Overlapping fields prefixed `gamalytic_` for cross-validation: `gamalytic_owners`, `gamalytic_players`, `gamalytic_reviews`

**Fallback cascade:** Gamalytic API -> Steam Web API player count -> SteamSpy CCU -> review-based estimation

**Triangulation:** Includes warning when Gamalytic and review-based estimates disagree significantly (>50% divergence)

**Example prompt:** "What are the revenue estimates for Slay the Spire (AppID 646570)?"

### fetch_commercial_batch

Fetch commercial data for multiple Steam games in a single call.

**Parameters:**
- `appids` (string, required): Comma-separated Steam AppIDs (e.g., "646570,2379780,247080")
- `detail_level` (string, optional): "full" or "summary" (default "summary" for batch efficiency)

**Returns:** JSON array of commercial data objects in the same order as the input AppIDs. Failed AppIDs return error objects in the array instead of commercial data.

**Implementation:** Uses concurrent fetching with rate limiting (5 req/s for Gamalytic API). Default detail_level is "summary" to reduce response size for batch calls.

**Example prompt:** "Get commercial data for these roguelike games: 646570, 2379780, 247080"

## Troubleshooting

### Server not appearing in Claude

1. Check that the path in config is correct
2. Restart Claude Desktop completely (not just the window)
3. Check Claude's MCP logs for errors

### API errors

1. Verify your STEAM_API_KEY is valid
2. Check that .env file exists with the key
3. Look at server stderr output for detailed errors

### Rate limiting

The server enforces 1.5-second delays between API requests. If you see rate limit errors, wait a moment and retry.

## Development

Run the server directly for testing:
```bash
uv run python -m src.server
```

The server communicates via stdio (stdin/stdout for JSON-RPC, stderr for logs).
