"""Workflow authority and production wiring checks for #158; no real user stores."""
from __future__ import annotations

import socket
import sqlite3
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests
from typer.testing import CliRunner

from ayvu import cli, cli_composition as root
from ayvu.cli_contracts import _BookHooks, _BookRequest, _BookOutcome
from ayvu.config_values import AyvuConfig
from ayvu.domain import LanguagePair, OutputPlan, TranslationOptions, UserMode
from ayvu.epub_io import TranslationReport
from ayvu.job import JobState
from ayvu.job_events import JobFailure, JobFailureFamily, JobOutcome, JobTerminalSummary
from ayvu.preflight import run_translation_preflight
from ayvu.translate_project import OutputValidation, TranslateProjectResult
from ayvu.translate_project_composition import ProjectTranslationRun


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path / 'home'))
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / 'config'))
    monkeypatch.chdir(tmp_path)


def deny_resources(monkeypatch):
    calls = []
    def denied(*args, **kwargs):
        calls.append('unexpected-resource')
        raise AssertionError('unexpected resource construction')
    for module, name in (
        (socket, 'socket'), (socket, 'getaddrinfo'), (socket, 'create_connection'),
        (sqlite3, 'connect'), (requests.Session, 'request'), (requests.Session, '__init__'),
        (root, 'TranslationCache'), (root, 'ProjectJsonRepository'),
        (root, 'ConfigStore'), (root, 'LocalLibraryFilesystem'), (root, 'ReaderLauncher'),
        (root.provider_composition, 'LibreTranslateTranslator'),
        (root.glossary_catalog_composition, 'managed_glossary_repository'),
    ):
        monkeypatch.setattr(module, name, denied)
    return calls


@pytest.mark.parametrize('args', [[], ['translate'], ['cache'], ['cache', 'inspect'], ['languages'], ['apply-review']])
def test_help_never_acquires_a_resource(args, monkeypatch):
    calls = deny_resources(monkeypatch)
    result = CliRunner().invoke(cli.app, [*args, '--help'])
    assert result.exit_code == 0, result.output
    assert calls == []


def test_inspection_opens_only_the_selected_source(minimal_epub_path, monkeypatch):
    original = minimal_epub_path.read_bytes()
    calls = deny_resources(monkeypatch)
    result = CliRunner().invoke(cli.app, ['inspect', str(minimal_epub_path)])
    assert result.exit_code == 0, result.output
    assert str(minimal_epub_path) in result.output
    assert 'Minimal Test Book' in result.output
    assert minimal_epub_path.read_bytes() == original
    assert calls == []


def test_cache_only_preflight_instruments_transport_and_provider_authority(minimal_epub_path, tmp_path, monkeypatch):
    calls = []
    def forbidden(*args, **kwargs):
        calls.append('forbidden')
        raise AssertionError('cache-only crossed its authority')
    # Instrument the actual HTTP and DNS/socket transports, not just a fake provider call counter.
    for module, name in ((socket, 'socket'), (socket, 'getaddrinfo'), (socket, 'create_connection'),
                         (requests.Session, 'request'), (root, 'ProjectJsonRepository'),
                         (root, 'ConfigStore')):
        monkeypatch.setattr(module, name, forbidden)
    monkeypatch.setattr(requests.Session, '__init__', forbidden)
    monkeypatch.setattr('ayvu.preflight._create_checked_translator_factory', forbidden)
    monkeypatch.setattr('ayvu.provider_service.ProviderRegistry.open', forbidden)
    # No secret resolver exists in the current CLI provider. Prevent entry to its entire composition boundary.
    monkeypatch.setattr('ayvu.provider_composition.CliProviderSession', forbidden)
    original = minimal_epub_path.read_bytes()
    result = run_translation_preflight(epub_path=minimal_epub_path, cache_path=tmp_path / 'only-cache.sqlite',
        glossary_path=None, translator_name='libretranslate', url='http://endpoint-canary.invalid',
        timeout=1, retries=0, language_pair=LanguagePair('en', 'pt'), dry_run=False, cache_only=True)
    assert result.translator is None and result.translator_factory is None and result.route is None
    assert calls == []
    assert minimal_epub_path.read_bytes() == original
    assert not (tmp_path / 'config').exists()


