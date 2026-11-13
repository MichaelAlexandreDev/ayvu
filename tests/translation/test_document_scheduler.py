"""Bounded real document concurrency, inverse completion and ordered observables."""
from dataclasses import replace
from threading import Barrier, Event, get_ident
from types import SimpleNamespace

import pytest

from ayvu.jobs import JobEvent, JobCorrelation
from ayvu.kernel import JobWarning
from ayvu.translation.document_scheduler import DocumentScheduler
from tests.support.document_workers_support import Background, Scene
from tests.support.translation_cache_v2_support import NOW


@pytest.mark.parametrize("workers", [1, 2, 4])
def test_effective_window_retains_completed_documents_until_consumption(workers):
    admitted, outputs, ready, release = [], [], Event(), Event()
    scheduler = DocumentScheduler()
    items = [SimpleNamespace(index=i) for i in range(1, 9)]
    def execute(item, check):
        admitted.append(item.index)
        if len(admitted) == workers:
            ready.set()
        if item.index == 1:
            assert release.wait(5)
        return item.index
    def consume():
        outputs.extend(value for _, value in scheduler.ordered(items, workers, execute))
    task = Background(consume)
    try:
        assert ready.wait(5)
        assert set(admitted) == set(range(1, workers + 1))
        assert not outputs
    finally:
        release.set()
        task.join()
    assert not task.errors and outputs == list(range(1, 9))


def test_two_documents_are_blocked_together_and_sqlite_connections_are_distinct(tmp_path, monkeypatch):
    import ayvu.knowledge.cache.read_only as reads
    scene = Scene(tmp_path, monkeypatch)
    original, barrier, observed = reads.exact_record, Barrier(2), []
    def read(connection, key):
        observed.append((get_ident(), connection))
        barrier.wait(timeout=5)
        return original(connection, key)
    monkeypatch.setattr(reads, "exact_record", read)
    result = scene.run()
    assert result.output_written and result.stats.from_cache == 4
    assert len({thread for thread, _ in observed}) == 2
    assert len({id(connection) for _, connection in observed}) == 4
    assert set(scene.created) == {thread for thread, _ in observed}
    for _, connection in observed:
        import sqlite3
        with pytest.raises(sqlite3.ProgrammingError, match="same thread"):
            connection.execute("SELECT 1")


def test_inverse_completion_keeps_output_events_stats_and_checkpoint_in_book_order(tmp_path, monkeypatch):
    scene = Scene(tmp_path, monkeypatch, workers=4, count=3)
    scene.block_documents(monkeypatch)
    original = scene.resources.documents.factory
    from contextlib import contextmanager
    @contextmanager
    def warnings(events, check):
        with original(events, check) as executor:
            execute = executor.execute_document
            def document(doc, index):
                result = execute(doc, index)
                events.begin_unit(0)
                for suffix in ("a", "b"):
                    events.emit(JobEvent(str(__import__('uuid').uuid4()), scene.env.job.job_id, 1, NOW,
                        JobCorrelation(doc.project_id), JobWarning(f"document_{index}_{suffix}")))
                return result
            executor.execute_document = document
            yield executor
    scene.resources.documents.factory = warnings
    task = Background(scene.run)
    try:
        assert all(event.wait(5) for event in scene.entered.values())
        for index in (3, 2, 1):
            scene.release[index].set()
            assert scene.finished[index].wait(5)
        result = task.join()
    finally:
        for event in scene.release.values():
            event.set()
        task.join()
    assert not task.errors and scene.physical == [3, 2, 1]
    assert [item.text for item in result.translations] == [f"PT:Document {i} unit 1" for i in (1, 2, 3)]
    assert result.stats.from_cache == 3 and result.stats.missing == 0
    assert [row[1] for row in scene.checkpoints] == [1, 2, 3]
    assert {row[0] for row in scene.checkpoints + scene.observed} == {task.thread.ident}
    assert [sequence for _, sequence, _ in scene.observed] == list(range(1, 10))
    payloads = [payload for _, _, payload in scene.observed]
    assert [payload.code for payload in payloads if isinstance(payload, JobWarning)] == [
        f"document_{i}_{suffix}" for i in (1, 2, 3) for suffix in ("a", "b")]
    assert [payload.completed for payload in payloads if not isinstance(payload, JobWarning)] == [1, 2, 3]
    assert scene.published[0][0] == task.thread.ident


