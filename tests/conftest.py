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
    "original_epub_unchanged": (
        "test_workflow_characterization.py",
        "kernel/test_cli_output_plan.py",
        "kernel/test_cli_translation_options.py",
        "interfaces/cli/test_cli_root_processing.py",
        "interfaces/cli/test_cli_root_resume.py",
        "interfaces/cli/test_cli_translate_output_configuration.py",
        "interfaces/cli/test_cli_translate_review_export.py",
        "interfaces/cli/test_cli_translate_markdown_reports.py",
        "interfaces/cli/test_cli_translate_cache_controls.py",
        "interfaces/cli/test_cli_translate_language.py",
        "interfaces/cli/test_cli_translate_execution_profiles.py",
        "interfaces/cli/test_cli_translate_target_profiles.py",
        "interfaces/cli/test_cli_console_security.py",
        "formats/epub/test_epub_io_paths.py",
        "v1/test_epub_translation_values.py",
        "v1/test_epub_translation_execution.py",
        "v1/test_epub_translation_metadata.py",
        "formats/epub/test_epub_io_language.py",
        "v1/test_epub_translation_selection.py",
        "formats/epub/test_epub_io_chapter_selection.py",
        "kernel/test_epub_chapter_selection.py",
        "v1/test_epub_review_legacy_refusal.py",
        "v1/test_epub_translation_title.py",
    ),
    "zip_structure_and_mimetype": (
        "formats/epub/test_epub_archive_open.py",
        "formats/epub/test_epub_archive_copy.py",
        "formats/epub/test_epub_format_contract.py",
        "formats/epub/test_epub_format_detection.py",
        "formats/epub/test_epub_format_extraction.py",
        "formats/epub/test_epub_format_reconstruction.py",
        "formats/epub/test_epub_format_validation.py",
        "formats/epub/test_epub_format_publication.py",
        "formats/epub/test_validation.py",
    ),
    "visible_text_and_tags": (
        "v1/test_html_translate_markers.py",
        "v1/test_html_translate_structure.py",
        "v1/test_html_translate_cache_memory.py",
        "v1/test_html_translate_terms_alt.py",
        "v1/test_html_apply_reviewed.py",
        "v1/test_html_translate_markup_literals.py",
        "formats/epub/test_html_structure_extraction.py",
        "formats/epub/test_html_structure_reconstruction.py",
        "formats/epub/test_html_structure_transient.py",
    ),
    "chunk_order_and_spacing": ("test_chunking.py",),
    "persisted_identities": ("test_persisted_identities.py",),
    "architecture_guardrails": ("test_size_ratchet.py", "test_import_contracts.py", "test_public_api.py"),
    "cache_keys_and_scope": (
        "v1/test_cache.py",
        "knowledge/cache/test_cache_identity_atomic_entries.py",
        "knowledge/cache/test_cache_identity_canonical.py",
        "knowledge/cache/test_cache_identity_scope_resolution.py",
        "knowledge/cache/test_cache_repository_v2.py",
        "test_translation_cache_v2.py",
    ),
    "resume": ("v1/test_resume.py", "v1/test_legacy_resume.py", "test_raw_results_resume.py"),
    "glossary": (
        "knowledge/glossary/test_glossary.py",
        "knowledge/glossary/test_glossary_catalog_commands.py",
        "knowledge/glossary/test_glossary_catalog_publication.py",
    ),
    # Keep the approved CLI family explicit as other CLI tests move into this package.
    "cli_entry_points": (
        "kernel/test_cli_output_plan.py",
        "kernel/test_cli_translation_options.py",
        "interfaces/cli/test_cli_root_processing.py",
        "interfaces/cli/test_cli_root_resume.py",
        "interfaces/cli/test_cli_translate_output_configuration.py",
        "interfaces/cli/test_cli_translate_review_export.py",
        "interfaces/cli/test_cli_translate_markdown_reports.py",
        "interfaces/cli/test_cli_translate_cache_controls.py",
        "interfaces/cli/test_cli_translate_language.py",
        "interfaces/cli/test_cli_translate_execution_profiles.py",
        "interfaces/cli/test_cli_translate_target_profiles.py",
        "interfaces/cli/test_cli_console_security.py",
        "interfaces/cli/test_cli_project_translation.py",
        "interfaces/cli/test_cli_project_review_application.py",
        "interfaces/cli/test_cli_project_review_status.py",
        "interfaces/cli/test_cli_project_review_races.py",
        "interfaces/cli/test_cli_project_review_reconciliation.py",
        "interfaces/cli/test_cli_project_policy_recovery.py",
        "interfaces/cli/test_cli_project_failure_presentation.py",
        "interfaces/cli/guided/test_guided_glossaries.py",
        "interfaces/cli/guided/test_guided_language.py",
        "interfaces/cli/guided/test_guided_library.py",
        "interfaces/cli/guided/test_guided_menu.py",
        "interfaces/cli/guided/test_guided_preferences.py",
        "interfaces/cli/guided/test_guided_translation.py",
        "interfaces/cli/test_cli_batch.py",
        "interfaces/cli/test_cli_cache.py",
        "interfaces/cli/test_cli_content.py",
        "interfaces/cli/test_cli_output.py",
        "interfaces/cli/test_cli_plan.py",
        "interfaces/cli/test_cli_preview.py",
        "interfaces/cli/test_cli_report.py",
        "interfaces/cli/test_cli_resume.py",
        "interfaces/cli/test_cli_review.py",
        "interfaces/cli/test_cli_run.py",
        "interfaces/cli/test_cli_translate.py",
        "interfaces/cli/test_cli_translator.py",
    ),
    "local_only_network": ("network/test_network_policy.py",),
    "module_boundaries": ("test_architecture_boundaries.py",),
    "documentation_links": ("test_documentation_links.py",),
    "ci_stages": ("test_ci_stages.py",),
}
CORE_FILES = frozenset(name for names in CORE_TEST_FILES.values() for name in names)


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    for item in items:
        if item.path.relative_to(TESTS).as_posix() in CORE_FILES:
            item.add_marker(pytest.mark.core)
