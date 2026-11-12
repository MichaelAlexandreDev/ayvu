"""Evidência sintética #167. Não importa nem habilita um runner em produção."""
from __future__ import annotations
import json
from uuid import uuid4
import pytest
from tests.research.local_worker.local_worker_spike_support import ROOT, bounded_case, root
from tests.research.local_worker.local_worker_spike_support import spike


def test_real_artifact_reconciliation_after_worker_crash_uses_existing_contract(root):
    from dataclasses import replace
    from datetime import datetime
    from ayvu.artifacts.model import ArtifactRequest, ArtifactState, abandon, new_artifact
    from ayvu.artifacts.service import ArtifactService, PayloadEvidence, StagedArtifact

    with spike.Session(root) as session:
        session.command('partial')
        session.receive()
        with pytest.raises(spike.Refusal, match='^worker_crashed$'):
            session.receive()
    now = datetime.fromisoformat(spike.STAMP)
    request = ArtifactRequest(project_id=spike.PROJECT, job_id=spike.JOB, role='checkpoint',
                              stage='synthetic', stage_version=1, media_type='application/octet-stream',
                              input_fingerprint='sha256:' + '0' * 64)
    artifact = new_artifact(request, str(uuid4()), now)

    class Repository:
        active = True
        current = artifact
        def list_staging(self, limit):
            return (StagedArtifact(self.current, self.active),)
        def settle_abandoned(self, artifact_id, revision, reason):
            assert artifact_id == self.current.artifact_id and revision == self.current.revision
            self.current = abandon(self.current, reason, expected_revision=revision, now=now)
            return self.current
        def settle_ready(self, *_):
            pytest.fail('incomplete_artifact_became_ready')

    class Payloads:
        reads = 0
        def evidence(self, artifact_id, size, digest):
            self.reads += 1
            assert not (root / 'fixture.ready').exists()
            return PayloadEvidence('absent')
        def complete_publication(self, *_):
            pytest.fail('incomplete_artifact_published')

    repository, payloads = Repository(), Payloads()
    service = ArtifactService(repository, payloads)
    assert service.reconcile().active == (artifact.artifact_id,)
    assert payloads.reads == 0
    repository.active = False  # perda da lease, nunca apenas PID desaparecido
    report = ArtifactService(repository, payloads).reconcile()
    assert report.abandoned == (artifact.artifact_id,) and not report.completed
    assert repository.current.state is ArtifactState.ABANDONED
    assert repository.current.reason_code == 'interrupted_before_digest'
    assert payloads.reads == 0
    # Mesmo um digest registrado não autoriza publicação sem arquivo final verificado.
    repository.current = replace(artifact, size=20, digest='sha256:' + '0' * 64)
    report = ArtifactService(repository, payloads).reconcile()
    assert report.abandoned == (artifact.artifact_id,) and not report.completed
    assert repository.current.reason_code == 'interrupted_before_publication'
    assert payloads.reads == 1


def test_clean_exit_without_consumed_result_is_not_success(root):
    with spike.Session(root) as session:
        session.command('success')
        session.receive()
        with pytest.raises(spike.Refusal, match='^invalid_message$'):
            session.finish()


def test_terminal_reply_forbids_another_frame(root):
    with spike.Session(root) as session:
        session.command('success')
        session.receive()
        session.receive()
        with pytest.raises(spike.Refusal, match='^invalid_message$'):
            session.receive()


def test_committed_evidence_contains_only_approved_fields():
    evidence = json.loads((ROOT / 'spikes/local-worker/results/linux-x86_64.json').read_text())
    assert set(evidence) == {'schema_version', 'platform', 'python', 'fixture', 'samples',
                             'throughput', 'startup_median_ms'}
    assert evidence['fixture'] == 'synthetic_only' and evidence['platform'] == 'linux_x86_64'
    fields = {'transport', 'case', 'startup_ms', 'cancel_ms', 'observation', 'cleanup_ms',
              'stdout_bytes', 'stderr_bytes', 'max_frame_buffer', 'elapsed_ms'}
    for row in evidence['samples']:
        assert set(row) <= fields
        assert row['transport'] in {'pipes', 'socketpair'} and row['case'] in spike.MODES
        assert row['observation'] in spike.CODES | {'clean_exit'}
        assert all(type(value) in {int, float} and 0 <= value < 2**63
                   for key, value in row.items() if key not in {'transport', 'case', 'observation'})
    raw = json.dumps(evidence)
    assert not any(value in raw for value in ('SYNTHETIC_RAW_OUTPUT', 'AYVU_SYNTHETIC_CANARY',
                                             '/home/', '/tmp/', 'event_id', 'pids'))


@pytest.mark.parametrize('variant', ['whitespace', 'duplicate', 'nan', 'infinity'])
def test_noncanonical_valid_envelope_is_rejected_before_schema_use(variant):
    raw = spike.canonical(spike.packet(str(uuid4()), 1, 'hello', {'versions': [1], 'event_schema': 1}))
    if variant == 'whitespace':
        raw = b' ' + raw
    elif variant == 'duplicate':
        raw = b'{"v":1,' + raw[1:]
    elif variant == 'nan':
        raw = raw.replace(b'"event_schema":1', b'"event_schema":NaN')
    else:
        raw = raw.replace(b'"event_schema":1', b'"event_schema":Infinity')
    with pytest.raises(spike.Refusal, match='^invalid_frame$'):
        spike.decode(raw)
