"""Rich output shared by the CLI commands: the console, error messages and the failure texts users see."""
from __future__ import annotations

from rich.console import Console
from rich.table import Table
from ...kernel import UserMode
from ...providers import DiscoveredLanguage


console = Console()
_RESUME_RACED = (
    "Esta tradução mudou enquanto a retomada começava.",
    "Rode 'ayvu resume' de novo para ver como ela está agora. Nada foi alterado.",
)


_BOOK_PROTECTED = (
    "Este EPUB tem conteúdo protegido ou criptografado.",
    "O Ayvu não traduz nem remove essa proteção. Escolha um EPUB sem proteção. O original não foi alterado.",
)


_BOOK_UNSAFE = (
    "Este EPUB tem uma estrutura interna insegura, e o Ayvu preferiu não abri-lo.",
    "Nomes de arquivo inseguros, arquivos repetidos ou declarações de entidade XML não são aceitos. "
    "Tente outra cópia do livro. O original não foi alterado.",
)


_BOOK_TOO_BIG = (
    "Este EPUB passa dos limites de tamanho ou de compressão aceitos.",
    "Isso protege contra arquivos que se expandem demais ao abrir. Tente outra cópia do livro. O original não foi alterado.",
)


_BOOK_BROKEN = (
    "Este EPUB está danificado ou incompleto.",
    "Abra o livro em um leitor para conferir e tente outra cópia. O original não foi alterado.",
)


_TRANSLATION_FAILURES: dict[str, tuple[str, str]] = {
    "runtime_mismatch": (
        "Um job rich/v2 só pode ser retomado com um rótulo de modelo.",
        "Declare um rótulo em Configurações (opção 5) ou em translator_models e repita.",
    ),
    "rich_translation_failed": (
        "A tradução rich/v2 parou; nenhum EPUB parcial foi publicado.",
        "Veja o código com --mode developer e use ayvu resume após corrigir a falha.",
    ),
    "provider_server_error": (
        "O tradutor falhou durante a tradução rich/v2; nenhum EPUB parcial foi publicado.",
        "Verifique o tradutor e use ayvu resume. Os segmentos concluídos permanecem no cache v2.",
    ),
    **dict.fromkeys(("encrypted_content", "encrypted_member", "malformed_encryption", "protected_member"), _BOOK_PROTECTED),
    **dict.fromkeys(
        ("unsafe_member_name", "unsafe_manifest_href", "member_name_too_long", "duplicate_member", "entity_declaration"),
        _BOOK_UNSAFE,
    ),
    **dict.fromkeys(
        ("suspicious_compression", "archive_too_large", "member_too_large", "document_too_large", "source_too_large", "too_many_members"),
        _BOOK_TOO_BIG,
    ),
    **dict.fromkeys(
        (
            "corrupt_archive",
            "corrupt_member",
            "unsupported_compression",
            "missing_container",
            "missing_member",
            "missing_package",
            "malformed_container",
            "malformed_package",
            "manifest_member_missing",
            "source_not_regular_file",
        ),
        _BOOK_BROKEN,
    ),
    "invalid_source_text": (
        "Um texto deste EPUB não pôde ser preparado para tradução.",
        "Ele tem caracteres que o Ayvu não aceita, como caracteres de controle. Nada foi publicado e o original não foi alterado.",
    ),
    "source_unreadable": (
        "Não foi possível ler o EPUB informado.",
        "Confirme que o arquivo existe e pode ser lido, e repita.",
    ),
    "source_changed": (
        "O EPUB mudou enquanto a tradução começava.",
        "Repita o comando com o arquivo já final. O que foi traduzido continua no cache.",
    ),
    "language_pair_mismatch": (
        "Os idiomas pedidos não são os do projeto local.",
        "Repita o comando. Se continuar, informe --source e --target explicitamente.",
    ),
    "no_chapters_matched": (
        "Nenhum capítulo corresponde à seleção.",
        "Veja os capítulos com 'ayvu inspect' e ajuste --chapters.",
    ),
    "nothing_to_materialize": (
        "Este EPUB não tem nada que o Ayvu possa traduzir.",
        "Confira o livro com 'ayvu inspect'. Nada foi publicado.",
    ),
    "job_in_progress": (
        "Esta mesma tradução já está em andamento em outro processo.",
        "Espere ela terminar. Para rodar ao mesmo tempo, use outra saída com --output.",
    ),
    "stale_revision": _RESUME_RACED,
    "continuation_exists": _RESUME_RACED,
    "job_not_resumable": _RESUME_RACED,
    "job_store_unavailable": (
        "Não foi possível registrar o trabalho no projeto local.",
        "Verifique as permissões e o espaço da pasta de processamento e repita. Nada foi traduzido.",
    ),
    "run_guard_unavailable": (
        "Não foi possível registrar o trabalho no projeto local.",
        "Verifique as permissões e o espaço da pasta de processamento e repita. Nada foi traduzido.",
    ),
    "lease_expired": (
        "A tradução ficou parada por mais de uma hora e o trabalho perdeu a validade.",
        "Isso costuma ser o computador suspenso. Nada foi publicado. Repita o comando: o que já foi traduzido está no cache.",
    ),
    "output_exists": (
        "Apareceu um arquivo no destino durante a tradução, e ele não foi alterado.",
        "Escolha outra saída com --output, ou use --overwrite para substituir {output}. O que foi traduzido está no cache.",
    ),
    "output_is_source": (
        "A saída é o próprio EPUB original, que o Ayvu nunca altera.",
        "Escolha outra saída com --output.",
    ),
    "provider_failed": (
        "O tradutor falhou e a tradução parou, como pedido por --fail-fast.",
        "Verifique o tradutor local e repita. O que já foi traduzido está no cache.",
    ),
    "text_failed": (
        "Um texto falhou e a tradução parou, como pedido por --fail-fast.",
        "Veja o detalhe com --mode developer e repita. O que já foi traduzido está no cache.",
    ),
    "publication_uncommitted": (
        "O EPUB foi publicado, mas o projeto local não conseguiu registrar isso.",
        "O arquivo está em {output}. Confira o livro antes de usar. Repetir com --overwrite gera um registro novo.",
    ),
    "job_record_failed": (
        "O projeto local não conseguiu registrar o fim deste trabalho.",
        "Verifique se {output} existe e confira o livro. Verifique as permissões e o espaço da pasta de processamento.",
    ),
    "provenance_failed": (
        "O EPUB foi gerado, mas o histórico dele não pôde ser gravado no projeto local.",
        "O livro está em {output}. Verifique as permissões e o espaço da pasta de processamento.",
    ),
    "unexpected_error": (
        "Não foi possível concluir a tradução deste EPUB.",
        "Veja o detalhe com --mode developer e repita. O que já foi traduzido está no cache e o original não foi alterado.",
    ),
}


