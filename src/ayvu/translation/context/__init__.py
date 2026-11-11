"""Translation context: what a unit may know about its neighbours, where it comes from, and how a run applies
it.
"""
from __future__ import annotations

from .execution import ContextExecution
from .model import ContextError

__all__ = [
    "ContextError", "ContextExecution",
]
