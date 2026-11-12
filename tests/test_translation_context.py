"""Contratos puros #103/#140/#141/#142/#153/#163 e limites de egress #159."""
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

from tests.support.translation_context_support import Source, build, egress_decision, entry, request


@pytest.mark.parametrize('field,cap', [('entries', 64), ('entry_bytes', 4096), ('total_bytes', 32768), ('neighbors', 8)])
def test_policy_exact_caps_and_maximum_plus_one_requests_never_call_source(field, cap):
    assert getattr(ContextLimits(**{field: cap}), field) == cap
    with pytest.raises(ContextError, match='context_limit'):
        ContextLimits(**{field: cap + 1})
    if field != 'neighbors':
        assert getattr(ContextSourceLimits(**{field: cap}), field) == cap
        with pytest.raises(ContextError, match='context_limit'):
            ContextSourceLimits(**{field: cap + 1})


@pytest.mark.parametrize('value', [-1, True, 1.5, '64', None])
def test_limits_are_closed_integers(value):
    with pytest.raises(ContextError, match='context_limit'):
        ContextLimits(entries=value)


@pytest.mark.parametrize('count', [64, 65])
@pytest.mark.parametrize('required', [False, True])
def test_selected_entry_count_maximum_and_maximum_plus_one(count, required):
    entries = tuple(entry(index=i, required=required) for i in range(count))
    loaded = []
    source = SnapshotContextCandidateSource(tuple(StoredContextRevision(e, 8,
        lambda cap: loaded.append(cap) or 'contexto') for e in entries))
    r = request(entries)
    if count == 65 and required:
        with pytest.raises(ContextError, match='context_required_limit'):
            TranslationContextBuilder(source).build(r, now=NOW)
        assert not loaded
    else:
        result = TranslationContextBuilder(source).build(r, now=NOW)
        assert len(result.selected) == (64 if count == 64 else 0)
        assert len(result.omitted) == (65 if count == 65 else 0)
        if count == 65:
            assert not loaded and all(o.reason is OmissionReason.LIMIT for o in result.omitted)


