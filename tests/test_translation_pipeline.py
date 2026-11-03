"""The pipeline on its own: plain ports, no document types and no durable identity."""
from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
import sys

import pytest

from ayvu.domain import LanguagePair
from ayvu.translation_memory import TranslationMemoryMatch
from ayvu.translation_pipeline import (
    DEFAULT_MAX_RECOVERY_CALLS, LEGACY_TEXT_STAGES, LegacyTextPipelineFacade, LinguisticSource,
)
from ayvu.translation_protection import protect_special_terms, protected_content_is_intact

PAIR = LanguagePair(source='en', target='pt')
ROOT = Path(__file__).parents[1] / 'src' / 'ayvu'


@dataclass
class Glossed:
    text: str
    usage: object = 'usage'


class Provider:
    def __init__(self, respond=None):
        self.respond = respond or (lambda text: f'[{text}]')
        self.calls = []

    def translate(self, text, source, target):
        self.calls.append((text, source, target))
        return self.respond(text)


class Cache:
    def __init__(self, stored=None):
        self.stored = dict(stored or {})

    def get(self, text, language_pair):
        return self.stored.get((text, language_pair.source, language_pair.target))

    def set(self, text, language_pair, translated):
        self.stored[(text, language_pair.source, language_pair.target)] = translated


class Memory:
    def __init__(self, found):
        self.found = found

    def lookup(self, text, language_pair):
        return self.found


def facade(provider=None, cache=None, **options):
    stages = []
    return LegacyTextPipelineFacade(provider or Provider(), cache or Cache(), options.pop('glossary', Glossed),
                                    on_stage=stages.append, **options), stages


def order(*names):
    return [stage.stage_id for stage in LEGACY_TEXT_STAGES if stage.stage_id in names]


ALL = [stage.stage_id for stage in LEGACY_TEXT_STAGES]


def test_stage_identities_and_order_are_stable():
    assert ALL == ['unit_whitespace', 'exact_reuse', 'legacy_fuzzy_reuse', 'execution_gates', 'protection', 'provider_chunks',
                   'placeholder_restoration', 'raw_cache_write', 'post_processing']
    assert {stage.version for stage in LEGACY_TEXT_STAGES} == {1}


def test_a_provider_translation_passes_every_stage_in_order():
    subject, stages = facade()
    outcome = subject.run(' Hello ', PAIR)
    assert stages == ALL
    assert (outcome.text, outcome.source, outcome.glossary_usage) == (' [Hello] ', LinguisticSource.PROVIDER, 'usage')


def test_blank_text_stops_at_the_first_stage():
    subject, stages = facade()
    outcome = subject.run('  ', PAIR)
    assert stages == ALL[:1] and (outcome.text, outcome.source, outcome.glossary_usage) == ('  ', LinguisticSource.BLANK, None)


def test_a_cache_hit_skips_to_post_processing():
    subject, stages = facade(cache=Cache({('Hello', 'en', 'pt'): 'Ola'}))
    outcome = subject.run('Hello', PAIR)
    assert stages == ['unit_whitespace', 'exact_reuse', 'post_processing'] and outcome.source is LinguisticSource.EXACT_CACHE


def test_applied_memory_skips_to_post_processing():
    found = TranslationMemoryMatch('Hello', 'Ola', 0.99, True)
    subject, stages = facade(memory=Memory(found))
    outcome = subject.run('Hello', PAIR)
    assert stages == ALL[:3] + ['post_processing'] and outcome.source is LinguisticSource.APPLIED_MEMORY
    assert outcome.memory_suggestion is None


