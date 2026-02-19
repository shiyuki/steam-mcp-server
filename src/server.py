# Configure logging FIRST - before any imports that might use logging
import sys
from pathlib import Path

# Add parent directory to path for direct execution
if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).parent.parent))

from src.logging_config import setup_logging, get_logger
setup_logging()

logger = get_logger(__name__)

from mcp.server.fastmcp import FastMCP

from src.config import Config

# Validate configuration on startup (fail fast)
try:
    Config.validate()
except ValueError as e:
    logger.error("Configuration error: %s", e)
    raise

# Initialize MCP server
mcp = FastMCP("steam-server")

# Register tools (imports mcp from this module)
from src.tools import register_tools
register_tools(mcp)
logger.info("Registered tools: search_genre, fetch_metadata, fetch_commercial, fetch_engagement, aggregate_engagement")


if __name__ == "__main__":
    mcp.run("stdio")
