"""Tests for the extracted translate command."""
from tests.support.cli_rich_runtime_support import declare_model
from ayvu.interfaces.cli.app import app
from ayvu.translation.preflight import PreflightError
from tests.support.cli_support import runner
from tests.support.cli_support import isolated_config


def test_translate_command_has_clear_error_for_unknown_translator(tmp_path):
    epub_path = tmp_path / "book.epub"
    epub_path.write_bytes(b"not a real epub")

    result = runner.invoke(app, ["translate", str(epub_path), "--translator", "unknown", "--dry-run"])

    assert result.exit_code == 1
    assert "Não foi possível preparar o tradutor." in result.output
    assert "Detalhe técnico:" in result.output
    assert "Unsupported translator:" in result.output
    assert "unknown" in result.output
    assert "Use --translator libretranslate." in result.output
    assert "Traceback" not in result.output


def test_translate_command_in_developer_mode_skips_confirmations(tmp_path, monkeypatch, isolated_config):
    declare_model(isolated_config)
    epub_path = tmp_path / "book.epub"
    epub_path.write_bytes(b"fake")
    monkeypatch.setattr("ayvu.interfaces.cli.output.default_translated_books_dir", lambda: tmp_path / "Traduzidos")

    monkeypatch.setattr(
        "ayvu.interfaces.cli.run.run_translation_preflight",
        lambda **_kwargs: (_ for _ in ()).throw(PreflightError("failed", "next")),
    )

    result = runner.invoke(app, ["translate", str(epub_path)])

    assert result.exit_code == 1
    assert "Default output folder:" not in result.output
    assert "Keep this output location?" not in result.output
    assert "failed" in result.output
    assert "next" in result.output


def test_translate_command_require_full_cache_requires_cache_only(tmp_path):
    epub_path = tmp_path / "book.epub"
    epub_path.write_bytes(b"fake epub")

    result = runner.invoke(app, ["translate", str(epub_path), "--require-full-cache"])

    assert result.exit_code == 1
    assert "--require-full-cache exige --cache-only" in result.output


def test_translate_command_missing_output_requires_cache_only(tmp_path):
    epub_path = tmp_path / "book.epub"
    epub_path.write_bytes(b"fake epub")

    result = runner.invoke(
        app, ["translate", str(epub_path), "--missing-output", str(tmp_path / "missing.txt")]
    )

    assert result.exit_code == 1
    assert "--missing-output exige --cache-only" in result.output


def test_translate_command_reports_unknown_performance_profile_without_traceback(
    minimal_epub_path,
    monkeypatch,
):
    called = False

    def fail_preflight(**_kwargs: object) -> object:
        nonlocal called
        called = True
        raise AssertionError("preflight should not run for an unknown performance profile")

    monkeypatch.setattr("ayvu.interfaces.cli.run.run_translation_preflight", fail_preflight)

    result = runner.invoke(
        app,
        [
            "--mode",
            "developer",
            "translate",
            str(minimal_epub_path),
            "--performance-profile",
            "turbo",
        ],
    )

    assert result.exit_code == 1
    assert "Perfil de performance não encontrado." in result.output
    assert "--performance-profile" in result.output
    assert "Traceback" not in result.output
    assert not called


def test_translate_command_rejects_invalid_workers(minimal_epub_path, monkeypatch):
    def fail_preflight(**_kwargs: object) -> object:
        raise AssertionError("preflight should not run with invalid workers")

    monkeypatch.setattr("ayvu.interfaces.cli.run.run_translation_preflight", fail_preflight)

    result = runner.invoke(
        app,
        ["--mode", "developer", "translate", str(minimal_epub_path), "--workers", "0"],
    )

    assert result.exit_code == 1
    assert "Quantidade de workers inválida." in result.output
    assert "Use --workers com valor 1 ou maior." in result.output
    assert "Traceback" not in result.output


def test_translate_command_rejects_parallel_workers_with_translation_memory(
    minimal_epub_path,
    monkeypatch,
):
    def fail_preflight(**_kwargs: object) -> object:
        raise AssertionError("preflight should not run with incompatible worker options")

    monkeypatch.setattr("ayvu.interfaces.cli.run.run_translation_preflight", fail_preflight)

    result = runner.invoke(
        app,
        [
            "--mode",
            "developer",
            "translate",
            str(minimal_epub_path),
            "--workers",
            "2",
            "--translation-memory",
        ],
    )

    assert result.exit_code == 1
    assert "--workers é incompatível com --translation-memory nesta versão." in result.output
    assert "Traceback" not in result.output


def test_translate_command_rejects_high_performance_profile_with_translation_memory(
    minimal_epub_path,
    monkeypatch,
):
    def fail_preflight(**_kwargs: object) -> object:
        raise AssertionError("preflight should not run with incompatible performance profile")

    monkeypatch.setattr("ayvu.interfaces.cli.run.run_translation_preflight", fail_preflight)

    result = runner.invoke(
        app,
        [
            "--mode",
            "developer",
            "translate",
            str(minimal_epub_path),
            "--performance-profile",
            "high",
            "--translation-memory",
        ],
    )

    assert result.exit_code == 1
    assert "--workers é incompatível com --translation-memory nesta versão." in result.output
    assert "Traceback" not in result.output


def test_translate_command_reports_unknown_profile_without_traceback(minimal_epub_path, monkeypatch, isolated_config):
    called = False

    def fail_preflight(**_kwargs: object) -> object:
        nonlocal called
        called = True
        raise AssertionError("preflight should not run for an unknown profile")

    monkeypatch.setattr("ayvu.interfaces.cli.run.run_translation_preflight", fail_preflight)

    declare_model(isolated_config)
    result = runner.invoke(
        app,
        ["--mode", "developer", "translate", str(minimal_epub_path), "--profile", "missing"],
    )

    assert result.exit_code == 1
    assert "Perfil de tradução não encontrado." in result.output
    assert "Use --profile com um perfil definido" in result.output
    assert "Traceback" not in result.output
    assert not called


def test_translate_chapters_option_reports_invalid_expression(minimal_epub_path, monkeypatch):
    def fail_preflight(**_kwargs: object) -> object:
        raise AssertionError("preflight should not run for invalid chapter selection")

    monkeypatch.setattr("ayvu.interfaces.cli.run.run_translation_preflight", fail_preflight)

    result = runner.invoke(
        app,
        ["--mode", "developer", "translate", str(minimal_epub_path), "--chapters", "3-1"],
    )

    assert result.exit_code == 1
    assert "Seleção de capítulos inválida." in result.output
    assert "chapter range is reversed" in result.output
    assert "Traceback" not in result.output


def test_translate_chapters_option_reports_unmatched_selection(minimal_epub_path, monkeypatch, isolated_config):
    def fail_preflight(**_kwargs: object) -> object:
        raise AssertionError("preflight should not run when no chapter matches")

    monkeypatch.setattr("ayvu.interfaces.cli.run.run_translation_preflight", fail_preflight)

    declare_model(isolated_config)
    result = runner.invoke(
        app,
        ["--mode", "developer", "translate", str(minimal_epub_path), "--chapters", "999"],
    )

    assert result.exit_code == 1
    assert "Nenhum capítulo corresponde à seleção informada." in result.output
    assert "no chapters matched selection" in result.output
    assert "Traceback" not in result.output