def request(tmp_path, **values):
    options = TranslationOptions(LanguagePair('en', 'pt'))
    book = _BookRequest(tmp_path / 'document-canary.epub',
        OutputPlan.for_translation(tmp_path / 'document-canary.epub', tmp_path / 'out.epub', options.language_pair),
        True, options, tmp_path / 'cache-canary.sqlite',
        lambda: SimpleNamespace(translator=object(), glossary=None),
        project_id='project', source_sha256='a' * 64, processing_dir=tmp_path / 'processing')
    return replace(book, **values)


def hooks():
    return _BookHooks(lambda *args: None, lambda *args: None, lambda *args: None, lambda *args: None)


@pytest.mark.parametrize('state,reason,report_present,warning', [
    (JobState.VALIDATED, 'completed', True, None),
    (JobState.PARTIALLY_COMPLETED, 'provenance_failed', True, 'provenance_failed'),
    (JobState.FAILED, 'translation_failed', False, None),
    (JobState.CANCELLED, 'cancelled', False, None),
])
def test_cli_and_framework_free_consumer_receive_the_same_authoritative_result(
        tmp_path, monkeypatch, state, reason, report_present, warning):
    seen = []
    result = TranslateProjectResult('project', 'job', state, JobOutcome(state, JobTerminalSummary(reason),
        JobFailure(JobFailureFamily.RESOURCE, reason) if state is JobState.FAILED else None),
        failure=None if report_present else JobFailure(JobFailureFamily.RESOURCE, 'translation_failed'),
        validation=OutputValidation(True, document_count=1), report_ref='opaque_report')
    report = TranslationReport(output_path=tmp_path / 'out.epub') if report_present else None
    def execute(translation, observer):
        # Real dataclass fields checked here; mismatching a production-only field cannot hide behind a permissive fake.
        assert translation.source_sha256 == 'a' * 64
        assert translation.replace_existing is True
        assert translation.output == book.output_plan
        assert translation.accepted_output is None
        assert observer.engine.on_chapter_start is callbacks.on_chapter_start
        seen.append(translation)
        return ProjectTranslationRun(result, report, None)
    monkeypatch.setattr(root.translate_project_composition, 'translate_in_project', execute)
    book, callbacks = request(tmp_path), hooks()
    other = root._translate_in_project(book, callbacks)
    terminal = cli._translate_in_project(book, callbacks)
    assert other == terminal
    assert terminal.job_result is result and terminal.report is report
    assert terminal.warning == warning
    assert len(seen) == 2
    if not report_present:
        assert terminal.failure == 'translation_failed'


def test_confirmation_is_handed_to_the_real_project_command_without_implicit_authority(tmp_path, monkeypatch):
    captured = []
    def execute(translation, observer):
        captured.append(translation.replace_existing)
        result = TranslateProjectResult('project', 'job', JobState.FAILED,
            failure=JobFailure(JobFailureFamily.INPUT, 'output_exists'))
        return ProjectTranslationRun(result, None, None)
    monkeypatch.setattr(root.translate_project_composition, 'translate_in_project', execute)
    root._translate_in_project(request(tmp_path, replace_existing=False), hooks())
    root._translate_in_project(request(tmp_path, replace_existing=True), hooks())
    assert captured == [False, True]


def test_legacy_resource_is_closed_when_translation_raises(tmp_path, monkeypatch):
    lifecycle = []
    class Cache:
        def __enter__(self):
            lifecycle.append('open')
            return self
        def __exit__(self, *args):
            lifecycle.append('close')
    monkeypatch.setattr(root, 'TranslationCache', lambda path: Cache())
    def fail(*args, **kwargs):
        raise RuntimeError('exception-canary')
    monkeypatch.setattr(root, 'translate_without_project', fail)
    with pytest.raises(RuntimeError, match='exception-canary'):
        root._translate_without_project(request(tmp_path), hooks())
    assert lifecycle == ['open', 'close']


