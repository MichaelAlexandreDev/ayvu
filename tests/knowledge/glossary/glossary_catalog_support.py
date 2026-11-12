from __future__ import annotations
import json
import multiprocessing
import os
import stat
from pathlib import Path
import pytest
from ayvu.knowledge.glossary.catalog import (
    Entry, GlossaryReference, create_glossary, list_glossaries, read_glossary,
    update_glossary,
)
from ayvu.knowledge.glossary.catalog_store import ManagedGlossaryRepository

def ref(name):
    return GlossaryReference(name)


def entry(term="wizard", translation="mago"):
    return Entry(term, "translate", translation)


def _create_worker(root, barrier, queue):
    repo = ManagedGlossaryRepository(root)
    barrier.wait()
    result = create_glossary(repo, ref("race.json"), (entry(),))
    queue.put(result.failure)


def _update_worker(root, revision, translation, barrier, queue):
    repo = ManagedGlossaryRepository(root)
    barrier.wait()
    result = update_glossary(repo, ref("race.json"), (entry(translation=translation),), revision)
    queue.put(result.failure)


def _run_pair(worker, arguments):
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(2)
    queue = context.Queue()
    processes = [context.Process(target=worker, args=(*args, barrier, queue)) for args in arguments]
    for process in processes:
        process.start()
    for process in processes:
        process.join(20)
        assert process.exitcode == 0
    return [queue.get(timeout=2) for _ in processes]


def _create_alias_worker(root, name, barrier, queue):
    repo = ManagedGlossaryRepository(root)
    barrier.wait()
    result = create_glossary(repo, ref(name), (entry(),))
    queue.put(result.failure)