@pytest.mark.parametrize('required', [False, True])
@pytest.mark.parametrize('size', [4096, 4097])
def test_entry_utf8_maximum_and_plus_one_at_local_selection(required, size):
    text = 'é' * (size // 2) + ('x' if size % 2 else '')
    e = entry(text, required=required)
    source = SnapshotContextCandidateSource((StoredContextRevision(e, size, lambda limit: pytest.fail('oversize loaded') if size > 4096 else text),))
    r = request((e,))
    if size == 4097 and required:
        with pytest.raises(ContextError, match='context_required_limit'):
            TranslationContextBuilder(source).build(r, now=NOW)
    else:
        out = TranslationContextBuilder(source).build(r, now=NOW)
        assert len(out.selected) == (size == 4096)
        if size == 4097:
            assert out.omitted[0].reason is OmissionReason.LIMIT


@pytest.mark.parametrize('extra', [0, 1])
@pytest.mark.parametrize('required', [False, True])
def test_total_execution_bytes_exact_maximum_and_plus_one(extra, required):
    # Inclui os sete LF da representação, cada entrada permanece <= 4096.
    texts = ('x' * 4096,) * 7 + ('x' * (4089 + extra),)
    entries = tuple(entry(t, index=i, required=required) for i, t in enumerate(texts))
    r = request(entries)
    src = Source(tuple(ContextCandidate(e, t) for e, t in zip(entries, texts)))
    if extra and required:
        with pytest.raises(ContextError, match='context_required_limit'):
            TranslationContextBuilder(src).build(r, now=NOW)
    else:
        out = TranslationContextBuilder(src).build(r, now=NOW)
        assert out.disclosure.utf8_bytes == (28678 if extra else 32768)
        assert len(out.selected) == (7 if extra else 8)


@pytest.mark.parametrize('count', [8, 9])
@pytest.mark.parametrize('required', [False, True])
def test_neighbor_maximum_and_plus_one(count, required):
    entries = tuple(entry(kind=ContextKind.NEIGHBOR, required=required, index=i) for i in range(count))
    r = request(entries)
    src = Source(tuple(ContextCandidate(e, 'contexto') for e in entries))
    if count == 9 and required:
        with pytest.raises(ContextError, match='context_required_limit'):
            TranslationContextBuilder(src).build(r, now=NOW)
    else:
        out = TranslationContextBuilder(src).build(r, now=NOW)
        assert len(out.selected) == 8
        assert tuple(o.reason for o in out.omitted) == (() if count == 8 else (OmissionReason.NEIGHBOR_LIMIT,))


@pytest.mark.parametrize('changes', [None, {}, {'expires_at': NOW}, {'issued_at': NOW + timedelta(seconds=1)},
    {'project_id': '00000000-0000-0000-0000-000000000001'}, {'operation_id': str(1)},
    {'purpose': 'translation'}, {'policy': 'bounded_context_v1'}, {'profile_revision': 'f' * 64},
    {'source_kinds': frozenset()}, {'categories': frozenset()}, {'source_kinds': {'terminology'}},
    {'categories': frozenset({'terminology'})}, {'issued_at': 'malformed'},
    {'unit': UnitReference('doc:v1:' + '1' * 64, 'rev:v1:' + '2' * 64, 'unit:v1:' + '3' * 64)}])
def test_every_invalid_authorization_fails_at_authorization_before_source(changes):
    r = request((entry(),))
    auth = None if changes is None else object() if changes == {} else replace(r.authorization, **changes)
    src = Source()
    with pytest.raises(ContextError, match='^context_authorization$'):
        TranslationContextBuilder(src).build(replace(r, authorization=auth), now=NOW)
    assert src.calls == []


@pytest.mark.parametrize('state', list(SupportState))
@pytest.mark.parametrize('required', [True, False])
def test_capability_states_are_not_inferred_from_provider_name(state, required):
    e = entry(required=required)
    r = request((e,), state=state)
    src = Source((ContextCandidate(e, 'contexto'),))
    builder = TranslationContextBuilder(src)
    if state in (SupportState.UNKNOWN, SupportState.CONFIGURATION_DEPENDENT):
        with pytest.raises(ContextError, match='context_preflight_required'):
            builder.build(r, now=NOW)
        assert src.calls == []
    elif state is SupportState.UNSUPPORTED and required:
        with pytest.raises(ContextError, match='context_required_unsupported'):
            builder.build(r, now=NOW)
        assert src.calls == []
    else:
        result = builder.build(r, now=NOW)
        assert (result.feature is None) == (state is SupportState.UNSUPPORTED)
        if state is SupportState.UNSUPPORTED:
            assert result.warnings[0].code == 'context_unsupported' and src.calls == []
            assert result.disclosure.utf8_bytes == 0


@pytest.mark.parametrize('change,code', [({'expires_at': NOW}, 'context_preflight_required'),
    ({'expires_at': NOW - timedelta(seconds=1)}, 'context_preflight_required')])
def test_stale_or_contradictory_capability_evidence_requires_bounded_preflight(change, code):
    r = request((entry(),))
    src = Source()
    with pytest.raises(ContextError, match=code):
        TranslationContextBuilder(src).build(replace(r, capabilities=replace(r.capabilities, **change)), now=NOW)
    assert not src.calls


@pytest.mark.parametrize('required', [True, False])
def test_known_provider_character_budget_smaller_than_local(required):
    e = entry(required=required)
    r = request((e,))
    limit = ProviderLimit('context_characters', LimitUnit.CHARACTERS, LimitScope.REQUEST, LimitOrigin.SERVER,
        LimitSemantics.KNOWN, EvidenceSource.PROVIDER_DOCUMENTATION, 3)
    r = replace(r, capabilities=replace(r.capabilities, snapshot=replace(r.capabilities.snapshot, limits=(limit,))))
    src = Source((ContextCandidate(e, 'contexto'),))
    if required:
        with pytest.raises(ContextError, match='context_required_limit'):
            TranslationContextBuilder(src).build(r, now=NOW)
    else:
        result = TranslationContextBuilder(src).build(r, now=NOW)
        assert not result.selected and result.omitted[0].reason is OmissionReason.LIMIT


def test_contradictory_103_fact_is_rejected_by_its_owner():
    with pytest.raises(ProviderCapabilityError, match='contradictory_capability_claim'):
        CapabilityFact(Capability.CONTEXT, SupportState.SUPPORTED, EvidenceSource.NONE)


@pytest.mark.parametrize('dimension', ['count', 'entry', 'total'])
@pytest.mark.parametrize('extra', [0, 1])
def test_source_checks_stored_sizes_before_loading_and_returns_content_free_oversize(dimension, extra):
    if dimension == 'count':
        texts, limits = ('x',) * (2 + extra), ContextSourceLimits(entries=2)
    elif dimension == 'entry':
        texts, limits = ('é' * 2048 + 'x' * extra,), ContextSourceLimits()
    else:
        texts, limits = ('x' * 4096,) * 8 + (('x',) if extra else ()), ContextSourceLimits()
    entries = tuple(entry(t, index=i) for i, t in enumerate(texts))
    loaded = []
    records = tuple(StoredContextRevision(e, len(t.encode()), lambda cap, text=t: loaded.append(cap) or text)
                    for e, t in zip(entries, texts))
    r = request(entries)
    source = SnapshotContextCandidateSource(records)
    result = source.read(ContextSourceRequest(r.snapshot, r.authorization.source_kinds, limits))
    if extra:
        assert result == ContextSourceResult(oversize=ContextSourceOversize())
        assert loaded == [] and result.candidates == ()
        assert set(vars(result.oversize)) == {'reason'}
    else:
        assert len(result.candidates) == len(entries) and loaded == [4096] * len(entries)


def test_source_count_exact_hard_maximum_and_bounded_request():
    entries = tuple(entry('x', index=i) for i in range(64))
    r = request(entries)
    src = Source(tuple(ContextCandidate(e, 'x') for e in entries))
    out = TranslationContextBuilder(src).build(r, now=NOW)
    assert len(out.selected) == 64 and len(src.calls) == 1
    assert src.calls[0].limits == ContextSourceLimits(64, 4096, 32768)
    assert src.calls[0].snapshot == r.snapshot
    assert src.calls[0].source_kinds == frozenset({ContextKind.TERMINOLOGY})


@pytest.mark.parametrize('attack', ['duplicate', 'changed_text', 'revision', 'cross_project', 'purpose', 'kind', 'extra', 'count'])
def test_corrupt_or_expanding_source_is_rejected_at_source_integrity(attack):
    e = entry()
    r = request((e,))
    c = ContextCandidate(e, 'contexto')
    candidates = (c,)
    if attack == 'duplicate': candidates = (c, c)
    if attack == 'changed_text': candidates = (replace(c, text='canary'),)
    if attack == 'revision': candidates = (replace(c, source=replace(e, revision='f' * 64)),)
    if attack == 'cross_project': candidates = (replace(c, source=replace(e, project_id='00000000-0000-0000-0000-000000000001')),)
    if attack == 'purpose': candidates = (object(),)
    if attack == 'kind': candidates = (replace(c, source=replace(e, kind=ContextKind.PROTECTED_TERM)),)
    if attack == 'extra': candidates = (c, ContextCandidate(entry(index=2), 'contexto'))
    if attack == 'count': candidates = (c,) * 65
    src = Source(candidates)
    with pytest.raises(ContextError, match='context_source_failed' if attack == 'count' else 'context_integrity'):
        TranslationContextBuilder(src).build(r, now=NOW)
    assert len(src.calls) == 1


def test_immutable_owner_revision_detects_content_changed_under_same_revision():
    e = entry('original')
    src = SnapshotContextCandidateSource((StoredContextRevision(e, 8, lambda cap: 'alterado'),))
    r = request((e,))
    with pytest.raises(ContextError, match='context_integrity'):
        TranslationContextBuilder(src).build(r, now=NOW)


def test_missing_required_fails_and_advisory_records_reason():
    for required in (False, True):
        e = entry(required=required)
        src = Source()
        if required:
            with pytest.raises(ContextError, match='context_required_missing'):
                TranslationContextBuilder(src).build(request((e,)), now=NOW)
        else:
            out = TranslationContextBuilder(src).build(request((e,)), now=NOW)
            assert out.omitted[0].reason is OmissionReason.MISSING


def test_ordering_and_tie_breakers_are_closed_and_ignore_insertion_order():
    texts = ['n', 'z', 'a', 'p', 'u', 't']
    kinds = [ContextKind.NEIGHBOR, ContextKind.TERMINOLOGY, ContextKind.TERMINOLOGY,
             ContextKind.PROFILE, ContextKind.CURRENT_UNIT, ContextKind.PROTECTED_TERM]
    es = tuple(entry(t, kind=k, index=i, position=(1,)) for i, (t, k) in enumerate(zip(texts, kinds)))
    r = request(es)
    cs = tuple(ContextCandidate(e, t) for e, t in zip(es, texts))
    first = TranslationContextBuilder(Source(cs)).build(r, now=NOW)
    second = TranslationContextBuilder(Source(tuple(reversed(cs)))).build(
        replace(r, snapshot=replace(r.snapshot, entries=tuple(reversed(es)))), now=NOW)
    assert first == second
    assert first.feature.items[0].text == 'p\nu\nz\na\nt\nn'
    assert [e.kind for e in first.selected] == list(ContextPolicyVersion.V1.precedence[:2]) + [ContextKind.TERMINOLOGY] * 2 + [ContextKind.PROTECTED_TERM, ContextKind.NEIGHBOR]


def test_restart_determinism_for_projection_disclosure_warnings_and_omissions(tmp_path):
    script = '''from dataclasses import replace
from tests.support.translation_context_support import *
es=tuple(entry(index=i,position=(1,)) for i in dict.fromkeys(int(x) for x in ORDER))
r=request(es,limits=ContextLimits(entries=2))
s=Source(tuple(ContextCandidate(e,'contexto') for e in es))
c=TranslationContextBuilder(s).build(r,now=NOW)
print(c.semantic_projection.hex(),c.disclosure.binding(),c.diagnostic(),tuple(w.code for w in c.warnings),
      tuple((e.reference,e.revision,e.position) for e in c.selected),
      tuple((o.source.reference,o.reason.value) for o in c.omitted))
'''
    outputs = []
    import os
    env = dict(os.environ, PYTHONPATH=os.pathsep.join((str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parents[1] / 'src'))))
    for order in (['3', '1', '2'], ['2', '3', '1']):
        outputs.append(subprocess.check_output([sys.executable, '-c', 'ORDER=' + repr(order) + '\n' + script], env=env, cwd=tmp_path))
    assert outputs[0] == outputs[1]