def test_production_library_wiring_is_lazy_and_per_workflow(tmp_path, monkeypatch):
    built = []
    class Filesystem:
        def __init__(self, *args):
            built.append(('filesystem', args))
    def service(*args):
        built.append(('service', args))
        return args
    monkeypatch.setattr(root, 'LocalLibraryFilesystem', Filesystem)
    monkeypatch.setattr(root, 'ReaderLauncher', lambda reader: ('reader', reader))
    monkeypatch.setattr(root, 'LibraryService', service)
    config = AyvuConfig(books_dir=tmp_path / 'books', reader_app='reader')
    first = root.library_service(config)
    second = root.library_service(config)
    assert first[0] is first[2] and first[0] is not second[0]
    assert built[0] == ('filesystem', (config.resolved_books_dir, config.folders.original, config.folders.translated))
    assert first[1] == ('reader', 'reader')


def test_checkpoint_factory_opens_nothing(tmp_path, monkeypatch):
    calls = deny_resources(monkeypatch)
    assert root.translation_checkpoint() is not root.translation_checkpoint()
    assert calls == []


def test_new_workflow_dtos_never_render_sensitive_values(tmp_path, caplog):
    book = request(tmp_path)
    outcome = _BookOutcome(raised=RuntimeError('credential-canary endpoint-canary document-canary glossary-canary cache-canary'))
    rendered = repr(book) + repr(outcome) + repr(hooks()) + repr(root.translation_checkpoint()) + caplog.text
    for canary in ('credential-canary', 'endpoint-canary', 'document-canary', 'glossary-canary', 'cache-canary'):
        assert canary not in rendered


def test_report_write_preserves_mode_replacement_and_empty_content(tmp_path):
    path = tmp_path / 'report.md'
    path.write_text('old')
    path.chmod(0o640)
    root.publish_cli_report(path, '')
    assert path.read_bytes() == b''
    assert path.stat().st_mode & 0o777 == 0o640
    assert root.report_destination(tmp_path, 'report') == tmp_path / 'report-2.md'


def test_absent_cache_inspection_does_not_create_any_store(tmp_path, monkeypatch):
    calls = deny_resources(monkeypatch)
    path = tmp_path / 'absent.sqlite'
    result = CliRunner().invoke(cli.app, ['cache', 'inspect', '--cache', str(path)])
    assert result.exit_code == 0, result.output
    assert 'No cache entries found.' in result.output
    assert not path.exists() and calls == []


@pytest.mark.parametrize('cache_only,dry_run,expected', [(False, False, True), (True, False, False), (False, True, False)])
def test_preparation_orders_route_checkpoint_and_progress_at_the_same_safe_point(
        tmp_path, monkeypatch, cache_only, dry_run, expected):
    events = []
    options = TranslationOptions(LanguagePair('en', 'pt'), cache_only=cache_only, dry_run=dry_run)
    monkeypatch.setattr(root, 'check_book', lambda path: events.append('inspect'))
    def preflight(**configuration):
        assert configuration['cache_only'] is cache_only and configuration['dry_run'] is dry_run
        events.append('preflight')
        return SimpleNamespace(route=None)
    def save(**values):
        assert values['overwrite'] is False and values['options'] is options
        events.append('checkpoint')
    root.prepare_translation(epub_path=tmp_path / 'book.epub', options=options,
        cache_path=tmp_path / 'cache.sqlite', glossary_path=None, translator_name='libretranslate',
        url='http://localhost:5000', timeout=1, retries=0, requests_per_second=None,
        retry_backoff=0.5, retry_backoff_max=8, output_path=tmp_path / 'out.epub', overwrite=False,
        processing_dir=tmp_path / 'processing', profile_name=None, preflight=preflight, save_checkpoint=save,
        on_route=lambda route: events.append('route'), on_ready=lambda: events.append('progress'))
    assert events == ['inspect', 'preflight', 'route', *(['checkpoint'] if expected else []), 'progress']


