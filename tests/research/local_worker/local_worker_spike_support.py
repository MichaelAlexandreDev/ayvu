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
from ayvu.jobs.events import JobEventSequence, decode_event
from tests.support.paths import PROJECT_ROOT

ROOT = PROJECT_ROOT


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
