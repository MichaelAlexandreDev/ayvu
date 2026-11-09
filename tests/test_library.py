"""Regression coverage for guided-library grouping and reader behavior."""
from ayvu.library_adapters import LocalLibraryFilesystem, ReaderLauncher
from ayvu.library_service import LibraryService


def test_scan_library_groups_originals_and_translations_by_book(tmp_path):
    original = tmp_path / 'Original'
    translated = tmp_path / 'Traduzidos'
    original.mkdir()
    translated.mkdir()
    for name in ('Beta.epub', 'Alpha.epub'):
        (original / name).write_bytes(b'')
    for name in ('Alpha-pt.epub', 'Beta-es.epub', 'Gamma-pt-BR.epub', 'ignore.txt'):
        (translated / name).write_bytes(b'')
    fs = LocalLibraryFilesystem(tmp_path)
    books = LibraryService(fs, ReaderLauncher(), fs).scan().books
    assert [book.name for book in books] == ['Alpha', 'Beta', 'Gamma']
    assert books[0].has_original and not books[2].has_original
    assert [book.translations[0].label for book in books] == [
        'Traduzido - Português', 'Traduzido - Espanhol', 'Traduzido - Português (Brasil)']


def test_scan_library_returns_empty_when_configured_dirs_do_not_exist(tmp_path):
    fs = LocalLibraryFilesystem(tmp_path / 'missing')
    assert LibraryService(fs, ReaderLauncher(), fs).scan().books == ()


def test_reader_uses_configured_flags_and_detected_system_reader():
    calls = []
    assert ReaderLauncher('foliate --new-window', runner=calls.append).launch('/book.epub', lambda: False, lambda: True) == 'opened'
    assert ReaderLauncher(runner=calls.append, which=lambda n: n if n == 'xdg-open' else None,
                          platform='linux').launch('/book.epub', lambda: False, lambda: True) == 'opened'
    assert calls == [['foliate', '--new-window', '/book.epub'], ['xdg-open', '/book.epub']]
