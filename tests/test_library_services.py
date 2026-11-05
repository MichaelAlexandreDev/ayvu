"""Contract tests for the framework-neutral library service."""
from ayvu.library_service import Candidate, LibraryService, MAX_ENTRIES, MAX_WARNINGS, Warning


class Filesystem:
    def __init__(self, candidates=(), warnings=()):
        from uuid import uuid4
        self.root_ref = uuid4().hex
        self.candidates = list(candidates)
        self.warnings = list(warnings)
        self.authorizations = []
        self.paths = {}

    def discover(self, cancelled, limit):
        result = []
        for candidate in self.candidates[:limit]:
            if cancelled():
                return result, self.warnings, True
            result.append(candidate)
        return result, self.warnings, False

    def authorize(self, token):
        self.authorizations.append(token)
        return self.paths.get(token)


class Launcher:
    def __init__(self):
        self.calls = []

    def launch(self, path, cancelled, revalidate):
        if not revalidate():
            return "unauthorized"
        self.calls.append(path)
        return 'opened'


def test_discovery_and_authorization_are_separate_injected_ports():
    discovery = Filesystem([Candidate('Book', 'original', 1)])
    authorization = Filesystem()
    authorization.paths[1] = '/authorized/Book.epub'
    launcher = Launcher()
    service = LibraryService(discovery, launcher, authorization)
    ref = service.scan().books[0].original.ref
    assert service.open(ref).status == 'opened'
    assert discovery.authorizations == []
    assert authorization.authorizations == [1, 1]


def test_groups_translations_and_orders_independent_of_input_order():
    candidates = [Candidate('Gamma-pt-BR', 'translation', 1), Candidate('Beta-es', 'translation', 2),
                  Candidate('Alpha-pt', 'translation', 3), Candidate('Beta', 'original', 4),
                  Candidate('Alpha', 'original', 5), Candidate('Alpha-Long', 'original', 6),
                  Candidate('Alpha-Long-fr', 'translation', 7)]
    fs = Filesystem(candidates)
    service = LibraryService(fs, Launcher(), fs)
    result = service.scan()
    assert [book.name for book in result.books] == ['Alpha', 'Alpha-Long', 'Beta', 'Gamma']
    assert [book.translations[0].label for book in result.books] == [
        'Traduzido - Português', 'Traduzido - Francês', 'Traduzido - Espanhol', 'Traduzido - Português (Brasil)']
    assert result.books[0].has_original and not result.books[-1].has_original
    assert service.describe(result.books[0].ref) == result.books[0]


def test_translation_prefers_original_over_ambiguous_language_suffix():
    fs = Filesystem([Candidate('Alpha-pt', 'original', 1),
                     Candidate('Alpha-pt-BR', 'translation', 2)])
    books = LibraryService(fs, Launcher(), fs).scan().books
    assert [book.name for book in books] == ['Alpha-pt']
    assert books[0].translations[0].language == 'BR'


def test_language_display_strips_whitespace_like_the_guided_library():
    fs = Filesystem([Candidate('Book-pt ', 'translation', 1)])
    book = LibraryService(fs, Launcher(), fs).scan().books[0]
    assert book.translations[0].label == 'Traduzido - Português'


def test_refs_are_opaque_and_a_new_scan_invalidates_old_refs():
    fs = Filesystem([Candidate('Book', 'original', 'secret-token')])
    fs.paths['secret-token'] = '/private/Book.epub'
    launcher = Launcher()
    service = LibraryService(fs, launcher, fs)
    first = service.scan().books[0]
    assert 'secret-token' not in repr(first) and '/private/' not in repr(first)
    assert service.open(first.original.ref).display_path is None
    assert '/private/' not in repr(service.open(first.original.ref))
    second = service.scan().books[0]
    assert second.ref != first.ref and second.original.ref != first.original.ref
    assert service.describe(first.ref) is None
    assert service.open(first.original.ref).status == 'stale'
    assert len(launcher.calls) == 2


def test_failed_authorization_and_cancellation_never_launch():
    fs = Filesystem([Candidate('Book', 'original', 1)])
    launcher = Launcher()
    service = LibraryService(fs, launcher, fs)
    ref = service.scan().books[0].original.ref
    assert service.open(ref).status == 'unauthorized'
    assert service.open(ref, lambda: True).status == 'cancelled'
    assert fs.authorizations == [1]
    assert launcher.calls == []


