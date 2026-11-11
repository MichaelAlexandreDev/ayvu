"""Golden values for identities that users' data depends on (REF-01, #266).

Caches, translation plans, profiles, context, provider decisions, approved memory and raw results are
keyed by digests. Each one is built from a literal domain tag and a canonical encoding. If any of them
changes, existing caches stop matching and resumes start over, with no error. The refactoring moves
almost every module, so these values are pinned here, independent of where the code lives.

A failure here is not a test to update. It means user data would be invalidated. Change one only on
purpose, with a migration and a version bump, and say so in the pull request.

What each identity keys (checked against its callers at 5796e49):
- content source digest: source divergence checks in the project store and job recovery;
- content document id: the document and unit identities that review packages and raw results carry;
- translation plan digest: the plan fingerprints recorded with each job;
- cache v1 text hash: the v1 SQLite cache key, together with the source and target languages;
- cache v2 source digest and semantic fingerprint: the v2 cache entry key (``<cache>.v2.sqlite``);
- profile revision: profile revisions in the project store and the plans that reference them;
- context content identity and projection: the context revision a job records;
- provider network binding: ties a network decision to one provider, configuration and operation;
- approved memory command fingerprint: ``tm_operations.command_hash`` in the memory store;
- raw-results reuse dimension: the raw-results payload v2 (#242).
"""

from __future__ import annotations

import ast
from decimal import Decimal
from functools import cache as memoize
from pathlib import Path

import pytest

from ayvu import approved_memory_service, cache_identity, project_profiles, provider_network
from ayvu.content import model as content
from ayvu.v1 import cache
from ayvu import translation_context, translation_document, translation_plan
from ayvu.approved_memory_store import _digest as memory_digest
from ayvu.cache_identity import CanonicalValue, SemanticOperationV2, SemanticRouteHop
from ayvu.provenance.model import ProducerIdentity

SOURCE = Path(__file__).resolve().parents[1] / "src" / "ayvu"
TEXT = "Olá, leitor — café."

# Each literal tag frames a digest. Moving code must not change the literal.
DOMAIN_TAGS = (
    "ayvu.content", "ayvu.translation_plan", "ayvu.semantic_operation", "ayvu.project_profile",
    "ayvu.profile_snapshot", "ayvu.translation_context", "ayvu.context_content.v1", "ayvu.context_egress.v1",
    "ayvu.provider_network",
)


