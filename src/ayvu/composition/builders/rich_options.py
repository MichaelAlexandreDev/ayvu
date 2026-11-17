"""The option checks of the rich runtime, before it prepares anything, and its translation-memory policy."""
from __future__ import annotations

from ...kernel import TranslationOptions
from ...translation import PreflightError
from ...translation.stages import RetrievalPolicy


def memory_policy(options) -> RetrievalPolicy:
    try:
        return RetrievalPolicy(fuzzy_auto_apply=options.conservative_auto_apply,
            suggest_threshold=options.suggest_threshold,
            apply_threshold=options.apply_threshold if options.conservative_auto_apply else max(0.98, options.apply_threshold),
            legacy_cache_enabled=options.legacy_cache_compatibility)
    except ValueError:
        raise PreflightError("Política de memória inválida no runtime rich/v2.",
            "Use --tm-apply-threshold 0.98 ou maior com --tm-conservative-auto-apply; fuzzy é sugestão por padrão.") from None


def check_rich_options(options: TranslationOptions) -> None:
    if options.workers > 1 and options.translation_memory is not None:
        raise PreflightError("--workers é incompatível com --translation-memory nesta versão.",
                             "Use --workers 1 com --translation-memory, ou remova --translation-memory para executar em paralelo.")
    if options.translation_memory is not None:
        memory_policy(options.translation_memory)
    if options.require_full_cache and not options.cache_only:
        raise PreflightError("--require-full-cache exige --cache-only.", "Adicione --cache-only.")
