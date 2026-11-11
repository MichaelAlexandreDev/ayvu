"""Translation providers (D1 of docs/architecture/decisions/package-structure.md).

The flat ``provider_*`` modules move here in REF-11; the setup error arrived first, in REF-04."""

from __future__ import annotations

from .errors import ProviderSetupError

__all__ = [
    "ProviderSetupError",
]
