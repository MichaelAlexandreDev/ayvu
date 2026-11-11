from __future__ import annotations
from .guided import menu
from . import preview as previews, settings
from pathlib import Path
from typing import Optional
import typer
from .render import console
from .options import NETWORK_POLICY_HELP, _invocation_network_policy
from .commands import cache, content, resume, review, translate, translator
from .commands.resume import _offer_detected_translation_resume, _print_processing_translation_states
from ...composition.cli import open_job_recovery
from ...kernel import UserMode
from ...network import NetworkPolicy


app = typer.Typer(help="Translate local EPUB files with a local HTTP translator.")


cache_app = typer.Typer(help="Inspect, clean, export, and import the translation cache.")


app.add_typer(cache_app, name="cache")


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    network_policy: Optional[NetworkPolicy] = typer.Option(None, "--network-policy", help=NETWORK_POLICY_HELP),
    mode: Optional[UserMode] = typer.Option(
        None,
        "--mode",
        help="Execution mode (common or developer). If not specified, it's inferred from usage.",
    ),
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
        if ctx.invoked_subcommand is None and not any(v for k, v in ctx.params.items() if k not in {"mode", "network_policy"}):
            mode = UserMode.COMMON
    ctx.ensure_object(dict)
    ctx.obj["mode"] = mode
    ctx.obj["network_policy"] = network_policy
    network_token = _invocation_network_policy.set(network_policy)
    ctx.call_on_close(lambda: _invocation_network_policy.reset(network_token))

    if ctx.invoked_subcommand is not None:
        return

    if preview is not None:
        previews._run_preview(preview, mode=mode)
        return

    config = settings._load_existing_config_or_default()
    recovery = open_job_recovery(settings._processing_dir(config))
    found = _print_processing_translation_states(recovery)
    if _offer_detected_translation_resume(found, recovery, mode=mode):
        return

    if menu._run_guided_main_flow(ctx, mode=mode):
        return

    console.print(ctx.get_help())


app.command()(content.inspect)


app.command('test-translator')(translator.test_translator)


app.command('languages')(translator.languages)


cache_app.command('inspect')(cache.cache_inspect)


cache_app.command('clean')(cache.cache_clean)


cache_app.command('export')(cache.cache_export)


cache_app.command('import')(cache.cache_import)


app.command()(translate.translate)


app.command()(resume.resume)


app.command()(content.extract)


app.command('apply-review')(review.apply_review)


if __name__ == "__main__":
    app()
