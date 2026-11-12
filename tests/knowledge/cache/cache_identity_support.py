from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
import subprocess
import sys
from threading import Barrier, Lock
from uuid import UUID
import pytest
from ayvu.knowledge.cache.identity_values import (
    CACHE_IDENTITY_VERSION, MAX_CACHE_ONLY_CANDIDATES, MAX_COLLECTION_ENTRIES, MAX_OPAQUE_SCALAR_BYTES,
    MAX_ROUTE_HOPS, CacheIdentityError, CacheValueKind, CanonicalValue, OpaqueSemanticRevision,
    SemanticRouteHop, TRANSLATION_STAGE_AGGREGATE,
)
from ayvu.knowledge.cache.identity import MAX_CANONICAL_PAYLOAD_BYTES, SemanticOperationV2
from ayvu.knowledge.cache.scope import AuthorizedCacheScope, CacheAuthorizationPolicy, CacheScope, CacheScopeKind
from ayvu.knowledge.cache.compatible_memory import (
    CacheCompatibilityIdentity, CompatibleMemoryCandidate, CompatibleMemoryQuery, candidate_matches_query,
)
from ayvu.knowledge.cache.cache_only import (
    CacheIdentityResolutionState, CacheOnlyMode, CacheOnlyRequest, PinnedExecutionIdentityReference,
    resolve_compatible_identities,
)
from ayvu.knowledge.cache.entries import (
    CacheLookupState, CacheMissReason, CacheProvenanceMetadata, ExactCacheEntry, ExactCacheLookup,
    ExactEntryKey, PutIfAbsentResult, PutIfAbsentState, exact_entry_key, source_content_digest,
)
from ayvu.provenance.model import ProducerIdentity

PROJECT = str(UUID(int=0x1234))


PRODUCER = ProducerIdentity("provider@1", "model@1", "adapter@1", "pipeline@1")


CAPABILITIES = "capfp:v1:" + "a" * 64


PIPELINE_REVISION = OpaqueSemanticRevision("ayvu", "pipeline", 2, "translation-pipeline-v2")


SEGMENTATION_REVISION = OpaqueSemanticRevision("ayvu", "segmentation", 1, "unit-v1")


CHUNKING_REVISION = OpaqueSemanticRevision("ayvu", "chunking", 1, "chunks-v1")


PROTECTION_REVISION = OpaqueSemanticRevision("ayvu", "protection", 1, "protected-v1")


NORMALIZATION_REVISION = OpaqueSemanticRevision("ayvu", "normalization", 1, "whitespace-v1")


VALUE_KIND = TRANSLATION_STAGE_AGGREGATE


def hop(source="en", target="pt", producer=PRODUCER, capability=CAPABILITIES, config=None):
    return SemanticRouteHop(source, target, producer, capability, config)


def operation(**changes):
    values = {
        "operation": "translate",
        "source_locale": "en",
        "target_locale": "pt",
        "route_hops": (hop(),),
        "pipeline_revision": PIPELINE_REVISION,
        "stage_versions": (("unit_whitespace", 1), ("protection", 1), ("provider_chunks", 1),
                           ("placeholder_restoration", 1), ("raw_cache_write", 1)),
        "prompt_strategy": "none",
        "prompt_revision": None,
        "context_strategy": "none",
        "context_revision": None,
        "terminology_revision": None,
        "generation_parameters": (("temperature", CanonicalValue("decimal", Decimal("0.25"))),
                                   ("top_p", CanonicalValue("decimal", Decimal("1")))),
        "segmentation_revision": SEGMENTATION_REVISION,
        "chunk_limit": 3000,
        "chunking_revision": CHUNKING_REVISION,
        "protection_revision": PROTECTION_REVISION,
        "normalization_revision": NORMALIZATION_REVISION,
        "format_constraints": (("markup", CanonicalValue("boolean", True)),),
        "version": CACHE_IDENTITY_VERSION,
    }
    values.update(changes)
    return SemanticOperationV2(**values)


