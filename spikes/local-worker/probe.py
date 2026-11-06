"""Pesquisa descartável #167. Somente stdlib e fixtures sintéticas; sem imports Ayvu."""
from __future__ import annotations

import json
import os
from pathlib import Path
import select
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
from uuid import UUID, uuid4

try:
    import resource
except ImportError:  # plataforma fora do escopo aceito: não simular rlimits
    resource = None

VERSION = 1
MAX_FRAME = 32 * 1024
MAX_DEPTH = 8
QUEUE_LIMIT = 8
STARTUP = 3.0
IO_TIMEOUT = 1.0
CANCEL_GRACE = 0.25
TERM_GRACE = 0.25
KILL_GRACE = 1.0
CASE_BUDGET = 8.0
OUTPUT_LIMIT = 16 * 1024
JOB = '00000000-0000-4000-8000-000000000001'
PROJECT = '00000000-0000-4000-8000-000000000002'
REQUEST = '00000000-0000-4000-8000-000000000003'
STAMP = '2026-11-06T00:00:00.000000+00:00'
ENTRYPOINT = Path(__file__).resolve()
MODES = frozenset({'success', 'cooperative', 'ignore', 'hang', 'crash', 'malformed',
                   'oversized', 'flood', 'noise', 'inheritance', 'tree', 'partial',
                   'incompatible', 'wrong_peer', 'truncated', 'late_success', 'memory', 'cpu'})
CODES = frozenset({'invalid_frame', 'oversized_frame', 'incompatible_version',
                   'invalid_peer', 'invalid_sequence', 'invalid_message', 'timeout',
                   'worker_crashed', 'launch_refused', 'output_limit', 'queue_full',
                   'forced_termination', 'startup_timeout', 'worker_refused'})


class Refusal(Exception):
    def __init__(self, code: str):
        super().__init__(code if code in CODES else 'worker_refused')


def canonical(data):
    return json.dumps(data, sort_keys=True, separators=(',', ':'), ensure_ascii=True,
                      allow_nan=False).encode('ascii')


def identity(value):
    try:
        return type(value) is str and str(UUID(value)) == value
    except (ValueError, TypeError, AttributeError):
        return False


def packet(run, seq, kind, body):
    return dict(v=VERSION, run=run, job=JOB, request=REQUEST, seq=seq, kind=kind, body=body)


def decode(raw):
    """JSON puro. Re-encoding canônico recusa chaves duplicadas e formas ambíguas."""
    if len(raw) > MAX_FRAME:
        raise Refusal('oversized_frame')
    # Limite lexical antes do parser; colchetes dentro de strings não contam.
    depth, quoted, escaped = 0, False, False
    for char in raw:
        if quoted:
            if escaped:
                escaped = False
            elif char == 92:
                escaped = True
            elif char == 34:
                quoted = False
        elif char == 34:
            quoted = True
        elif char in (91, 123):
            depth += 1
            if depth > MAX_DEPTH:
                raise Refusal('invalid_frame')
        elif char in (93, 125):
            depth -= 1
    try:
        data = json.loads(raw)
        if canonical(data) != raw:
            raise ValueError
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise Refusal('invalid_frame') from None
    if type(data) is not dict or set(data) != {'v', 'run', 'job', 'request', 'seq', 'kind', 'body'}:
        raise Refusal('invalid_message')
    if type(data['v']) is not int or data['v'] != VERSION:
        raise Refusal('incompatible_version')
    if (not all(identity(data[key]) for key in ('run', 'job', 'request'))
            or type(data['seq']) is not int or not 1 <= data['seq'] < 2**31
            or data['kind'] not in ('hello', 'command', 'cancel', 'event', 'result')
            or type(data['body']) is not dict):
        raise Refusal('invalid_message')
    return data


