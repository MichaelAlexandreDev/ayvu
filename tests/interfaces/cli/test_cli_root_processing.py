from tests.support.cli_rich_runtime_support import declare_model
import json
from ayvu.interfaces.cli.app import app
from ayvu.translation.preflight import PreflightError
from ayvu.recovery.checkpoint import ResumeStateStore
from tests.support.cli_support import _resume_state, isolated_config, runner


def test_root_command_shows_processing_translation_state(tmp_path, monkeypatch):
    processing_dir = tmp_path / "Processando"
    state = _resume_state(tmp_path)
    ResumeStateStore(processing_dir).save(state)
    monkeypatch.setattr("ayvu.interfaces.cli.settings.default_processing_dir", lambda: processing_dir)

    result = runner.invoke(app, [], input="n\n0\n")

    assert result.exit_code == 0
    assert "Translations in progress were found." in result.output
    assert "Processing translations" in result.output
    assert "book.epub" in result.output
    assert "book-pt.epub" in result.output
    assert "cache.sqlite" in result.output
    assert "Execution" in result.output
    assert "1 (sequential)" in result.output
    assert "Continue detected translation?" in result.output
    assert "Detected translation was not resumed." in result.output
    assert "Choose an option" in result.output
    assert "Generate preview" in result.output
    assert "Canceled." in result.output


def test_root_command_uses_configured_processing_dir(isolated_config, tmp_path):
    books_dir = tmp_path / "Biblioteca"
    processing_dir = books_dir / "Em-Andamento"
    state = _resume_state(tmp_path)
    ResumeStateStore(processing_dir).save(state)
    isolated_config.write_text(
        json.dumps(
            {
                "version": 1,
                "default_target_language": "pt",
                "books_dir": str(books_dir),
                "folders": {"processing": "Em-Andamento"},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    result = runner.invoke(app, [], input="n\n0\n")

    assert result.exit_code == 0
    assert "Translations in progress were found." in result.output
    assert "Processing translations" in result.output
    assert "book.epub" in result.output
    assert "Detected translation was not resumed." in result.output
    assert "Canceled." in result.output


def test_root_command_in_developer_mode_skips_guided_prompts(tmp_path, monkeypatch):
    epub_path = tmp_path / "book.epub"
    epub_path.write_bytes(b"fake")
    monkeypatch.setattr("ayvu.interfaces.cli.settings.default_processing_dir", lambda: tmp_path / "missing")

    result = runner.invoke(app, ["--mode", "developer", "--preview", str(epub_path)])

    assert "Generate a translation preview?" not in result.output
    assert "Não foi possível ler o EPUB informado." in result.output


def test_common_mode_hides_technical_detail_for_expected_error(tmp_path, monkeypatch, isolated_config):
    declare_model(isolated_config)
    epub_path = tmp_path / "book.epub"
    epub_path.write_bytes(b"fake epub")
    monkeypatch.setattr("ayvu.interfaces.cli.output.default_translated_books_dir", lambda: tmp_path / "Traduzidos")
    monkeypatch.setattr(
        "ayvu.interfaces.cli.run.run_translation_preflight",
        lambda **_kwargs: (_ for _ in ()).throw(
            PreflightError(
                "Não foi possível ler o EPUB informado.",
                "Confirme que o arquivo é um EPUB válido e legível e tente novamente.",
                detail="book.epub: invalid zip header",
            )
        ),
    )

    result = runner.invoke(app, ["--mode", "common", "translate", str(epub_path)], input="y\n")

    assert result.exit_code == 1
    assert "Não foi possível ler o EPUB informado." in result.output
    assert "Confirme que o arquivo é um EPUB válido e legível e tente novamente." in result.output
    assert "Detalhe técnico:" not in result.output
    assert "invalid zip header" not in result.output
    assert "Traceback" not in result.output


def test_developer_mode_shows_technical_detail_for_expected_error(tmp_path, monkeypatch, isolated_config):
    declare_model(isolated_config)
    epub_path = tmp_path / "book.epub"
    epub_path.write_bytes(b"fake epub")
    monkeypatch.setattr("ayvu.interfaces.cli.output.default_translated_books_dir", lambda: tmp_path / "Traduzidos")
    monkeypatch.setattr(
        "ayvu.interfaces.cli.run.run_translation_preflight",
        lambda **_kwargs: (_ for _ in ()).throw(
            PreflightError(
                "Não foi possível ler o EPUB informado.",
                "Confirme que o arquivo é um EPUB válido e legível e tente novamente.",
                detail="book.epub: invalid zip header",
            )
        ),
    )

    result = runner.invoke(app, ["--mode", "developer", "translate", str(epub_path)])

    assert result.exit_code == 1
    assert "Não foi possível ler o EPUB informado." in result.output
    assert "Detalhe técnico:" in result.output
    assert "invalid zip header" in result.output
    assert "Traceback" not in result.output
