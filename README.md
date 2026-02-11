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

Get detailed metadata for a specific game.

**Parameters:**
- `appid` (integer, required): Steam AppID (e.g., 646570 for Slay the Spire)

**Example prompt:** "Get metadata for Steam AppID 646570"

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
