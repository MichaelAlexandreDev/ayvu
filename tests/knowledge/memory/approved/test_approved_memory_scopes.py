from dataclasses import replace
from uuid import UUID, uuid4

import pytest

from ayvu.knowledge.memory.approved.model import MAX_VALUE_BYTES, Candidate, MemoryScope, MemoryScopeError, MemorySource, MemoryValidationError, MemoryValueError, ScopeKind
from ayvu.knowledge.memory.approved.service import CreateCandidate
from ayvu.knowledge.memory.approved.store import SqliteApprovedMemoryRepository, memory_migration_runner
from ayvu.provenance.model import ActorKind
from tests.support.approved_memory_fixtures import IMPORT_SCOPE, PAIR, PIN, PROJECT_SCOPE, SCOPES, USER_SCOPE, WORK_SCOPE, final, source
from tests.support.content_fixtures import PROJECT
from ayvu.kernel import LanguagePair

from tests.knowledge.memory.approved.approved_memory_support import env


@pytest.mark.parametrize('scope', SCOPES)
def test_project_work_user_and_import_scopes_require_explicit_allowance(env, scope):
    origin = env.evidence('tm_import', str(uuid4())).record_id if scope is IMPORT_SCOPE else None
    revision = env.approve(env.create(scope=scope, import_origin=origin))
    assert env.repo.get(revision.revision_id, scopes=(scope,)) == revision
    assert env.repo.current(env.source, PAIR, scopes=(scope,)) == revision
    other = USER_SCOPE if scope != USER_SCOPE else WORK_SCOPE
    with pytest.raises(MemoryScopeError, match='^scope_denied$'):
        env.repo.get(revision.revision_id, scopes=(other,))
    assert env.repo.list_entries(scopes=(other,)).revisions == ()
    assert env.repo.current(env.source, PAIR, scopes=(other,)) is None
    restricted = env.open(allowed=frozenset({other}))
    with pytest.raises(MemoryScopeError, match='^scope_denied$'):
        restricted.list_entries(scopes=(scope,))
    with pytest.raises(MemoryScopeError, match='^scope_denied$'):
        restricted.create(env.creation(scope=scope, import_origin=origin))


def test_explicit_precedence_no_source_text_fallback_or_cross_project_leak(env):
    project = env.approve(env.create())
    work = env.approve(env.create(scope=WORK_SCOPE))
    assert env.repo.current(env.source, PAIR, scopes=(WORK_SCOPE, PROJECT_SCOPE)) == work
    assert env.repo.current(env.source, PAIR, scopes=(PROJECT_SCOPE, WORK_SCOPE)) == project
    same_text_other_unit = source(logical_key='other-chapter')
    assert same_text_other_unit.canonical_text == env.source.canonical_text
    assert env.repo.current(same_text_other_unit, PAIR, scopes=SCOPES) is None
    other_project = replace(env.source, project_id=str(UUID(int=888)))
    assert env.repo.current(other_project, PAIR, scopes=SCOPES) is None
    assert env.repo.current(env.source, LanguagePair('en', 'es'), scopes=SCOPES) is None
    with pytest.raises(MemoryScopeError, match='^scope_denied$'):
        replace(project.value, scope=MemoryScope(ScopeKind.PROJECT, str(UUID(int=999))))
    with pytest.raises(MemoryScopeError, match='^invalid_query_scopes$'):
        env.repo.list_entries(scopes=())


def test_listing_and_history_are_bounded_and_deterministic(env):
    revisions = [env.create(scope=PROJECT_SCOPE if index % 2 == 0 else WORK_SCOPE) for index in range(7)]
    found, cursor = [], 0
    while True:
        page = env.repo.list_entries(scopes=(PROJECT_SCOPE, WORK_SCOPE), limit=2, after=cursor)
        found.extend(page.revisions)
        if page.next_cursor is None:
            break
        cursor = page.next_cursor
    assert found == revisions
    assert env.repo.list_entries(scopes=(PROJECT_SCOPE,), entry_id=revisions[0].entry_id).revisions == (revisions[0],)
    # Instrument the actual SQLite connection, including bounds in the SQL executed.
    statements = []
    real_connect = env.repo._runner.connect

    def traced_connect(*args, **kwargs):
        db = real_connect(*args, **kwargs)
        db.set_trace_callback(statements.append)
        return db

    env.repo._runner.connect = traced_connect
    assert len(env.repo.list_entries(scopes=SCOPES, limit=1).revisions) == 1
    assert any('ORDER BY r.sequence LIMIT 2' in sql for sql in statements)
    assert not any(sql.strip().upper() == 'SELECT * FROM TM_REVISIONS' for sql in statements)
    for limit, after in ((0, 0), (257, 0), (1, -1), (True, 0)):
        with pytest.raises(MemoryValidationError, match='^invalid_page$'):
            env.repo.list_entries(scopes=SCOPES, limit=limit, after=after)


def test_unicode_source_normalization_content_and_structural_identity_are_distinct():
    composed, decomposed = source('Café'), source('Cafe\u0301')
    assert composed.canonical_text == decomposed.canonical_text == 'Café'
    assert composed.search_key == decomposed.search_key == 'café'
    assert composed.content_id != decomposed.content_id and composed.unit_id != decomposed.unit_id
    spaced, cased = source('Straße\t\n café'), source('STRASSE CAFÉ')
    assert spaced.search_key == cased.search_key == 'strasse café'
    assert spaced.canonical_text != cased.canonical_text
    same_text_different_unit = source('Café', logical_key='different')
    assert composed.content_id == same_text_different_unit.content_id
    assert composed.unit_id != same_text_different_unit.unit_id


