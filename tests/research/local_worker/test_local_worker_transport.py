"""Evidência sintética #167. Não importa nem habilita um runner em produção."""
from __future__ import annotations
import os
import signal
import socket
import struct
import subprocess
import sys
import time
from uuid import uuid4
import pytest
from tests.research.local_worker.local_worker_spike_support import bounded_case, root
from tests.research.local_worker.local_worker_spike_support import spike


@pytest.mark.parametrize('field,value,code', [('run', str(uuid4()), 'invalid_peer'),
                                             ('job', str(uuid4()), 'invalid_peer'),
                                             ('request', str(uuid4()), 'invalid_peer'),
                                             ('seq', 1, 'invalid_sequence'),
                                             ('seq', 3, 'invalid_sequence'),
                                             ('kind', 'command', 'invalid_message')])
def test_bound_peer_correlation_order_and_direction(root, field, value, code):
    with spike.Session(root) as session:
        injected = spike.packet(session.run, 2, 'result', {})
        injected[field] = value
        original = session.wire.receive
        session.wire.receive = lambda _: injected
        with pytest.raises(spike.Refusal, match=f'^{code}$'):
            session.receive()
        assert session.in_sequence == 1  # recusa não avança o estado
        session.wire.receive = original


def test_executable_deserialization_and_redaction(root, capfd, caplog, monkeypatch):
    monkeypatch.chdir(root)
    token = uuid4().hex
    marker = root / 'must_not_exist'
    # Formas conhecidas de conteúdo executável: tratadas exclusivamente como bytes.
    bodies = [b"cos\nsystem\n(S'touch must_not_exist'\ntR.", b'\x80\x04N.',
              b'{"__reduce__":"' + token.encode() + b'"}',
              b'eval("' + token.encode() + b'")', b'[' * 9 + b']' * 9,
              b'{"v":1,"v":1}', b'{"v":NaN}', b'\xff', b'']
    for raw in bodies:
        with pytest.raises(spike.Refusal) as failure:
            spike.decode(raw)
        assert str(failure.value) in {'invalid_frame', 'invalid_message'}
        assert token not in str(failure.value) + repr(failure.value)
        assert failure.value.__cause__ is None
    assert not marker.exists()
    assert token not in ''.join(capfd.readouterr()) + caplog.text


def test_command_schema_rejects_object_hooks_and_raw_values_in_worker(root):
    token = uuid4().hex
    with spike.Session(root) as session:
        session.send('command', {'mode': 'success', '__reduce__': token})
        with pytest.raises(spike.Refusal, match='^worker_crashed$'):
            session.receive()
        assert not (root / 'fixture.ready').exists()


@pytest.mark.parametrize('transport', ['socketpair', 'pipes'])
def test_partial_artifact_remains_staging_after_crash_and_restart(root, transport):
    with spike.Session(root, transport) as session:
        session.command('partial')
        assert session.receive()['kind'] == 'event'
        with pytest.raises(spike.Refusal, match='^worker_crashed$'):
            session.receive()
    assert (root / 'fixture.part').read_bytes() == b'synthetic-incomplete'
    assert not (root / 'fixture.ready').exists()
    with spike.Session(root, transport) as replacement:
        replacement.command('success')
        replacement.receive()
        assert replacement.receive()['body'] == {'artifacts': []}
        replacement.finish()
    assert not (root / 'fixture.ready').exists()


def test_controlled_loopback_reachability_is_a_residual_risk(root):
    try:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(('127.0.0.1', 0))
        listener.listen(1)
    except PermissionError:
        pytest.skip('sandbox recusa listener loopback: probe de rede não executado, sem garantia Ayvu')
    with listener:
        with spike.Session(root) as session:
            session.command('inheritance', port=listener.getsockname()[1], descriptor=999999)
            authority = session.receive()['body']
            session.finish()
            assert authority['loopback_reachable'] is True


def test_rlimit_address_space_is_not_resident_memory_or_tree_containment(root):
    with spike.Session(root) as session:
        session.command('memory')
        assert session.receive()['body'] == {'allocation_refused': True}
        session.finish()


def test_rlimit_cpu_is_per_process_not_cpu_rate_or_tree_quota(root):
    with spike.Session(root) as session:
        session.command('cpu')
        with pytest.raises(spike.Refusal, match='^worker_crashed$'):
            # RLIMIT_CPU counts CPU time, so a busy runner needs far more wall time than 1 s.
            session.receive(30.0)
        session.process.wait(timeout=spike.KILL_GRACE)
        assert session.process.returncode == -signal.SIGXCPU


def test_fault_poisoning_prevents_later_success(root):
    with spike.Session(root) as session:
        session.command('late_success')
        session.receive()
        with pytest.raises(spike.Refusal, match='^timeout$'):
            session.receive(0.01)
        with pytest.raises(spike.Refusal, match='^timeout$'):
            session.finish()
        with pytest.raises(spike.Refusal, match='^timeout$'):
            session.receive()


def test_frame_bounds_and_partial_header_use_one_deadline():
    left, right = socket.socketpair()
    try:
        wire = spike.Wire(left.fileno(), left.fileno())
        os.write(right.fileno(), struct.pack('!I', spike.MAX_FRAME + 1))
        with pytest.raises(spike.Refusal, match='^oversized_frame$'):
            wire.receive()
        assert wire.bytes_read == 4  # antes de ler/alocar o corpo
        os.write(right.fileno(), b'\x00\x00')
        started = time.monotonic()
        with pytest.raises(spike.Refusal, match='^timeout$'):
            wire.receive(0.02)
        assert time.monotonic() - started < spike.CANCEL_GRACE
    finally:
        left.close()
        right.close()


def test_invalid_handshake_and_executable_bytes_are_rejected_over_real_transport(root):
    for raw in (b'cos\nsystem\n(S"synthetic"\ntR.',
                spike.canonical(spike.packet(str(uuid4()), 1, 'hello', {'versions': [2], 'event_schema': 1})),
                spike.canonical(spike.packet(str(uuid4()), 1, 'hello', {'versions': [True], 'event_schema': 1})),
                spike.canonical(spike.packet(str(uuid4()), 1, 'hello', {'versions': [1], 'event_schema': 1.0})),
                spike.canonical(spike.packet(str(uuid4()), 1, 'command', {'mode': 'success'}))):
        parent, child = socket.socketpair()
        process = subprocess.Popen([sys.executable, '-I', '-S', str(spike.ENTRYPOINT), 'worker', str(child.fileno())],
                                   cwd=root, env=spike.minimal_environment(), shell=False, close_fds=True,
                                   pass_fds=(child.fileno(),), stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        child.close()
        try:
            wire = spike.Wire(parent.fileno(), parent.fileno())
            wire.write(struct.pack('!I', len(raw)) + raw, time.monotonic() + spike.IO_TIMEOUT)
            stdout, stderr = process.communicate(timeout=spike.STARTUP)
            assert process.returncode == 70 and not stdout and not stderr
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=spike.KILL_GRACE)
            parent.close()
