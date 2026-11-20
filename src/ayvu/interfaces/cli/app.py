from __future__ import annotations
from .guided import menu
from . import preview as previews, settings, translate_options
from pathlib import Path
from typing import Optional
import typer
from .render import console
from .options import CONSENT_REMOTE_HELP, NETWORK_POLICY_HELP, _invocation_network_policy, _invocation_remote_consent
from .commands import cache, consent, content, resume, review, translate, translator
from .commands.resume import _offer_detected_translation_resume, _print_processing_translation_states
from ...composition.cli import CliSurface, installed_cli_surface, open_job_recovery
from ...kernel import UserMode
from ...network import NetworkPolicy


def main(
    ctx: typer.Context,
    network_policy: Optional[NetworkPolicy] = typer.Option(None, "--network-policy", help=NETWORK_POLICY_HELP),
    consent_remote: bool = typer.Option(False, "--consent-remote", help=CONSENT_REMOTE_HELP),
    mode: Optional[UserMode] = typer.Option(
        None,
        "--mode",
        help="Execution mode (common or developer). If not specified, it's inferred from usage.",
    ),
    translator_name: Optional[str] = typer.Option(None, "--translator", help="Preview translator: libretranslate or ollama."),
    model: Optional[str] = typer.Option(None, "--model", help="Preview model or declared label."),
    url: Optional[str] = typer.Option(None, "--url", help="Preview provider URL."),
    preview: Optional[Path] = typer.Option(
        None,
        "--preview",
        help="Generate a translated EPUB preview with default settings.",
    ),
) -> None:
    """Translate local EPUB files with a local HTTP translator."""
    if mode is None:
        mode = UserMode.DEVELOPER
        # COMMON mode is only when running 'ayvu' without subcommands or options (except for help)
        # Typer sets invoked_subcommand when a subcommand is used.
        # ctx.params contains options of the callback itself.
        if ctx.invoked_subcommand is None and not any(v for k, v in ctx.params.items() if k not in {"mode", "network_policy", "consent_remote"}):
            mode = UserMode.COMMON
    surface = ctx.obj
    ctx.ensure_object(dict)
    ctx.obj["mode"] = mode
    ctx.obj["network_policy"] = network_policy
    network_token = _invocation_network_policy.set(network_policy)
    ctx.call_on_close(lambda: _invocation_network_policy.reset(network_token))
    consent_token = _invocation_remote_consent.set(True if consent_remote else False if mode == UserMode.DEVELOPER else None)
    ctx.call_on_close(lambda: _invocation_remote_consent.reset(consent_token))

    if ctx.invoked_subcommand is not None:
        return

    if preview is not None:
        config = settings._load_existing_config_or_default()
        selected = settings._select_translator(config, mode, translator=translator_name, model=model, url=url)
        previews._run_preview(preview, mode=mode, config=selected.config, translator_name=selected.translator, url=selected.url)
        return

    config = settings._load_existing_config_or_default()
    recovery = open_job_recovery(settings._processing_dir(config))
    found = _print_processing_translation_states(recovery)
    if _offer_detected_translation_resume(found, recovery, mode=mode):
        return

    if menu._run_guided_main_flow(ctx, mode=mode, surface=surface):
        return

    console.print(ctx.get_help())


def create_app(surface: CliSurface) -> typer.Typer:
    """Bind a resolved surface to independent command registrations and invocation state."""
    # The immutable initial object is replaced by main's fresh invocation dictionary.
    application = typer.Typer(
        help="Translate local EPUB files with a local HTTP translator.",
        context_settings={"obj": surface},
    )
    cache_app = typer.Typer(help="Inspect, clean, export, and import the translation cache.")
    consent_app = typer.Typer(help="Manage the consent recorded for remote translation providers.")
    application.callback(invoke_without_command=True)(main)
    bindings = {
        ("inspect",): content.inspect,
        ("test-translator",): translator.test_translator,
        ("languages",): translator.languages,
        ("cache", "inspect"): cache.cache_inspect,
        ("cache", "clean"): cache.cache_clean,
        ("cache", "export"): cache.cache_export,
        ("cache", "import"): cache.cache_import,
        ("translate",): translate.translate,
        ("resume",): resume.resume,
        ("extract",): content.extract,
        ("apply-review",): review.apply_review,
        ("consent", "revoke"): consent.revoke,
    }
    for path, handler in bindings.items():
        if path in surface.commands:
            target = {"cache": cache_app, "consent": consent_app}.get(path[0], application)
            target.command(path[-1])(handler)
    if cache_app.registered_commands:
        application.add_typer(cache_app, name="cache")
    if consent_app.registered_commands:
        application.add_typer(consent_app, name="consent")
    return application


app = create_app(installed_cli_surface())


if __name__ == "__main__":
    app()