def test_fresh_import_and_help_have_no_store_or_transport_side_effects(tmp_path):
    code = '''
import os, sys, requests
def deny(*args, **kwargs):
    raise AssertionError('provider session constructed')
requests.Session.__init__ = deny
def audit(event, args):
    if event.startswith('socket.') or event in {'sqlite3.connect', 'subprocess.Popen'}:
        raise AssertionError(event)
    if event == 'open' and isinstance(args[0], str):
        path = os.path.abspath(args[0])
        if any(path.startswith(os.environ[name] + os.sep) for name in ('HOME', 'XDG_CONFIG_HOME')):
            raise AssertionError('user store opened')
sys.addaudithook(audit)
from ayvu.cli import app
app(['--help'])
'''
    result = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True,
        env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'}, check=False)
    assert result.returncode == 0, result.stderr
    assert 'Translate local EPUB files' in result.stdout
    assert not (tmp_path / 'home').exists() and not (tmp_path / 'config').exists()


@pytest.mark.parametrize('settings,detail', [
    ({'translator_name': 'unknown'}, 'Unsupported translator: unknown. Supported translators: libretranslate.'),
    ({'retries': -1}, 'retry count must be zero or greater'),
    ({'retry_backoff': -1}, 'retry backoff must be zero or greater'),
    ({'retry_backoff_max': -1}, 'retry backoff max must be zero or greater'),
])
def test_cache_only_keeps_configuration_refusals_without_constructing_provider(tmp_path, monkeypatch, settings, detail):
    calls = deny_resources(monkeypatch)
    values = dict(epub_path=tmp_path / 'unused.epub', cache_path=tmp_path / 'unused.sqlite',
        glossary_path=None, translator_name='libretranslate', url='http://unused.invalid',
        timeout=1, retries=0, language_pair=LanguagePair('en', 'pt'), dry_run=False, cache_only=True)
    with pytest.raises(cli.PreflightError) as raised:
        root.run_translation_preflight(**{**values, **settings})
    assert raised.value.summary == 'Não foi possível preparar o tradutor.'
    assert raised.value.next_step == 'Use --translator libretranslate.'
    assert raised.value.detail == detail
    assert calls == []


def test_preview_translation_does_not_validate_twice(tmp_path, monkeypatch):
    class Cache:
        def __enter__(self): return self
        def __exit__(self, *args): pass
    report = TranslationReport(output_written=True)
    monkeypatch.setattr(root, 'TranslationCache', lambda path: Cache())
    monkeypatch.setattr(root, 'translate_without_project', lambda *args, **kwargs: report)
    monkeypatch.setattr(root, 'validate_output', lambda *args, **kwargs: pytest.fail('preview validates in presentation once'))
    assert root._translate_without_project(request(tmp_path), hooks(), validate=False).report is report


def test_compatibility_facades_export_identical_contracts():
    from ayvu import config, config_values, preflight, resume, batch_output_policy, provider_composition
    from ayvu import cli_contracts as contracts
    for name in ('AyvuConfig', 'FolderNames', 'TranslationProfile', 'ConfigError', 'config_from_preferences'):
        assert getattr(config, name) is getattr(config_values, name)
    assert preflight.PreflightError is contracts.PreflightError
    assert resume.ResumeStateError is contracts.ResumeStateError
    assert batch_output_policy.BatchOutputError is contracts.BatchOutputError
    assert provider_composition.ProviderSetupError is contracts.ProviderSetupError


def test_existing_cache_inspection_uses_only_selected_readonly_sqlite(tmp_path, monkeypatch):
    from ayvu.cache import SCHEMA
    path = tmp_path / 'selected.sqlite'
    connect = sqlite3.connect
    connection = connect(path)
    connection.executescript(SCHEMA)
    connection.close()
    before = path.read_bytes()
    files = set(tmp_path.iterdir())
    forbidden = deny_resources(monkeypatch)
    opened = []
    def readonly(database, *args, **kwargs):
        assert kwargs.get('uri') is True
        assert str(path) in database and 'mode=ro' in database
        opened.append(database)
        return connect(database, *args, **kwargs)
    monkeypatch.setattr(sqlite3, 'connect', readonly)
    result = CliRunner().invoke(cli.app, ['cache', 'inspect', '--cache', str(path)])
    assert result.exit_code == 0, result.output
    assert 'No cache entries found.' in result.output
    assert opened and forbidden == []
    assert path.read_bytes() == before and set(tmp_path.iterdir()) == files
