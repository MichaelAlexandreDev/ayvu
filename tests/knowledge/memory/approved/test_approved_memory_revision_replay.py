from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier

import pytest

from ayvu.knowledge.memory.approved.model import MemoryState, MemoryValueError, PipelineStage, TargetValue, ValueKind
from ayvu.provenance.model import ConfigurationRef
from tests.support.approved_memory_fixtures import PIN, SCOPES, final

from tests.knowledge.memory.approved.approved_memory_support import env


def test_provider_and_glossary_values_need_distinct_final_revisions_and_review(env):
    provider_value = TargetValue('Olá', ValueKind.PROVIDER_RESPONSE, PipelineStage.PROVIDER, PIN)
    provider = env.create(target=provider_value)
    with pytest.raises(MemoryValueError, match='^approval_requires_linguistic_final$'):
        env.approve(provider)
    final_revision = env.repo.derive(env.derivation(provider, target=final()))
    approved = env.approve(final_revision)
    glossary_pin = replace(PIN, glossary=ConfigurationRef('glossary', 'glossary:' + 'd' * 64))
    derived = env.repo.derive(env.derivation(approved, target=TargetValue(
        'Termo novo', ValueKind.GLOSSARY_DERIVED, PipelineStage.TERMINOLOGY_APPLIED, glossary_pin)))
    assert derived.state is MemoryState.MACHINE_CANDIDATE
    with pytest.raises(MemoryValueError, match='^approval_requires_linguistic_final$'):
        env.approve(derived, prior=approved)
    next_final = env.repo.derive(env.derivation(derived, target=final('Termo novo', glossary_pin)))
    replacement = env.approve(next_final, prior=approved)
    assert replacement.value.target.pin == glossary_pin
    assert env.repo.get(provider.revision_id, scopes=SCOPES).value.target == provider_value
    assert env.repo.get(approved.revision_id, scopes=SCOPES).state is MemoryState.SUPERSEDED


def test_concurrent_idempotent_replay_creates_one_candidate_and_approval(env):
    create = env.creation()
    barrier = Barrier(2)

    def attempt(command):
        barrier.wait(timeout=5)
        return env.open().create(command)

    with ThreadPoolExecutor(2) as pool:
        candidates = list(pool.map(attempt, (create, create)))
    assert candidates[0] == candidates[1]
    review = env.review(candidates[0])

    def approve(command):
        barrier.wait(timeout=5)
        return env.open().review(command)

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(approve, (review, review)))
    assert results[0] == results[1]
    assert env.raw('SELECT COUNT(*) FROM tm_revisions') == [(1,)]
    assert env.raw('SELECT COUNT(*) FROM tm_events') == [(2,)]
