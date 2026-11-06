"""Evidência sintética #167. Não importa nem habilita um runner em produção."""
from __future__ import annotations

import ctypes
import importlib.util
import json
import os
from pathlib import Path
import select
import signal
import socket
import struct
import subprocess
import sys
import time
from uuid import uuid4

import pytest

from ayvu.job_events import JobEventSequence, decode_event

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('local_worker_spike', ROOT / 'spikes/local-worker/probe.py')
spike = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(spike)
pytestmark = pytest.mark.skipif(sys.platform != 'linux',
                                reason='evidência exige Linux; demais plataformas não provadas')


@pytest.fixture
def root(tmp_path):
    work = tmp_path / 'work'
    work.mkdir(mode=0o700)
    (work / 'cwd_marker').write_bytes(b'0')
    return work


@pytest.fixture
def reaper():
    # Só o harness adota órfãos para não depender do init do contêiner.
    libc = ctypes.CDLL(None, use_errno=True)
    previous = ctypes.c_int()
    assert libc.prctl(37, ctypes.byref(previous), 0, 0, 0) == 0
    assert libc.prctl(36, 1, 0, 0, 0) == 0
    yield
    assert libc.prctl(36, previous.value, 0, 0, 0) == 0


def exited_and_reap(pids):
    libc = ctypes.CDLL(None, use_errno=True)
    for pid in pids:
        fd = libc.pidfd_open(pid, 0)
        if fd < 0 and ctypes.get_errno() == 3:
            continue
        assert fd >= 0, 'pidfd_open_unavailable'
        try:
            assert select.select([fd], [], [], spike.KILL_GRACE)[0], 'orphan_cleanup_timeout'
        finally:
            os.close(fd)
        try:
            os.waitpid(pid, 0)
        except ChildProcessError:
            pass


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


@pytest.mark.parametrize('transport', ['socketpair', 'pipes'])
def test_launch_allowlist_environment_descriptors_cwd_and_residual_authority(root, monkeypatch, transport):
    outside = root.parent / 'outside'
    outside.mkdir()
    canary = uuid4().hex
    (outside / 'canary').write_text(canary)
    monkeypatch.setenv('AYVU_SYNTHETIC_CANARY', canary)
    descriptor = os.open(outside / 'canary', os.O_RDONLY)
    os.set_inheritable(descriptor, True)
    try:
        with spike.Session(root, transport) as session:
            session.command('inheritance', port=None, descriptor=descriptor)
            authority = session.receive()['body']
            session.finish()
            assert authority == dict(environment_inherited=False, descriptor_inherited=False,
                                     cwd_explicit=True, filesystem_readable=True, filesystem_writable=True,
                                     loopback_reachable=None, process_spawn=True)
            assert canary not in json.dumps(authority) + repr(session)
    finally:
        os.close(descriptor)
    with spike.Session(root) as session:
        with pytest.raises(spike.Refusal, match='^launch_refused$'):
            session.command('arbitrary_command')
    with pytest.raises(spike.Refusal, match='^launch_refused$'):
        spike.Session(root, 'shell')


def test_default_subprocess_inherits_environment_descriptor_and_cwd(root):
    # Contraprova sem a mitigação: somente um canário sintético; imprime booleanos.
    environment = {**spike.minimal_environment(), 'AYVU_SYNTHETIC_CANARY': uuid4().hex}
    fd = os.open(root / 'cwd_marker', os.O_RDONLY)
    os.set_inheritable(fd, True)
    code = ('import os,json; print(json.dumps(["AYVU_SYNTHETIC_CANARY" in os.environ,'
            f'os.fstat({fd}).st_size == 1,os.path.isfile("cwd_marker")]))')
    # Um pai intermediário recebe SÓ a fixture. Seu filho usa os defaults reais,
    # sem copiar o ambiente ou descritores reais do processo de pytest.
    parent_code = ('import subprocess,sys; '
                   'r=subprocess.run([sys.executable,"-I","-S","-c",sys.argv[1]],'
                   'close_fds=False,capture_output=True,timeout=3); '
                   'sys.stdout.buffer.write(r.stdout); sys.exit(r.returncode)')
    try:
        result = subprocess.run([sys.executable, '-I', '-S', '-c', parent_code, code], cwd=root,
                                env=environment, close_fds=True, pass_fds=(fd,),
                                capture_output=True, timeout=spike.STARTUP)
        assert result.returncode == 0
        assert json.loads(result.stdout) == [True, True, True]
    finally:
        os.close(fd)


