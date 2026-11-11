"""Translate EPUBs from the command line."""
from __future__ import annotations

from .. import batch, run as translation_run, settings, translate_options
from pathlib import Path
from typing import Optional
import typer
from ....kernel import DEFAULT_PERFORMANCE_PROFILE, UserMode
from ....network import NetworkPolicy

from ..render import _print_expected_error
from ..options import DEFAULT_CACHE_PATH, DEFAULT_TRANSLATOR_URL, NETWORK_POLICY_HELP, _selected_network_policy


def translate(
    ctx: typer.Context,
    network_policy: Optional[NetworkPolicy] = typer.Option(None, "--network-policy", help=NETWORK_POLICY_HELP),
    epub_paths: list[Path] = typer.Argument(
        ...,
        exists=True,
        dir_okay=False,
        readable=True,
        help="One or more input EPUB files.",
    ),
    output: Optional[Path] = typer.Option(
        None,
        "--output",
        "-o",
        help="Output EPUB path. Defaults to <input-stem>-<target>.epub.",
    ),
    output_dir: Optional[Path] = typer.Option(
        None,
        "--output-dir",
        help="Directory for translated EPUBs. Defaults to the translated books folder.",
        file_okay=False,
        dir_okay=True,
    ),
    review_output: Optional[Path] = typer.Option(
        None,
        "--review-output",
        help="Optional v2 review package for the completed project translation.",
    ),
    source: Optional[str] = typer.Option(
        None,
        "--source",
        help="Source language. If omitted, Ayvu reads the language metadata from the EPUB.",
    ),
    target: Optional[str] = typer.Option(
        None,
        "--target",
        help="Target language. Defaults to the profile target or pt.",
    ),
    translator_name: str = typer.Option("libretranslate", "--translator", help="Translator backend."),
    url: str = typer.Option(DEFAULT_TRANSLATOR_URL, "--url", help="Translator base URL."),
    cache_path: Path = typer.Option(DEFAULT_CACHE_PATH, "--cache", help="SQLite cache path."),
    glossary_path: Optional[Path] = typer.Option(None, "--glossary", help="Optional JSON glossary."),
    profile_name: Optional[str] = typer.Option(
        None,
        "--profile",
        help="Translation profile from the Ayvu config.",
    ),
    performance_profile_name: str = typer.Option(
        DEFAULT_PERFORMANCE_PROFILE,
        "--performance-profile",
        help="Performance profile: low, standard, high, or custom.",
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Process without writing translated EPUB."),
    fail_fast: bool = typer.Option(False, "--fail-fast", help="Stop at the first chapter/text error."),
    chapters: Optional[str] = typer.Option(
        None,
        "--chapters",
        help="Translate only selected chapters, e.g. '1-3,5,*chapter2*'.",
    ),
    continue_on_error: bool = typer.Option(
        False,
        "--continue-on-error",
        help="Continue translating remaining EPUBs after a batch item fails.",
    ),
    overwrite: bool = typer.Option(False, "--overwrite", help="Allow replacing an existing output file."),
    timeout: float = typer.Option(30.0, "--timeout", help="Translator HTTP timeout in seconds."),
    retries: Optional[int] = typer.Option(
        None,
        "--retries",
        help="Override the performance profile HTTP retry count.",
    ),
    requests_per_second: Optional[float] = typer.Option(
        None,
        "--requests-per-second",
        help="Override the performance profile maximum translator HTTP requests per second.",
    ),
    retry_backoff: Optional[float] = typer.Option(
        None,
        "--retry-backoff",
        help="Override the performance profile initial retry backoff delay in seconds.",
    ),
    retry_backoff_max: Optional[float] = typer.Option(
        None,
        "--retry-backoff-max",
        help="Override the performance profile maximum retry backoff delay in seconds.",
    ),
    translate_metadata: bool = typer.Option(
        False,
        "--translate-metadata",
        help="Also translate the EPUB title metadata and navigation text.",
    ),
    translate_alt_text: bool = typer.Option(
        False,
        "--translate-alt-text",
        help="Also translate the alt text of images. Text inside images (OCR) is out of scope.",
    ),
    chunk_limit: Optional[int] = typer.Option(
        None,
        "--chunk-limit",
        help="Override the performance profile maximum characters sent per request.",
    ),
    workers: Optional[int] = typer.Option(
        None,
        "--workers",
        help="Override the performance profile number of parallel document workers.",
    ),
    cache_only: bool = typer.Option(
        False,
        "--cache-only",
        help="Rebuild the EPUB using only cached translations; never call the translator.",
    ),
    require_full_cache: bool = typer.Option(
        False,
        "--require-full-cache",
        help="With --cache-only, only write the EPUB when every text is in the cache.",
    ),
    missing_output: Optional[Path] = typer.Option(
        None,
        "--missing-output",
        help="With --cache-only, path to save the original texts missing from the cache.",
    ),
    translation_memory: bool = typer.Option(
        False,
        "--translation-memory/--no-translation-memory",
        help="Enable translation memory: approved repository in rich/v2; legacy cache fuzzy reuse without a model label. Off by default.",
    ),
    tm_conservative_auto_apply: bool = typer.Option(
        False, "--tm-conservative-auto-apply",
        help="Rich/v2 only: allow approved fuzzy reuse at a threshold of at least 0.98, with exact structural evidence.",
    ),
    tm_legacy_cache_compatibility: bool = typer.Option(
        False, "--tm-legacy-cache-compatibility",
        help="Rich/v2 only: opt into machine/unreviewed cache compatibility; v1 rows without stage metadata are excluded.",
    ),
    tm_apply_threshold: float = typer.Option(
        0.95,
        "--tm-apply-threshold",
        help="Similarity threshold (0-1). Rich/v2 fuzzy auto-apply also requires --tm-conservative-auto-apply and at least 0.98.",
    ),
    tm_suggest_threshold: float = typer.Option(
        0.80,
        "--tm-suggest-threshold",
        help="Minimum similarity for a review suggestion (0-1). Rich/v2 fuzzy matches are suggestions by default.",
    ),
) -> None:
    """Translate EPUB visible text while preserving EPUB structure."""
    mode = ctx.obj.get("mode", UserMode.DEVELOPER)
    epub_list = list(epub_paths)
    if len(epub_list) > 100:
        _print_expected_error(
            "Um lote aceita no máximo 100 EPUBs.",
            "Divida os arquivos em lotes de até 100 EPUBs.",
            mode,
        )
        raise typer.Exit(code=1)
    if len(epub_list) > 1 and missing_output is not None:
        _print_expected_error(
            "Não é possível usar --missing-output com múltiplos EPUBs.",
            "Remova --missing-output para usar os arquivos automáticos de cada EPUB.",
            mode,
        )
        raise typer.Exit(code=1)
    translate_options._validate_translate_command_options(
        epub_paths=epub_list,
        output=output,
        output_dir=output_dir,
        review_output=review_output,
        mode=mode,
    )
    config = settings._load_existing_config_or_default()
    translate_options._validate_cache_only_options(
        cache_only=cache_only,
        require_full_cache=require_full_cache,
        missing_output=missing_output,
        mode=mode,
    )
    resolved_output_dir = translate_options._resolve_translation_output_dir(output_dir, mode=mode)
    chapter_selection = translate_options._parse_chapter_selection(chapters, mode=mode)
    tm_options = translate_options._build_translation_memory_options(
        enabled=translation_memory,
        apply_threshold=tm_apply_threshold,
        suggest_threshold=tm_suggest_threshold,
        conservative_auto_apply=tm_conservative_auto_apply,
        legacy_cache_compatibility=tm_legacy_cache_compatibility,
        mode=mode,
    )
    execution_controls = translate_options._resolve_execution_controls(
        performance_profile_name=performance_profile_name,
        workers=workers,
        requests_per_second=requests_per_second,
        retries=retries,
        retry_backoff=retry_backoff,
        retry_backoff_max=retry_backoff_max,
        chunk_limit=chunk_limit,
        mode=mode,
    )
    translate_options._validate_worker_options(
        workers=execution_controls.workers,
        translation_memory=tm_options,
        mode=mode,
    )

    if len(epub_list) > 1:
        batch._run_batch_translation(
            epub_paths=epub_list,
            output_dir=resolved_output_dir,
            source=source,
            target=target,
            translator_name=translator_name,
            url=url,
            network_policy=_selected_network_policy(network_policy),
            cache_path=cache_path,
            glossary_path=glossary_path,
            profile_name=profile_name,
            performance_profile_name=execution_controls.performance_profile_name,
            dry_run=dry_run,
            fail_fast=fail_fast,
            chapter_selection=chapter_selection,
            continue_on_error=continue_on_error,
            overwrite=overwrite,
            timeout=timeout,
            retries=execution_controls.retries,
            requests_per_second=execution_controls.requests_per_second,
            retry_backoff=execution_controls.retry_backoff,
            retry_backoff_max=execution_controls.retry_backoff_max,
            chunk_limit=execution_controls.chunk_limit,
            workers=execution_controls.workers,
            mode=mode,
            config=config,
            translate_metadata=translate_metadata,
            translate_alt_text=translate_alt_text,
            cache_only=cache_only,
            require_full_cache=require_full_cache,
            translation_memory=tm_options,
        )
        return

    translation_run._run_translation(
        epub_path=epub_list[0],
        output=output,
        output_dir=resolved_output_dir,
        review_output=review_output,
        source=source,
        target=target,
        translator_name=translator_name,
        url=url,
        network_policy=_selected_network_policy(network_policy),
        cache_path=cache_path,
        glossary_path=glossary_path,
        profile_name=profile_name,
        performance_profile_name=execution_controls.performance_profile_name,
        dry_run=dry_run,
        fail_fast=fail_fast,
        chapter_selection=chapter_selection,
        overwrite=overwrite,
        timeout=timeout,
        retries=execution_controls.retries,
        requests_per_second=execution_controls.requests_per_second,
        retry_backoff=execution_controls.retry_backoff,
        retry_backoff_max=execution_controls.retry_backoff_max,
        chunk_limit=execution_controls.chunk_limit,
        workers=execution_controls.workers,
        mode=mode,
        config=config,
        translate_metadata=translate_metadata,
        translate_alt_text=translate_alt_text,
        cache_only=cache_only,
        require_full_cache=require_full_cache,
        missing_output=missing_output,
        translation_memory=tm_options,
        confirm_output_location=resolved_output_dir is None,
    )
