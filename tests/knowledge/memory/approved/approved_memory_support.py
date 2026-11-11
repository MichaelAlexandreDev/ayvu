from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, fields, replace
import logging
import sqlite3
from threading import Barrier
from uuid import UUID, uuid4
import pytest
from ayvu.knowledge.memory.approved.model import (
    MAX_VALUE_BYTES, NORMALIZATION_VERSION, SEARCH_VERSION, TRANSITIONS, Candidate, MemoryConflictError,
    MemoryNotFoundError, MemoryProvenanceError, MemoryScope, MemoryScopeError, MemorySource, MemoryState,
    MemoryStorageError, MemoryTransitionError, MemoryValidationError, MemoryValueError, PipelineStage, ScopeKind,
    TargetValue, ValueKind, require_transition,
)
from ayvu.knowledge.memory.approved.service import CreateCandidate, ReviewRevision
from ayvu.knowledge.memory.approved.store import MEMORY_MIGRATIONS, SqliteApprovedMemoryRepository, memory_migration_runner
from ayvu.v1.cache import CacheKey, TranslationCache
from ayvu.provenance.model import ActorKind, ConfigurationRef
from tests.support.approved_memory_fixtures import IMPORT_SCOPE, NOW, PAIR, PIN, PROJECT_SCOPE, SCOPES, USER_SCOPE, WORK_SCOPE, MemoryEnvironment, document, final, source
from tests.support.content_fixtures import PROJECT
from ayvu.kernel import LanguagePair

@pytest.fixture
def env(tmp_path):
    return MemoryEnvironment(tmp_path)
