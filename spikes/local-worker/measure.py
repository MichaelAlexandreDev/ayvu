"""Emite apenas métricas allowlisted; execute da raiz e redirecione o JSON."""
from __future__ import annotations

import importlib.util
import json
import platform
from pathlib import Path
import signal
import statistics
import tempfile
import time

spec = importlib.util.spec_from_file_location('worker_research', Path(__file__).with_name('probe.py'))
p = importlib.util.module_from_spec(spec)
spec.loader.exec_module(p)


def collect():
    if platform.system() != 'Linux' or platform.machine() != 'x86_64':
        raise p.Refusal('launch_refused')
    results = []
    throughput = []
    with tempfile.TemporaryDirectory(prefix='ayvu-worker-synthetic-') as temporary:
        root = Path(temporary)
        for transport in ('pipes', 'socketpair'):
            for mode in ('success', 'cooperative', 'hang', 'ignore', 'crash', 'malformed',
                         'oversized', 'truncated', 'incompatible', 'wrong_peer', 'noise', 'partial', 'memory'):
                for _ in range(7 if mode == 'success' else 1):
                    results.append(p.measure(root, transport, mode))
            with p.Session(root, transport) as session:
                session.command('flood')
                queue = p.BoundedQueue()
                start = time.monotonic()
                for _ in range(128):
                    queue.receive(session)
                    queue.items.pop(0)
                elapsed = time.monotonic() - start
                throughput.append(dict(transport=transport, frames=128,
                                       elapsed_ms=round(elapsed * 1000, 3),
                                       bytes_received=session.wire.bytes_read,
                                       frames_per_second=round(128 / elapsed, 1),
                                       max_frame_buffer=session.wire.max_read))
    return dict(schema_version=1, platform='linux_x86_64', python=platform.python_version(),
                fixture='synthetic_only', samples=results, throughput=throughput,
                startup_median_ms={transport: round(statistics.median(
                    row['startup_ms'] for row in results if row['transport'] == transport and row['case'] == 'success'), 3)
                    for transport in ('pipes', 'socketpair')})


if __name__ == '__main__':
    def budget_exceeded(*_):
        raise p.Refusal('timeout')
    signal.signal(signal.SIGALRM, budget_exceeded)
    signal.alarm(120)
    try:
        print(json.dumps(collect(), sort_keys=True, indent=2))
    except BaseException:
        # Nenhum traceback, caminho, corpo ou exceção bruta é emitido.
        print('{"error":"measurement_failed"}')
        raise SystemExit(1) from None
