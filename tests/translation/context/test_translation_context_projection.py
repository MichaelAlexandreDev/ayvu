from dataclasses import replace
from datetime import timedelta
import subprocess
import sys
import pytest
from ayvu.translation.context.execution import plan_with_context, require_context_egress
from ayvu.providers.capabilities import SupportState
from ayvu.translation.context.model import ContextCandidate, ContextError, ContextLimits, TranslationContextBuilder
from tests.support.translation_document_support import NOW
from tests.support.translation_plan_support import plan
from tests.support.translation_context_support import Source, build, egress_decision, entry, request
from tests.support.paths import PROJECT_ROOT


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
    env = dict(os.environ, PYTHONPATH=os.pathsep.join((str(PROJECT_ROOT), str(PROJECT_ROOT / 'src'))))
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