class Wire:
    """Um frame em memória; nenhuma fila implícita, I/O não bloqueante com deadline."""
    def __init__(self, read_fd, write_fd):
        self.read_fd, self.write_fd = read_fd, write_fd
        for fd in {read_fd, write_fd}:
            os.set_blocking(fd, False)
        self.max_read = 0
        self.bytes_read = 0
        self.bytes_written = 0

    def _wait(self, fd, write, deadline):
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not any(select.select([] if write else [fd], [fd] if write else [], [], remaining)[:2]):
            raise Refusal('timeout')

    def write(self, raw, deadline):
        view = memoryview(raw)
        while view:
            self._wait(self.write_fd, True, deadline)
            try:
                size = os.write(self.write_fd, view)
            except BlockingIOError:
                continue
            except OSError:
                raise Refusal('worker_crashed') from None
            view = view[size:]
            self.bytes_written += size

    def exact(self, size, deadline):
        chunks = bytearray()
        while len(chunks) < size:
            self._wait(self.read_fd, False, deadline)
            try:
                raw = os.read(self.read_fd, size - len(chunks))
            except BlockingIOError:
                continue
            except OSError:
                raise Refusal('worker_crashed') from None
            if not raw:
                raise Refusal('worker_crashed')
            chunks.extend(raw)
            self.bytes_read += len(raw)
            self.max_read = max(self.max_read, len(chunks))
        return bytes(chunks)

    def send(self, message, timeout=IO_TIMEOUT):
        raw = canonical(message)
        if len(raw) > MAX_FRAME:
            raise Refusal('oversized_frame')
        self.write(struct.pack('!I', len(raw)) + raw, time.monotonic() + timeout)

    def receive(self, timeout=IO_TIMEOUT):
        deadline = time.monotonic() + timeout
        size = struct.unpack('!I', self.exact(4, deadline))[0]
        if size > MAX_FRAME:
            raise Refusal('oversized_frame')
        if size == 0:
            raise Refusal('invalid_frame')
        return decode(self.exact(size, deadline))


class BoundedQueue:
    """Crédito explícito; ao encher, o produtor deve parar antes de ler outro frame."""
    def __init__(self):
        self.items = []

    def receive(self, session):
        if len(self.items) >= QUEUE_LIMIT:
            raise Refusal('queue_full')
        self.items.append(session.receive())


def minimal_environment():
    return {'LANG': 'C', 'LC_ALL': 'C'}


def event(sequence, kind='progress'):
    payloads = {
        'progress': dict(phase='synthetic', unit='units', completed=0, total=1, diagnostics={}),
        'cancellation_requested': dict(reason_code='operator_cancelled', requested_at=STAMP),
        'cancellation_acknowledged': dict(reason_code='operator_cancelled', safe_point='synthetic_safe_point'),
        'outcome': dict(state='cancelled', reason_code='operator_cancelled', counters=[], failure=None),
    }
    return canonical(dict(schema_version=1, event_id=f'00000000-0000-4000-8000-{sequence + 10:012d}',
                          job_id=JOB, sequence=sequence, occurred_at=STAMP,
                          correlation=dict(project_id=PROJECT, operation_id=REQUEST),
                          kind=kind, payload=payloads[kind])).decode('ascii')


