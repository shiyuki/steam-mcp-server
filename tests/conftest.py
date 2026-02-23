"""Shared pytest configuration and fixtures."""


def pytest_configure(config):
    config.addinivalue_line("markers", "integration: live API tests (skipped by default)")
