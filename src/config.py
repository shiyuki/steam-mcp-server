"""Configuration management for Steam MCP Server."""

import logging
import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    """Configuration loaded from environment variables.

    Call Config.validate() at server startup to ensure required values are present.
    """

    STEAM_API_KEY: str = os.getenv("STEAM_API_KEY", "")
    RATE_LIMIT_DELAY: float = float(os.getenv("RATE_LIMIT_DELAY", "1.5"))
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")
    GAMALYTIC_API_KEY: str = os.getenv("GAMALYTIC_API_KEY", "")
    REPORTS_DIR: str = os.getenv("REPORTS_DIR", "")

    @classmethod
    def validate(cls) -> bool:
        """Validate configuration. Call at server startup, not at import.

        Raises:
            ValueError: If STEAM_API_KEY is missing or RATE_LIMIT_DELAY is too low.

        Returns:
            bool: True if configuration is valid.
        """
        if not cls.STEAM_API_KEY:
            raise ValueError(
                "STEAM_API_KEY is required. "
                "Copy .env.example to .env and add your Steam API key."
            )
        if cls.RATE_LIMIT_DELAY < 1.0:
            raise ValueError("RATE_LIMIT_DELAY must be >= 1.0 seconds")
        if not cls.REPORTS_DIR:
            logging.getLogger(__name__).warning(
                "REPORTS_DIR is not set. Report generation will not work. "
                "Set REPORTS_DIR in your environment to enable reports."
            )
        return True


# NOTE: validate() is NOT called here - server.py calls it at startup
