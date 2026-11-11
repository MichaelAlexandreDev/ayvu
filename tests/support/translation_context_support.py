from dataclasses import replace, FrozenInstanceError
from datetime import timedelta
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import pytest
from ayvu.translation.context.execution import (
    ContextExecution, ResolvedContextEgressDecision, plan_with_context, require_context_egress,
)
from ayvu.translation.context.source import SnapshotContextCandidateSource, StoredContextRevision, snapshot_context_sources
from ayvu.knowledge.glossary.catalog import Entry, Snapshot
from ayvu.projects.profiles.model import ProjectReference, ProjectTranslationProfile
from ayvu.providers.ports import Requirement, UnitReference
from ayvu.providers.network import ResolvedProviderNetworkDecision, NetworkClass
from ayvu.providers.capabilities import (
    Capability, CapabilityFact, EvidenceSource, LimitOrigin, LimitScope, LimitSemantics, LimitUnit,
    ProviderCapabilityError, ProviderLimit, SupportState,
)
from ayvu.translation.context.model import (
    ContextAuthorization, ContextBuildRequest, ContextCandidate, ContextCapabilityEvidence, ContextCategory,
    ContextDestination, ContextEntryRevision, ContextError, ContextKind, ContextLimits, ContextPolicyVersion,
    ContextPurpose, ContextSnapshot, ContextSourceLimits, ContextSourceOversize, ContextSourceRequest, ContextSourceResult,
    OmissionReason, TranslationContextBuilder, content_identity,
)
from tests.support.project_profiles_support import snapshot as profile_snapshot, binding, facts
from tests.support.translation_document_support import document, cue, NOW, PROJECT
from tests.support.translation_plan_support import snapshot as capabilities, plan

OPERATION = '00000000-0000-0000-0000-000000000999'


DESTINATION = ContextDestination('provider:' + '1' * 64, None, 'destination:' + '2' * 64, 1)


def egress_decision(disclosure):
    network = ResolvedProviderNetworkDecision(True, NetworkClass.LOOPBACK_ONLY, 'loopback_permitted',
        'sha256:' + disclosure.binding(), disclosure.destination.policy_revision, 'network_policy',
        NOW, NOW + timedelta(minutes=3))
    return ResolvedContextEgressDecision(disclosure, True, NOW, NOW + timedelta(minutes=3), network)


def entry(text='contexto', *, index=1, kind=ContextKind.TERMINOLOGY, required=False, revision=None, position=None):
    return ContextEntryRevision(PROJECT, 'context_source:' + f'{index:064x}', revision or f'{index:064x}',
        content_identity(PROJECT, text), kind, Requirement.REQUIRED if required else Requirement.ADVISORY,
        (index,) if position is None else position)


def request(entries=(), *, state=SupportState.SUPPORTED, limits=ContextLimits(), source_limits=ContextSourceLimits()):
    d = document()
    p = replace(profile_snapshot(), project=ProjectReference(PROJECT))
    s = ContextSnapshot(p, UnitReference(d.document_id, d.revision_id, d.units[0].unit_id), d.units[0], entries)
    auth = ContextAuthorization(PROJECT, OPERATION, ContextPurpose.TRANSLATION, frozenset(e.kind for e in entries),
        frozenset(e.category for e in entries), ContextPolicyVersion.V1, p.reference.value, s.unit,
        NOW - timedelta(seconds=1), NOW + timedelta(minutes=5))
    evidence = ContextCapabilityEvidence(capabilities(context=state), NOW + timedelta(minutes=5))
    return ContextBuildRequest(s, auth, OPERATION, evidence, DESTINATION, limits, source_limits)


class Source:
    def __init__(self, candidates=(), result=None):
        self.candidates, self.result, self.calls = candidates, result, []

    def read(self, r):
        self.calls.append(r)
        return self.result or ContextSourceResult(self.candidates)


def build(r=None, texts=('contexto',), **changes):
    entries = tuple(entry(t, index=i + 1, **changes) for i, t in enumerate(texts))
    r = r or request(entries)
    source = Source(tuple(ContextCandidate(e, text) for e, text in zip(r.snapshot.entries, texts)))
    return TranslationContextBuilder(source).build(r, now=NOW)
