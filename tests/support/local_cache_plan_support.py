"""Real v2 evidence built from negotiated fake-provider plans."""
from dataclasses import fields
from uuid import UUID

import pytest

from ayvu.knowledge.cache import (
    TRANSLATION_STAGE_AGGREGATE, CacheAuthorizationPolicy, CacheProvenanceMetadata,
    ExactCacheEntry, PutIfAbsentState, exact_entry_key,
)
from ayvu.knowledge.cache.read_only import ReadOnlyExactCache
from ayvu.knowledge.cache.repository import SQLiteExactCache
from ayvu.provenance import ProducerIdentity
from ayvu.providers import ProviderRequirements, ProviderRoute
from ayvu.translation import LocalCachePlanIntent, LocalCachePlanResolver, resolve_plan
from tests.support.translation_plan_support import snapshot

SOURCE = "  A source with café and Unicode.  "
SCOPE = CacheAuthorizationPolicy(str(UUID(int=0x4321))).resolve()
PRODUCER = ProducerIdentity("fake@1", "declared:local-model", "adapter@1", "pipeline@1")


class FakeProvider:
    def capabilities(self):
        return snapshot()


def plan(**changes):
    options = {"chunk_limit": 3000, **changes}
    producer = options.pop("producer", PRODUCER)
    route = options.pop("route", ProviderRoute("en", "pt"))
    return resolve_plan(FakeProvider().capabilities(), ProviderRequirements(()), producer, route, **options)


def intent_for(resolved):
    local = {field.name: getattr(resolved, field.name) for field in fields(LocalCachePlanIntent)
             if field.name not in {"source_locale", "target_locale"}}
    return LocalCachePlanIntent(resolved.route.source, resolved.route.target, **local)


@pytest.fixture
def evidence(tmp_path):
    path = tmp_path / "local.v2.sqlite"
    writer = SQLiteExactCache(path)
    reader = ReadOnlyExactCache(path)

    class Evidence:
        def store(self, resolved, *, source=SOURCE, scope=SCOPE, value="Translated text"):
            operation = resolved.semantic_operation
            assert operation is not None
            key = exact_entry_key(scope, TRANSLATION_STAGE_AGGREGATE, operation.fingerprint, source)
            entry = ExactCacheEntry(key, value,
                                    CacheProvenanceMetadata("translate", resolved.producer, operation.pipeline_revision),
                                    source, operation.canonical_payload())
            assert writer.put_if_absent(entry).state is PutIfAbsentState.INSERTED
            return key

        def resolver(self, intent=None, *, scope=SCOPE, port=None):
            return LocalCachePlanResolver(reader if port is None else port, scope,
                                          intent or LocalCachePlanIntent("en", "pt", PRODUCER, 3000))

        def records(self):
            from ayvu.knowledge.cache import source_content_digest
            return reader.canonical_records(SCOPE, TRANSLATION_STAGE_AGGREGATE, source_content_digest(SOURCE), limit=256)

    result = Evidence()
    result.path = path
    return result
