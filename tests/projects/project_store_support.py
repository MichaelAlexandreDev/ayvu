from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import hashlib
import json
import os
import multiprocessing
from pathlib import Path
from threading import Barrier
from uuid import uuid4
import time
import pytest
from ayvu.projects.model import ProjectConflictError, ProjectError, ProjectSourceError
from ayvu.projects.service import resolve_project
from ayvu.projects.store import (
    ProjectStoreError,
    ProjectJsonRepository, build_project_request, canonical_source_path, configuration_references, decode_manifest, encode_manifest,
    file_digest, source_digest, translation_selections,
)

@pytest.fixture
def project_input(tmp_path):
    source = tmp_path / 'source.epub'
    source.write_bytes(b'synthetic-original')
    repository = ProjectJsonRepository(tmp_path / 'processing')
    request = build_project_request(source, source='en', target='pt')
    return source, repository, request


def _resolve_in_process(processing, source, barrier, queue):
    request = build_project_request(Path(source), source='en', target='pt')
    barrier.wait(timeout=10)
    result = ProjectJsonRepository(Path(processing)).resolve(request)
    queue.put((result.project.project_id, result.created))


def _guard(project_input):
    _, repository, request = project_input
    project = repository.resolve(request).project
    return repository, project, repository.run_guard(project.project_id)
