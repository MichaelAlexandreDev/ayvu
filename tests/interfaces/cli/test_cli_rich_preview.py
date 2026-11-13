"""Root and guided rich previews publish only a validated EPUB."""
from pathlib import Path
from zipfile import ZipFile

import pytest

from ayvu.interfaces.cli.app import app
from ayvu.interfaces.cli import preview
from ayvu.composition.builders import translate_transient
from ayvu.formats.epub.validation import validate_output_epub
from ayvu.preferences import AyvuConfig
from tests.support.cli_rich_runtime_support import config, isolate, runner, text
from tests.support.rich_preview_support import PreviewTransport, preview_book, preview_guard, members, KEYS


@pytest.mark.parametrize("guided", [False, True])
@pytest.mark.parametrize("folder", ["Samples", "Preview"])
def test_root_and_guided_use_configured_preview_folder(tmp_path, monkeypatch, guided, folder):
    config(tmp_path, folders={"preview": folder})
    source = preview_book(tmp_path / "book.epub")
    destination = tmp_path / "books" / folder / "book-preview.epub"
    session = PreviewTransport()
    monkeypatch.setattr("requests.Session", lambda: session)
    with preview_guard(monkeypatch, tmp_path, destination) as sql:
        result = runner.invoke(app, [] if guided else ["--mode", "developer", "--preview", str(source)],
                               input=f"2\n{source}\n1\n" if guided else None)
    assert result.exit_code == 0, text(result)
    assert "Preview salvo em:" in text(result) and "Validação OK:" in text(result)
    assert validate_output_epub(destination).ok and len(session.posts) == 12
    assert members(destination)[KEYS[12]] == members(source)[KEYS[12]]
    assert not sql and session.closed == 1


@pytest.mark.parametrize("accept", [False, True])
def test_overwrite_confirmation_keeps_or_replaces_the_existing_file(tmp_path, monkeypatch, accept):
    config(tmp_path)
    source = preview_book(tmp_path / "book.epub")
    destination = tmp_path / "books" / "Preview" / "book-preview.epub"
    destination.parent.mkdir(parents=True)
    destination.write_bytes(b"previous preview")
    session = PreviewTransport()
    monkeypatch.setattr("requests.Session", lambda: session)
    with preview_guard(monkeypatch, tmp_path, destination, published=accept):
        result = runner.invoke(app, ["--mode", "common", "--preview", str(source)], input="y\n" if accept else "n\n")
    assert result.exit_code == (0 if accept else 1), text(result)
    assert "Overwrite existing preview EPUB?" in result.output
    if accept:
        assert validate_output_epub(destination).ok and session.posts
    else:
        assert destination.read_bytes() == b"previous preview" and session.requests == []


def test_developer_overwrite_is_refused_before_provider_preparation(tmp_path, monkeypatch):
    config(tmp_path)
    source = preview_book(tmp_path / "book.epub")
    destination = tmp_path / "books" / "Preview" / "book-preview.epub"
    destination.parent.mkdir(parents=True)
    destination.write_bytes(b"previous preview")
    session = PreviewTransport()
    monkeypatch.setattr("requests.Session", lambda: session)
    with preview_guard(monkeypatch, tmp_path, destination, published=False):
        result = runner.invoke(app, ["--mode", "developer", "--preview", str(source)])
    assert result.exit_code == 1 and "existing preview was not changed" in text(result)
    assert not session.requests


@pytest.mark.parametrize("existing", [False, True])
def test_provider_failure_publishes_no_partial_epub(tmp_path, monkeypatch, existing):
    config(tmp_path)
    source = preview_book(tmp_path / "book.epub")
    destination = tmp_path / "books" / "Preview" / "book-preview.epub"
    if existing:
        destination.parent.mkdir(parents=True)
        destination.write_bytes(b"previous preview")
    session = PreviewTransport(failing="Document 01.")
    monkeypatch.setattr("requests.Session", lambda: session)
    with preview_guard(monkeypatch, tmp_path, destination, published=False):
        result = runner.invoke(app, ["--mode", "common", "--preview", str(source)], input="y\n")
    assert result.exit_code == 1, text(result)
    assert session.posts[0] == "Document 07." and session.closed == 1
    assert not destination.exists() or destination.read_bytes() == b"previous preview"