def test_cancellation_stops_discovery_between_candidates():
    fs = Filesystem([Candidate(str(i), 'original', i) for i in range(4)])
    service = LibraryService(fs, Launcher(), fs)
    count = [0]
    def cancelled():
        count[0] += 1
        return count[0] >= 3
    result = service.scan(cancelled)
    assert result.cancelled and count[0] >= 3
    assert len(result.books) < 4


def test_limits_and_invalid_names_yield_bounded_results():
    fs = Filesystem([Candidate('valid', 'original', 1), Candidate('bad\nname', 'original', 2)] +
                    [Candidate(str(i), 'original', i + 3) for i in range(MAX_ENTRIES)],
                    [Warning('root', 'stat_failed')] * (MAX_WARNINGS + 1))
    result = LibraryService(fs, Launcher(), fs).scan()
    assert len(result.books) == MAX_ENTRIES - 1
    assert len(result.warnings) == MAX_WARNINGS
    assert all('\n' not in book.name for book in result.books)


def test_invalid_name_warning_does_not_discard_valid_entries():
    fs = Filesystem([Candidate('ok', 'original', 1), Candidate('\udcff', 'original', 2)])
    result = LibraryService(fs, Launcher(), fs).scan()
    assert [book.name for book in result.books] == ['ok']
    assert [warning.reason for warning in result.warnings] == ['invalid_filename']
    assert '\udcff' not in repr(result)


def make_local(tmp_path, *, reader='reader --new-window'):
    from ayvu.library_adapters import LocalLibraryFilesystem, ReaderLauncher
    root = tmp_path / 'library'
    (root / 'Original').mkdir(parents=True)
    (root / 'Traduzidos').mkdir()
    calls = []
    fs = LocalLibraryFilesystem(root)
    service = LibraryService(fs, ReaderLauncher(reader, runner=calls.append), fs)
    return root, service, calls


def test_real_scanner_uses_only_filenames_and_open_passes_hostile_path_as_one_argument(tmp_path, monkeypatch):
    from pathlib import Path
    root, service, calls = make_local(tmp_path)
    name = '-book "quoted";$(echo hostile).epub'
    target = root / 'Original' / name
    target.write_bytes(b'not an EPUB: must never be read')
    def forbidden(*args, **kwargs):
        raise AssertionError('discovery must not read EPUB content')
    monkeypatch.setattr(Path, 'open', forbidden)
    result = service.scan()
    assert len(result.books) == 1
    opened = service.open(result.books[0].original.ref)
    assert opened.status == 'opened'
    assert opened.display_path is None
    assert calls == [['reader', '--new-window', str(target)]]
    assert str(root) not in repr(result) and str(root) not in repr(opened)


def test_missing_and_replaced_entries_fail_before_launch(tmp_path):
    root, service, calls = make_local(tmp_path)
    path = root / 'Original' / 'Book.epub'
    path.write_bytes(b'old')
    ref = service.scan().books[0].original.ref
    path.unlink()
    assert service.open(ref).status == 'unauthorized'
    path.write_bytes(b'replaced contents')
    assert service.open(ref).status == 'unauthorized'
    assert calls == []


def test_symlink_and_prefix_collision_are_rejected(tmp_path):
    root, service, calls = make_local(tmp_path)
    outside = tmp_path / 'library-escape'
    outside.mkdir()
    target = outside / 'Book.epub'
    target.write_bytes(b'x')
    link = root / 'Original' / 'Book.epub'
    link.symlink_to(target)
    result = service.scan()
    assert result.books == ()
    assert [w.reason for w in result.warnings] == ['invalid_entry']
    link.unlink()
    link.write_bytes(b'valid')
    ref = service.scan().books[0].original.ref
    link.unlink()
    link.symlink_to(target)
    assert service.open(ref).status == 'unauthorized'
    assert calls == []


def test_stat_failure_is_isolated_and_does_not_leak_exception(tmp_path, monkeypatch):
    from pathlib import Path
    root, service, _ = make_local(tmp_path)
    for name in ('Good.epub', 'Bad.epub'):
        (root / 'Original' / name).write_bytes(b'')
    original = Path.stat
    def stat(path, *args, **kwargs):
        if path.name == 'Bad.epub':
            raise PermissionError('/secret permission detail')
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'stat', stat)
    result = service.scan()
    assert [book.name for book in result.books] == ['Good']
    assert [warning.reason for warning in result.warnings] == ['entry_unavailable']
    assert '/secret' not in repr(result)


