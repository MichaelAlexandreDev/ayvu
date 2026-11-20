"""CLI defaults and option help shared by the commands, and the network policy chosen for this invocation."""
from __future__ import annotations

from contextvars import ContextVar
from pathlib import Path
from ...network import NetworkPolicy


DEFAULT_SOURCE_LANGUAGE = "en"
DEFAULT_TARGET_LANGUAGE = "pt"
DEFAULT_TRANSLATOR_URL = "http://localhost:5000"
NETWORK_POLICY_HELP = ("Política HTTP desta execução: none bloqueia DNS/sockets; loopback (padrão) permite somente HTTP local; "
                       "remote permite também HTTPS aos hosts que um provedor remoto declara. "
                       "Não é salva. Leitores externos ficam fora da garantia offline.")
CONSENT_REMOTE_HELP = ("Developer mode: record this project's consent to send the book's visible text and glossary terms "
                       "to the selected remote provider. Without it, a remote translation is refused.")


_invocation_network_policy: ContextVar[NetworkPolicy | None] = ContextVar("network_policy", default=None)
# The invocation's answer to a remote egress notice: True (the flag), False (developer mode without it),
# or None, which means the guided and common modes ask. Never saved.
_invocation_remote_consent: ContextVar[bool | None] = ContextVar("remote_consent", default=None)
DEFAULT_CACHE_PATH = Path(".cache/traducoes.sqlite")
DEFAULT_PREVIEW_DOCUMENT_LIMIT = 12
DEFAULT_RETRY_BACKOFF = 0.5
DEFAULT_RETRY_BACKOFF_MAX = 8.0


def _selected_network_policy(value: NetworkPolicy | None = None) -> NetworkPolicy | None:
    """Read CLI request input only; composition resolves the compatibility default.

    Guided menus, settings and recovery keep the invocation's selection without
    persisting it in preferences, projects or checkpoints.
    """
    if value is not None:
        return value
    return _invocation_network_policy.get()
