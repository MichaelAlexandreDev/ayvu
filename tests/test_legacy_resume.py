import json
import os

import pytest

from ayvu.domain import LanguagePair, TranslationOptions
from ayvu.legacy_resume import MAX_LEGACY_STATE_BYTES, LegacyResumeError, inspect_legacy_resume, scan_legacy_states
from ayvu.resume import ResumeStateStore, TranslationResumeState

CANARIES = ('/home/reader/private/moby-dick.epub', 'user:hunter2@example.test', 'The Whale Chapter')


@pytest.fixture
def state_file(tmp_path):
    state = TranslationResumeState.create(
        input_path=tmp_path / 'home-reader-private' / 'moby-dick.epub',
        output_path=tmp_path / 'home-reader-private' / 'moby-dick-pt.epub',
        cache_path=tmp_path / 'cache.sqlite',
        translator_name='libretranslate',
        url='http://user:hunter2@example.test:5000',
        glossary_path=None,
        options=TranslationOptions(language_pair=LanguagePair(source='en', target='pt')),
        overwrite=False,
        timeout=30.0,
        retries=2,
    )
    state = state.start_chapter('The Whale Chapter', 4)
    state = state.record_chapter('The Whale Chapter', 4, True)
    return ResumeStateStore(tmp_path).save(state)


def rewrite(path, **changes):
    data = json.loads(path.read_text())
    data.update(changes)
    path.write_text(json.dumps(data))


def test_inspection_reports_only_safe_fields(state_file):
    view = inspect_legacy_resume(state_file)
    assert (view.version, view.status, view.source, view.target) == (1, 'running', 'en', 'pt')
    assert (view.total_chapters, view.completed_chapters, view.failed_chapters, view.failed_segments) == (4, 1, 0, 0)


def test_inspection_exposes_no_path_url_or_chapter_name(state_file):
    text = repr(inspect_legacy_resume(state_file))
    for canary in CANARIES:
        assert canary not in text
    assert 'hunter2' not in text and 'moby' not in text and str(state_file.parent) not in text


def test_inspection_does_not_change_the_file_or_stop_the_legacy_store(state_file):
    before = state_file.read_bytes()
    stat_before = state_file.stat()
    inspect_legacy_resume(state_file)
    after = state_file.stat()
    assert state_file.read_bytes() == before
    assert (after.st_mtime_ns, after.st_size, after.st_ino) == (stat_before.st_mtime_ns, stat_before.st_size, stat_before.st_ino)
    assert sorted(path.name for path in state_file.parent.iterdir() if path.name.endswith('.json')) == [state_file.name]
    scan = ResumeStateStore(state_file.parent).scan()
    assert [item.status for item in scan.running] == ['running'] and scan.invalid == ()


def test_completed_checkpoint_is_supported(state_file):
    rewrite(state_file, status='completed')
    assert inspect_legacy_resume(state_file).status == 'completed'


@pytest.mark.parametrize('version', [0, 2, 99])
def test_unsupported_version_is_distinct(state_file, version):
    rewrite(state_file, version=version)
    with pytest.raises(LegacyResumeError, match='^legacy_state_unsupported_version$'):
        inspect_legacy_resume(state_file)


@pytest.mark.parametrize('content', [
    b'not json', b'[]', b'"text"', b'\xff\xfe', b'{}', b'[' * 200000,
    b'{"version": 1, "status": "unknown /home/reader/private"}',
])
def test_invalid_content_is_typed_and_does_not_quote_the_file(tmp_path, content):
    path = tmp_path / 'broken.ayvu-state.json'
    path.write_bytes(content)
    with pytest.raises(LegacyResumeError, match='^legacy_state_invalid$') as caught:
        inspect_legacy_resume(path)
    assert caught.value.__cause__ is None and 'reader' not in str(caught.value)


@pytest.mark.parametrize('field', ['source', 'target'])
def test_language_fields_must_look_like_languages(state_file, field):
    rewrite(state_file, **{field: '/home/reader/private'})
    with pytest.raises(LegacyResumeError, match='^legacy_state_invalid$'):
        inspect_legacy_resume(state_file)


def test_missing_file_is_typed_and_names_no_path(tmp_path):
    with pytest.raises(LegacyResumeError, match='^legacy_state_not_found$') as caught:
        inspect_legacy_resume(tmp_path / 'missing.json')
    assert str(tmp_path) not in str(caught.value)


def test_symlink_is_not_followed(state_file, tmp_path):
    link = tmp_path / 'link.ayvu-state.json'
    link.symlink_to(state_file)
    with pytest.raises(LegacyResumeError, match='^legacy_state_unreadable$'):
        inspect_legacy_resume(link)


def test_directory_is_refused(tmp_path):
    with pytest.raises(LegacyResumeError, match='^legacy_state_unreadable$'):
        inspect_legacy_resume(tmp_path)


@pytest.mark.skipif(not hasattr(os, 'mkfifo'), reason='POSIX FIFO')
def test_fifo_does_not_block(tmp_path):
    path = tmp_path / 'pipe.json'
    os.mkfifo(path)
    with pytest.raises(LegacyResumeError, match='^legacy_state_unreadable$'):
        inspect_legacy_resume(path)


def test_oversized_file_is_refused_before_parsing(tmp_path):
    path = tmp_path / 'big.json'
    path.write_bytes(b' ' * (MAX_LEGACY_STATE_BYTES + 1))
    with pytest.raises(LegacyResumeError, match='^legacy_state_too_large$'):
        inspect_legacy_resume(path)


def test_scan_gives_one_entry_per_file_and_a_message_only_for_whoever_asks(tmp_path):
    (tmp_path / 'bad.ayvu-state.json').write_bytes(b'\xff')
    (tmp_path / 'newer.ayvu-state.json').write_text('{"version": 2}')
    (tmp_path / 'other.json').write_text('{}')

    entries = scan_legacy_states(tmp_path)

    assert [entry.path.name for entry in entries] == ['bad.ayvu-state.json', 'newer.ayvu-state.json']
    assert [entry.failure for entry in entries] == ['legacy_state_invalid', 'legacy_state_unsupported_version']
    assert entries[0].message == f'Resume state file is not valid JSON: {tmp_path / "bad.ayvu-state.json"}'
    assert entries[1].message == f'Invalid resume state file {tmp_path / "newer.ayvu-state.json"}: Unsupported resume state version: 2.'
    assert all(str(tmp_path) not in repr(entry) and entry.state is None for entry in entries)


def test_scan_of_an_absent_folder_is_empty_and_creates_nothing(tmp_path):
    assert scan_legacy_states(tmp_path / 'absent') == ()
    assert not (tmp_path / 'absent').exists()


def test_a_reference_is_stable_and_says_nothing_readable(tmp_path):
    (tmp_path / 'My Book-pt.ayvu-state.json').write_bytes(b'{')
    first, again = scan_legacy_states(tmp_path)[0], scan_legacy_states(tmp_path)[0]
    assert first.reference == again.reference and first.reference.startswith('legacy:') and 'Book' not in first.reference
