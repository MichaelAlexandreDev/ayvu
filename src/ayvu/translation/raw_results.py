"""Canonical raw-result payloads, reuse dimensions and their validation."""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, is_dataclass
from decimal import Decimal
from enum import StrEnum

from ..artifacts import ArtifactRequest
from ..knowledge.cache import AuthorizedCacheScope, TRANSLATION_STAGE_AGGREGATE, exact_entry_key
from ..knowledge.memory.approved import MemoryScope
from .stages import RetrievalDecision, RetrievalSource
from ..content import ContentDocument
from ..jobs import TranslationJob
from .resume_reuse import REUSE_DIMENSIONS
from .plan import ResolvedTranslationPlan
from ..content import TextParts, protected_content_is_intact
from ..kernel import JobFailure, JobFailureFamily
from .document_contracts import TranslationExecutionError, UnitSource, _template


# Version 2 records the producing dimensions, so a continuation can prove or name what changed.
RAW_RESULTS_VERSION = 2
_RAW_RESULTS_VERSIONS = frozenset({1, RAW_RESULTS_VERSION})
RAW_RESULTS_ROLE = "raw_results"
RAW_RESULTS_STAGE = "raw_cache_write"
RAW_RESULTS_MEDIA_TYPE = "application/json"


def _decision_metadata(decision: RetrievalDecision, source: UnitSource) -> dict:
    key = decision.cache_identity
    scope = decision.scope
    return {
        "source": decision.source.value, "auto_applied": decision.auto_applied,
        "reason": decision.reason, "score": decision.score,
        "entry_id": decision.entry_id, "revision_id": decision.revision_id,
        "provenance_id": decision.provenance_id, "revision_number": decision.revision_number,
        "cache_identity": None if key is None else {
            "scope_kind": key.scope.scope.kind.value, "scope_id": key.scope.scope.scope_id,
            "value_kind": key.value_kind.value, "semantic_fingerprint": key.semantic_fingerprint,
            "source_content_digest": key.source_content_digest,
        },
        "lifecycle": decision.lifecycle.value, "reviewed": decision.reviewed, "dataset_eligible": False,
        "value_stage": "linguistic_final" if source is UnitSource.APPROVED_MEMORY else "pre_glossary",
        "output_kind": "linguistic_final" if source is UnitSource.APPROVED_MEMORY else "glossary_derived",
        "output_stage": "linguistic_final" if source is UnitSource.APPROVED_MEMORY else "terminology_applied",
        "approval_inherited_after_transformation": False,
        "scope": (None if scope is None else [scope.kind.value, scope.owner_id] if isinstance(scope, MemoryScope)
                  else [scope.scope.kind.value, scope.scope.scope_id]),
    }


def _canonical_value(value: object) -> object:
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, Decimal):
        # The cache identity's form; JSON has no exact decimal type.
        return format(value, "f")
    if is_dataclass(value) and not isinstance(value, type):
        return {key: _canonical_value(item) for key, item in asdict(value).items()}
    if isinstance(value, dict):
        return {str(key): _canonical_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_canonical_value(item) for item in value]
    return value


