# Configure logging FIRST - before any imports that might use logging
import sys
from pathlib import Path

# Add parent directory to path for direct execution
if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).parent.parent))

from src.logging_config import setup_logging, get_logger
setup_logging()

logger = get_logger(__name__)

import asyncio
from mcp.server import Server
from mcp.server.stdio import stdio_server

from src.config import Config
from src.tools import register_tools

# Validate configuration on startup (fail fast)
try:
    Config.validate()
except ValueError as e:
    logger.error("Configuration error: %s", e)
    raise

# Initialize MCP server
mcp = Server(name="steam-server")

# Register tools
register_tools(mcp)
logger.info("Registered tools: search_genre, fetch_metadata")


async def main():
    """Run MCP server with stdio transport."""
    logger.info("Starting steam-server MCP server")

    async with stdio_server() as (read_stream, write_stream):
        await mcp.run(
            read_stream,
            write_stream,
            mcp.create_initialization_options()
        )


if __name__ == "__main__":
    asyncio.run(main())
