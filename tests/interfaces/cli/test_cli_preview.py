"""Root previews execute the transient rich runtime over an instrumented transport."""
from ayvu.interfaces.cli.options import DEFAULT_PREVIEW_DOCUMENT_LIMIT
from ayvu.interfaces.cli.app import app
from tests.support.cli_support import isolated_config, runner
from tests.support.cli_rich_runtime_support import Transport, declare_model
from tests.support.cli_translate_project_support import _book
from tests.support.rich_preview_support import KEYS, members, preview_book


def test_preview_option_generates_preview_with_default_settings(isolated_config, tmp_path, monkeypatch):
    declare_model(isolated_config)
    epub_path = preview_book(tmp_path / "book.epub")
    preview_dir = tmp_path / "Preview"
    session = Transport()
    monkeypatch.setattr("ayvu.providers.libretranslate.client.requests.Session", lambda: session)
    monkeypatch.setattr("ayvu.interfaces.cli.output.default_preview_books_dir", lambda: preview_dir)

    result = runner.invoke(app, ["--preview", str(epub_path)])

    assert result.exit_code == 0, result.output
    assert "Preview output folder:" in result.output
    assert str(preview_dir) in result.output and "book-preview.epub" in result.output
    assert "Preview salvo em:" in result.output
    assert (preview_dir / "book-preview.epub").is_file()
    assert len(session.posts) == DEFAULT_PREVIEW_DOCUMENT_LIMIT
    assert members(preview_dir / "book-preview.epub")[KEYS[DEFAULT_PREVIEW_DOCUMENT_LIMIT]] == members(epub_path)[KEYS[DEFAULT_PREVIEW_DOCUMENT_LIMIT]]
    assert session.requests[0] == ("GET", "http://localhost:5000/languages", None)
    assert all(payload["source"] == "en" and payload["target"] == "pt"
               for method, _, payload in session.requests if method == "POST")
    assert not (tmp_path / "Processando").exists()
    assert not (tmp_path / ".cache" / "traducoes.sqlite.v2.sqlite").exists()


def test_preview_option_uses_configured_preview_dir(isolated_config, tmp_path, monkeypatch):
    books_dir = tmp_path / "Biblioteca"
    declare_model(isolated_config, books_dir=str(books_dir), folders={"preview": "Amostras"})
    epub_path = _book(tmp_path / "book.epub")
    session = Transport()
    monkeypatch.setattr("ayvu.providers.libretranslate.client.requests.Session", lambda: session)

    result = runner.invoke(app, ["--preview", str(epub_path)])

    output_path = books_dir / "Amostras" / "book-preview.epub"
    assert result.exit_code == 0, result.output
    assert str(output_path.parent) in result.output.replace("\n", "")
    assert output_path.is_file() and session.posts