@pytest.mark.parametrize('options,source', [
    ({'cache_only': True}, LinguisticSource.CACHE_ONLY_MISSING), ({'dry_run': True}, LinguisticSource.DRY_RUN),
    ({'cache_only': True, 'dry_run': True}, LinguisticSource.CACHE_ONLY_MISSING),
], ids=['cache-only', 'dry-run', 'both'])
def test_gates_stop_before_the_provider_and_write_nothing(options, source):
    provider, cache = Provider(), Cache()
    subject, stages = facade(provider, cache)
    outcome = subject.run(' Hello ', PAIR, **options)
    assert stages == ALL[:4] and provider.calls == [] and cache.stored == {}
    assert (outcome.text, outcome.source, outcome.glossary_usage) == (' Hello ', source, None)


def test_the_provider_receives_the_language_pair_and_the_cache_is_keyed_by_it():
    provider, cache = Provider(), Cache()
    facade(provider, cache)[0].run('Hello', LanguagePair(source='fr', target='de'))
    assert provider.calls == [('Hello', 'fr', 'de')] and cache.stored == {('Hello', 'fr', 'de'): '[Hello]'}


def test_the_glossary_sees_the_raw_translation_and_the_cache_keeps_it():
    seen, cache = [], Cache()

    def glossary(text):
        seen.append(text)
        return Glossed(text.replace('[', '<').replace(']', '>'))

    outcome = facade(cache=cache, glossary=glossary)[0].run('Hello', PAIR)
    assert seen == ['[Hello]'] and outcome.text == '<Hello>' and cache.stored == {('Hello', 'en', 'pt'): '[Hello]'}


def test_a_suggestion_is_returned_with_the_provider_translation():
    found = TranslationMemoryMatch('Hello', 'Ola', 0.6, False)
    outcome = facade(memory=Memory(found))[0].run('Hello', PAIR)
    assert outcome.source is LinguisticSource.PROVIDER and outcome.memory_suggestion == found


CORRUPTIONS = {
    'dropped': lambda text: text.replace('__AYVU_PROTECTED_0__', ''),
    'duplicated': lambda text: text.replace('__AYVU_PROTECTED_0__', '__AYVU_PROTECTED_0__ __AYVU_PROTECTED_0__'),
    'invented': lambda text: text + ' __AYVU_PROTECTED_9__',
    'rewritten': lambda text: text.replace('__AYVU_PROTECTED_', 'AYVU PROTECTED ').replace('__', ''),
    'renumbered': lambda text: text.replace('_0__', '_1__'),
}


@pytest.mark.parametrize('kind', CORRUPTIONS)
def test_a_corrupted_placeholder_never_reaches_the_cache_and_never_reaches_the_provider_again(kind):
    def respond(text):
        return CORRUPTIONS[kind](text) if '__AYVU' in text else f'<{text}>'

    provider, cache = Provider(respond), Cache()
    outcome = facade(provider, cache)[0].run('Use foo_bar now', PAIR)
    assert outcome.text == '<Use> foo_bar <now>' and cache.stored[('Use foo_bar now', 'en', 'pt')] == outcome.text
    assert [call[0] for call in provider.calls[1:]] == ['Use', 'now']


def test_reordered_placeholders_never_reach_the_cache():
    def respond(text):
        return text.replace('__AYVU_PROTECTED_0__', '@').replace('__AYVU_PROTECTED_1__', '__AYVU_PROTECTED_0__').replace('@', '__AYVU_PROTECTED_1__') if '__AYVU' in text else f'<{text}>'

    provider, cache = Provider(respond), Cache()
    outcome = facade(provider, cache)[0].run('Use foo_bar and bar_baz now', PAIR)
    assert outcome.text == '<Use> foo_bar <and> bar_baz <now>'


@pytest.mark.parametrize('response', [lambda text: '', lambda text: '   '], ids=['empty', 'blank'])
def test_an_empty_recovery_fails_before_the_cache_write(response):
    cache = Cache()
    subject, stages = facade(Provider(response), cache)
    with pytest.raises(ValueError):
        subject.run('Hello', PAIR)
    assert cache.stored == {} and 'raw_cache_write' not in stages and 'post_processing' not in stages


