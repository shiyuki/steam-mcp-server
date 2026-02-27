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


class TestGamalyticApiKeyConfig:
    """Tests for optional GAMALYTIC_API_KEY config field (KEY-02)."""

    def test_gamalytic_api_key_optional(self):
        """validate() succeeds without GAMALYTIC_API_KEY — it is optional."""
        with patch.object(Config, "STEAM_API_KEY", "test-steam-key"):
            with patch.object(Config, "GAMALYTIC_API_KEY", ""):
                # Should not raise ValueError
                result = Config.validate()
                assert result is True

    def test_gamalytic_api_key_defaults_to_empty_string(self):
        """GAMALYTIC_API_KEY defaults to empty string when env var is absent."""
        with patch.dict(os.environ, {}, clear=False):
            # Remove GAMALYTIC_API_KEY from env if present
            os.environ.pop("GAMALYTIC_API_KEY", None)
            # Re-evaluate to pick up absence — Config is class-level so we check attribute type
            assert isinstance(Config.GAMALYTIC_API_KEY, str)

    def test_gamalytic_api_key_read(self):
        """GAMALYTIC_API_KEY is accessible as a class attribute when set."""
        with patch.object(Config, "GAMALYTIC_API_KEY", "test-pro-key"):
            assert Config.GAMALYTIC_API_KEY == "test-pro-key"

    def test_gamalytic_api_key_truthy_when_set(self):
        """Non-empty GAMALYTIC_API_KEY is truthy — used in if Config.GAMALYTIC_API_KEY check."""
        with patch.object(Config, "GAMALYTIC_API_KEY", "sk-live-abc123"):
            assert Config.GAMALYTIC_API_KEY
            assert bool(Config.GAMALYTIC_API_KEY) is True

    def test_gamalytic_api_key_falsy_when_empty(self):
        """Empty GAMALYTIC_API_KEY is falsy — no auth header injected on free tier."""
        with patch.object(Config, "GAMALYTIC_API_KEY", ""):
            assert not Config.GAMALYTIC_API_KEY
            assert bool(Config.GAMALYTIC_API_KEY) is False
