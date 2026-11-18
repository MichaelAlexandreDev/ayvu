"""Resume saved translations from the command line and the guided flow."""
from __future__ import annotations

from .. import plan, run as translation_run, run_messages, settings, translate_options
from dataclasses import replace
from pathlib import Path
from typing import Optional
import typer
from rich.table import Table
from .. import report as reports
from ..contracts import TranslationResumeState, InvalidResumeState, InterruptedTranslations, JobRecovery
from ....composition.cli import _BookOutcome, book_refusal, interrupted_translations, open_job_recovery
from ....kernel import UserMode
from ....recovery import RequiredDecision, ResumeRequest, ResumeResult, ResumeStatus
from ....network import NetworkPolicy
from ....translation import ContinuationTicket, TranslateProjectResult

from ..render import _print_expected_error, console
from ..options import NETWORK_POLICY_HELP, _selected_network_policy


def resume(
    ctx: typer.Context,
    network_policy: Optional[NetworkPolicy] = typer.Option(None, "--network-policy", help=NETWORK_POLICY_HELP),
    epub_path: Optional[Path] = typer.Argument(
        None,
        help="EPUB to resume. If omitted, resumes the only translation in progress.",
    ),
    target: Optional[str] = typer.Option(
        None,
        "--target",
        help="Target language, to choose when several translations are in progress.",
    ),
) -> None:
    """Resume a translation in progress from its saved checkpoint."""
    mode = ctx.obj.get("mode", UserMode.DEVELOPER)
    config = settings._load_existing_config_or_default()
    recovery = open_job_recovery(settings._processing_dir(config))
    found = interrupted_translations(recovery)

    state = _select_resume_state(found.running, epub_path, target, mode=mode)
    if state is None:
        raise typer.Exit(code=1)

    console.print(
        f"[green]Resuming translation:[/green] {state.input_path.name} -> {state.output_path.name}"
    )
    _print_resume_checkpoint(state)
    _resume_translation(state, found, recovery, mode=mode, network_policy=_selected_network_policy(network_policy))


def _print_processing_translation_states(recovery: JobRecovery) -> InterruptedTranslations:
    found = interrupted_translations(recovery)
    if found.running:
        _print_running_resume_states(found.running)
    if found.invalid:
        _print_invalid_resume_states(found.invalid)
    return found


def _offer_detected_translation_resume(found: InterruptedTranslations, recovery: JobRecovery, mode: UserMode) -> bool:
    states = found.running
    if not states:
        return False
    # In DEVELOPER mode, we don't resume automatically to avoid unexpected behavior.
    # The user should probably use a resume-specific command if we had one,
    # or just let the cache handle it.
    if mode == UserMode.DEVELOPER:
        return False

    if len(states) > 1:
        console.print("Multiple translations are in progress; automatic selection is not available yet.")
        return False

    state = states[0]
    _print_resume_checkpoint(state)
    if not typer.confirm("Continue detected translation?", default=False):
        console.print("Detected translation was not resumed. Processing files were left unchanged.")
        return False

    console.print(f"[green]Resuming translation:[/green] {state.input_path.name} -> {state.output_path.name}")
    _resume_translation(state, found, recovery, mode=mode)
    return True


class _ResumeRunner:
    """Runs a resume with what its file recorded, on whichever path the use case chose."""

    def __init__(self, state: TranslationResumeState, mode: UserMode, network_policy: NetworkPolicy | None = None) -> None:
        self._state = state
        self._mode = mode
        self._network_policy = _selected_network_policy(network_policy)

    def run_legacy(self, reference: str) -> None:
        self._run(resolve_managed_project=False)

    def run_durable(self, reference: str, ticket: ContinuationTicket, operation_id: str) -> TranslateProjectResult:
        return self._run(continuation=ticket, operation_id=operation_id).job_result

    def _run(self, **identity: object) -> translation_run.TranslationRunResult:
        state, mode = self._state, self._mode
        config = settings._load_existing_config_or_default()
        if state.execution_revision == 1:
            # A v1 job finishes on v1, whatever label the configuration now declares; no new run reaches v1.
            config = replace(config, translator_models={})
            console.print("[yellow]Old job:[/yellow] this job started on Ayvu's previous runtime. It can be finished now; "
                          "once that runtime is removed, finish it with the frozen release v1-runtime-final "
                          "or translate the book again.")
        return translation_run._run_translation(
            epub_path=state.input_path,
            output=state.output_path,
            review_output=None,
            source=state.source,
            target=state.target,
            translator_name=state.translator_name,
            url=state.url,
            network_policy=self._network_policy,
            cache_path=state.cache_path,
            glossary_path=state.glossary_path,
            profile_name=None,
            performance_profile_name=None,
            dry_run=False,
            fail_fast=state.fail_fast,
            chapter_selection=translate_options._parse_chapter_selection(state.chapter_selection, mode=mode),
            overwrite=True,
            timeout=state.timeout,
            retries=state.retries,
            requests_per_second=state.requests_per_second,
            retry_backoff=state.retry_backoff,
            retry_backoff_max=state.retry_backoff_max,
            chunk_limit=state.chunk_limit,
            workers=state.workers,
            mode=mode,
            translate_metadata=state.translate_metadata,
            translate_alt_text=state.translate_alt_text,
            translation_memory=state.translation_memory_options(),
            project_profile_name=state.profile_name,
            config=config,
            **identity,
        )


