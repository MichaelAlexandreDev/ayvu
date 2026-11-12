"""Evidência sintética #167. Não importa nem habilita um runner em produção."""
from __future__ import annotations
import signal
import subprocess
import time
import pytest
from ayvu.jobs.events import JobEventSequence, decode_event
from tests.research.local_worker.local_worker_spike_support import bounded_case, root
from tests.research.local_worker.local_worker_spike_support import spike


@pytest.mark.parametrize('transport', ['socketpair', 'pipes'])
def test_success_startup_and_canonical_progress(root, transport):
    with spike.Session(root, transport) as session:
        assert session.startup_ms < spike.STARTUP * 1000
        session.command('success')
        progress = session.receive()
        event = decode_event(progress['body']['event'])
        sequence = JobEventSequence(spike.JOB)
        sequence.accept(event)
        assert event.sequence == 1 and event.payload.completed == 0
        assert session.receive()['body'] == {'artifacts': []}
        session.finish()
        assert session.wire.max_read <= spike.MAX_FRAME


@pytest.mark.parametrize('transport', ['socketpair', 'pipes'])
def test_cooperative_cancel_uses_canonical_ack_and_outcome(root, transport):
    with spike.Session(root, transport) as session:
        sequence = JobEventSequence(spike.JOB)
        session.command('cooperative')
        sequence.accept(decode_event(session.receive()['body']['event']))
        started = time.monotonic()
        session.send('cancel', {'reason_code': 'operator_cancelled', 'requested_at': spike.STAMP})
        kinds = []
        for _ in range(3):
            event = decode_event(session.receive(spike.CANCEL_GRACE)['body']['event'])
            sequence.accept(event)
            kinds.append(event.kind.value)
        assert kinds == ['cancellation_requested', 'cancellation_acknowledged', 'outcome']
        assert sequence.outcome.state.value == 'cancelled'
        assert session.receive()['body'] == {'artifacts': []}
        session.finish()
        assert time.monotonic() - started < spike.CANCEL_GRACE


@pytest.mark.parametrize('transport', ['socketpair', 'pipes'])
@pytest.mark.parametrize('mode', ['hang', 'ignore'])
def test_timeout_and_ignored_cancel_require_sigkill(root, transport, mode):
    with spike.Session(root, transport) as session:
        session.command(mode)
        assert session.receive()['kind'] == 'event'  # a carga já está pronta
        if mode == 'ignore':
            session.send('cancel', {'reason_code': 'operator_cancelled', 'requested_at': spike.STAMP})
        with pytest.raises(spike.Refusal, match='^timeout$'):
            session.receive(spike.CANCEL_GRACE)
        elapsed = session.terminate()
        assert session.process.returncode == -signal.SIGKILL
        assert elapsed < (spike.TERM_GRACE + spike.KILL_GRACE) * 1000


@pytest.mark.parametrize('mode,code', [('crash', 'worker_crashed'), ('malformed', 'invalid_frame'),
                                      ('oversized', 'oversized_frame'), ('truncated', 'worker_crashed'),
                                      ('incompatible', 'incompatible_version'), ('wrong_peer', 'invalid_peer')])
@pytest.mark.parametrize('transport', ['socketpair', 'pipes'])
def test_failure_is_specific_and_never_success(root, transport, mode, code):
    with spike.Session(root, transport) as session:
        session.command(mode)
        with pytest.raises(spike.Refusal, match=f'^{code}$'):
            session.receive()
        assert not (root / 'fixture.ready').exists()


def test_late_result_is_not_accepted_after_deadline(root):
    with spike.Session(root) as session:
        session.command('late_success')
        assert session.receive()['kind'] == 'event'
        with pytest.raises(spike.Refusal, match='^timeout$'):
            session.receive(spike.CANCEL_GRACE)
        session.terminate()
        assert session.process.returncode < 0


@pytest.mark.parametrize('transport', ['socketpair', 'pipes'])
def test_stdout_stderr_are_bounded_discarded_and_refused(root, transport, capfd):
    with spike.Session(root, transport) as session:
        session.command('noise')
        assert session.receive()['kind'] == 'result'
        with pytest.raises(spike.Refusal, match='^output_limit$'):
            session.finish()
        assert session.counts['stderr_bytes'] == spike.OUTPUT_LIMIT + 1
        if transport == 'socketpair':
            assert session.counts['stdout_bytes'] == spike.OUTPUT_LIMIT + 1
    assert 'SYNTHETIC_RAW_OUTPUT' not in ''.join(capfd.readouterr())


@pytest.mark.parametrize('transport', ['socketpair', 'pipes'])
def test_real_transport_backpressure_and_queue_credit(root, transport):
    with spike.Session(root, transport) as session:
        session.command('flood')
        queue = spike.BoundedQueue()
        for _ in range(spike.QUEUE_LIMIT):
            queue.receive(session)
        read_before = session.wire.bytes_read
        # Crédito esgotado: a fila recusa ANTES de qualquer nova leitura do transporte.
        with pytest.raises(spike.Refusal, match='^queue_full$'):
            queue.receive(session)
        assert session.wire.bytes_read == read_before
        # O worker tenta 100000 frames (>50 MiB); SO limita a fila e bloqueia o produtor.
        with pytest.raises(subprocess.TimeoutExpired):
            session.process.wait(timeout=spike.CANCEL_GRACE)
        assert session.process.poll() is None
        assert session.wire.max_read <= spike.MAX_FRAME
        queue.items.pop(0)
        queue.receive(session)  # drenar libera crédito, sem deadlock
        assert len(queue.items) == spike.QUEUE_LIMIT
