from pathlib import Path
from ayvu.kernel import LanguagePair, OutputPlan
from tests.support.cli_support import isolated_config


def test_output_plan_keeps_explicit_output():
    output = Path("traduzidos/livro-final.epub")
    language_pair = LanguagePair(source="en", target="pt")

    plan = OutputPlan.for_translation(Path("livro.epub"), output, language_pair)

    assert plan.path == output
    assert plan.explicit_output


def test_output_plan_uses_target_suffix_in_default_output_dir():
    language_pair = LanguagePair(source="en", target="pt-BR")
    default_dir = Path("Documentos/Livros/Traduzidos")

    plan = OutputPlan.for_translation(
        Path("books/livro.epub"),
        None,
        language_pair,
        default_dir=default_dir,
    )

    assert plan.path == Path("Documentos/Livros/Traduzidos/livro-pt-BR.epub")
    assert not plan.explicit_output


def test_output_plan_uses_translated_suffix_when_target_is_blank():
    language_pair = LanguagePair(source="en", target=" ")
    default_dir = Path("Documentos/Livros/Traduzidos")

    plan = OutputPlan.for_translation(
        Path("books/livro.epub"),
        None,
        language_pair,
        default_dir=default_dir,
    )

    assert plan.path == Path("Documentos/Livros/Traduzidos/livro-translated.epub")


def test_output_plan_dry_run_does_not_block_existing_output(tmp_path):
    output = tmp_path / "livro-pt.epub"
    output.write_text("already here", encoding="utf-8")

    plan = OutputPlan(path=output, dry_run=True)

    assert not plan.blocks_existing_file(overwrite=False)


def test_output_plan_uses_preview_suffix_in_default_preview_dir():
    default_dir = Path("Documentos/Livros/Preview")

    plan = OutputPlan.for_preview(Path("books/livro.epub"), default_dir=default_dir)

    assert plan.path == Path("Documentos/Livros/Preview/livro-preview.epub")
    assert not plan.explicit_output
