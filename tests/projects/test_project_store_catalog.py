from uuid import uuid4


import pytest

from ayvu.projects.model import ProjectSourceError
from ayvu.projects.store import ProjectJsonRepository, build_project_request, canonical_source_path, configuration_references, file_digest, source_digest, translation_selections

from tests.projects.project_store_support import project_input


def test_read_catalog_creates_nothing_and_takes_no_lock(tmp_path):
    repository = ProjectJsonRepository(tmp_path / 'absent')
    assert repository.read_catalog().projects == () and not (tmp_path / 'absent').exists()


def test_read_catalog_reads_while_a_writer_holds_the_lock(project_input):
    _, repository, request = project_input
    project = repository.resolve(request).project
    with repository._transaction():
        assert repository.read_catalog().projects == (project,)


def test_read_catalog_reports_a_bad_entry_and_keeps_the_rest(project_input):
    _, repository, request = project_input
    project = repository.resolve(request).project
    (repository.root / '.staging-x').mkdir()
    (repository.root / 'stray').write_text('x')
    bad = repository.root / str(uuid4())
    bad.mkdir()
    (bad / 'project.json').write_text('{')
    catalog = repository.read_catalog()
    assert catalog.projects == (project,)
    assert sorted(catalog.failures) == sorted(['incomplete_project_creation', 'invalid_workspace_entry', 'invalid_manifest'])
    assert 'stray' not in repr(catalog)


def test_selections_hold_exactly_what_identity_is_made_of():
    selections = translation_selections(translator='t', url='u', chunk_limit=9, translate_metadata=True, translate_alt_text=False, chapters='1', translation_memory=None)
    assert selections == {'translator': 't', 'url': 'u', 'chunk_limit': 9, 'translate_metadata': True, 'translate_alt_text': False, 'chapters': '1', 'translation_memory': None}


def test_configuration_references_are_what_a_request_holds_and_touch_no_book(project_input):
    source, _, _ = project_input
    selections = {'translator': 't', 'url': 'http://h'}
    request = build_project_request(source, source='en', target='pt', selections=selections, profile_name='p')
    references = configuration_references(selections=selections, profile_name='p')
    assert (references.provider_ref, references.configuration_ref, references.profile_ref, references.glossary_ref) == (
        request.provider_ref, request.configuration_ref, request.profile_ref, request.glossary_ref)
    assert request.source_path == canonical_source_path(source)


def test_digests_can_be_stopped_between_chunks_and_a_missing_file_says_so(project_input, tmp_path):
    source, _, request = project_input
    assert source_digest(source) == (request.source_size, request.source_sha256) == file_digest(source)

    class Stop(Exception):
        pass

    def stop():
        raise Stop

    with pytest.raises(Stop):
        source_digest(source, between_chunks=stop)
    with pytest.raises(ProjectSourceError) as missing:
        file_digest(tmp_path / 'gone')
    assert str(missing.value) == 'source_missing'