def test_keyboard_cancellation_keeps_destination_and_creates_no_directories(tmp_path, monkeypatch):
    config(tmp_path)
    source = preview_book(tmp_path / "book.epub")
    destination = tmp_path / "books" / "Preview" / "book-preview.epub"
    def interrupt(_):
        raise KeyboardInterrupt()
    session = PreviewTransport(on_post=interrupt)
    monkeypatch.setattr("requests.Session", lambda: session)
    with preview_guard(monkeypatch, tmp_path, destination, published=False):
        result = runner.invoke(app, ["--mode", "developer", "--preview", str(source)])
    assert result.exit_code == 130 and session.closed == 1
    assert not destination.parent.exists()


def test_label_selects_transient_before_v1_preflight_or_cache(tmp_path, monkeypatch):
    config(tmp_path)
    source = preview_book(tmp_path / "book.epub", repeated=True)
    destination = tmp_path / "books" / "Preview" / "book-preview.epub"
    selected = []
    original = translate_transient.translate
    def translate(book, hooks, label, **kwargs):
        selected.append((label, book.preview, book.translator_name, book.translator_models))
        return original(book, hooks, label, **kwargs)
    monkeypatch.setattr(translate_transient, "translate", translate)
    monkeypatch.setattr("ayvu.interfaces.cli.run.run_translation_preflight", lambda **_: pytest.fail("v1 preflight"))
    session = PreviewTransport()
    monkeypatch.setattr("requests.Session", lambda: session)
    with preview_guard(monkeypatch, tmp_path, destination):
        result = runner.invoke(app, ["--mode", "developer", "--preview", str(source)])
    assert result.exit_code == 0, text(result)
    assert selected == [("local-model-2026", True, "libretranslate", {"libretranslate": "local-model-2026"})]
    assert session.posts == ["Repeated reader."]


def test_internal_preview_dir_property_is_respected(tmp_path, monkeypatch):
    source = preview_book(tmp_path / "book.epub")
    configured = AyvuConfig(books_dir=tmp_path / "library", translator_models={"libretranslate": "local-model-2026"})
    session = PreviewTransport()
    monkeypatch.setattr("requests.Session", lambda: session)
    destination = configured.preview_dir / "book-preview.epub"
    with preview_guard(monkeypatch, tmp_path, destination):
        preview._run_preview(source, config=configured)
    assert validate_output_epub(destination).ok


@pytest.mark.parametrize("cancelled", [False, True])
def test_discovery_failure_closes_session_without_creating_output(tmp_path, monkeypatch, cancelled):
    config(tmp_path)
    source = preview_book(tmp_path / "book.epub")
    destination = tmp_path / "books" / "Preview" / "book-preview.epub"
    class Unavailable(PreviewTransport):
        def get(self, *args, **kwargs):
            if cancelled:
                raise KeyboardInterrupt()
            return self._response({"error": "unavailable"}, 500)
    session = Unavailable()
    monkeypatch.setattr("requests.Session", lambda: session)
    with preview_guard(monkeypatch, tmp_path, destination, published=False):
        result = runner.invoke(app, ["--mode", "developer", "--preview", str(source)])
    assert result.exit_code == (130 if cancelled else 1) and session.closed == 1
    assert session.posts == []


def test_validation_warnings_keep_the_epub_and_report_with_exit_one(tmp_path, monkeypatch):
    config(tmp_path)
    source = preview_book(tmp_path / "book.epub")
    original = members(source)
    original[KEYS[0]] = original[KEYS[0]].replace(b'</p>', b' <img src="missing.png"/></p>')
    with ZipFile(source, "w") as archive:
        for name, value in original.items():
            archive.writestr(name, value)
    destination = tmp_path / "books" / "Preview" / "book-preview.epub"
    session = PreviewTransport()
    monkeypatch.setattr("requests.Session", lambda: session)
    with preview_guard(monkeypatch, tmp_path, destination):
        result = runner.invoke(app, ["--mode", "developer", "--preview", str(source)])
    assert result.exit_code == 1, text(result)
    assert destination.is_file() and not validate_output_epub(destination).ok
    assert "Translation report" in text(result) and "Validation warnings" in text(result)
    assert "missing.png" in text(result) and "Preview salvo em:" not in text(result)
