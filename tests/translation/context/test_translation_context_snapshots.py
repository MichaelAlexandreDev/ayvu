from dataclasses import replace
from datetime import timedelta
import pytest
from ayvu.translation.context.execution import require_context_egress
from ayvu.translation.context.source import SnapshotContextCandidateSource, StoredContextRevision, snapshot_context_sources
from ayvu.knowledge.glossary.catalog import Entry, Snapshot
from ayvu.projects.profiles.model import ProjectReference, ProjectTranslationProfile
from ayvu.providers.ports import Requirement
from ayvu.providers.network import NetworkClass
from ayvu.providers.capabilities import EvidenceSource, LimitOrigin, LimitScope, LimitSemantics, LimitUnit, ProviderLimit, SupportState
from ayvu.translation.context.model import ContextCandidate, ContextCapabilityEvidence, ContextCategory, ContextError, ContextKind, ContextLimits, ContextPolicyVersion, ContextSourceLimits, ContextSourceRequest, OmissionReason, TranslationContextBuilder
from tests.support.project_profiles_support import snapshot as profile_snapshot, binding, facts
from tests.support.translation_document_support import document, cue, NOW, PROJECT
from tests.support.translation_context_support import Source, build, egress_decision, entry, request


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
