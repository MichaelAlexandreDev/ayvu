"""Tests for the extracted CLI translation workflow."""
from types import SimpleNamespace
import pytest
import typer
from ayvu.interfaces.cli.app import app
from ayvu.interfaces.cli.translate_options import _build_translation_memory_options
from ayvu.formats.epub.validation import ValidationResult
from ayvu.kernel import TranslationMemoryOptions, UserMode
from ayvu.interfaces.cli import translate_options as cli_translate_options
from test_cli import isolated_config, FakeCache, _fake_translation, runner


def test_review_output_conflicts_redact_destination_path(tmp_path, capsys):
    review_path = tmp_path / "PRIVATE_REVIEW_PATH_CANARY_148.csv"
    review_path.write_text("exists", encoding="utf-8")
    with pytest.raises(typer.Exit):
        cli_translate_options._resolve_review_output_path(review_path, False, False, UserMode.DEVELOPER)
    assert "PRIVATE_REVIEW_PATH_CANARY_148" not in capsys.readouterr().out


def test_build_translation_memory_options_disabled_returns_none():
    assert (
        _build_translation_memory_options(
            enabled=False,
            apply_threshold=0.95,
            suggest_threshold=0.80,
            mode=UserMode.DEVELOPER,
        )
        is None
    )


def test_build_translation_memory_options_returns_validated_options():
    options = _build_translation_memory_options(
        enabled=True,
        apply_threshold=0.9,
        suggest_threshold=0.7,
        mode=UserMode.DEVELOPER,
    )

    assert options == TranslationMemoryOptions(apply_threshold=0.9, suggest_threshold=0.7)


def test_build_translation_memory_options_rejects_invalid_thresholds():
    with pytest.raises(typer.Exit):
        _build_translation_memory_options(
            enabled=True,
            apply_threshold=0.5,
            suggest_threshold=0.9,
            mode=UserMode.DEVELOPER,
        )


def _mock_pipeline_with_route(monkeypatch, route):
    from ayvu.v1.epub import TranslationReport as _Report

    def fake_preflight(**_kwargs: object) -> object:
        return SimpleNamespace(translator=object(), glossary=None, route=route)

    def fake_translate(input_path, output_path, **kwargs) -> _Report:
        return _Report(
            output_path=output_path,
            input_path=input_path,
            detected_language=kwargs["options"].source,
            target_language=kwargs["options"].target,
        )

    monkeypatch.setattr("ayvu.interfaces.cli.run.run_translation_preflight", fake_preflight)
    monkeypatch.setattr("ayvu.composition.cli.TranslationCache", lambda _path: FakeCache())
    _fake_translation(monkeypatch, fake_translate)
    monkeypatch.setattr(
        "ayvu.composition.builders.translate_project.validate_output",
        lambda _path, on_progress=None: ValidationResult(ok=True, document_count=1),
    )
    monkeypatch.setattr("ayvu.interfaces.cli.report._offer_markdown_report", lambda *_args, **_kwargs: None)


def test_translate_developer_mode_prints_direct_route(minimal_epub_path, tmp_path, monkeypatch):
    from ayvu.providers.libretranslate.client import TranslationRoute

    monkeypatch.setattr("ayvu.interfaces.cli.output.default_translated_books_dir", lambda: tmp_path / "Traduzidos")
    _mock_pipeline_with_route(monkeypatch, TranslationRoute(source="en", target="pt"))

    result = runner.invoke(app, ["--mode", "developer", "translate", str(minimal_epub_path)])

    assert result.exit_code == 0
    assert "Route: en -> pt" in result.output
    assert "intermediário" not in result.output


def test_translate_developer_mode_prints_intermediate_route_with_warning(
    minimal_epub_path, tmp_path, monkeypatch
):
    from ayvu.providers.libretranslate.client import TranslationRoute

    monkeypatch.setattr("ayvu.interfaces.cli.output.default_translated_books_dir", lambda: tmp_path / "Traduzidos")
    _mock_pipeline_with_route(
        monkeypatch, TranslationRoute(source="fr", target="pt", intermediate="en")
    )

    result = runner.invoke(
        app,
        ["--mode", "developer", "translate", str(minimal_epub_path), "--source", "fr"],
    )

    assert result.exit_code == 0
    assert "Route: fr -> en -> pt" in result.output
    assert "qualidade pode ficar comprometida" in result.output


def test_translate_common_mode_warns_about_intermediate_route(
    minimal_epub_path, tmp_path, monkeypatch
):
    from ayvu.providers.libretranslate.client import TranslationRoute

    monkeypatch.setattr("ayvu.interfaces.cli.output.default_translated_books_dir", lambda: tmp_path / "Traduzidos")
    _mock_pipeline_with_route(
        monkeypatch, TranslationRoute(source="fr", target="pt", intermediate="en")
    )

    result = runner.invoke(
        app,
        ["--mode", "common", "translate", str(minimal_epub_path), "--source", "fr"],
        input="y\n",
    )

    assert result.exit_code == 0
    assert "A tradução passará por 2 etapas" in result.output
    assert "fr -> en -> pt" in result.output
    assert "qualidade pode ficar comprometida" in result.output