def test_153_raw_identity_changes_for_exact_selected_effects_only():
    base = build()
    p = plan()
    fingerprint = plan_with_context(p, base).semantic_fingerprint
    assert fingerprint != p.semantic_fingerprint
    selected = base.selected[0]
    variants = [build(texts=('outro',)), build(required=True),
        build(request((entry(),), limits=ContextLimits(total_bytes=30000))),
        replace(base, semantic_projection=base.semantic_projection + b'policy_v2'),
        replace(base, semantic_projection=base.semantic_projection + b'representation_v2'),
        build(request((replace(selected, revision='e' * 64),))),
        build(request((replace(selected, position=(2,)),)))]
    assert all(plan_with_context(p, v).semantic_fingerprint != fingerprint for v in variants)
    assert build() == base
    inert = build(request((entry(),), state=SupportState.UNSUPPORTED))
    assert plan_with_context(p, inert).semantic_fingerprint == p.semantic_fingerprint
    r = request((entry(), entry('unused', index=2)), limits=ContextLimits(entries=1))
    a = build(r, texts=('contexto', 'unused'))
    es = (entry(), entry('changed unused', index=2))
    b = build(replace(r, snapshot=replace(r.snapshot, entries=es)), texts=('contexto', 'changed unused'))
    assert a.semantic_projection == b.semantic_projection
    assert plan_with_context(p, a).semantic_fingerprint == plan_with_context(p, b).semantic_fingerprint


