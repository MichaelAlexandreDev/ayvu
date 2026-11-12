from dataclasses import replace
import pytest
from ayvu.translation.context.source import SnapshotContextCandidateSource, StoredContextRevision
from ayvu.translation.context.model import ContextCandidate, ContextError, ContextKind, ContextPolicyVersion, ContextSourceLimits, ContextSourceOversize, ContextSourceRequest, ContextSourceResult, OmissionReason, TranslationContextBuilder
from tests.support.translation_document_support import NOW
from tests.support.translation_context_support import Source, entry, request


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