def canonical_pairs(subject=None):
    subject = subject or operation()
    return (
        ("operation", subject.operation),
        ("source_locale", subject.source_locale),
        ("target_locale", subject.target_locale),
        ("route_hops", subject.route_hops),
        ("pipeline_revision", subject.pipeline_revision),
        ("stage_versions", subject.stage_versions),
        ("prompt_strategy", subject.prompt_strategy),
        ("prompt_revision", subject.prompt_revision),
        ("context_strategy", subject.context_strategy),
        ("context_revision", subject.context_revision),
        ("terminology_revision", subject.terminology_revision),
        ("generation_parameters", subject.generation_parameters),
        ("segmentation_revision", subject.segmentation_revision),
        ("chunk_limit", subject.chunk_limit),
        ("chunking_revision", subject.chunking_revision),
        ("protection_revision", subject.protection_revision),
        ("normalization_revision", subject.normalization_revision),
        ("format_constraints", subject.format_constraints),
        ("version", subject.version),
    )


def _payload_at_limit():
    def size(candidate):
        try:
            return len(candidate.canonical_payload())
        except CacheIdentityError as error:
            if error.code == "canonical_payload_limit_exceeded":
                return MAX_CANONICAL_PAYLOAD_BYTES + 1
            raise

    for count in range(1, MAX_COLLECTION_ENTRIES + 1):
        prefix = tuple((f"pad_{index:03d}", CanonicalValue("string", "x" * MAX_OPAQUE_SCALAR_BYTES))
                       for index in range(count - 1))
        key = f"pad_{count - 1:03d}"

        def with_length(length):
            return operation(format_constraints=(*prefix, (key, CanonicalValue("string", "x" * length))))

        smallest, largest = with_length(1), with_length(MAX_OPAQUE_SCALAR_BYTES)
        minimum_size, maximum_size = size(smallest), size(largest)
        if minimum_size <= MAX_CANONICAL_PAYLOAD_BYTES <= maximum_size:
            low, high = 1, MAX_OPAQUE_SCALAR_BYTES
            while low <= high:
                middle = (low + high) // 2
                candidate = with_length(middle)
                candidate_size = size(candidate)
                if candidate_size == MAX_CANONICAL_PAYLOAD_BYTES:
                    return candidate, middle
                if candidate_size < MAX_CANONICAL_PAYLOAD_BYTES:
                    low = middle + 1
                else:
                    high = middle - 1
    raise AssertionError("could not construct the named canonical payload boundary")


class AtomicFake:
    def __init__(self):
        self.entries = {}
        self.lock = Lock()

    def put_if_absent(self, entry):
        with self.lock:
            current = self.entries.get(entry.key)
            if current is None:
                self.entries[entry.key] = entry
                return PutIfAbsentResult(PutIfAbsentState.INSERTED)
            if current.value == entry.value and current.provenance == entry.provenance:
                return PutIfAbsentResult(PutIfAbsentState.IDEMPOTENT)
            return PutIfAbsentResult(PutIfAbsentState.CONFLICT)


def _concurrent_put(entries):
    cache = AtomicFake()
    gate = Barrier(len(entries))

    def submit(entry):
        gate.wait()
        return cache.put_if_absent(entry).state

    with ThreadPoolExecutor(max_workers=len(entries)) as pool:
        statuses = tuple(pool.map(submit, entries))
    return cache, statuses


def _entry(value, *, model="model@1"):
    scope = CacheAuthorizationPolicy(PROJECT).resolve()
    key = exact_entry_key(scope, VALUE_KIND, operation().fingerprint, "source")
    producer = ProducerIdentity("provider@1", model, "adapter@1", "pipeline@1")
    provenance = CacheProvenanceMetadata("translate", producer, PIPELINE_REVISION)
    return ExactCacheEntry(key, value, provenance)
