from __future__ import annotations

import base64
import os
from pathlib import Path

import pytest
from ebooklib import epub


# Rich wraps at the terminal width, so a long temporary path would split across lines. This
# runs before any test imports the CLI, because a Console reads COLUMNS when it is created.
os.environ["COLUMNS"] = "500"

MINIMAL_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII="
)


@pytest.fixture
def minimal_epub_path(tmp_path: Path) -> Path:
    path = tmp_path / "minimal.epub"
    book = epub.EpubBook()
    book.set_identifier("ayvu-minimal-test")
    book.set_title("Minimal Test Book")
    book.set_language("en")
    book.add_author("Ayvu Tests")

    chapter_one = epub.EpubHtml(title="Chapter One", file_name="text/chapter1.xhtml", lang="en")
    chapter_one.content = """
    <h1>Chapter One</h1>
    <p>Hello reader. Visit <a href="chapter2.xhtml#answer">chapter two</a>.</p>
    <p><img src="../images/pixel.png" alt="Pixel" /></p>
    """

    chapter_two = epub.EpubHtml(title="Chapter Two", file_name="text/chapter2.xhtml", lang="en")
    chapter_two.content = """
    <h1 id="answer">Chapter Two</h1>
    <p>Goodbye reader.</p>
    """

    image = epub.EpubItem(
        uid="pixel",
        file_name="images/pixel.png",
        media_type="image/png",
        content=MINIMAL_PNG,
    )

    book.add_item(chapter_one)
    book.add_item(chapter_two)
    book.add_item(image)
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.toc = (
        epub.Link("text/chapter1.xhtml", "Chapter One", "chapter-one"),
        epub.Link("text/chapter2.xhtml", "Chapter Two", "chapter-two"),
    )
    book.spine = ["nav", chapter_one, chapter_two]

    epub.write_epub(str(path), book)
    return path


# Stage 3 of the test stages (docs/release-workflow.md): pull requests on GitHub run only the
# tests marked `core`, to stay within the free Actions minutes. Each entry names an invariant
# from AGENTS.md and the test files that protect it, as paths under tests/; tests/test_ci_stages.py checks the map.
TESTS = Path(__file__).resolve().parent
CORE_TEST_FILES: dict[str, tuple[str, ...]] = {
    "original_epub_unchanged": ("test_workflow_characterization.py", "test_cli.py", "test_epub_io.py"),
    "zip_structure_and_mimetype": ("test_epub_archive.py", "test_epub_format.py", "test_validation.py"),
    "visible_text_and_tags": ("v1/test_html_translate.py", "test_html_structure.py"),
    "chunk_order_and_spacing": ("test_chunking.py",),
    "persisted_identities": ("test_persisted_identities.py",),
    "architecture_guardrails": ("test_size_ratchet.py", "test_import_contracts.py", "test_public_api.py"),
    "cache_keys_and_scope": (
        "v1/test_cache.py", "test_cache_identity.py", "test_translation_cache_v2.py", "test_cache_repository_v2.py",
    ),
    "resume": ("v1/test_resume.py", "v1/test_legacy_resume.py", "test_raw_results_resume.py"),
    "glossary": ("test_glossary.py", "test_glossary_catalog.py"),
    "cli_entry_points": ("test_cli.py", "test_cli_translate_project.py"),
    "local_only_network": ("test_network_policy.py",),
    "module_boundaries": ("test_architecture_boundaries.py",),
    "documentation_links": ("test_documentation_links.py",),
    "ci_stages": ("test_ci_stages.py",),
}
CORE_FILES = frozenset(name for names in CORE_TEST_FILES.values() for name in names)


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    for item in items:
        if item.path.relative_to(TESTS).as_posix() in CORE_FILES:
            item.add_marker(pytest.mark.core)