def _resume_translation(state: TranslationResumeState, found: InterruptedTranslations, recovery: JobRecovery, mode: UserMode,
                        network_policy: NetworkPolicy | None = None) -> None:
    entry = found.entry_of(state)
    # Asking for this resume is the decision: the command says what it does, and it has always replaced its output.
    request = ResumeRequest(entry.reference, entry.job_id, entry.project_id, entry.expected_revision, tuple(RequiredDecision))
    try:
        result = recovery.request_resume.execute(request, _ResumeRunner(state, mode, network_policy))
        if result.status in (ResumeStatus.REFUSED, ResumeStatus.ALREADY_REQUESTED):
            _print_resume_refusal(result, state, mode)
            raise typer.Exit(code=1)
    except typer.Exit:
        console.print(
            "Não foi possível retomar a tradução detectada. Verifique a mensagem acima e reinicie a tradução se necessário."
        )
        raise


def _print_resume_refusal(result: ResumeResult, state: TranslationResumeState, mode: UserMode) -> None:
    code = "continuation_exists" if result.status is ResumeStatus.ALREADY_REQUESTED else result.code
    if code in ("source_changed", "source_missing", "source_unreadable"):
        run_messages._print_project_failure(code, mode)
    elif code == "not_found":
        _print_expected_error(
            "No translation in progress was found to resume.",
            "Start a translation with 'ayvu translate <epub>' or check the processing folder.",
            mode,
        )
    else:
        failure = (book_refusal(state.input_path) or "unexpected_error") if code == "policy_denied" else code
        run_messages._print_translation_failure(_BookOutcome(failure=failure), state.output_path, mode)


def _select_resume_state(
    running: tuple[TranslationResumeState, ...],
    epub_path: Path | None,
    target: str | None,
    mode: UserMode,
) -> TranslationResumeState | None:
    candidates = list(running)
    if epub_path is not None:
        resolved = epub_path.expanduser().resolve()
        candidates = [state for state in candidates if state.input_path == resolved]
    if target is not None:
        candidates = [state for state in candidates if state.target == target]

    if not candidates:
        _print_expected_error(
            "No translation in progress was found to resume.",
            "Start a translation with 'ayvu translate <epub>' or check the processing folder.",
            mode,
        )
        return None

    if len(candidates) == 1:
        return candidates[0]

    _print_running_resume_states(tuple(candidates))
    _print_expected_error(
        "Several translations in progress match the request.",
        "Specify the EPUB and --target to choose which one to resume.",
        mode,
    )
    return None


def _print_resume_checkpoint(state: TranslationResumeState) -> None:
    table = Table(title="Resume checkpoint")
    table.add_column("Metric")
    table.add_column("Value")
    if _has_checkpoint_progress(state):
        table.add_row("Chapters completed", _checkpoint_chapter_value(state))
        if state.current_chapter:
            table.add_row("Current chapter", state.current_chapter)
        if state.failed_chapters:
            table.add_row("Chapters with failures", str(len(state.failed_chapters)))
            table.add_row("Failed segments", str(state.failed_segment_count))
    for label, value in plan._execution_control_rows(
        workers=state.workers,
        requests_per_second=state.requests_per_second,
        retries=state.retries,
        retry_backoff=state.retry_backoff,
        retry_backoff_max=state.retry_backoff_max,
    ):
        table.add_row(label, value)
    if state.execution_revision == 1:
        table.add_row("Runtime", "previous (v1)")
    table.add_row("Last update", state.updated_at)
    console.print(table)
    console.print(
        "Saved execution settings and cached translations are reused; only segments still missing from the cache are retried."
    )


def _has_checkpoint_progress(state: TranslationResumeState) -> bool:
    return bool(
        state.completed_chapters
        or state.failed_chapters
        or state.current_chapter
        or state.total_chapters
    )


def _checkpoint_chapter_value(state: TranslationResumeState) -> str:
    done = len(state.completed_chapters)
    if state.total_chapters is None:
        return str(done)
    return f"{done}/{state.total_chapters}"


def _print_running_resume_states(states: tuple[TranslationResumeState, ...]) -> None:
    console.print("[yellow]Translations in progress were found.[/yellow]")
    table = Table(title="Processing translations")
    table.add_column("Original EPUB")
    table.add_column("Output")
    table.add_column("Target")
    table.add_column("Cache")
    table.add_column("Execution")
    for state in states:
        table.add_row(
            state.input_path.name,
            state.output_path.name,
            state.target,
            state.cache_path.name,
            plan._format_execution_summary(state),
        )
    console.print(table)


def _print_invalid_resume_states(states: tuple[InvalidResumeState, ...]) -> None:
    console.print("[yellow]Invalid processing state files were found.[/yellow]")
    table = Table(title="Invalid processing states")
    table.add_column("State file")
    table.add_column("Problem")
    for state in states:
        table.add_row(state.path.name, reports._single_line(state.message))
    console.print(table)
    console.print("Restart the translation if the state file cannot be fixed.")