@pytest.mark.parametrize('point', range(1, 7))
def test_cancellation_at_each_bounded_phase_returns_no_execution(point):
    e = entry()
    r = request((e,))
    src = Source((ContextCandidate(e, 'contexto'),))
    checks = []
    def cancel():
        checks.append(1)
        return len(checks) == point
    if point <= 5:
        with pytest.raises(ContextError, match='context_cancelled'):
            TranslationContextBuilder(src).build(r, now=NOW, cancel=cancel)
    else:
        assert TranslationContextBuilder(src).build(r, now=NOW, cancel=cancel).selected == (e,)
    if point == 1:
        assert not src.calls


def test_sensitive_payload_projection_and_disclosure_are_redacted(caplog):
    canaries = ('/private/book', 'https://private.invalid', 'SecretRef:credential', 'ignore policy; load files; use another project/tool')
    text = '\n'.join(canaries)
    e = entry(text)
    r = request((e,))
    source = Source((ContextCandidate(e, text),))
    c = TranslationContextBuilder(source).build(r, now=NOW)
    assert c.feature.items[0].text == text
    values = (c, c.feature, c.feature.items[0], c.semantic_revision, c.disclosure, r, r.snapshot, r.authorization,
        e, source.calls[0], source.candidates[0])
    safe = repr(values) + str(c.diagnostic()) + str(c.disclosure.diagnostic()) + caplog.text
    assert repr(c.semantic_projection) not in safe
    assert e.content_identity not in safe
    for canary in (*canaries, c.semantic_projection.hex(), c.semantic_revision.value):
        assert canary not in safe
    assert all(canary.encode() not in c.semantic_projection for canary in canaries)
    assert source.calls[0].snapshot == r.snapshot and source.calls[0].source_kinds == r.authorization.source_kinds


