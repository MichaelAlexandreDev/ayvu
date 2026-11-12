"""Tests for the extracted CLI translation workflow."""
import json
from types import SimpleNamespace
from ayvu.interfaces.cli.app import app
from ayvu.v1.epub import TranslationReport
from ayvu.v1.html_translate import HtmlTranslationStats
from ayvu.translation.preflight import PreflightError
from tests.support.cli_support import isolated_config, FakeCache, _fake_translation, runner


def test_translate_resolves_and_reuses_project_before_preflight(minimal_epub_path, tmp_path, monkeypatch):
    processing = tmp_path / 'project-processing'
    monkeypatch.setattr('ayvu.interfaces.cli.settings._processing_dir', lambda _config: processing)
    calls = []
    def stop(**kwargs):
        calls.append(kwargs)
        raise PreflightError('synthetic-stop', 'test complete')
    monkeypatch.setattr('ayvu.interfaces.cli.run.run_translation_preflight', stop)
    original = minimal_epub_path.read_bytes()
    args = ['translate', str(minimal_epub_path), '--source', 'en', '--target', 'pt',
            '--output', str(tmp_path / 'translated.epub')]
    for _ in range(2):
        result = runner.invoke(app, args)
        assert result.exit_code == 1 and 'synthetic-stop' in result.output
    manifests = list(processing.glob('projects/*/project.json'))
    assert len(manifests) == 1
    assert json.loads(manifests[0].read_text())['revision'] == 1
    assert len(calls) == 2
    assert minimal_epub_path.read_bytes() == original


def test_project_storage_failure_prevents_preflight(minimal_epub_path, tmp_path, monkeypatch):
    processing = tmp_path / 'blocked-processing'
    processing.write_bytes(b'blocked')
    monkeypatch.setattr('ayvu.interfaces.cli.settings._processing_dir', lambda _config: processing)
    calls = []
    monkeypatch.setattr('ayvu.interfaces.cli.run.run_translation_preflight', lambda **kw: calls.append(kw))
    result = runner.invoke(app, ['translate', str(minimal_epub_path), '--source', 'en',
                                 '--output', str(tmp_path / 'translated.epub')])
    assert result.exit_code == 1
    assert calls == []
    assert 'projeto local' in result.output
    assert not (tmp_path / 'translated.epub').exists()


def test_project_rejects_secret_endpoint_without_reflecting_it(minimal_epub_path, tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr('ayvu.interfaces.cli.run.run_translation_preflight', lambda **kw: calls.append(kw))
    result = runner.invoke(app, ['translate', str(minimal_epub_path), '--source', 'en',
                                 '--url', 'http://user:secret-canary@localhost',
                                 '--output', str(tmp_path / 'translated.epub')])
    assert result.exit_code == 1
    assert calls == []
    assert 'secret-canary' not in result.output
    assert 'sem usuário' in result.output


def test_dry_run_does_not_require_project_storage(minimal_epub_path, tmp_path, monkeypatch):
    processing = tmp_path / 'blocked'
    processing.write_bytes(b'blocked')
    monkeypatch.setattr('ayvu.interfaces.cli.settings._processing_dir', lambda _config: processing)
    calls = []
    def stop(**kwargs):
        calls.append(kwargs)
        raise PreflightError('synthetic-dry-run-stop', 'test complete')
    monkeypatch.setattr('ayvu.interfaces.cli.run.run_translation_preflight', stop)
    result = runner.invoke(app, ['translate', str(minimal_epub_path), '--source', 'en', '--dry-run'])
    assert len(calls) == 1
    assert 'synthetic-dry-run-stop' in result.output
    assert processing.read_bytes() == b'blocked'


def test_translate_command_handles_keyboard_interrupt_cleanly(tmp_path, monkeypatch):
    epub_path = tmp_path / "book.epub"
    output_path = tmp_path / "book-pt.epub"
    cache_path = tmp_path / "cache.sqlite"
    epub_path.write_bytes(b"fake epub")

    def interrupt_translation(*_args: object, **kwargs: object) -> TranslationReport:
        kwargs["on_chapter_start"](1, 3, "chapter-one.xhtml")
        kwargs["on_text_processed"]("translated")
        kwargs["on_text_processed"]("cache")
        kwargs["on_text_processed"]("error")
        kwargs["on_chapter_done"](1, 3, "chapter-one.xhtml", HtmlTranslationStats())
        kwargs["on_chapter_start"](2, 3, "chapter-two.xhtml")
        raise KeyboardInterrupt

    monkeypatch.setattr("ayvu.interfaces.cli.settings.default_processing_dir", lambda: tmp_path / "Processando")
    monkeypatch.setattr(
        "ayvu.interfaces.cli.run.run_translation_preflight",
        lambda **_kwargs: SimpleNamespace(translator=object(), glossary=None, route=None),
    )
    monkeypatch.setattr("ayvu.composition.cli.TranslationCache", lambda _path: FakeCache())
    _fake_translation(monkeypatch, interrupt_translation)

    result = runner.invoke(
        app,
        [
            "translate",
            str(epub_path),
            "--output",
            str(output_path),
            "--cache",
            str(cache_path),
            "--workers",
            "2",
            "--requests-per-second",
            "2.5",
            "--retries",
            "4",
            "--retry-backoff",
            "1",
            "--retry-backoff-max",
            "10",
        ],
    )

    assert result.exit_code == 1
    assert "Translation interrupted by user." in result.output
    assert "Partial translation progress" in result.output
    assert "Chapters processed" in result.output
    assert "1/3" in result.output
    assert "Texts processed" in result.output
    assert "3" in result.output
    assert "Texts translated" in result.output
    assert "Texts from cache" in result.output
    assert "Text errors" in result.output
    assert "Workers" in result.output
    assert "2 (parallel" in result.output
    assert "2.5 requests/second" in result.output
    assert "shared across workers" in result.output
    assert "4 retries" in result.output
    assert "chapter-two.xhtml" in result.output
    assert "Cached translations saved before the interruption can be reused" in result.output
    assert str(cache_path) in result.output
    assert "Translated EPUB was not written:" in result.output
    assert "Traceback" not in result.output
    assert not output_path.exists()