def test_platform_selection_and_launch_errors_do_not_launch_real_process():
    from ayvu.library_adapters import ReaderLauncher
    calls = []
    assert ReaderLauncher(None, runner=calls.append, which=lambda name: name if name == 'gio' else None,
                          platform='linux').launch('/tmp/book.epub', lambda: False, lambda: True) == 'opened'
    assert calls == [['gio', 'open', '/tmp/book.epub']]
    assert ReaderLauncher(None, runner=calls.append, which=lambda _: None, platform='win32').launch('/tmp/book.epub', lambda: False, lambda: True) == 'unavailable'
    assert ReaderLauncher("reader '", runner=calls.append).launch('/tmp/book.epub', lambda: False, lambda: True) == 'configuration_invalid'
    def fail(_argv):
        raise OSError('/secret process output')
    assert ReaderLauncher('reader', runner=fail).launch('/tmp/book.epub', lambda: False, lambda: True) == 'launch_failed'
    assert ReaderLauncher('reader', runner=calls.append).launch('/tmp/book.epub', lambda: True, lambda: True) == 'cancelled'
    assert len(calls) == 1


def test_revalidation_occurs_immediately_before_process_creation(tmp_path):
    from ayvu.library_adapters import ReaderLauncher
    calls = []
    launcher = ReaderLauncher('reader', runner=calls.append)
    assert launcher.launch(str(tmp_path / 'book.epub'), lambda: False, lambda: False) == 'unauthorized'
    assert calls == []


def test_macos_reader_selection_uses_injected_process(tmp_path):
    from ayvu.library_adapters import ReaderLauncher
    calls = []
    path = str(tmp_path / 'book.epub')
    launcher = ReaderLauncher(runner=calls.append, which=lambda name: name if name == 'open' else None,
                              platform='darwin')
    assert launcher.launch(path, lambda: False, lambda: True) == 'opened'
    assert calls == [['open', path]]


def test_typed_requests_use_only_current_opaque_references():
    from ayvu.library_service import ScanRequest, ListRequest, DescribeRequest, OpenRequest
    fs = Filesystem([Candidate('Book', 'original', 'opaque-token')])
    fs.paths['opaque-token'] = '/private/Book.epub'
    launcher = Launcher()
    service = LibraryService(fs, launcher, fs)
    assert service.scan_request(ScanRequest('/private/root')).warnings[0].reason == 'unknown_root'
    assert service.list_entries(ListRequest('/private/root')).status == 'unknown_root'
    assert service.describe_entry(DescribeRequest('/private/Book.epub')).status == 'stale'
    assert service.open_entry(OpenRequest('/private/Book.epub')).status == 'stale'
    assert not fs.authorizations and not launcher.calls
    scan = service.scan_request(ScanRequest(service.root_ref))
    listing = service.list_entries(ListRequest(service.root_ref))
    book = scan.books[0]
    assert listing.status == 'ok' and listing.books == scan.books
    assert service.describe_entry(DescribeRequest(book.ref)).book == book
    assert service.open_entry(OpenRequest(book.original.ref)).status == 'opened'


def test_directory_failure_does_not_discard_other_folder(tmp_path, monkeypatch):
    import os
    root, service, _ = make_local(tmp_path)
    (root / 'Traduzidos' / 'Good-pt.epub').write_bytes(b'')
    original_scandir = os.scandir
    def scandir(path):
        if str(path).endswith('Original'):
            raise PermissionError('/private directory failure')
        return original_scandir(path)
    monkeypatch.setattr(os, 'scandir', scandir)
    result = service.scan()
    assert [book.name for book in result.books] == ['Good']
    assert [warning.reason for warning in result.warnings] == ['directory_unavailable']
    assert result.warnings[0].ref == service.root_ref
    assert '/private' not in repr(result)


def test_disappearing_entry_isolated_after_directory_listing(tmp_path, monkeypatch):
    from pathlib import Path
    root, service, _ = make_local(tmp_path)
    for name in ('Gone.epub', 'Good.epub'):
        (root / 'Original' / name).write_bytes(b'')
    original_resolve = Path.resolve
    def resolve(path, *args, **kwargs):
        if path.name == 'Gone.epub':
            raise FileNotFoundError('/private vanished')
        return original_resolve(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'resolve', resolve)
    result = service.scan()
    assert [book.name for book in result.books] == ['Good']
    assert [warning.reason for warning in result.warnings] == ['entry_unavailable']
    assert '/private' not in repr(result)