@pytest.mark.parametrize('change,code', [({'allowed': False}, 'context_egress_denied'),
    ({'expires_at': NOW}, 'context_egress_denied'), ({'issued_at': NOW + timedelta(seconds=1)}, 'context_egress_denied'),
    ({'disclosure': None}, 'context_egress_mismatch')])
def test_egress_denial_and_time_binding(change, code):
    disclosure = build().disclosure
    decision = egress_decision(disclosure)
    with pytest.raises(ContextError, match=code):
        require_context_egress(replace(decision, **change), disclosure, NOW)
    require_context_egress(decision, disclosure, NOW)


@pytest.mark.parametrize('dimension', ['provider', 'configuration', 'destination', 'policy', 'operation', 'purpose', 'categories', 'entries', 'bytes'])
def test_egress_exact_disclosure_binding(dimension):
    disclosure = build().disclosure
    dest = disclosure.destination
    changed = {'provider': lambda: replace(disclosure, destination=replace(dest, provider_id='provider:' + 'f' * 64)),
        'configuration': lambda: replace(disclosure, destination=replace(dest, configuration_ref='config:' + 'f' * 64)),
        'destination': lambda: replace(disclosure, destination=replace(dest, destination_ref='destination:' + 'f' * 64)),
        'policy': lambda: replace(disclosure, destination=replace(dest, policy_revision=2)),
        'operation': lambda: replace(disclosure, operation_id='00000000-0000-0000-0000-000000000123'),
        'purpose': lambda: object(),
        'categories': lambda: replace(disclosure, categories=()), 'entries': lambda: replace(disclosure, selected_entries=0),
        'bytes': lambda: replace(disclosure, utf8_bytes=0)}[dimension]()
    decision = replace(egress_decision(disclosure), disclosure=changed)
    with pytest.raises(ContextError, match='context_egress_mismatch'):
        require_context_egress(decision, disclosure, NOW)


def test_owner_snapshots_supply_only_explicit_sources_and_matching_managed_terms():
    from ayvu.content.model import ContextReference
    from tests.support.translation_document_support import locator
    d = document(cue('a', 'Alice and Bob', 1, context=(ContextReference('previous', locator('b')),)), cue('b', 'vizinho', 2))
    gloss = binding(name='managed.json')
    p = replace(profile_snapshot(ProjectTranslationProfile(glossary=gloss), plan=facts(glossary=gloss, native=True)), project=ProjectReference(PROJECT))
    terminology = Snapshot(gloss.reference, (Entry('Alice', 'translate', 'Alícia'), Entry('Bob', 'preserve'), Entry('unrelated', 'translate', 'x')), gloss.revision.value)
    s, source = snapshot_context_sources(p, d, d.units[0].unit_id, neighbors=(d.units[1].unit_id,), include_current=True,
        include_profile=True, terminology=terminology)
    assert {e.kind for e in s.entries} == set(ContextKind)
    assert len(s.entries) == 5
    r = request()
    auth = replace(r.authorization, profile_revision=p.reference.value, unit=s.unit,
        source_kinds=frozenset(ContextKind), categories=frozenset(ContextCategory))
    out = TranslationContextBuilder(source).build(replace(r, snapshot=s, authorization=auth), now=NOW)
    assert out.feature.items[0].text == 'pt\nAlice and Bob\nAlice\nAlícia\nBob\nvizinho'
    assert out.disclosure.utf8_bytes == len(out.feature.items[0].text.encode())
    assert set(out.disclosure.categories) == set(ContextCategory)
    with pytest.raises(ContextError, match='invalid_context'):
        snapshot_context_sources(p, d, d.units[0].unit_id, neighbors=('unit:v1:' + 'f' * 64,))
    with pytest.raises(ContextError, match='context_integrity'):
        snapshot_context_sources(p, d, d.units[0].unit_id, terminology=replace(terminology, revision='f' * 64))


