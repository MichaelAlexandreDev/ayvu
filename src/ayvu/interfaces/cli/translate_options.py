"""Resolve and validate translation command options."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import typer
from ...preferences import AyvuConfig, TranslationProfile
from .contracts import SelectedChapter
from ...composition.cli import default_glossaries_dir, detect_epub_language, resolve_chapter_selection, translation_modality
from ...kernel import ChapterSelection, ChapterSelectionError, DEFAULT_PERFORMANCE_PROFILE, PERFORMANCE_PROFILES, PerformanceProfile, TranslationMemoryError, TranslationMemoryOptions, TranslationOptions, UserMode

from .render import _print_epub_read_error, _print_expected_error, console
from .options import DEFAULT_SOURCE_LANGUAGE, DEFAULT_TARGET_LANGUAGE


@dataclass(frozen=True)
class ExecutionControls:
    performance_profile_name: str
    workers: int
    requests_per_second: float | None
    retries: int
    retry_backoff: float
    retry_backoff_max: float
    chunk_limit: int


def _resolve_source_language(
    explicit_source: str | None,
    epub_path: Path,
    mode: UserMode,
) -> tuple[str, bool]:
    if explicit_source is not None and explicit_source.strip():
        return explicit_source.strip(), False

    try:
        detected = detect_epub_language(epub_path)
    except Exception:
        return DEFAULT_SOURCE_LANGUAGE, True

    if detected is None:
        console.print(
            "[yellow]Idioma do EPUB ausente ou inválido nos metadados.[/yellow] "
            f"Usando '{DEFAULT_SOURCE_LANGUAGE}' como padrão. "
            "Use --source para escolher outro idioma de origem."
        )
        return DEFAULT_SOURCE_LANGUAGE, True

    return detected, True


def _resolve_execution_controls(
    *,
    performance_profile_name: str,
    workers: int | None,
    requests_per_second: float | None,
    retries: int | None,
    retry_backoff: float | None,
    retry_backoff_max: float | None,
    chunk_limit: int | None,
    mode: UserMode,
) -> ExecutionControls:
    profile = _resolve_performance_profile(performance_profile_name, mode=mode)
    return ExecutionControls(
        performance_profile_name=profile.name,
        workers=workers if workers is not None else profile.workers,
        requests_per_second=(
            requests_per_second
            if requests_per_second is not None
            else profile.requests_per_second
        ),
        retries=retries if retries is not None else profile.retries,
        retry_backoff=retry_backoff if retry_backoff is not None else profile.retry_backoff,
        retry_backoff_max=(
            retry_backoff_max if retry_backoff_max is not None else profile.retry_backoff_max
        ),
        chunk_limit=chunk_limit if chunk_limit is not None else profile.chunk_limit,
    )


def _resolve_performance_profile(name: str, mode: UserMode) -> PerformanceProfile:
    normalized = name.strip().lower() if name else DEFAULT_PERFORMANCE_PROFILE
    profile = PERFORMANCE_PROFILES.get(normalized)
    if profile is not None:
        return profile

    available = ", ".join(sorted(PERFORMANCE_PROFILES))
    _print_expected_error(
        "Perfil de performance não encontrado.",
        "Use --performance-profile com um dos perfis disponíveis, ou remova a opção para usar standard.",
        mode,
        detail=f"Perfil solicitado: {name}. Perfis disponíveis: {available}.",
    )
    raise typer.Exit(code=1)


def _resolve_requested_profile(
    config: AyvuConfig,
    profile_name: str | None,
    mode: UserMode,
) -> tuple[str | None, TranslationProfile | None]:
    if profile_name is None or not profile_name.strip():
        return None, None

    name = profile_name.strip()
    profile = config.profiles.get(name)
    if profile is not None:
        return name, profile

    available = ", ".join(sorted(config.profiles)) or "nenhum perfil configurado"
    _print_expected_error(
        "Perfil de tradução não encontrado.",
        "Use --profile com um perfil definido em config.json, ou remova --profile para rodar sem perfil.",
        mode,
        detail=f"Perfil solicitado: {name}. Perfis disponíveis: {available}.",
    )
    raise typer.Exit(code=1)


def _resolve_target_language(
    explicit_target: str | None,
    profile: TranslationProfile | None,
    default_target: str = DEFAULT_TARGET_LANGUAGE,
) -> str:
    if explicit_target is not None and explicit_target.strip():
        return explicit_target.strip()
    if profile is not None and profile.target_language is not None:
        return profile.target_language
    return default_target


def _resolve_glossary_path(
    explicit_glossary_path: Path | None,
    profile: TranslationProfile | None,
) -> Path | None:
    if explicit_glossary_path is not None:
        return explicit_glossary_path
    if profile is None or profile.glossary is None:
        return None

    profile_glossary = profile.glossary.expanduser()
    if profile_glossary.is_absolute():
        return profile_glossary
    return default_glossaries_dir() / profile_glossary


def _parse_chapter_selection(value: str | None, mode: UserMode) -> ChapterSelection | None:
    if value is None:
        return None
    try:
        return ChapterSelection.parse(value)
    except ChapterSelectionError as exc:
        _print_expected_error(
            "Seleção de capítulos inválida.",
            "Use índices 1-based, faixas e padrões separados por vírgula, por exemplo --chapters '1-3,5,*chapter2*'.",
            mode,
            detail=str(exc),
        )
        raise typer.Exit(code=1) from exc


def _resolve_chapter_selection_for_display(
    epub_path: Path,
    selection: ChapterSelection,
    options: TranslationOptions,
    mode: UserMode,
) -> list[SelectedChapter]:
    try:
        return resolve_chapter_selection(
            epub_path,
            selection,
            translate_metadata=options.translate_metadata,
            max_documents=options.max_documents,
        )
    except ChapterSelectionError as exc:
        _print_expected_error(
            "Nenhum capítulo corresponde à seleção informada.",
            "Confira --chapters e use índices 1-based, faixas ou padrões que existam no EPUB.",
            mode,
            detail=str(exc),
        )
        raise typer.Exit(code=1) from exc
    except Exception as exc:
        _print_epub_read_error(str(exc), mode)
        raise typer.Exit(code=1) from exc


def _validate_translate_command_options(
    epub_paths: list[Path],
    output: Path | None,
    output_dir: Path | None,
    review_output: Path | None,
    mode: UserMode,
    missing_output: Path | None = None,
) -> None:
    if len(epub_paths) > 100:
        _print_expected_error(
            "Um lote aceita no máximo 100 EPUBs.",
            "Divida os arquivos em lotes de até 100 EPUBs.",
            mode,
        )
        raise typer.Exit(code=1)
    if len(epub_paths) > 1 and missing_output is not None:
        _print_expected_error(
            "Não é possível usar --missing-output com múltiplos EPUBs.",
            "Remova --missing-output para usar os arquivos automáticos de cada EPUB.",
            mode,
        )
        raise typer.Exit(code=1)
    if not epub_paths:
        _print_expected_error(
            "Nenhum EPUB informado para tradução.",
            "Informe pelo menos um arquivo .epub no comando translate.",
            mode,
        )
        raise typer.Exit(code=1)

    if output is not None and output_dir is not None:
        _print_expected_error(
            "Opções de saída incompatíveis.",
            "Use --output para um EPUB específico ou --output-dir para uma pasta de saída, não ambos.",
            mode,
        )
        raise typer.Exit(code=1)

    if len(epub_paths) == 1:
        return

    if output is not None:
        _print_expected_error(
            "Não é possível usar --output com múltiplos EPUBs.",
            "Use --output-dir para escolher a pasta do batch, ou traduza um EPUB por vez com --output.",
            mode,
        )
        raise typer.Exit(code=1)

    if review_output is not None:
        _print_expected_error(
            "Não é possível usar --review-output com múltiplos EPUBs.",
            "Gere arquivos de revisão traduzindo um EPUB por vez.",
            mode,
        )
        raise typer.Exit(code=1)


def _validate_cache_only_options(
    cache_only: bool,
    require_full_cache: bool,
    missing_output: Path | None,
    mode: UserMode,
) -> None:
    if cache_only:
        return

    if require_full_cache:
        _print_expected_error(
            "--require-full-cache exige --cache-only.",
            "Adicione --cache-only para reconstruir apenas com o cache, ou remova --require-full-cache.",
            mode,
        )
        raise typer.Exit(code=1)

    if missing_output is not None:
        _print_expected_error(
            "--missing-output exige --cache-only.",
            "Adicione --cache-only para listar os textos ausentes do cache, ou remova --missing-output.",
            mode,
        )
        raise typer.Exit(code=1)


def _build_translation_memory_options(
    enabled: bool,
    apply_threshold: float,
    suggest_threshold: float,
    mode: UserMode,
    conservative_auto_apply: bool = False,
    legacy_cache_compatibility: bool = False,
) -> TranslationMemoryOptions | None:
    if not enabled:
        if conservative_auto_apply or legacy_cache_compatibility:
            _print_expected_error("As opções de política de memória requerem --translation-memory.",
                                  "Adicione --translation-memory.", mode)
            raise typer.Exit(code=1)
        return None

    options = TranslationMemoryOptions(
        apply_threshold=apply_threshold,
        suggest_threshold=suggest_threshold,
        conservative_auto_apply=conservative_auto_apply,
        legacy_cache_compatibility=legacy_cache_compatibility,
    )
    try:
        options.validate()
    except TranslationMemoryError as exc:
        _print_expected_error(
            "Configuração inválida da memória de tradução.",
            "Use limiares entre 0 e 1, com a sugestão menor ou igual à aplicação.",
            mode,
            detail=str(exc),
        )
        raise typer.Exit(code=1) from exc
    return options


def _print_rich_memory_notices(
    translation_memory: TranslationMemoryOptions | None,
    config: AyvuConfig,
    translator_name: str,
    *,
    explicit_apply_threshold: bool,
) -> None:
    """With a model label, say how the memory flags behave; unlabeled runs print nothing."""
    if translation_memory is None or translation_modality(False, config.translator_models, translator_name) != "rich":
        return
    console.print("[dim]Memória no runtime rich/v2: o cache v1 não é usado como memória.[/dim]")
    if explicit_apply_threshold and not translation_memory.conservative_auto_apply:
        console.print("[yellow]--tm-apply-threshold só vale com --tm-conservative-auto-apply; "
                      "sem ela, a memória aproximada fica só como sugestão.[/yellow]")


def _validate_worker_options(
    workers: int,
    translation_memory: TranslationMemoryOptions | None,
    mode: UserMode,
) -> None:
    if workers < 1:
        _print_expected_error(
            "Quantidade de workers inválida.",
            "Use --workers com valor 1 ou maior.",
            mode,
            detail=f"--workers {workers}",
        )
        raise typer.Exit(code=1)

    if workers > 1 and translation_memory is not None:
        _print_expected_error(
            "--workers é incompatível com --translation-memory nesta versão.",
            "Use --workers 1 com --translation-memory, ou remova --translation-memory para executar em paralelo.",
            mode,
        )
        raise typer.Exit(code=1)


def _resolve_translation_output_dir(output_dir: Path | None, mode: UserMode) -> Path | None:
    if output_dir is None:
        return None

    path = output_dir.expanduser()
    if path.exists() and not path.is_dir():
        _print_expected_error(
            "A pasta de saída informada não é um diretório.",
            "Escolha um diretório em --output-dir ou remova a opção para usar o padrão.",
            mode,
            detail=f"Output directory: {path}",
        )
        raise typer.Exit(code=1)
    return path


def _resolve_review_output_path(
    review_output: Path | None,
    dry_run: bool,
    overwrite: bool,
    mode: UserMode,
) -> Path | None:
    if review_output is None:
        return None

    if dry_run:
        _print_expected_error(
            "Não é possível gerar arquivo de revisão em dry-run.",
            "Remova --dry-run para gerar traduções revisáveis, ou remova --review-output.",
            mode,
        )
        raise typer.Exit(code=1)

    path = review_output.expanduser()
    if path.exists() and not overwrite:
        _print_expected_error(
            "Arquivo de revisão já existe.",
            "Use --overwrite para substituí-lo ou escolha outro caminho em --review-output.",
            mode,
            detail="review_output_exists",
        )
        raise typer.Exit(code=1)

    return path