def _dimension_digest(value: object) -> str:
    data = json.dumps(_canonical_value(value), sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(data.encode("ascii")).hexdigest()


def reuse_dimensions(document: ContentDocument, plan: ResolvedTranslationPlan) -> dict[str, str]:
    """One opaque digest per ``REUSE_DIMENSIONS`` name, from what produced raw text for this document."""
    producer = plan.producer
    raw_stages = [[item.stage_id, item.version] for item in plan.stages if item.stage_id != "post_processing"]
    values = {
        "input": [document.revision_id, document.source_fingerprint, document.source_locale],
        "configuration": [plan.policy_ref, plan.configuration_revision, plan.glossary_ref if plan.glossary_native else None,
                          plan.context_ref, plan.context_strategy, plan.context_revision, plan.prompt_strategy,
                          plan.prompt_revision, plan.terminology_revision, plan.generation_parameters],
        "provider": [producer.provider, producer.model, producer.adapter],
        # Only the languages: each hop's producer and capabilities are already their own dimensions.
        "route": [plan.route.source, plan.route.intermediate, plan.route.target,
                  None if plan.route_hops is None else [[hop.source_locale, hop.target_locale] for hop in plan.route_hops]],
        "capability": [plan.capabilities, plan.chunk_limit],
        "format": [document.adapter, plan.format_constraints],
        "locator": [document.project_id, document.document_id, document.logical_key,
                    [unit.unit_id for unit in document.units]],
        "pipeline": [producer.pipeline, plan.pipeline_revision, plan.segmentation_revision, plan.chunking_revision,
                     plan.protection_revision, plan.normalization_revision, plan.schema_version],
        "stage": raw_stages,
    }
    assert tuple(values) == REUSE_DIMENSIONS
    return {name: _dimension_digest(value) for name, value in values.items()}


def _raw_payload(document: ContentDocument, plan: ResolvedTranslationPlan,
                 raw: list[tuple[str, UnitSource, str | None]], results=()) -> bytes:
    decisions = {result.unit_id: result.retrieval_decision for result in results}
    def entry(unit_id, source, text):
        value = {"raw": text, "source": source.value, "unit_id": unit_id}
        decision = decisions.get(unit_id)
        if decision is not None:
            value["decision"] = _decision_metadata(decision, source)
        return value
    return json.dumps({
        "dimensions": reuse_dimensions(document, plan),
        "document_id": document.document_id,
        "raw_fingerprint": plan.raw_fingerprint,
        "revision_id": document.revision_id,
        "schema_version": RAW_RESULTS_VERSION,
        "units": [entry(unit_id, source, text) for unit_id, source, text in raw],
    }, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("ascii")


def _json_object(payload: bytes | None) -> dict | None:
    if payload is None:
        return None
    try:
        data = json.loads(payload)
    except (ValueError, RecursionError):
        return None
    return data if type(data) is dict else None


def _artifact_request(self, document: ContentDocument, job: TranslationJob,
                      execution_nonce: str | None = None) -> ArtifactRequest:
    version = next(item.version for item in self._plan.stages if item.stage_id == RAW_RESULTS_STAGE)
    semantic_fingerprint = self._plan.semantic_fingerprint
    if semantic_fingerprint is None:
        # An unidentified producer cannot reuse even a job-local raw artifact.
        identity = json.dumps([document.document_id, document.revision_id, job.job_id, execution_nonce],
                              separators=(",", ":"))
    else:
        identity = json.dumps([document.document_id, document.revision_id, semantic_fingerprint], separators=(",", ":"))
    if self._approved_memory is not None or self._retrieval_policy.legacy_cache_enabled or self._translation_context is not None:
        # Never replay a final TM value as pre-glossary raw text, or bypass review withdrawal.
        identity = json.dumps([identity, "retrieval_revalidated", execution_nonce], separators=(",", ":"))
    return ArtifactRequest(document.project_id, job.job_id, RAW_RESULTS_ROLE, RAW_RESULTS_STAGE, version,
                           f"sha256:{hashlib.sha256(identity.encode('ascii')).hexdigest()}", RAW_RESULTS_MEDIA_TYPE)


def _decode_raw(self, payload: bytes, document: ContentDocument, scope: AuthorizedCacheScope):
    invalid = TranslationExecutionError(JobFailure(JobFailureFamily.INTEGRITY, "invalid_raw_results"))
    try:
        data = json.loads(payload)
        keys = {"document_id", "raw_fingerprint", "revision_id", "schema_version", "units"}
        if (type(data) is not dict or data.get("schema_version") not in _RAW_RESULTS_VERSIONS
                or set(data) != (keys if data["schema_version"] == 1 else keys | {"dimensions"})
                or type(data["schema_version"]) is not int or data["document_id"] != document.document_id
                or data["revision_id"] != document.revision_id or data["raw_fingerprint"] != self._plan.raw_fingerprint
                or type(data["units"]) is not list or len(data["units"]) != len(document.units)):
            raise invalid
        decoded = []
        for unit, entry in zip(document.units, data["units"]):
            if (type(entry) is not dict or set(entry) not in ({"raw", "source", "unit_id"}, {"raw", "source", "unit_id", "decision"})
                    or entry["unit_id"] != unit.unit_id):
                raise invalid
            source, raw = UnitSource(entry["source"]), entry["raw"]
            template, _ = _template(unit)
            core = TextParts.from_text(template).core
            if source is UnitSource.BLANK:
                if raw is not None or core:
                    raise invalid
            elif (source not in (UnitSource.EXACT_CACHE, UnitSource.PROVIDER) or not isinstance(raw, str)
                    or not protected_content_is_intact(core, raw)):
                raise invalid
            decision = None
            if "decision" in entry:
                if source not in (UnitSource.EXACT_CACHE, UnitSource.PROVIDER):
                    raise invalid
                cached = source is UnitSource.EXACT_CACHE
                fingerprint = self._plan.semantic_fingerprint
                if cached and fingerprint is None:
                    raise invalid
                decision = RetrievalDecision(RetrievalSource.EXACT_CACHE if cached else RetrievalSource.PROVIDER,
                    True, "eligible_exact_cache" if cached else "no_eligible_reuse", scope=scope if cached else None,
                    cache_identity=exact_entry_key(scope, TRANSLATION_STAGE_AGGREGATE, fingerprint, unit.source_text)
                                   if cached else None)
                if entry["decision"] != _decision_metadata(decision, source):
                    raise invalid
            decoded.append((source, raw, decision))
        return decoded
    except (ValueError, TypeError, RecursionError) as error:
        if isinstance(error, TranslationExecutionError):
            raise
        raise invalid from None
