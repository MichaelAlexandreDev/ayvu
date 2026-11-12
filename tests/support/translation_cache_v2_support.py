from __future__ import annotations
import socket
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4
import pytest
from ayvu.artifacts.model import new_artifact
from ayvu.artifacts.service import ReuseResult
from ayvu.knowledge.cache.scope import CacheAuthorizationPolicy, CacheScope, CacheScopeKind
from ayvu.knowledge.cache.compatible_memory import CacheCompatibilityIdentity, CompatibleMemoryCandidate
from ayvu.knowledge.cache.entries import (
    CacheLookupState, CacheMissReason, CacheProvenanceMetadata, ExactCacheEntry, ExactCacheLookup,
    ExactEntryKey, PutIfAbsentResult, PutIfAbsentState, exact_entry_key,
)
from ayvu.knowledge.cache.cache_only import (
    CacheOnlyRequest, PinnedExecutionIdentityReference, ResolvedCacheExecutionIdentity,
)
from ayvu.knowledge.cache.identity_values import OpaqueSemanticRevision, TRANSLATION_STAGE_AGGREGATE
from ayvu.content.model import (
    AdapterIdentity,
    LocaleState,
    Locator,
    ProtectedPart,
    SemanticKind,
    SourceLocale,
    UnitContent,
    build_document,
)
from ayvu.jobs.model import JobLease, JobRequest, JobState, TranslationJob
from ayvu.provenance.model import ProducerIdentity
from ayvu.providers.ports import DeliveryState, ProviderRoute, ProviderStatus, ProviderUsage, TranslationProviderResult
from ayvu.translation.document import DocumentTranslator
from ayvu.translation.document_contracts import ExecutionContext, TranslationExecutionError, UnitSource
from ayvu.translation.plan import ResolvedTranslationPlan
from ayvu.translation.stages.memory_retrieval import RetrievalPolicy
from ayvu.kernel import JobFailureFamily

PROJECT = str(UUID(int=0x4321))


NOW = datetime(2026, 11, 5, 12, tzinfo=UTC)


OWNER = "a" * 64


PRODUCER = ProducerIdentity("provider@1", "model@1", "adapter@1", "pipeline@1")


ROUTE = ProviderRoute("en", "pt")


CAPABILITIES = "capfp:v1:" + "b" * 64


KIND = TRANSLATION_STAGE_AGGREGATE


def make_plan(**changes):
    values = {"producer": PRODUCER, "route": ROUTE, "capabilities": CAPABILITIES, "chunk_limit": 3000}
    values.update(changes)
    return ResolvedTranslationPlan(**values)


def document(*texts, key="chapter-a.txt", protected=False):
    units = []
    for index, text in enumerate(texts or (" Hello ", "Good night"), 1):
        if protected and index == 1:
            text = " Hello <b>"
            parts = (ProtectedPart(7, 10, "markup"),)
        else:
            parts = ()
        units.append(UnitContent(Locator("paragraph", 1, f"p-{index}"), text, SemanticKind("paragraph"), (index,),
                                 protected_parts=parts))
    return build_document(
        PROJECT, key, AdapterIdentity("plain", 1, 1), SourceLocale(LocaleState.EXPLICIT, "en"), tuple(units),
    )


class Provider:
    def __init__(self, plan, respond=None):
        self.plan = plan
        self.respond = respond or (lambda text: f"[{text}]")
        self.requests = []

    def execute(self, request):
        self.requests.append(request)
        return TranslationProviderResult(
            request.request_id, ProviderStatus.SUCCEEDED, self.plan.producer, DeliveryState.COMPLETED,
            text=self.respond(request.text), route=self.plan.route, usage=ProviderUsage(),
        )


class MemoryCache:
    """In-memory v2 port fake. It models exact keys and never promotes fuzzy results."""

    def __init__(self):
        self.entries = {}
        self.calls = []
        self.duplicate_keys = set()
        self.pinned = {}

    def resolve_pinned(self, reference, scope):
        self.calls.append(("resolve_pinned", reference, scope))
        return self.pinned.get(reference.boundary_reference)

    def lookup(self, key):
        self.calls.append(("lookup", key))
        if key in self.duplicate_keys:
            return ExactCacheLookup(CacheLookupState.CORRUPT, reason=CacheMissReason.DUPLICATE_EXACT_KEY)
        entry = self.entries.get(key)
        if entry is None:
            return ExactCacheLookup(CacheLookupState.MISS, reason=CacheMissReason.ENTRY_NOT_FOUND)
        return ExactCacheLookup(CacheLookupState.HIT, value=entry.value)

    def resolve_compatible(self, scope, value_kind, source_digest, *, limit):
        self.calls.append(("resolve", scope, value_kind, source_digest, limit))
        return tuple(key for key in self.entries if key.scope == scope and key.value_kind == value_kind
                     and key.source_content_digest == source_digest)

    def put_if_absent(self, entry):
        self.calls.append(("put_if_absent", entry.key))
        winner = self.entries.get(entry.key)
        if winner is None:
            self.entries[entry.key] = entry
            return PutIfAbsentResult(PutIfAbsentState.INSERTED)
        if winner.value == entry.value and winner.provenance == entry.provenance:
            return PutIfAbsentResult(PutIfAbsentState.IDEMPOTENT)
        return PutIfAbsentResult(PutIfAbsentState.CONFLICT)


class Memory:
    def __init__(self, candidate=None):
        self.candidate = candidate
        self.queries = []

    def lookup(self, query):
        self.queries.append(query)
        return self.candidate(query) if callable(self.candidate) else self.candidate


class FakeArtifacts:
    def __init__(self):
        self.calls = []
        self.reuse_calls = []

    def find_reusable(self, request):
        self.reuse_calls.append(request)
        return ReuseResult(None, "artifact_not_found")

    def publish(self, request, chunks, *, owner_token, cancellation=None):
        payload = b"".join(chunks)
        self.calls.append((request, payload))
        return new_artifact(request, str(uuid4()), NOW)


class FakeProvenance:
    def __init__(self):
        self.calls = []

    def append(self, request):
        self.calls.append(request)
        return SimpleNamespace(record_id=str(uuid4()))


class FakeEvents:
    def __init__(self):
        self.calls = []

    def emit(self, event):
        self.calls.append(event)


@dataclass
class Glossed:
    text: str
    usage: object = "glossary-applied"


class Env:
    def __init__(self, plan=None, *, cache=None, memory=None, provider=None, glossary=None):
        self.plan = plan or make_plan()
        self.cache = cache
        self.memory = memory
        self.provider = provider or Provider(self.plan)
        self.glossary = glossary or (lambda value: Glossed(value.upper()))
        self.artifacts = FakeArtifacts()
        self.provenance = FakeProvenance()
        self.events = FakeEvents()
        self.job = TranslationJob(
            str(UUID(int=0x8765)),
            JobRequest(PROJECT, "translate", 1, "sha256:" + "c" * 64),
            JobState.RUNNING, 1, NOW, NOW,
            JobLease(OWNER, NOW, NOW + timedelta(minutes=15)),
        )

    def translate(self, subject=None, *, context=None, **kwargs):
        translator = DocumentTranslator(
            self.plan, self.provider, self.glossary, self.artifacts, self.provenance, self.events,
            exact_cache=self.cache, memory=self.memory,
            retrieval_policy=RetrievalPolicy(legacy_cache_enabled=self.memory is not None), clock=lambda: NOW, new_id=lambda: str(uuid4()),
        )
        return translator.translate(subject or document(), context or ExecutionContext(self.job, OWNER), **kwargs)
