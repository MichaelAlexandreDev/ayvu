from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime
from threading import Barrier, Lock
import pytest
from ayvu.knowledge.cache.identity_values import (
    CACHE_ENTRY_V2_LIMITS as LIMITS, CacheIdentityError, TRANSLATION_STAGE_AGGREGATE,
)
from ayvu.knowledge.cache.scope import CacheAuthorizationPolicy
from ayvu.knowledge.cache.entries import (
    CacheLookupState, CacheProvenanceMetadata, ExactCacheEntry, PutIfAbsentState, exact_entry_key,
)
from ayvu.knowledge.cache.repository import SQLiteExactCache
from tests.support.translation_cache_v2_support import Env, PROJECT, make_plan
from ayvu.kernel import LanguagePair, TranslationMemoryOptions

def entry(source="source", value="translation", metadata=b"{}"):
    plan = make_plan()
    operation = plan.semantic_operation
    key = exact_entry_key(CacheAuthorizationPolicy(PROJECT).resolve(), TRANSLATION_STAGE_AGGREGATE,
                          operation.fingerprint, source)
    return ExactCacheEntry(key, value, CacheProvenanceMetadata("translate", plan.producer, operation.pipeline_revision),
                           source, operation.canonical_payload(), metadata)