@pytest.mark.parametrize('change', [{'reference': '/private/file'}, {'revision': ''},
    {'content_identity': ''}, {'kind': 'notes'}, {'purpose': 'tools'}, {'requirement': 'required'},
    {'position': (-1,)}, {'position': (True,)}, {'position': (1,) * 17},
    {'kind': ContextKind.NEIGHBOR, 'position': ()}])
def test_candidate_revision_schema_rejects_unversioned_or_untrusted_selectors(change):
    with pytest.raises(ContextError, match='invalid_context'):
        replace(entry(), **change)


@pytest.mark.parametrize('field,cap', [('entries', 64), ('entry_bytes', 4096), ('total_bytes', 32768)])
def test_even_deserialized_over_cap_source_requests_fail_before_port(field, cap):
    # Simula uma desserialização que não chamou o construtor do contrato.
    limits = ContextSourceLimits()
    object.__setattr__(limits, field, cap + 1)
    source = Source()
    with pytest.raises(ContextError, match='context_limit'):
        TranslationContextBuilder(source).build(replace(request(), source_limits=limits), now=NOW)
    assert not source.calls


@pytest.mark.parametrize('field,value', [('purpose', 'tool_use'), ('revision', ''), ('kind', 'notes')])
def test_malformed_snapshot_entries_fail_closed_before_any_port(field, value):
    e = entry()
    r = request((e,))
    object.__setattr__(e, field, value)
    source = Source()
    with pytest.raises(ContextError, match='context_authorization'):
        TranslationContextBuilder(source).build(r, now=NOW)
    assert not source.calls


@pytest.mark.parametrize('variation', ['unknown_limit', 'future', 'configuration', 'wrong_unit', 'fractional'])
def test_unresolved_capability_limit_or_binding_never_loads_context(variation):
    r = request((entry(),))
    snapshot = r.capabilities.snapshot
    code = 'context_preflight_required'
    if variation == 'future':
        snapshot = replace(snapshot, observed_at=NOW + timedelta(seconds=1))
    elif variation == 'configuration':
        snapshot = replace(snapshot, configuration_ref='config:' + 'a' * 64)
        code = 'context_capabilities'
    else:
        semantics = LimitSemantics.UNKNOWN if variation == 'unknown_limit' else LimitSemantics.KNOWN
        limit = ProviderLimit('context_characters',
            LimitUnit.REQUESTS if variation == 'wrong_unit' else LimitUnit.CHARACTERS,
            LimitScope.REQUEST, LimitOrigin.SERVER, semantics,
            EvidenceSource.NONE if variation == 'unknown_limit' else EvidenceSource.PROVIDER_DOCUMENTATION,
            None if variation == 'unknown_limit' else 3.5 if variation == 'fractional' else 10)
        snapshot = replace(snapshot, limits=(limit,))
        if variation == 'fractional':
            code = 'context_capabilities'
    source = Source()
    with pytest.raises(ContextError, match=code):
        TranslationContextBuilder(source).build(replace(r, capabilities=replace(r.capabilities, snapshot=snapshot)), now=NOW)
    assert not source.calls


def test_unicode_provider_budget_and_whole_entry_minimization():
    text = '😀é'
    r = request((entry(text),))
    limit = ProviderLimit('context_characters', LimitUnit.CHARACTERS, LimitScope.REQUEST, LimitOrigin.SERVER,
        LimitSemantics.KNOWN, EvidenceSource.PROVIDER_DOCUMENTATION, 6)
    r = replace(r, capabilities=replace(r.capabilities, snapshot=replace(r.capabilities.snapshot, limits=(limit,))))
    out = build(r, texts=(text,))
    assert out.feature.items[0].text == text and out.disclosure.utf8_bytes == 6
    r = replace(r, limits=ContextLimits(total_bytes=5))
    out = build(r, texts=(text,))
    assert out.feature is None and out.omitted[0].reason is OmissionReason.LIMIT


