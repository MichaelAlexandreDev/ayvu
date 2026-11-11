from dataclasses import replace
from datetime import timedelta
import hashlib
from types import SimpleNamespace
from uuid import UUID
import pytest
from ayvu.artifacts.model import Artifact, ArtifactState
from ayvu.translation.context.execution import ContextExecution, ResolvedContextEgressDecision
from ayvu.jobs.model import JobRequest, JobState, new_job, transition_job, acquire_lease
from ayvu.jobs.events import encode_event
from ayvu.jobs.execution import InMemoryJobEventLog
from ayvu.provenance.model import ProvenanceRecord, encode_record, decode_record
from ayvu.providers.ports import Requirement
from ayvu.providers.capabilities import Capability, ProviderRequirements, SupportState
from ayvu.providers.libretranslate.adapter import LIBRETRANSLATE_PRODUCER, LibreTranslateProvider, libretranslate_snapshot
from ayvu.translation.context.model import ContextCandidate, ContextCategory, ContextError, ContextLimits, TranslationContextBuilder
from ayvu.translation.document import DocumentTranslator
from ayvu.translation.document_contracts import ExecutionContext, TranslationExecutionError, UnitSource
from ayvu.translation.plan import resolve_plan
from ayvu.providers.libretranslate.client import LibreTranslateTranslator
from tests.support.provider_fakes import FakeSession, http_response
from tests.support.translation_document_support import document, cue, Provider, PlanCache, Glossed, NOW, PROJECT, PRODUCER, ROUTE
from tests.support.translation_context_support import request, entry, Source, egress_decision
from ayvu.kernel import normalize_failure, JobWarning, TranslationOptions, LanguagePair, OutputPlan, JobFailure, JobFailureFamily

class Gate:
    def __init__(self, change=lambda x: x):
        self.change, self.calls = change, []

    def decision(self, disclosure):
        self.calls.append(disclosure)
        return self.change(egress_decision(disclosure))


class Artifacts:
    def __init__(self):
        self.published = []

    def publish(self, request, chunks, *, owner_token, cancellation):
        payload = b''.join(chunks)
        artifact = Artifact(str(UUID(int=100 + len(self.published))), request, ArtifactState.READY, 1, NOW, NOW,
            len(payload), 'sha256:' + hashlib.sha256(payload).hexdigest())
        self.published.append((artifact, payload))
        return artifact


class Provenance:
    def __init__(self):
        self.records = []

    def append(self, request):
        record = ProvenanceRecord(str(UUID(int=500 + len(self.records))), NOW, request)
        # Serialização real #138, inclusive o round trip que o SQLite usa.
        assert decode_record(encode_record(record)) == record
        self.records.append(record)
        return record


def running():
    job = new_job(JobRequest(PROJECT, 'translate_project', 1, 'sha256:' + 'a' * 64), str(UUID(int=71)), NOW)
    for state in (JobState.PREFLIGHT, JobState.RUNNING):
        job = transition_job(job, state, expected_revision=job.revision, now=NOW)
    return acquire_lease(job, 'a' * 64, NOW, timedelta(minutes=30))


def scene(text='contexto', *, state=SupportState.SUPPORTED, gate=None, cache=None, provider=None, required=False):
    d = document(cue('a', 'Hello world', 1))
    e = entry(text, required=required)
    r = request((e,), state=state)
    r = replace(r, snapshot=replace(r.snapshot, unit=replace(r.snapshot.unit, document_id=d.document_id,
        revision_id=d.revision_id, unit_id=d.units[0].unit_id), current=d.units[0]))
    r = replace(r, authorization=replace(r.authorization, unit=r.snapshot.unit))
    source = Source((ContextCandidate(e, text),))
    gate = gate or Gate()
    execution = ContextExecution(TranslationContextBuilder(source), (r,), gate)
    execution.prepare(NOW)
    p = resolve_plan(r.capabilities.snapshot, ProviderRequirements((Capability.PLAIN_TEXT_INPUT,)), PRODUCER, ROUTE,
                     chunk_limit=3000)
    job = running()
    artifacts, provenance, events = Artifacts(), Provenance(), InMemoryJobEventLog(job.job_id)
    provider, cache = provider or Provider(), cache or PlanCache()
    translator = DocumentTranslator(p, provider, lambda t: Glossed(t + '!'), artifacts, provenance, events,
        exact_cache=cache, translation_context=execution, clock=lambda: NOW)
    context = ExecutionContext(job, job.lease.owner_token)
    return SimpleNamespace(**locals())
