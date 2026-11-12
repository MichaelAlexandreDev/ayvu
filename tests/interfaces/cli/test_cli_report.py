from tests.support.cli_support import isolated_config
import json
from pathlib import Path
from ayvu.interfaces.cli.report import _offer_markdown_report, _render_markdown_report, _save_markdown_report
from ayvu.v1.epub import TranslationReport
from ayvu.knowledge.glossary.rules import GlossaryUsage
from ayvu.kernel import UserMode


def test_render_markdown_report_includes_memory_rows():
    report = TranslationReport(
        chapters_processed=1,
        texts_translated=2,
        texts_from_cache=1,
        texts_from_memory=3,
        memory_suggestions=4,
        output_path=Path("books/book-pt.epub"),
        input_path=Path("books/book.epub"),
        detected_language="en",
        target_language="pt",
    )

    markdown = _render_markdown_report(report, dry_run=False)

    assert "- Texts from memory: 3" in markdown
    assert "- Memory suggestions: 4" in markdown


def test_render_markdown_report_includes_translation_context():
    glossary_usage = GlossaryUsage(
        applied_terms={"Game Loop": 2, "Observer": 1},
        required_terms_missing=["Object Pool"],
        forbidden_terms_found={"AntiPattern": 1},
    )
    report = TranslationReport(
        chapters_processed=2,
        texts_translated=3,
        texts_from_cache=1,
        errors=["chapter.xhtml: failed\nwhile translating"],
        glossary_terms_configured=4,
        glossary_usage=glossary_usage,
        output_path=Path("books/book-pt.epub"),
        input_path=Path("books/book.epub"),
        detected_language="en",
        target_language="pt",
    )

    markdown = _render_markdown_report(report, dry_run=False)

    assert "# Translation report" in markdown
    assert "- Original EPUB: books/book.epub" in markdown
    assert "- Detected language: en" in markdown
    assert "- Translated language: pt" in markdown
    assert "- Output: books/book-pt.epub" in markdown
    assert "- Chapters processed: 2" in markdown
    assert "- Texts translated: 3" in markdown
    assert "- Texts from cache: 1" in markdown
    assert "- Errors: 1" in markdown
    assert "- Glossary terms configured: 4" in markdown
    assert "- Glossary terms applied: 3" in markdown
    assert "- Required glossary terms missing: 1" in markdown
    assert "- Forbidden glossary terms found: 1" in markdown
    assert "## Glossary usage" in markdown
    assert "- Game Loop: 2" in markdown
    assert "- Observer: 1" in markdown
    assert "## Glossary warnings" in markdown
    assert "- Required terms missing: Object Pool" in markdown
    assert "- Forbidden terms found: AntiPattern (1)" in markdown
    assert "- chapter.xhtml: failed while translating" in markdown


def test_save_markdown_report_uses_default_reports_dir_without_overwriting(tmp_path, monkeypatch):
    reports_dir = tmp_path / "reports"
    report = TranslationReport(
        output_path=Path("book-pt.epub"),
        input_path=Path("book.epub"),
        target_language="pt",
    )
    monkeypatch.setattr("ayvu.interfaces.cli.report._default_reports_dir", lambda: reports_dir)

    first_path = _save_markdown_report(report, dry_run=False)
    second_path = _save_markdown_report(report, dry_run=False)

    assert first_path == reports_dir / "book-pt-report.md"
    assert second_path == reports_dir / "book-pt-report-2.md"
    assert first_path.read_text(encoding="utf-8").startswith("# Translation report")
    assert second_path.read_text(encoding="utf-8").startswith("# Translation report")


def test_save_markdown_report_uses_configured_reports_dir(isolated_config, tmp_path):
    books_dir = tmp_path / "Biblioteca"
    isolated_config.write_text(
        json.dumps(
            {
                "version": 1,
                "default_target_language": "pt",
                "books_dir": str(books_dir),
                "folders": {"reports": "Relatorios-MD"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    report = TranslationReport(
        output_path=Path("book-pt.epub"),
        input_path=Path("book.epub"),
        target_language="pt",
    )

    report_path = _save_markdown_report(report, dry_run=False)

    assert report_path == books_dir / "Relatorios-MD" / "book-pt-report.md"
    assert report_path.read_text(encoding="utf-8").startswith("# Translation report")


def test_offer_markdown_report_does_not_save_when_declined(monkeypatch):
    saved = False

    def fake_save_report(_report: TranslationReport, _dry_run: bool) -> Path:
        nonlocal saved
        saved = True
        return Path("report.md")

    monkeypatch.setattr("ayvu.interfaces.cli.report.typer.confirm", lambda *_args, **_kwargs: False)
    monkeypatch.setattr("ayvu.interfaces.cli.report._save_markdown_report", fake_save_report)

    _offer_markdown_report(TranslationReport(), dry_run=False, mode=UserMode.COMMON)

    assert not saved
