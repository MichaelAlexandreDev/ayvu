from tests.support.cli_rich_runtime_support import declare_model
from ebooklib import epub
from ayvu.interfaces.cli.app import app
from tests.support.cli_support import _mock_translation_pipeline
from tests.support.cli_support import isolated_config, runner


def test_translate_auto_detects_source_language_from_epub(minimal_epub_path, tmp_path, monkeypatch, isolated_config):
    monkeypatch.setattr("ayvu.interfaces.cli.output.default_translated_books_dir", lambda: tmp_path / "Traduzidos")
    calls: dict[str, object] = {}
    _mock_translation_pipeline(monkeypatch, calls)

    declare_model(isolated_config)
    result = runner.invoke(app, ["--mode", "developer", "translate", str(minimal_epub_path)])

    assert result.exit_code == 0
    assert calls["options"].source == "en"
    assert "Translation plan" in result.output
    assert "inferido do EPUB" in result.output


def test_translate_explicit_source_overrides_epub_metadata(minimal_epub_path, tmp_path, monkeypatch, isolated_config):
    monkeypatch.setattr("ayvu.interfaces.cli.output.default_translated_books_dir", lambda: tmp_path / "Traduzidos")
    calls: dict[str, object] = {}
    _mock_translation_pipeline(monkeypatch, calls)

    declare_model(isolated_config)
    result = runner.invoke(
        app,
        ["--mode", "developer", "translate", str(minimal_epub_path), "--source", "fr"],
    )

    assert result.exit_code == 0
    assert calls["options"].source == "fr"
    assert "inferido do EPUB" not in result.output


def test_translate_metadata_flag_reaches_translation_options(minimal_epub_path, tmp_path, monkeypatch, isolated_config):
    monkeypatch.setattr("ayvu.interfaces.cli.output.default_translated_books_dir", lambda: tmp_path / "Traduzidos")
    calls: dict[str, object] = {}
    _mock_translation_pipeline(monkeypatch, calls)

    declare_model(isolated_config)
    result = runner.invoke(
        app,
        ["--mode", "developer", "translate", str(minimal_epub_path), "--translate-metadata"],
    )

    assert result.exit_code == 0
    assert calls["options"].translate_metadata


def test_translate_alt_text_flag_reaches_translation_options(minimal_epub_path, tmp_path, monkeypatch, isolated_config):
    monkeypatch.setattr("ayvu.interfaces.cli.output.default_translated_books_dir", lambda: tmp_path / "Traduzidos")
    calls: dict[str, object] = {}
    _mock_translation_pipeline(monkeypatch, calls)

    declare_model(isolated_config)
    result = runner.invoke(
        app,
        ["--mode", "developer", "translate", str(minimal_epub_path), "--translate-alt-text"],
    )

    assert result.exit_code == 0
    assert calls["options"].translate_alt_text


def test_translate_chapters_option_reaches_translation_options_and_prints_selection(
    minimal_epub_path, tmp_path, monkeypatch, isolated_config
):
    monkeypatch.setattr("ayvu.interfaces.cli.output.default_translated_books_dir", lambda: tmp_path / "Traduzidos")
    calls: dict[str, object] = {}
    _mock_translation_pipeline(monkeypatch, calls)

    declare_model(isolated_config)
    result = runner.invoke(
        app,
        ["--mode", "developer", "translate", str(minimal_epub_path), "--chapters", "2"],
    )

    options = calls["options"]
    assert result.exit_code == 0
    assert options.chapter_selection is not None
    assert options.chapter_selection.source == "2"
    assert "Selected chapters" in result.output
    assert "Chapter Two" in result.output
    assert "text/chapter2.xhtml" in result.output


def test_translate_warns_when_epub_language_metadata_is_missing(tmp_path, monkeypatch, isolated_config):
    epub_path = tmp_path / "no-lang.epub"
    book = epub.EpubBook()
    book.set_identifier("ayvu-no-lang")
    book.set_title("No Language")
    book.add_author("Ayvu Tests")
    chapter = epub.EpubHtml(title="Ch", file_name="text/ch.xhtml")
    chapter.content = "<h1>Title</h1><p>Body.</p>"
    book.add_item(chapter)
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.spine = ["nav", chapter]
    epub.write_epub(str(epub_path), book)

    monkeypatch.setattr("ayvu.interfaces.cli.output.default_translated_books_dir", lambda: tmp_path / "Traduzidos")
    calls: dict[str, object] = {}
    _mock_translation_pipeline(monkeypatch, calls)

    declare_model(isolated_config)
    result = runner.invoke(app, ["--mode", "developer", "translate", str(epub_path)])

    assert result.exit_code == 0
    assert "Idioma do EPUB ausente ou inválido" in result.output
    assert calls["options"].source == "en"


def test_translate_common_mode_shows_detected_source_hint(minimal_epub_path, tmp_path, monkeypatch, isolated_config):
    monkeypatch.setattr("ayvu.interfaces.cli.output.default_translated_books_dir", lambda: tmp_path / "Traduzidos")
    calls: dict[str, object] = {}
    _mock_translation_pipeline(monkeypatch, calls)

    declare_model(isolated_config)
    result = runner.invoke(
        app,
        ["--mode", "common", "translate", str(minimal_epub_path)],
        input="y\n",
    )

    assert result.exit_code == 0
    assert "Translation plan" in result.output
    assert "Idioma de origem detectado do EPUB: en" in result.output