@memoize
def string_constants() -> tuple[str, ...]:
    found = []
    for path in sorted(SOURCE.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                found.append(node.value)
    return tuple(found)


@pytest.mark.parametrize("tag", DOMAIN_TAGS)
def test_each_domain_tag_is_still_written_once_in_the_source(tag):
    assert string_constants().count(tag) == 1, (
        f"{tag!r} frames a persisted digest; renaming or duplicating it invalidates user data")


def test_no_new_dotted_ayvu_tag_appears_without_being_pinned_here():
    dotted = {value for value in string_constants() if value.startswith("ayvu.") and " " not in value}
    assert dotted == set(DOMAIN_TAGS), sorted(dotted - set(DOMAIN_TAGS))


@pytest.mark.parametrize("name,value", [
    ("content IDENTITY_VERSION", content.IDENTITY_VERSION),
    ("translation_plan PLAN_SCHEMA_VERSION", translation_plan.PLAN_SCHEMA_VERSION),
    ("provider_network NETWORK_DECISION_VERSION", provider_network.NETWORK_DECISION_VERSION),
    ("cache_identity CACHE_IDENTITY_VERSION", cache_identity.CACHE_IDENTITY_VERSION),
    ("project_profiles SCHEMA_VERSION", project_profiles.SCHEMA_VERSION),
])
def test_identity_versions_are_unchanged(name, value):
    expected = {
        "content IDENTITY_VERSION": 1, "translation_plan PLAN_SCHEMA_VERSION": 1,
        "provider_network NETWORK_DECISION_VERSION": 1, "cache_identity CACHE_IDENTITY_VERSION": 2,
        "project_profiles SCHEMA_VERSION": 1,
    }[name]
    assert value == expected, f"{name} is part of persisted identities"


@pytest.mark.parametrize("name,expected", [
    ("PIPELINE_SEMANTIC_REVISION", ("ayvu", "pipeline", 2, "translation-pipeline-v2")),
    ("SEGMENTATION_REVISION", ("ayvu", "segmentation", 1, "translation-unit-v1")),
    ("CHUNKING_REVISION", ("ayvu", "chunking", 1, "provider-chunks-v1")),
    ("PROTECTION_REVISION", ("ayvu", "protection", 1, "protected-input-v1")),
    ("NORMALIZATION_REVISION", ("ayvu", "normalization", 1, "outer-whitespace-v1")),
])
def test_pipeline_revisions_are_unchanged(name, expected):
    revision = getattr(translation_plan, name)
    assert (revision.owner, revision.namespace, revision.version, revision.value) == expected
    assert revision.snapshot_reference is None


def semantic_operation() -> SemanticOperationV2:
    producer = ProducerIdentity("provider@1", "model@1", "adapter@1", "pipeline@1")
    hop = SemanticRouteHop("en", "pt", producer, "capfp:v1:" + "a" * 64)
    plan = translation_plan
    return SemanticOperationV2(
        "translate", "en", "pt", (hop,), plan.PIPELINE_SEMANTIC_REVISION,
        (("unit_whitespace", 1), ("protection", 1), ("provider_chunks", 1), ("placeholder_restoration", 1),
         ("raw_cache_write", 1)),
        "none", None, "none", None, None, (("temperature", CanonicalValue("decimal", Decimal("0.25"))),),
        plan.SEGMENTATION_REVISION, 3000, plan.CHUNKING_REVISION, plan.PROTECTION_REVISION,
        plan.NORMALIZATION_REVISION, (("markup", CanonicalValue("boolean", True)),))


GOLDEN = {
    "content source digest": (
        lambda: content.source_digest("project-1", TEXT),
        "scd:v1:def57be0a6dcadcc562f98c63e3f4d5264354508dd51e18347c80dd8306921b7"),
    "content document id": (
        lambda: content._document_id("project-1", "epub", "text/chapter1.xhtml"),
        "doc:v1:7829777ba181b2caa68a6ae0d6694c41e7c035435b9d3d2afd109be11cc560f5"),
    "translation plan digest": (
        lambda: translation_plan._digest("producer", {"name": "libretranslate", "version": 1}),
        "sha256:417f5ef5c9e27d9f30217d8f581a8b06e261205c6e6bc985415fcce911fd3879"),
    "cache v1 text hash": (
        lambda: cache.text_hash(TEXT),
        "8d4642afb4cbbf6a2f242cab16bd32bc8c9a6a90954b17146c3fcb8ab92e9ec7"),
    "cache v2 source digest": (
        lambda: cache_identity.source_content_digest(TEXT),
        "sha256:8d4642afb4cbbf6a2f242cab16bd32bc8c9a6a90954b17146c3fcb8ab92e9ec7"),
    "cache v2 semantic fingerprint": (
        lambda: semantic_operation().fingerprint,
        "sha256:b8c3c8a5eca5ebf26533b226a2e28644928bfbd64d57bbc270aa3cb030a936f0"),
    "profile revision": (
        lambda: project_profiles.ProjectTranslationProfile(target_language="pt").revision.value,
        "4fb63521a5f0984e110867d2946a61627cc5dd8f6539c14a6c5bba5c34cbb0db"),
    "context content identity": (
        lambda: translation_context.content_identity("project-1", TEXT),
        "b078b0ba3073ce238565d61eb30296380e3c0273e60e40665e61f2a15e96b15f"),
    "context projection hash": (
        lambda: translation_context._hash(("ayvu.translation_context", "x")),
        "e780ea9b58c59974490105155f3d4a83160ee9b22f12a868f808023c2e6c7901"),
    "provider network binding": (
        lambda: provider_network.network_binding("libretranslate", None, provider_network.ProviderOperation.TEST),
        "sha256:e65085942b920bfd1289420203d960a5d6d394a4b169b7fc7be7333b7f2cc9ae"),
    "approved memory command fingerprint": (
        lambda: memory_digest(["CreateCandidate", {"operation_id": "op-1"}]),
        "838c5b97825375dc91efac203b085c365af7c02c0a374d5b773f1668bf304af5"),
    "raw-results reuse dimension": (
        lambda: translation_document._dimension_digest({"input": ["rev-1", "sha256:x", "en"], "title": "Capítulo"}),
        "sha256:aca5b426889afe54ec666956d99cafb27f364e79b2fb3ef16c44e8bff8ea9792"),
}


@pytest.mark.parametrize("name", sorted(GOLDEN))
def test_golden_identity(name):
    compute, expected = GOLDEN[name]
    assert compute() == expected, f"the {name} changed: existing user data would stop matching"


# The approved memory store hashes the command's class name, so these names are part of its data.
FINGERPRINTED_COMMANDS = ("CreateCandidate", "DeriveRevision", "ReviewRevision")


@pytest.mark.parametrize("name", FINGERPRINTED_COMMANDS)
def test_fingerprinted_command_classes_keep_their_names(name):
    command = getattr(approved_memory_service, name, None)
    assert command is not None and command.__name__ == name, (
        f"{name} enters tm_operations.command_hash; renaming it breaks replay of recorded operations")
