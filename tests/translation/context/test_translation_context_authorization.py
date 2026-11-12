"""Contratos puros #103/#140/#141/#142/#153/#163 e limites de egress #159."""
from dataclasses import replace
from datetime import timedelta
import pytest
from ayvu.translation.context.source import SnapshotContextCandidateSource, StoredContextRevision
from ayvu.providers.ports import UnitReference
from ayvu.providers.capabilities import Capability, CapabilityFact, EvidenceSource, LimitOrigin, LimitScope, LimitSemantics, LimitUnit, ProviderCapabilityError, ProviderLimit, SupportState
from ayvu.translation.context.model import ContextCandidate, ContextError, ContextKind, ContextLimits, ContextSourceLimits, OmissionReason, TranslationContextBuilder
from tests.support.translation_document_support import NOW
from tests.support.translation_context_support import Source, entry, request


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
