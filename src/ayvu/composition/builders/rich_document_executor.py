"""Map rich results and wire offline document execution for durable jobs."""
from __future__ import annotations

from dataclasses import replace

from ...content import ContentDocument
from ...formats.epub import html_unit_template, normalize_translated_text, without_xml_invalid, review_text_from_template
from ...kernel import JobFailure, JobFailureFamily
from ...knowledge.cache.read_only import ReadOnlyExactCache
from ...translation import (
    DocumentTranslator, ExecutionContext,
    RuntimeContext, TranslationExecutionError, UnitExecution, UnitSource,
    ALT_TEXT_KIND, METADATA_KIND, STATUS_CACHE, STATUS_MEMORY, STATUS_MISSING, STATUS_TRANSLATED,
)


def prepare_offline(runtime):
    """Only the explicit local preparation callback may run in this branch."""
    if runtime._translation_context is not None:
        raise TranslationExecutionError(JobFailure(JobFailureFamily.INPUT, "offline_context_preparation_required"))
    if runtime._prepare_offline is None:
        raise TranslationExecutionError(JobFailure(JobFailureFamily.INPUT, "offline_preparation_required"))
    runtime._prepared = runtime._prepare_offline()
    runtime._cache = ReadOnlyExactCache(runtime._cache_path)


def reconstruction_text(unit, tokenized):
    """One format projection for online and offline rich hits."""
    kind = unit.content.kind.name
    if kind == METADATA_KIND:
        if without_xml_invalid(tokenized) != tokenized:
            raise TranslationExecutionError(JobFailure(JobFailureFamily.INTEGRITY, "invalid_title_result"))
        return tokenized
    return without_xml_invalid(tokenized) if kind == ALT_TEXT_KIND else normalize_translated_text(tokenized)


def map_offline_execution(unit, result):
    if result.status == STATUS_MISSING:
        return result
    return replace(result, reconstruction_text=reconstruction_text(unit, result.reconstruction_text))


class RichDocumentExecutor:
    def __init__(self, translator: DocumentTranslator, context: RuntimeContext) -> None:
        self._translator, self._context = translator, context

    def __repr__(self) -> str:
        return "DocumentExecutor()"

    def execute_document(self, document: ContentDocument, index: int):
        context = self._context
        job = context.current_job() if context.current_job is not None else context.job
        # Raw results replay only from earlier jobs of this continuation chain, after every dimension is proved.
        result = self._translator.translate(document, ExecutionContext(
            job, context.job.lease.owner_token, context.cancellation,
            progress_phase=f"document_{index}", reusable_jobs=context.reusable_jobs),
            replay_raw_results=bool(context.reusable_jobs))
        executions = []
        for unit, translated in zip(document.units, result.units, strict=True):
            template = html_unit_template(unit)
            tokenized = translated.tokenized_text
            if tokenized is None:
                raise TranslationExecutionError(JobFailure(JobFailureFamily.INTEGRITY, "missing_tokenized_result"))
            status = (STATUS_CACHE if translated.source in (UnitSource.EXACT_CACHE, UnitSource.ARTIFACT_REPLAY) else
                      STATUS_MEMORY if translated.source in (UnitSource.APPROVED_MEMORY, UnitSource.APPLIED_MEMORY) else
                      STATUS_MISSING if translated.source is UnitSource.CACHE_ONLY_MISSING else STATUS_TRANSLATED)
            rebuilt = reconstruction_text(unit, tokenized)
            executions.append(UnitExecution(rebuilt, status, template, translated.text,
                memory_suggestion=None if not translated.retrieval_suggestions else translated.retrieval_suggestions[0].source_snippet,
                glossary_usage=translated.glossary_usage))
        return tuple(executions)
