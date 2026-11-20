from tests.support.cli_rich_runtime_support import declare_model
from types import SimpleNamespace
from ayvu.interfaces.cli.app import app
from ayvu.translation import TranslationReport
from ayvu.formats.epub.validation import ValidationResult
from tests.support.cli_support import _fake_translation
from tests.support.cli_support import isolated_config, runner


def test_translate_command_cache_only_saves_missing_texts_file(tmp_path, monkeypatch, isolated_config):
    epub_path = tmp_path / "book.epub"
    output_path = tmp_path / "book-pt.epub"
    missing_path = tmp_path / "missing.txt"
    epub_path.write_bytes(b"fake epub")

    report = TranslationReport(
        chapters_processed=1,
        texts_from_cache=1,
        texts_missing=2,
        missing_texts=["Hello reader.", "Another line."],
        output_path=output_path,
        output_written=True,
        input_path=epub_path,
        detected_language="en",
        target_language="pt",
    )
    captured: dict[str, object] = {}

    def fake_translate_epub(*_args: object, **kwargs: object) -> TranslationReport:
        captured["options"] = kwargs["options"]
        return report

    def fake_preflight(**kwargs: object) -> SimpleNamespace:
        captured["preflight_kwargs"] = kwargs
        return SimpleNamespace(translator=object(), glossary=None, route=None)

    monkeypatch.setattr("ayvu.composition.cli._local_preflight", fake_preflight)
    _fake_translation(monkeypatch, fake_translate_epub)
    monkeypatch.setattr(
        "ayvu.composition.builders.translate_project.validate_output",
        lambda _path, on_progress=None: ValidationResult(ok=True, warnings=[], document_count=1),
    )

    declare_model(isolated_config)
    result = runner.invoke(
        app,
        [
            "translate",
            str(epub_path),
            "--output",
            str(output_path),
            "--cache-only",
            "--missing-output",
            str(missing_path),
        ],
    )

    assert result.exit_code == 0
    assert "Texts missing" in result.output
    assert captured["options"].cache_only is True
    assert captured["preflight_kwargs"]["translator_name"] == "libretranslate"
    assert "cache_path" not in captured["preflight_kwargs"]
    assert missing_path.exists()
    content = missing_path.read_text(encoding="utf-8")
    assert "Missing texts: 2" in content
    assert "Hello reader." in content
    assert "Another line." in content


def test_translate_command_cache_only_require_full_cache_blocks_output(tmp_path, monkeypatch, isolated_config):
    epub_path = tmp_path / "book.epub"
    output_path = tmp_path / "book-pt.epub"
    missing_path = tmp_path / "missing.txt"
    epub_path.write_bytes(b"fake epub")

    report = TranslationReport(
        chapters_processed=1,
        texts_from_cache=1,
        texts_missing=2,
        missing_texts=["Hello reader.", "Another line."],
        output_path=output_path,
        output_written=False,
        input_path=epub_path,
        detected_language="en",
        target_language="pt",
    )
    monkeypatch.setattr(
        "ayvu.composition.cli._local_preflight",
        lambda **_kwargs: SimpleNamespace(translator=object(), glossary=None, route=None),
    )
    _fake_translation(monkeypatch, lambda *_args, **_kwargs: report)

    def fail_validate(*_args: object, **_kwargs: object) -> ValidationResult:
        raise AssertionError("validation must not run when output is blocked")

    monkeypatch.setattr("ayvu.composition.builders.translate_project.validate_output", fail_validate)

    declare_model(isolated_config)
    result = runner.invoke(
        app,
        [
            "translate",
            str(epub_path),
            "--output",
            str(output_path),
            "--cache-only",
            "--require-full-cache",
            "--missing-output",
            str(missing_path),
        ],
    )

    assert result.exit_code == 1
    assert "Cache incompleto" in result.output
    assert "(cache incompleto, nenhum arquivo gerado)" in result.output
    assert missing_path.exists()