def _print_network_selection(decision) -> None:
    if decision is not None:
        origin = "padrão de compatibilidade" if decision.defaulted else "seleção explícita"
        console.print(f"[dim]Política HTTP: {decision.policy.value} ({origin}); somente o HTTP do Ayvu está coberto.[/dim]")


def _print_expected_error(summary: str, next_step: str, mode: UserMode, detail: str = "") -> None:
    console.print(f"[red]{summary}[/red]")
    if mode == UserMode.DEVELOPER and detail:
        console.print(f"[dim]Detalhe técnico: {detail}[/dim]")
    console.print(next_step)


def _print_epub_read_error(detail: str, mode: UserMode) -> None:
    _print_expected_error(
        "Não foi possível ler o EPUB informado.",
        "Confirme que o arquivo é um EPUB válido e legível e tente novamente.",
        mode,
        detail=detail,
    )


_CONTENT_NAME_FAILURES = frozenset({"unsafe_output_name", "duplicate_output_target"})


def _print_content_failure(code: str, mode: UserMode) -> None:
    """Says which side failed: the book, a chapter's name or the chosen output."""
    if code in _CONTENT_NAME_FAILURES:
        _print_expected_error(
            "Um capítulo deste EPUB tem um nome que não pode virar arquivo.",
            "Nada foi gravado. O original não foi alterado.",
            mode,
            detail=code,
        )
    elif code.startswith("output_") or code in {"invalid_output_directory", "source_alias", "source_alias_unknown"}:
        _print_expected_error(
            "Não foi possível gravar na pasta de saída.",
            "Confirme que --output é uma pasta gravável, diferente do livro, e tente novamente. O original não foi alterado.",
            mode,
            detail=code,
        )
    elif code == "source_changed":
        _print_expected_error(
            "O EPUB mudou enquanto era lido.",
            "Espere o arquivo terminar de ser alterado e tente novamente.",
            mode,
            detail=code,
        )
    elif _TRANSLATION_FAILURES.get(code) is _BOOK_PROTECTED:
        # A protected book is not a broken one, so "confirm that it is valid" would send the person the wrong way.
        message, hint = _BOOK_PROTECTED
        _print_expected_error(message, hint, mode, detail=code)
    else:
        _print_epub_read_error(code, mode)


def _print_languages(languages: tuple[DiscoveredLanguage, ...], title: str = "LibreTranslate languages") -> None:
    table = Table(title=title)
    table.add_column("Language")
    table.add_column("Code")
    table.add_column("State")
    table.add_column("Targets")

    for language in languages:
        table.add_row(
            language.name,
            language.code,
            language.state,
            _display_language_targets(language.targets),
        )
    console.print(table)


def _display_language_targets(targets: tuple[str, ...]) -> str:
    if not targets:
        return "-"
    if len(targets) <= 8:
        return ", ".join(targets)
    first_targets = ", ".join(targets[:8])
    return f"{first_targets} (+{len(targets) - 8})"