def test_recovery_calls_are_bounded():
    assert DEFAULT_MAX_RECOVERY_CALLS == 64
    cache = Cache()
    provider = Provider(lambda text: 'x __AYVU' if '__AYVU' in text else 'ok')
    subject, _ = facade(provider, cache, max_recovery_calls=1)
    with pytest.raises(ValueError, match='^A recuperação excedeu o limite de chamadas ao tradutor.$'):
        subject.run('one foo_bar two', PAIR)
    assert cache.stored == {}


def test_a_result_with_altered_protected_terms_fails_before_the_cache_write():
    cache = Cache()
    provider = Provider(lambda text: 'x __AYVU' if '__AYVU' in text else 'foo_bar')
    subject, stages = facade(provider, cache)
    with pytest.raises(ValueError, match='^O tradutor devolveu marcadores internos ou perdeu termos protegidos.$'):
        subject.run('Use foo_bar now', PAIR)
    assert cache.stored == {} and 'raw_cache_write' not in stages
    assert not protected_content_is_intact('Use foo_bar now', 'Use now')


def test_non_document_text_runs_without_any_format_type():
    provider = Provider()
    outcome = facade(provider)[0].run('A subtitle cue, not markup <at all>', PAIR)
    assert outcome.source is LinguisticSource.PROVIDER and provider.calls[0][0] == 'A subtitle cue, not markup <at all>'


def test_chunks_join_in_the_order_they_were_sent():
    provider = Provider()
    text = 'First sentence here. Second sentence here. Third sentence here.'
    outcome = facade(provider)[0].run(text, PAIR, chunk_limit=25)
    assert ''.join(call[0] for call in provider.calls) == text and len(provider.calls) > 1
    assert outcome.text == ''.join(f'[{call[0]}]' for call in provider.calls)


def imported_roots(name):
    roots = set()
    for node in ast.walk(ast.parse((ROOT / name).read_text())):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split('.')[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            roots.add(node.module.split('.')[0] if not node.level else f'.{node.module}')
    return roots


@pytest.mark.parametrize('name', ['translation_pipeline.py', 'translation_protection.py'])
def test_the_pipeline_imports_no_format_provider_or_storage(name):
    roots = imported_roots(name)
    assert not roots & {'bs4', 'ebooklib', 'lxml', 'requests', 'sqlite3', 'typer', 'rich', 'xml', 'html', 'zipfile', 'os'}
    assert not roots & {'.cache', '.glossary', '.translator', '.html_translate', '.epub_io', '.artifact_store', '.provenance_store'}


def test_the_facade_cannot_create_durable_state():
    roots = imported_roots('translation_pipeline.py')
    assert not roots & {'.artifact', '.artifact_service', '.provenance', '.provenance_service', '.content', '.job', '.job_store'}
    subject, _ = facade()
    assert not {name for name in vars(subject) if 'project' in name or 'job' in name or 'unit' in name}


def test_private_text_stays_out_of_representations():
    subject, _ = facade()
    assert repr(subject) == 'LegacyTextPipelineFacade()'
    assert sys.modules['ayvu.translation_pipeline'].__doc__


def test_protection_keeps_its_exact_placeholder_format():
    protected = protect_special_terms('Use foo_bar now')
    assert protected.text == 'Use __AYVU_PROTECTED_0__ now' and protected.terms == (('__AYVU_PROTECTED_0__', 'foo_bar'),)
    assert protected.restore('Usar __AYVU_PROTECTED_0__ agora') == 'Usar foo_bar agora'


def test_the_html_module_keeps_its_public_names():
    from ayvu import html_translate
    for name in ('translate_text', 'translate_html', 'extract_visible_text', 'apply_reviewed_html', 'TextTranslationResult',
                 'MAX_PROTECTED_RECOVERY_CALLS', 'TAG_TOKEN_PATTERN'):
        assert hasattr(html_translate, name), name