@pytest.mark.parametrize("failure", [1, 2, 3])
def test_first_middle_last_failure_is_fatal_and_drains_before_return(tmp_path, monkeypatch, failure):
    scene = Scene(tmp_path, monkeypatch, workers=4, count=3)
    scene.block_documents(monkeypatch, failure=failure)
    task = Background(scene.run)
    try:
        assert all(event.wait(5) for event in scene.entered.values())
        scene.release[failure].set()
        assert scene.finished[failure].wait(5)
        assert not task.done.is_set()
    finally:
        for event in scene.release.values():
            event.set()
        task.join()
    assert len(task.errors) == 1 and str(task.errors[0]) == f"document_failure_{failure}"
    assert not scene.published
    assert all(event.is_set() for event in scene.finished.values())


def test_selected_document_uses_book_index_while_callbacks_keep_selected_ordinal(tmp_path, monkeypatch):
    from ayvu.kernel import ChapterSelection
    scene = Scene(tmp_path, monkeypatch, workers=4)
    scene.formats.inspect = lambda *a, **k: SimpleNamespace(failure=None,
        inventory=SimpleNamespace(documents=[SimpleNamespace(logical_key=d.logical_key, title=None) for d in scene.documents]))
    scene.request = replace(scene.request, chapter_selection=ChapterSelection.parse("3"))
    indices = []
    from ayvu.translation.cache_only_execution import CacheOnlyDocumentExecutor
    original = CacheOnlyDocumentExecutor.execute_document
    def execute(executor, doc, index=0):
        indices.append(index)
        return original(executor, doc, index)
    monkeypatch.setattr(CacheOnlyDocumentExecutor, "execute_document", execute)
    result = scene.run()
    assert result.output_written and indices == [3]
    assert [row[1] for row in scene.checkpoints] == [1]


@pytest.mark.parametrize("invalid_title", [False, True])
def test_metadata_is_before_workers_at_index_zero_with_its_existing_error_rule(tmp_path, monkeypatch, invalid_title):
    from ayvu.content import build_document, SemanticKind
    from ayvu.formats import BookFacts
    scene = Scene(tmp_path, monkeypatch, workers=4, warm=False)
    doc = scene.documents[0]
    title = build_document(doc.project_id, "metadata", doc.adapter, doc.source_locale,
        (replace(doc.units[0].content, kind=SemanticKind("title")),))
    scene.documents = (title, *scene.documents)
    scene.request = replace(scene.request, translate_metadata=True, facts=BookFacts(title_holds_other_nodes=invalid_title))
    indices = []
    from ayvu.translation.cache_only_execution import CacheOnlyDocumentExecutor
    original = CacheOnlyDocumentExecutor.execute_document
    def execute(executor, doc, index=0):
        indices.append(index)
        return original(executor, doc, index)
    monkeypatch.setattr(CacheOnlyDocumentExecutor, "execute_document", execute)
    result = scene.run()
    assert result.output_written and result.chapters_processed == 4
    assert (indices[0] == 0) is (not invalid_title)
    assert (len(result.stats.errors) == 1) is invalid_title
    assert result.stats.missing == (4 if invalid_title else 5)


def test_worker_resources_never_repeat_migration_or_cache_preparation(tmp_path, monkeypatch):
    scene = Scene(tmp_path, monkeypatch, workers=4)
    from tests.support.transient_translation_support import forbidden
    monkeypatch.setattr("ayvu.kernel.sqlite.SqliteMigrationRunner.migrate", forbidden)
    monkeypatch.setattr("ayvu.knowledge.cache.repository.SQLiteExactCache.__init__", forbidden)
    monkeypatch.setattr("ayvu.projects.schema.open_provenance_repository", forbidden)
    assert scene.run().stats.from_cache == 4
