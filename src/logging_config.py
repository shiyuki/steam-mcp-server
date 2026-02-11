import logging
import sys

def setup_logging(level: str = "INFO"):
    """Configure all logging to stderr (required for MCP stdio transport)."""
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    ))

    root = logging.getLogger()
    root.handlers.clear()  # Remove any existing handlers
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper()))

def get_logger(name: str) -> logging.Logger:
    """Get a logger instance. Call setup_logging() first."""
    return logging.getLogger(name)
