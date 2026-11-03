from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import unquote, urlsplit

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PRODUCT_SCOPE = PROJECT_ROOT / "docs" / "product-scope.md"
PRODUCT_THREAT_MODEL = PROJECT_ROOT / "docs" / "architecture" / "product-threat-model.md"
MODULE_BOUNDARIES = PROJECT_ROOT / "docs" / "architecture" / "modular-monolith-boundaries.md"
GLOBAL_PREFERENCES = PROJECT_ROOT / "docs" / "architecture" / "global-preferences.md"
LOCAL_STORAGE = PROJECT_ROOT / "docs" / "architecture" / "local-project-storage.md"
PROJECT_MIGRATIONS = PROJECT_ROOT / "docs" / "architecture" / "project-migrations.md"
JOB_LIFECYCLE = PROJECT_ROOT / "docs" / "architecture" / "job-lifecycle.md"
JOB_EVENTS = PROJECT_ROOT / "docs" / "architecture" / "job-events-cancellation.md"
JOB_ARTIFACTS = PROJECT_ROOT / "docs" / "architecture" / "job-artifacts.md"
MARKDOWN_LINK = re.compile(r"\[[^\]]+\]\(([^)]+)\)")


def _resolve_local_markdown_target(document: Path, target: str) -> Path | None:
    target = target.strip()
    parsed = urlsplit(target)
    if target.startswith("#") or parsed.scheme.lower() in {"http", "https", "mailto"}:
        return None

    path = unquote(parsed.path)
    if not path:
        return None
    return (document.parent / path).resolve()


@pytest.mark.parametrize(
    "document",
    [PRODUCT_SCOPE, PRODUCT_THREAT_MODEL, MODULE_BOUNDARIES, GLOBAL_PREFERENCES, LOCAL_STORAGE, PROJECT_MIGRATIONS, JOB_LIFECYCLE, JOB_EVENTS, JOB_ARTIFACTS],
    ids=["product-scope", "product-threat-model", "module-boundaries", "global-preferences", "local-storage", "project-migrations", "job-lifecycle", "job-events", "job-artifacts"],
)
def test_local_markdown_links_resolve_inside_repository(document: Path) -> None:
    """Keep simple inline links in product and architecture docs navigable."""
    assert document.is_file()

    for match in MARKDOWN_LINK.finditer(document.read_text(encoding="utf-8")):
        resolved = _resolve_local_markdown_target(document, match.group(1))
        if resolved is None:
            continue

        assert resolved.is_relative_to(PROJECT_ROOT.resolve())
        assert resolved.is_file(), f"broken local link in {document.name}: {match.group(1).strip()}"


@pytest.mark.parametrize(
    "target",
    [
        "../../../outside.md",
        "%2e%2e/%2e%2e/%2e%2e/outside.md",
    ],
)
def test_local_markdown_target_resolution_detects_path_escape(target: str) -> None:
    resolved = _resolve_local_markdown_target(PRODUCT_THREAT_MODEL, target)

    assert resolved is not None
    assert not resolved.is_relative_to(PROJECT_ROOT.resolve())
