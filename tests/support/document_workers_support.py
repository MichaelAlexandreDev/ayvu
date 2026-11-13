"""Controlled rich worker scenes, real exact-cache reads and coordinator events."""
from contextlib import contextmanager
from dataclasses import replace
from threading import Event, Thread, get_ident
from types import SimpleNamespace
from uuid import uuid4

from ayvu.composition.builders.rich_workers import offline_work
from ayvu.jobs import CancellationSource, InMemoryJobEventLog, JobCorrelation
from ayvu.kernel import LanguagePair, TranslationOptions
from ayvu.translation import EngineObserver, EngineRequest, RuntimeContext, TranslateObserver, TranslationEngine
from ayvu.translation.document_contracts import SAFE_POINTS
from ayvu.translation.engine import ENGINE_SAFE_POINTS
from ayvu.translation.project_run import _Run
from tests.support.rich_cache_only_support import warm_units
from tests.support.translation_cache_v2_support import Env, NOW, document


class Background:
    def __init__(self, work):
        self.done, self.errors, self.result = Event(), [], None
        def run():
            try:
                self.result = work()
            except BaseException as error:
                self.errors.append(error)
            finally:
                self.done.set()
        self.thread = Thread(target=run)
        self.thread.start()

    def join(self):
        assert self.done.wait(10), "coordinator did not finish draining"
        self.thread.join()
        return self.result


class Scene:
    def __init__(self, root, monkeypatch, *, count=4, warm=True, workers=2):
        self.documents = tuple(document(f"Document {i} unit {j}", key=f"doc-{i}.xhtml")
                               for i in range(1, count + 1) for j in (1,))
        self.cache = root / "cache.sqlite"
        if warm:
            self.plan, self.env, _ = warm_units(self.cache, self.documents)
            self.env.artifacts.calls.clear()
        else:
            self.env = Env()
            self.plan = replace(self.env.plan, producer=replace(self.env.plan.producer, model="declared:offline"), route_hops=None)
        self.source = CancellationSource((*SAFE_POINTS, *ENGINE_SAFE_POINTS))
        self.lost, self.now = Event(), NOW
        self.observed, self.checkpoints, self.published, self.created = [], [], [], []
        self.context = RuntimeContext(self.env.job, self.source, self._run_sink(),
            current_job=lambda: self.env.job, lease_lost=self.lost.is_set)
        self.runtime = SimpleNamespace(_options=TranslationOptions(LanguagePair("en", "pt"), cache_only=True, workers=workers),
            _prepared=SimpleNamespace(glossary=None), producer=self.plan.producer, _cache_path=self.cache,
            _database=root / "project.sqlite", _project_id=self.env.job.request.project_id, _clock=lambda: self.now)
        def repository(*args, **kwargs):
            self.created.append(get_ident())
            return self.env.provenance
        monkeypatch.setattr("ayvu.composition.builders.rich_workers.SqliteProvenanceRepository", repository)
        self.resources = offline_work(self.runtime, self.context)
        self.formats = SimpleNamespace(extract=lambda *a, **k: SimpleNamespace(failure=None, documents=self.documents),
            reconstruct=self._reconstruct)
        self.request = EngineRequest("epub", object(), object(), self.env.job.request.project_id, workers=workers)

    def _run_sink(self):
        sink = object.__new__(_Run)
        sink._job, sink._clock, sink._new_id = self.env.job, lambda: self.now, lambda: str(uuid4())
        sink._log, sink._sequence = InMemoryJobEventLog(self.env.job.job_id), 0
        sink._correlation = JobCorrelation(self.env.job.request.project_id)
        sink._observer = TranslateObserver(EngineObserver(on_chapter_done=self._done), on_event=lambda event:
            self.observed.append((get_ident(), event.sequence, event.payload)))
        self.sink = sink
        return sink

    def _done(self, index, total, name, stats):
        self.checkpoints.append((get_ident(), index, name, stats.from_cache, stats.missing))

    def _reconstruct(self, adapter, source, request, publication, **kwargs):
        self.published.append((get_ident(), request))
        return SimpleNamespace(failure=None, artifact=None)

    def run(self):
        return TranslationEngine(self.formats, None, documents=self.resources.documents,
            observer=self.sink._engine_observer(), cancellation=self.source.token).run(self.request)

    def block_documents(self, monkeypatch, *, failure=None):
        from ayvu.translation.cache_only_execution import CacheOnlyDocumentExecutor
        original = CacheOnlyDocumentExecutor.execute_document
        self.entered = {i: Event() for i in range(1, len(self.documents) + 1)}
        self.release = {i: Event() for i in self.entered}
        self.finished = {i: Event() for i in self.entered}
        self.physical = []
        def execute(executor, doc, index=0):
            self.entered[index].set()
            assert self.release[index].wait(10)
            try:
                if index == failure:
                    raise ValueError(f"document_failure_{index}")
                result = original(executor, doc, index)
                self.physical.append(index)
                return result
            finally:
                self.finished[index].set()
        monkeypatch.setattr(CacheOnlyDocumentExecutor, "execute_document", execute)
