from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import unquote, urlsplit

import pytest


from tests.support.paths import PROJECT_ROOT
PRODUCT_SCOPE = PROJECT_ROOT / "docs" / "product-scope.md"
PRODUCT_THREAT_MODEL = PROJECT_ROOT / "docs" / "architecture" / "product-threat-model.md"
MODULE_BOUNDARIES = PROJECT_ROOT / "docs" / "architecture" / "modular-monolith-boundaries.md"
GLOBAL_PREFERENCES = PROJECT_ROOT / "docs" / "architecture" / "modules" / "preferences.md"
LOCAL_STORAGE = PROJECT_ROOT / "docs" / "architecture" / "decisions" / "local-project-storage.md"
PROJECT_MIGRATIONS = PROJECT_ROOT / "docs" / "architecture" / "modules" / "projects-storage.md"
JOB_LIFECYCLE = PROJECT_ROOT / "docs" / "architecture" / "decisions" / "job-lifecycle.md"
JOB_EVENTS = PROJECT_ROOT / "docs" / "architecture" / "modules" / "jobs.md"
JOB_ARTIFACTS = PROJECT_ROOT / "docs" / "architecture" / "decisions" / "job-artifacts.md"
PROVENANCE = PROJECT_ROOT / "docs" / "architecture" / "decisions" / "provenance.md"
CONTENT_MODEL = PROJECT_ROOT / "docs" / "architecture" / "decisions" / "content-model.md"
PROVIDER_CONTRACTS = PROJECT_ROOT / "docs" / "architecture" / "decisions" / "provider-contracts.md"
PROVIDER_CAPABILITIES = PROJECT_ROOT / "docs" / "architecture" / "decisions" / "provider-capabilities.md"
TRANSLATION_PIPELINE = PROJECT_ROOT / "docs" / "architecture" / "translation-pipeline.md"
PROVIDER_DISCOVERY = PROJECT_ROOT / "docs" / "architecture" / "modules" / "providers.md"
FORMAT_ADAPTER = PROJECT_ROOT / "docs" / "architecture" / "decisions" / "format-adapter.md"
HTML_STRUCTURE = PROJECT_ROOT / "docs" / "architecture" / "decisions" / "html-structure.md"
EPUB_FORMAT = PROJECT_ROOT / "docs" / "architecture" / "decisions" / "epub-format.md"
LOCAL_WORKER = PROJECT_ROOT / "docs" / "architecture" / "adr-local-worker-isolation.md"
AI_ASSISTANCE = PROJECT_ROOT / "docs" / "architecture" / "decisions" / "ai-assistance-authority.md"
REFACTOR_BASELINE = PROJECT_ROOT / "docs" / "architecture" / "refactor-baseline.md"
PACKAGE_STRUCTURE = PROJECT_ROOT / "docs" / "architecture" / "decisions" / "package-structure.md"
V1_RETIREMENT = PROJECT_ROOT / "docs" / "architecture" / "decisions" / "v1-retirement.md"
WHERE_CODE_GOES = PROJECT_ROOT / "docs" / "architecture" / "where-code-goes.md"
WORKER_SPIKE = PROJECT_ROOT / "spikes" / "local-worker" / "README.md"
FORMAT_COMPATIBILITY = PROJECT_ROOT / "docs" / "architecture" / "modules" / "formats-compatibility.md"
MARKDOWN_LINK = re.compile(r"\[[^\]]+\]\(([^)]+)\)")
C1_DOCUMENTS = tuple(
    PROJECT_ROOT / "docs" / "architecture" / "modules" / name
    for name in (
        "preferences.md", "knowledge-glossary.md", "knowledge-cache.md",
        "knowledge-memory.md", "library.md", "review.md", "projects-profiles.md",
    )
)
C1_ENTRY_DOCUMENTS = (PROJECT_ROOT / "README.md", PROJECT_ROOT / "docs" / "tutorial-modo-comum-e-dev.md")

C2_DOCUMENTS = tuple(
    PROJECT_ROOT / "docs" / "architecture" / directory / name
    for directory, name in (
        ("modules", "projects.md"),
        ("modules", "projects-storage.md"),
        ("modules", "jobs.md"),
        ("modules", "artifacts.md"),
        ("modules", "provenance.md"),
        ("modules", "recovery.md"),
        ("decisions", "project-lifecycle.md"),
    )
)

C3_CONTRACTS = tuple(
    PROJECT_ROOT / "docs" / "architecture" / "modules" / name
    for name in ("content.md", "content-use-cases.md", "formats.md", "formats-epub.md", "formats-html.md")
)
C3_DOCUMENTS = (
    *C3_CONTRACTS, FORMAT_COMPATIBILITY, PROVIDER_DISCOVERY, CONTENT_MODEL,
    FORMAT_ADAPTER, EPUB_FORMAT, HTML_STRUCTURE, PROVIDER_CONTRACTS, PROVIDER_CAPABILITIES,
)


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
    [PRODUCT_SCOPE, PRODUCT_THREAT_MODEL, MODULE_BOUNDARIES, GLOBAL_PREFERENCES, LOCAL_STORAGE, PROJECT_MIGRATIONS, JOB_LIFECYCLE, JOB_EVENTS, JOB_ARTIFACTS, PROVENANCE, CONTENT_MODEL, PROVIDER_CONTRACTS, PROVIDER_CAPABILITIES, TRANSLATION_PIPELINE, PROVIDER_DISCOVERY, FORMAT_ADAPTER, FORMAT_COMPATIBILITY, HTML_STRUCTURE, EPUB_FORMAT, LOCAL_WORKER, WORKER_SPIKE, AI_ASSISTANCE, REFACTOR_BASELINE, PACKAGE_STRUCTURE, V1_RETIREMENT, WHERE_CODE_GOES, *C1_DOCUMENTS[1:], *C1_ENTRY_DOCUMENTS, *C2_DOCUMENTS, *C3_CONTRACTS],
    ids=["product-scope", "product-threat-model", "module-boundaries", "preferences", "local-storage", "project-migrations", "job-lifecycle", "job-events", "job-artifacts", "provenance", "content-model", "provider-contracts", "provider-capabilities", "translation-pipeline", "provider-discovery", "format-adapter", "format-compatibility", "html-structure", "epub-format", "local-worker", "worker-spike", "ai-assistance-authority", "refactor-baseline", "package-structure", "v1-retirement", "where-code-goes", *(path.stem for path in C1_DOCUMENTS[1:]), "readme", "tutorial", *(path.relative_to(PROJECT_ROOT).as_posix() for path in C2_DOCUMENTS), *(path.relative_to(PROJECT_ROOT).as_posix() for path in C3_CONTRACTS)],
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


@pytest.mark.parametrize("document", (*C1_DOCUMENTS, GLOBAL_PREFERENCES.with_name("preferences-fields.json")))
def test_c1_module_documents_fit_the_line_budget(document: Path) -> None:
    assert len(document.read_text(encoding="utf-8").splitlines()) <= 400


@pytest.mark.parametrize("document", C2_DOCUMENTS)
def test_c2_module_documents_fit_the_line_budget(document: Path) -> None:
    assert len(document.read_text(encoding="utf-8").splitlines()) <= 400


@pytest.mark.parametrize("document", C3_DOCUMENTS)
def test_c3_documents_fit_the_line_budget(document: Path) -> None:
    assert len(document.read_text(encoding="utf-8").splitlines()) <= 400


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