class Session:
    def __init__(self, root, transport='socketpair'):
        if transport not in ('socketpair', 'pipes') or not root.is_dir() or root.is_symlink():
            raise Refusal('launch_refused')
        self.run = str(uuid4())
        self.transport = transport
        self.out_sequence = 0
        self.in_sequence = 0
        self.closed = False
        self.failed = None
        self.mode = None
        self.result_received = False
        self.counts = {'stdout_bytes': 0, 'stderr_bytes': 0}
        self.output_exceeded = threading.Event()
        self.threads = []
        self.child_socket = None
        self.parent_socket = None
        start = time.monotonic()
        if transport == 'socketpair':
            self.parent_socket, self.child_socket = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
            # A fila de aplicação é limitada separadamente. Não depender de
            # getsockopt/setsockopt: alguns hosts recusam essas duas operações.
            fd = self.child_socket.fileno()
            args = [sys.executable, '-I', '-S', str(ENTRYPOINT), 'worker', str(fd)]
            inherited = (fd,)
        else:
            args = [sys.executable, '-I', '-S', str(ENTRYPOINT), 'worker', 'pipes']
            inherited = ()
        try:
            self.process = subprocess.Popen(args, cwd=root, env=minimal_environment(), shell=False,
                                            close_fds=True, pass_fds=inherited, start_new_session=True,
                                            stdin=subprocess.PIPE if transport == 'pipes' else subprocess.DEVNULL,
                                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
        except OSError:
            if self.parent_socket:
                self.parent_socket.close()
                self.child_socket.close()
            raise Refusal('launch_refused') from None
        if transport == 'socketpair':
            self.child_socket.close()
            fd = self.parent_socket.fileno()
            self.wire = Wire(fd, fd)
            self._drain(self.process.stdout, 'stdout_bytes')
        else:
            self.wire = Wire(self.process.stdout.fileno(), self.process.stdin.fileno())
        self._drain(self.process.stderr, 'stderr_bytes')
        try:
            self.send('hello', {'versions': [VERSION], 'event_schema': 1})
            message = self.receive(STARTUP)
            if message['kind'] != 'hello' or message['body'] != {'version': VERSION, 'event_schema': 1}:
                raise Refusal('incompatible_version')
        except Refusal:
            self.close()
            raise
        self.startup_ms = (time.monotonic() - start) * 1000

    def __repr__(self):
        return 'Session(<redacted>)'

    def _drain(self, stream, key):
        def drain():
            while True:
                try:
                    raw = stream.read(4096)
                except (OSError, ValueError):
                    break
                if not raw:
                    break
                self.counts[key] = min(OUTPUT_LIMIT + 1, self.counts[key] + len(raw))
                if self.counts[key] > OUTPUT_LIMIT:
                    self.output_exceeded.set()
        thread = threading.Thread(target=drain, daemon=True)
        thread.start()
        self.threads.append(thread)

    def send(self, kind, body):
        self.out_sequence += 1
        self.wire.send(packet(self.run, self.out_sequence, kind, body))

    def receive(self, timeout=IO_TIMEOUT):
        if self.failed:
            raise Refusal(self.failed)
        if self.result_received:
            self.failed = 'invalid_message'
            raise Refusal(self.failed)
        try:
            message = self.wire.receive(timeout)
            if (message['run'] != self.run or message['job'] != JOB or message['request'] != REQUEST):
                raise Refusal('invalid_peer')
            if message['seq'] != self.in_sequence + 1:
                raise Refusal('invalid_sequence')
            kind, body = message['kind'], message['body']
            if self.in_sequence == 0:
                valid = (kind == 'hello' and body == {'version': VERSION, 'event_schema': 1}
                         and type(body.get('version')) is int and type(body.get('event_schema')) is int)
            elif kind == 'event':
                valid = (set(body) == {'event'} and body['event'] in
                         [event(1), event(2, 'cancellation_requested'),
                          event(3, 'cancellation_acknowledged'), event(4, 'outcome')])
            elif kind == 'result' and self.mode == 'tree':
                valid = (set(body) == {'pids'} and type(body['pids']) is list
                         and len(body['pids']) == 3 and all(type(pid) is int and pid > 0 for pid in body['pids']))
            elif kind == 'result' and self.mode == 'inheritance':
                valid = (set(body) == {'environment_inherited', 'descriptor_inherited', 'cwd_explicit',
                                      'filesystem_readable', 'filesystem_writable', 'loopback_reachable', 'process_spawn'}
                         and all(type(value) is bool or (key == 'loopback_reachable' and value is None)
                                 for key, value in body.items()))
            elif kind == 'result' and self.mode == 'memory':
                valid = body == {'allocation_refused': True}
            else:
                valid = kind == 'result' and body == {'artifacts': []}
            if not valid:
                raise Refusal('invalid_message')
        except Refusal as error:
            self.failed = str(error)
            raise
        self.in_sequence += 1
        self.result_received = message['kind'] == 'result'
        return message

    def command(self, mode, **parameters):
        if mode not in MODES:
            raise Refusal('launch_refused')
        self.mode = mode
        self.send('command', dict(mode=mode, **parameters))

    def terminate(self):
        start = time.monotonic()
        # Mesmo se o líder já morreu: seus descendentes podem manter o grupo vivo.
        for sig, grace in ((signal.SIGTERM, TERM_GRACE), (signal.SIGKILL, KILL_GRACE)):
            try:
                os.killpg(self.process.pid, sig)
            except ProcessLookupError:
                pass
            try:
                self.process.wait(timeout=grace)
            except subprocess.TimeoutExpired:
                pass
        if self.process.poll() is None:
            raise Refusal('timeout')
        return (time.monotonic() - start) * 1000

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.terminate()
        if self.parent_socket:
            self.parent_socket.close()
        for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
            if stream:
                stream.close()
        for thread in self.threads:
            thread.join(KILL_GRACE)

    def finish(self, timeout=IO_TIMEOUT):
        """Saída limpa é necessária mas nunca publica artefato nem decide JobOutcome."""
        if self.failed:
            raise Refusal(self.failed)
        try:
            self.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            raise Refusal('timeout') from None
        for thread in self.threads:
            thread.join(KILL_GRACE)
        if self.output_exceeded.is_set():
            raise Refusal('output_limit')
        if self.process.returncode != 0:
            raise Refusal('worker_crashed')
        if not self.result_received:
            raise Refusal('invalid_message')

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def _probe_authority(body):
    if (set(body) != {'mode', 'port', 'descriptor'}
            or (body['port'] is not None and (type(body['port']) is not int or not 1 <= body['port'] <= 65535))
            or type(body['descriptor']) is not int):
        raise Refusal('invalid_message')
    inherited = False
    try:
        os.fstat(body['descriptor'])
        inherited = True
    except OSError:
        pass
    # Só fixtures relativas no root temporário. Nenhum arquivo real é inspecionado.
    outside = Path('../outside/canary')
    readable = outside.is_file() and bool(outside.read_bytes())
    writable = False
    if outside.parent.is_dir():
        Path('../outside/worker_created').write_bytes(b'0')
        writable = True
    reachable = None
    if body['port'] is not None:
        reachable = False
        try:
            with socket.create_connection(('127.0.0.1', body['port']), timeout=0.25):
                reachable = True
        except OSError:
            pass
    child = subprocess.Popen([sys.executable, '-I', '-S', '-c', 'pass'], env=minimal_environment(),
                             close_fds=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, shell=False)
    child.wait(timeout=1)
    return dict(environment_inherited='AYVU_SYNTHETIC_CANARY' in os.environ,
                descriptor_inherited=inherited, cwd_explicit=Path('cwd_marker').is_file(),
                filesystem_readable=readable, filesystem_writable=writable,
                loopback_reachable=reachable, process_spawn=child.returncode == 0)


def _tree():
    # Filho e neto aguardam sinais. PID via IPC só para o teste abrir pidfds; nunca diagnóstico.
    ready_read, ready_write = os.pipe()
    child = os.fork()
    if child == 0:
        os.close(ready_read)
        grandchild = os.fork()
        if grandchild == 0:
            os.close(ready_write)
            while True:
                signal.pause()
        os.write(ready_write, str(grandchild).encode('ascii'))
        os.close(ready_write)
        while True:
            signal.pause()
    os.close(ready_write)
    if not select.select([ready_read], [], [], IO_TIMEOUT)[0]:
        os.close(ready_read)
        raise Refusal('timeout')
    grandchild = int(os.read(ready_read, 32))
    os.close(ready_read)
    return [os.getpid(), child, grandchild]


def worker(endpoint):
    if endpoint == 'pipes':
        wire = Wire(0, 1)
    else:
        fd = int(endpoint)
        # Não herdar a autoridade de IPC em futuros execs.
        os.set_inheritable(fd, False)
        wire = Wire(fd, fd)
    hello = wire.receive(STARTUP)
    run = hello['run']
    body = hello['body']
    if (hello != packet(run, 1, 'hello', {'versions': [VERSION], 'event_schema': 1})
            or type(body.get('event_schema')) is not int
            or type(body.get('versions')) is not list
            or any(type(version) is not int for version in body['versions'])):
        raise Refusal('incompatible_version')
    sequence = 0

    def emit(kind, body):
        nonlocal sequence
        sequence += 1
        wire.send(packet(run, sequence, kind, body))

    emit('hello', {'version': VERSION, 'event_schema': 1})
    command = wire.receive()
    if command['run'] != run or command['job'] != JOB or command['request'] != REQUEST or command['seq'] != 2 or command['kind'] != 'command':
        raise Refusal('invalid_peer')
    body = command['body']
    mode = body.get('mode')
    if mode not in MODES or (mode != 'inheritance' and set(body) != {'mode'}):
        raise Refusal('invalid_message')
    if mode == 'inheritance':
        emit('result', _probe_authority(body))
        return
    if mode == 'memory':
        resource.setrlimit(resource.RLIMIT_AS, (96 * 1024 * 1024, 96 * 1024 * 1024))
        try:
            bytearray(128 * 1024 * 1024)
        except MemoryError:
            emit('result', {'allocation_refused': True})
            return
        raise Refusal('worker_refused')
    if mode == 'cpu':
        resource.setrlimit(resource.RLIMIT_CPU, (1, 2))
        while True:
            pass
    if mode == 'crash':
        os._exit(19)
    if mode == 'malformed':
        wire.write(struct.pack('!I', 4) + b'nope', time.monotonic() + 1)
        return
    if mode == 'oversized':
        wire.write(struct.pack('!I', MAX_FRAME + 1), time.monotonic() + 1)
        return
    if mode == 'truncated':
        wire.write(struct.pack('!I', 10) + b'{', time.monotonic() + 1)
        return
    if mode in ('incompatible', 'wrong_peer'):
        message = packet(str(uuid4()) if mode == 'wrong_peer' else run, 2, 'result', {})
        message['v'] = 2 if mode == 'incompatible' else VERSION
        wire.send(message)
        return
    if mode in ('hang', 'ignore', 'tree'):
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    if mode == 'tree':
        emit('result', {'pids': _tree()})
    elif mode == 'flood':
        for _ in range(100000):
            emit('event', {'event': event(1)})
        return
    elif mode == 'noise':
        for fd in ((1, 2) if endpoint != 'pipes' else (2,)):
            for _ in range(9):
                os.write(fd, b'SYNTHETIC_RAW_OUTPUT' * 256)
        emit('result', {'artifacts': []})
        return
    else:
        emit('event', {'event': event(1)})
    if mode in ('hang', 'ignore', 'tree'):
        # Leitor separado observa EOF do pai mesmo se a carga principal bloquear.
        def watch_parent():
            while True:
                try:
                    wire.receive(CASE_BUDGET)
                except Refusal as error:
                    if str(error) == 'timeout':
                        continue
                    os.killpg(os.getpgrp(), signal.SIGKILL)
        threading.Thread(target=watch_parent, daemon=True).start()
        threading.Event().wait(CASE_BUDGET * 2)
        return
    if mode == 'cooperative':
        cancel = wire.receive()
        if cancel != packet(run, 3, 'cancel', {'reason_code': 'operator_cancelled', 'requested_at': STAMP}):
            raise Refusal('invalid_message')
        for seq, kind in ((2, 'cancellation_requested'), (3, 'cancellation_acknowledged'), (4, 'outcome')):
            emit('event', {'event': event(seq, kind)})
    if mode == 'partial':
        Path('fixture.part').write_bytes(b'synthetic-incomplete')
        os._exit(23)
    if mode == 'late_success':
        threading.Event().wait(CANCEL_GRACE * 2)
    emit('result', {'artifacts': []})


def measure(root, transport, mode):
    started = time.monotonic()
    result = {'transport': transport, 'case': mode}
    with Session(root, transport) as session:
        result['startup_ms'] = round(session.startup_ms, 3)
        session.command(mode)
        try:
            message = session.receive(CANCEL_GRACE)
            if message['kind'] == 'event' and mode == 'cooperative':
                before = time.monotonic()
                session.send('cancel', {'reason_code': 'operator_cancelled', 'requested_at': STAMP})
                for _ in range(4):
                    message = session.receive(CANCEL_GRACE)
                result['cancel_ms'] = round((time.monotonic() - before) * 1000, 3)
            elif mode in ('hang', 'ignore'):
                if mode == 'ignore':
                    session.send('cancel', {'reason_code': 'operator_cancelled', 'requested_at': STAMP})
                session.receive(CANCEL_GRACE)
            if message['kind'] != 'result':
                message = session.receive()
            session.finish()
            result['observation'] = 'clean_exit'
        except Refusal as error:
            result['observation'] = str(error)
            result['cleanup_ms'] = round(session.terminate(), 3)
        result.update(session.counts)
        result['max_frame_buffer'] = session.wire.max_read
    result['elapsed_ms'] = round((time.monotonic() - started) * 1000, 3)
    return result


if __name__ == '__main__':
    # CLI interna sem argparse: nunca reflete argumentos desconhecidos em stderr.
    try:
        if len(sys.argv) == 3 and sys.argv[1] == 'worker' and (sys.argv[2] == 'pipes' or sys.argv[2].isdigit()):
            worker(sys.argv[2])
        else:
            raise Refusal('launch_refused')
    except BaseException:
        os._exit(70)
