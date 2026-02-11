"""Tests for configuration validation."""

import os
import pytest
from unittest.mock import patch

from src.config import Config


class TestConfigValidate:
    def test_validate_raises_on_empty_api_key(self):
        with patch.object(Config, "STEAM_API_KEY", ""):
            with pytest.raises(ValueError, match="STEAM_API_KEY is required"):
                Config.validate()

    def test_validate_raises_on_low_rate_limit(self):
        with patch.object(Config, "STEAM_API_KEY", "test-key"):
            with patch.object(Config, "RATE_LIMIT_DELAY", 0.5):
                with pytest.raises(ValueError, match="RATE_LIMIT_DELAY must be >= 1.0"):
                    Config.validate()

    def test_validate_passes_with_valid_config(self):
        with patch.object(Config, "STEAM_API_KEY", "test-key"):
            with patch.object(Config, "RATE_LIMIT_DELAY", 1.5):
                assert Config.validate() is True

    def test_validate_passes_at_minimum_rate_limit(self):
        with patch.object(Config, "STEAM_API_KEY", "test-key"):
            with patch.object(Config, "RATE_LIMIT_DELAY", 1.0):
                assert Config.validate() is True