def test_real_uninterpretable_filename_is_a_warning(tmp_path):
    import os
    root, service, _ = make_local(tmp_path)
    (root / 'Original' / 'Good.epub').write_bytes(b'')
    name = os.fsencode(root / 'Original') + b'/bad-\xff.epub'
    fd = os.open(name, os.O_CREAT | os.O_WRONLY, 0o600)
    os.close(fd)
    result = service.scan()
    assert [book.name for book in result.books] == ['Good']
    assert [warning.reason for warning in result.warnings] == ['invalid_filename']


def test_authorization_checks_non_epub_and_traversal_even_with_matching_identity(tmp_path):
    from dataclasses import replace
    from ayvu.library_adapters import LocalLibraryFilesystem, _identity
    root, _, _ = make_local(tmp_path)
    path = root / 'Original' / 'Book.epub'
    path.write_bytes(b'')
    fs = LocalLibraryFilesystem(root)
    candidates, _, _ = fs.discover(lambda: False, 10)
    token = candidates[0].token
    non_epub = root / 'Original' / 'Book.txt'
    non_epub.write_bytes(b'')
    assert fs.authorize(replace(token, path=non_epub, identity=_identity(non_epub.stat()))) is None
    outside = tmp_path / 'outside.epub'
    outside.write_bytes(b'')
    traversal = root / 'Original' / '..' / '..' / 'outside.epub'
    assert fs.authorize(replace(token, path=traversal, identity=_identity(outside.stat()))) is None


def test_cancellation_during_directory_iteration_stops_before_stat(tmp_path, monkeypatch):
    import os
    from pathlib import Path
    root, service, _ = make_local(tmp_path)
    (root / 'Original' / 'Book.epub').write_bytes(b'')
    original_scandir = os.scandir
    stopped = [False]
    class Scan:
        def __init__(self, wrapped): self.wrapped = wrapped
        def __enter__(self): self.wrapped.__enter__(); return self
        def __exit__(self, *args): return self.wrapped.__exit__(*args)
        def __iter__(self):
            for item in self.wrapped:
                stopped[0] = True
                yield item
    monkeypatch.setattr(os, 'scandir', lambda path: Scan(original_scandir(path)))
    original_stat = Path.stat
    def stat(path, *args, **kwargs):
        if path.name == 'Book.epub':
            raise AssertionError('entry stat must not occur after cancellation')
        return original_stat(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'stat', stat)
    result = service.scan(lambda: stopped[0])
    assert result.cancelled and result.books == ()


def test_discovery_and_detail_do_not_open_files_write_or_access_network(tmp_path, monkeypatch):
    import builtins
    import os
    import socket
    from pathlib import Path
    root, service, _ = make_local(tmp_path)
    (root / 'Original' / 'Book.epub').write_bytes(b'not an epub')
    def forbidden(*args, **kwargs):
        raise AssertionError('file opening, writing, or network is forbidden during discovery')
    monkeypatch.setattr(Path, 'open', forbidden)
    monkeypatch.setattr(builtins, 'open', forbidden)
    monkeypatch.setattr(os, 'open', forbidden)
    monkeypatch.setattr(socket, 'socket', forbidden)
    monkeypatch.setattr(Path, 'write_bytes', forbidden)
    monkeypatch.setattr(Path, 'write_text', forbidden)
    result = service.scan()
    assert len(result.books) == 1
    assert service.describe(result.books[0].ref) == result.books[0]


def test_process_port_rejects_a_shell_string_before_invoking_runner():
    import pytest
    from ayvu.library_adapters import ProcessLauncher
    calls = []
    port = ProcessLauncher(calls.append)
    with pytest.raises(ValueError, match='invalid process arguments'):
        port.launch('reader', '--flag /book.epub')
    assert calls == []


def test_configured_shells_are_rejected_before_revalidation_or_process():
    from ayvu.library_adapters import ReaderLauncher
    calls = []
    revalidated = []
    for command in ('sh -c echo', '/bin/bash -c echo', 'dash -c echo', 'zsh -c echo',
                    'fish -c echo', 'ksh -c echo', 'cmd /c echo', 'cmd.exe /c echo',
                    'powershell -Command echo', 'powershell.exe -Command echo',
                    'pwsh -Command echo', 'pwsh.exe -Command echo'):
        launcher = ReaderLauncher(command, runner=calls.append)
        assert launcher.launch('/private/book.epub', lambda: False,
                               lambda: revalidated.append(True) or True) == 'configuration_invalid'
    assert calls == [] and revalidated == []