@pytest.mark.parametrize('transport', ['socketpair', 'pipes'])
def test_force_cleanup_includes_child_and_grandchild(root, transport, reaper):
    pids = []
    try:
        with spike.Session(root, transport) as session:
            session.command('tree')
            pids = session.receive()['body']['pids']
            assert pids[0] == session.process.pid and len(set(pids)) == 3
            session.terminate()
            exited_and_reap(pids[1:])
            assert session.process.returncode == -signal.SIGKILL
    finally:
        # Contenção do harness continua válida mesmo quando mutamos o cleanup.
        if pids:
            try:
                os.killpg(pids[0], signal.SIGKILL)
            except ProcessLookupError:
                pass
            exited_and_reap(pids[1:])


@pytest.mark.parametrize('transport', ['socketpair', 'pipes'])
def test_parent_crash_eof_cleans_worker_child_and_grandchild(root, transport, reaper):
    code = ('import importlib.util,os,json,sys; '
            'spec=importlib.util.spec_from_file_location("p",sys.argv[1]); '
            'p=importlib.util.module_from_spec(spec); spec.loader.exec_module(p); '
            's=p.Session(p.Path.cwd(),sys.argv[2]); s.command("tree"); '
            'data=s.receive()["body"]; print(json.dumps(data),flush=True); '
            'os._exit(29)')
    parent = subprocess.Popen([sys.executable, '-I', '-S', '-c', code, str(spike.ENTRYPOINT), transport],
                              cwd=root, env=spike.minimal_environment(), shell=False, close_fds=True,
                              stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        output, errors = parent.communicate(timeout=spike.STARTUP + spike.KILL_GRACE)
        assert parent.returncode == 29 and not errors
        pids = json.loads(output)['pids']
        exited_and_reap(pids)
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait(timeout=spike.KILL_GRACE)


def test_peer_injection_and_stale_endpoint_cannot_address_anonymous_channel(root):
    # Endpoint sem nome não pode ser conectado nem substituído por um pathname antigo.
    stale = root / 'stale.sock'
    stale.write_bytes(b'synthetic-stale')
    with spike.Session(root) as session:
        # Outro processo tenta usar o número do descritor sem receber a autoridade.
        code = "import os,sys\ntry:\n os.write(int(sys.argv[1]),b'injection')\nexcept OSError:\n sys.exit(31)"
        attacker = subprocess.run([sys.executable, '-I', '-S', '-c', code,
                                   str(session.wire.write_fd)], close_fds=True,
                                  env=spike.minimal_environment(), cwd=root,
                                  capture_output=True, timeout=spike.STARTUP)
        assert attacker.returncode == 31 and not attacker.stdout and not attacker.stderr
        assert stale.read_bytes() == b'synthetic-stale'
        session.command('success')
        session.receive()
        assert session.receive()['kind'] == 'result'
        session.finish()
    assert stale.is_file()  # limpeza nunca remove endpoint alheio
    assert session.parent_socket.fileno() == -1


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
            session.receive(3.0)
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


@pytest.fixture(autouse=True)
def bounded_case():
    previous = signal.getsignal(signal.SIGALRM)
    def expired(*_):
        raise AssertionError('synthetic_case_budget_exceeded')
    signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, spike.CASE_BUDGET)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def test_real_artifact_reconciliation_after_worker_crash_uses_existing_contract(root):
    from dataclasses import replace
    from datetime import datetime
    from ayvu.artifact import ArtifactRequest, ArtifactState, abandon, new_artifact
    from ayvu.artifact_service import ArtifactService, PayloadEvidence, StagedArtifact

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
