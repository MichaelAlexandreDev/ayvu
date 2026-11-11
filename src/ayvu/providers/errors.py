"""Errors of building a translation provider from settings."""

from __future__ import annotations


class ProviderSetupError(ValueError):
    """A bounded error: the settings could not build a provider."""