@pytest.mark.parametrize('field', ['source', 'target'])
@pytest.mark.parametrize('character', ['x', 'é', '🙂'])
@pytest.mark.parametrize('extra', [0, 1])
def test_utf8_64k_n_and_n_plus_one_rejected_never_truncated(env, field, character, extra):
    text = character * (MAX_VALUE_BYTES // len(character.encode())) + 'x' * extra
    assert len(text.encode()) == MAX_VALUE_BYTES + extra
    if extra:
        with pytest.raises(MemoryValueError, match='^linguistic_value_too_large$'):
            env.create(**({'src': source(text)} if field == 'source' else {'target': final(text)}))
        assert env.repo.list_entries(scopes=SCOPES).revisions == ()
    else:
        value = env.create(**({'src': source(text)} if field == 'source' else {'target': final(text)}))
        loaded = env.open().get(value.revision_id, scopes=SCOPES)
        assert (loaded.value.source.canonical_text if field == 'source' else loaded.value.target.text) == text


def test_oversized_source_rejected_before_normalization_and_keys_are_validated():
    # NFC would shrink this below the bound; input must still be refused.
    with pytest.raises(MemoryValueError, match='^linguistic_value_too_large$'):
        source('e\u0301' * 21846)
    with pytest.raises(MemoryValidationError, match='^invalid_search_policy$'):
        replace(source(), search_key='forged')
    with pytest.raises(MemoryValidationError, match='^invalid_search_policy$'):
        replace(source(), normalization_version='unversioned')
    for value in ('\ud800',):
        with pytest.raises(MemoryValueError, match='^invalid_linguistic_value$'):
            final(value)


def test_only_selected_unit_text_is_persisted_with_references_not_document_paths(env):
    from tests.support.content_fixtures import epub_document, unit
    doc = epub_document(logical_key='private/source/book.xhtml', units=[
        unit('private/source/book.xhtml#p1', 'SELECTED UNIT', order=(1,)),
        unit('private/source/book.xhtml#p2', 'UNRELATED SECRET DOCUMENT PAYLOAD', order=(2,))])
    selected = MemorySource.from_document(doc, doc.units[0].unit_id)
    revision = env.create(src=selected)
    assert revision.value.source.canonical_text == 'SELECTED UNIT'
    for database in (env.database, env.project_database):
        raw = database.read_bytes()
        assert b'private/source/book.xhtml' not in raw
        assert b'UNRELATED SECRET DOCUMENT PAYLOAD' not in raw


def test_cross_project_read_and_write_require_explicit_scope_and_provenance_mapping(env):
    from ayvu.content.model import build_document
    from ayvu.jobs.model import JobRequest
    from ayvu.jobs.store import SqliteJobRepository
    from ayvu.projects.schema import project_migration_runner
    from ayvu.provenance.model import ProducerIdentity, ProvenanceRequest, SourceKind, SubjectRef
    from ayvu.provenance.store import SqliteProvenanceRepository
    from tests.support.content_fixtures import ENGLISH, EPUB, unit
    other_id = str(UUID(int=555))
    other_scope = MemoryScope(ScopeKind.PROJECT, other_id)
    doc = build_document(other_id, 'chapter', EPUB, ENGLISH, [unit('body', env.source.canonical_text)])
    src = MemorySource.from_document(doc, doc.units[0].unit_id)
    jobs = SqliteJobRepository(project_migration_runner(), env.project_database, other_id)
    job = jobs.create(JobRequest(other_id, 'translate_epub', 1, 'sha256:' + 'b' * 64))
    provenance = SqliteProvenanceRepository(project_migration_runner(), env.project_database, other_id)
    operation_id = str(uuid4())
    evidence = provenance.append(ProvenanceRequest(
        other_id, job.job_id, 'tm_create', ActorKind.SYSTEM, SourceKind.PIPELINE,
        ProducerIdentity('not_applicable', 'not_applicable', 'epub@1', 'linguistic@1'),
        subject=SubjectRef('content_unit', 1, src.unit_id), configuration_refs=PIN.references,
        operation_id=operation_id))
    command = CreateCandidate(operation_id, Candidate(src, PAIR, final(), other_scope, evidence.record_id))
    with pytest.raises(MemoryScopeError, match='^scope_denied$'):
        env.repo.create(command)
    broader = SqliteApprovedMemoryRepository(memory_migration_runner(), env.database,
        allowed_scopes=frozenset((*SCOPES, other_scope)), provenance={PROJECT: env.provenance, other_id: provenance})
    created = broader.create(command)
    assert broader.get(created.revision_id, scopes=(other_scope,)) == created
    assert broader.list_entries(scopes=(PROJECT_SCOPE,)).revisions == ()
    with pytest.raises(MemoryScopeError, match='^scope_denied$'):
        broader.get(created.revision_id, scopes=(PROJECT_SCOPE,))
    with pytest.raises(MemoryScopeError, match='^scope_denied$'):
        env.repo.get(created.revision_id, scopes=(other_scope,))
