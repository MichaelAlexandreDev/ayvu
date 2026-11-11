import json
from ayvu.interfaces.cli.guided import library as cli_module
from ayvu.interfaces.cli.app import app
from tests.support.cli_support import _library_config, isolated_config, runner


def test_a_folder_pointing_outside_the_library_is_told_and_not_taken_for_an_empty_library(isolated_config, tmp_path):
    books_dir = tmp_path / "Biblioteca"
    books_dir.mkdir()
    outside = tmp_path / "fora"
    outside.mkdir()
    (outside / "z.epub").write_bytes(b"x")
    (books_dir / "Original").symlink_to(outside)
    _library_config(isolated_config, books_dir)

    result = runner.invoke(app, [], input="3\n")

    assert result.exit_code == 0
    assert "Library warning (1)" in result.output
    assert "points outside" in result.output


def test_skipped_files_are_told_while_the_valid_books_stay_listed(isolated_config, tmp_path):
    books_dir = tmp_path / "Biblioteca"
    original = books_dir / "Original"
    original.mkdir(parents=True)
    (original / "valid.epub").write_bytes(b"x")
    (original / "a\tb.epub").write_bytes(b"x")
    outside = tmp_path / "z.epub"
    outside.write_bytes(b"x")
    (original / "link.epub").symlink_to(outside)
    _library_config(isolated_config, books_dir)

    result = runner.invoke(app, [], input="3\n0\n")

    assert result.exit_code == 0
    assert "valid" in result.output
    assert "Library warning (1):" in result.output and "not a file inside" in result.output
    assert "invalid characters" in result.output


def test_a_long_name_that_the_filesystem_accepts_stays_in_the_library(isolated_config, tmp_path):
    books_dir = tmp_path / "Biblioteca"
    original = books_dir / "Original"
    original.mkdir(parents=True)
    (original / ("L" * 240 + ".epub")).write_bytes(b"x")
    _library_config(isolated_config, books_dir)

    result = runner.invoke(app, [], input="3\n0\n")

    assert result.exit_code == 0
    assert "Library has no EPUB books yet." not in result.output
    assert "Library warning" not in result.output


def test_book_information_keeps_the_full_paths(isolated_config, tmp_path, monkeypatch):
    monkeypatch.setenv("COLUMNS", "300")
    books_dir = tmp_path / "Biblioteca"
    (books_dir / "Original").mkdir(parents=True)
    (books_dir / "Traduzidos").mkdir()
    (books_dir / "Original" / "book.epub").write_bytes(b"x")
    (books_dir / "Traduzidos" / "book-pt.epub").write_bytes(b"x")
    _library_config(isolated_config, books_dir)

    result = runner.invoke(app, [], input="3\n1\n3\n0\n")

    flat = "".join(result.output.split())
    assert result.exit_code == 0
    assert "".join(str(books_dir / "Original" / "book.epub").split()) in flat
    assert "".join(str(books_dir / "Traduzidos" / "book-pt.epub").split()) in flat


def test_a_book_replaced_after_the_listing_is_not_blamed_on_the_reader(isolated_config, tmp_path, monkeypatch):
    books_dir = tmp_path / "Biblioteca"
    original = books_dir / "Original"
    original.mkdir(parents=True)
    book = original / "book.epub"
    book.write_bytes(b"x")
    _library_config(isolated_config, books_dir, reader_app="reader")
    calls = []
    monkeypatch.setattr("ayvu.library.adapters.subprocess.Popen", lambda argv: calls.append(argv))
    real_book_actions = cli_module._library_book_actions

    def replace_then_list(listed):
        book.write_bytes(b"replaced")
        return real_book_actions(listed)

    monkeypatch.setattr(cli_module, "_library_book_actions", replace_then_list)

    result = runner.invoke(app, [], input="3\n1\n1\n")

    assert calls == []
    assert "Could not open EPUB" in result.output
    assert "Open the library again" in result.output
    assert "Configure Reader app" not in result.output


def test_root_command_shows_empty_guided_library(isolated_config, tmp_path):
    books_dir = tmp_path / "Biblioteca"
    isolated_config.write_text(
        json.dumps({"version": 1, "default_target_language": "pt", "books_dir": str(books_dir)}) + "\n",
        encoding="utf-8",
    )

    result = runner.invoke(app, [], input="3\n")

    assert result.exit_code == 0
    assert "Open library" in result.output
    assert "Library has no EPUB books yet." in result.output
    assert str(books_dir / "Original") in result.output.replace("\n", "")
    assert str(books_dir / "Traduzidos") in result.output.replace("\n", "")
    assert "Library warning" not in result.output


def test_root_command_opens_library_translation(isolated_config, tmp_path, monkeypatch):
    books_dir = tmp_path / "Biblioteca"
    original_dir = books_dir / "Original"
    translated_dir = books_dir / "Traduzidos"
    original_dir.mkdir(parents=True)
    translated_dir.mkdir()
    (original_dir / "Book.epub").write_bytes(b"")
    translated_path = translated_dir / "Book-pt.epub"
    translated_path.write_bytes(b"")
    isolated_config.write_text(
        json.dumps(
            {
                "version": 1,
                "default_target_language": "pt",
                "books_dir": str(books_dir),
                "reader_app": "foliate",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    opened: list[list[str]] = []
    from ayvu.library.adapters import ReaderLauncher
    monkeypatch.setattr("ayvu.composition.cli.ReaderLauncher", lambda reader_app: ReaderLauncher(reader_app, runner=opened.append))

    result = runner.invoke(app, [], input="3\n1\n2\n")

    assert result.exit_code == 0
    assert "Library" in result.output
    assert "Book" in result.output
    assert "Traduzido - Português" in result.output
    assert "Open Traduzido - Português" in result.output
    assert opened == [["foliate", str(translated_path)]]


def test_root_command_shows_library_book_information(isolated_config, tmp_path):
    books_dir = tmp_path / "Biblioteca"
    original_dir = books_dir / "Original"
    translated_dir = books_dir / "Traduzidos"
    original_dir.mkdir(parents=True)
    translated_dir.mkdir()
    original_path = original_dir / "Book.epub"
    translated_path = translated_dir / "Book-pt.epub"
    original_path.write_bytes(b"")
    translated_path.write_bytes(b"")
    isolated_config.write_text(
        json.dumps({"version": 1, "default_target_language": "pt", "books_dir": str(books_dir)}) + "\n",
        encoding="utf-8",
    )

    result = runner.invoke(app, [], input="3\n1\n3\n0\n")

    assert result.exit_code == 0
    assert "Book information" in result.output
    assert "Original EPUB" in result.output
    assert "Translations" in result.output
    assert "Traduzido - Português" in result.output
    assert "Translation files" in result.output


def test_root_command_reports_library_open_failure(isolated_config, tmp_path, monkeypatch):
    books_dir = tmp_path / "Biblioteca"
    original_dir = books_dir / "Original"
    original_dir.mkdir(parents=True)
    (original_dir / "Book.epub").write_bytes(b"")
    isolated_config.write_text(
        json.dumps({"version": 1, "default_target_language": "pt", "books_dir": str(books_dir)}) + "\n",
        encoding="utf-8",
    )

    from ayvu.library.adapters import ReaderLauncher
    monkeypatch.setattr("ayvu.composition.cli.ReaderLauncher", lambda reader_app: ReaderLauncher(reader_app, which=lambda _: None))

    result = runner.invoke(app, [], input="3\n1\n1\n")

    assert result.exit_code == 0
    assert "Could not open EPUB:" in result.output
    assert "No EPUB reader app" in result.output
    assert "Configure Reader app in Settings" in result.output