def test_snapshot_adapter_never_loads_unrequested_sources_or_other_projects():
    e, extra = entry(), entry(index=2)
    foreign = replace(extra, project_id='00000000-0000-0000-0000-000000000001')
    loaded = []
    source = SnapshotContextCandidateSource((StoredContextRevision(e, 8, lambda cap: loaded.append(e.reference) or 'contexto'),
        StoredContextRevision(extra, 8, lambda cap: pytest.fail('unrequested source')),
        StoredContextRevision(foreign, 8, lambda cap: pytest.fail('foreign source'))))
    out = TranslationContextBuilder(source).build(request((e,)), now=NOW)
    assert out.selected == (e,) and loaded == [e.reference]
    with pytest.raises(ContextError, match='invalid_context'):
        ContextSourceRequest(request((e,)).snapshot, frozenset(ContextKind), ContextSourceLimits())


def test_managed_required_terms_cannot_be_downgraded_to_advisory():
    d = document(cue('a', 'Alice', 1))
    gloss = binding(name='managed.json')
    p = replace(profile_snapshot(ProjectTranslationProfile(glossary=gloss), plan=facts(glossary=gloss, native=True)),
        project=ProjectReference(PROJECT))
    terminology = Snapshot(gloss.reference, (Entry('Alice', 'preserve', required=True),), gloss.revision.value)
    snapshot, source = snapshot_context_sources(p, d, d.units[0].unit_id, terminology=terminology)
    assert snapshot.entries[0].requirement is Requirement.REQUIRED
    r = request(snapshot.entries, state=SupportState.UNSUPPORTED)
    r = replace(r, snapshot=snapshot, authorization=replace(r.authorization, profile_revision=p.reference.value, unit=snapshot.unit))
    with pytest.raises(ContextError, match='context_required_unsupported'):
        TranslationContextBuilder(source).build(r, now=NOW)


@pytest.mark.parametrize('phase', ['capabilities', 'policy', 'cancellation'])
def test_injected_phase_failures_return_only_a_normalized_code_and_no_payload(monkeypatch, phase):
    canary = 'SECRET-CANARY /private/path https://private.invalid'
    def broken(*a, **kw):
        raise RuntimeError(canary)
    r = request((entry(),))
    source = Source((ContextCandidate(r.snapshot.entries[0], 'contexto'),))
    cancel = None
    if phase == 'capabilities':
        monkeypatch.setattr(ContextCapabilityEvidence, 'limits', broken)
    elif phase == 'policy':
        monkeypatch.setattr(ContextPolicyVersion, 'precedence', property(broken))
    else:
        cancel = broken
    with pytest.raises(ContextError, match='context_failed') as error:
        TranslationContextBuilder(source).build(r, now=NOW, cancel=cancel)
    assert canary not in str(error.value) + repr(error.value) and not source.calls


@pytest.mark.parametrize('network_class', [NetworkClass.STRICT_OFFLINE, NetworkClass.UNCLASSIFIED])
def test_contradictory_159_allow_never_turns_offline_or_unknown_into_authority(network_class):
    disclosure = build().disclosure
    decision = egress_decision(disclosure)
    decision = replace(decision, network=replace(decision.network, network_class=network_class))
    with pytest.raises(ContextError, match='context_egress_denied'):
        require_context_egress(decision, disclosure, NOW)


def test_policy_taxonomy_is_closed_and_snapshot_metadata_is_bounded():
    with pytest.raises(ValueError):
        ContextPolicyVersion('bounded_context_v2')
    with pytest.raises(ValueError):
        ContextKind('chapter_notes')
    with pytest.raises(ContextError, match='invalid_context'):
        request(tuple(entry(index=i) for i in range(66)))
    e = entry()
    with pytest.raises(ContextError, match='context_integrity'):
        request((e, e))
    with pytest.raises(ContextError, match='context_integrity'):
        SnapshotContextCandidateSource((StoredContextRevision(e, 8, lambda cap: 'contexto'),) * 2)
