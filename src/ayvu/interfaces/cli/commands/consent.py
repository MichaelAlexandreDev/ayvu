"""consent revoke: remove the consent a project recorded for remote translation (#379)."""
from __future__ import annotations

from typing import Optional

import typer

from ....composition.cli import revoke_remote_consent
from ....kernel import UserMode
from ....projects import ProjectError
from .. import settings
from ..render import _print_expected_error, console


def revoke(
    ctx: typer.Context,
    target: str = typer.Argument(..., help="The EPUB whose projects to revoke, or one project id."),
    translator_name: Optional[str] = typer.Option(None, "--translator",
                                                  help="Revoke only this remote provider's consent."),
) -> None:
    """Revoke the recorded consent to send a book's text to a remote provider. The next remote run asks again."""
    mode = ctx.obj.get("mode", UserMode.DEVELOPER)
    provider = translator_name.strip().lower() if translator_name is not None else None
    try:
        result = revoke_remote_consent(settings._processing_dir(settings._load_existing_config_or_default()),
                                       target, provider)
    except ProjectError as error:
        _print_expected_error("No project was found for this EPUB or project id.",
                              "Pass the EPUB you translated, or a project id from the processing folder.",
                              mode, detail=str(error))
        raise typer.Exit(code=1) from None
    if result.removed:
        console.print(f"[green]Consent revoked:[/green] {result.removed} record(s) in {result.projects} project(s). "
                      "The next remote translation asks again.")
    else:
        console.print(f"No remote consent was recorded ({result.projects} project(s) checked). Nothing changed.")
