"""TranslateProject with the production stores and the EPUB adapter, under a fake translator.

A "crash" here is an exception nothing in production catches, raised by a file operation or a
translator, after which every later file operation dies too, as they would in a dead process. Recovery
then runs through a new use case, as a new process would, with the clock past the old lease.
"""
from __future__ import annotations
import threading
from datetime import timedelta
from ayvu.jobs.model import JobState, JobStoreError
from tests.support.translate_project_support import world


def test_the_lease_is_renewed_while_a_long_call_blocks(world):
    command = world.command()
    use_case = world.use_case(command, lease_renewal=timedelta(milliseconds=1))
    jobs = use_case._ports.jobs
    blocking, renewed = threading.Event(), threading.Event()
    original = jobs.renew_lease
    def renew(*args):
        # Only the renewal made while the call blocks reaches the store. Renewing every millisecond
        # throughout kept the store busy on a slow machine, so publication failed as busy (#261).
        if not blocking.is_set() or renewed.is_set():
            return None
        result = original(*args)
        renewed.set()
        return result
    jobs.renew_lease = renew
    def block(text):
        blocking.set()
        assert renewed.wait(timeout=30)
    world.translator.on_call = block
    assert use_case.execute(command).state is JobState.VALIDATED
    assert renewed.is_set()


def test_a_busy_store_during_renewal_is_tried_again(world):
    command = world.command()
    use_case = world.use_case(command, lease_renewal=timedelta(milliseconds=1))
    jobs = use_case._ports.jobs
    attempts = []
    second = threading.Event()
    def renew(*args):
        attempts.append(1)
        if len(attempts) >= 2:
            second.set()
        raise JobStoreError("busy")
    jobs.renew_lease = renew
    world.translator.on_call = lambda text: second.wait(timeout=30)
    assert use_case.execute(command).state is JobState.VALIDATED and len(attempts) >= 2
