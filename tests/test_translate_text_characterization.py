"""The observable contract of ``translate_text``, written against the code before its extraction.

These tests name the exact order of cache, memory, provider and glossary work, because a
reordering would keep most output correct while silently losing or corrupting cached work.
"""
from __future__ import annotations

import pytest

from ayvu.cache import CacheKey, TranslationCache
from ayvu.domain import LanguagePair, TranslationMemoryOptions
from ayvu.html_translate import translate_text
from ayvu.translation_memory import TranslationMemory
from ayvu.translator import Translator

PAIR = LanguagePair(source='en', target='pt')


class Events(list):
    pass


class RecordingTranslator(Translator):
    def __init__(self, events, respond=None):
        self.events = events
        self.respond = respond or (lambda text: f'[{text}]')

    def translate(self, text, source, target):
        self.events.append(('provider', text, source, target))
        return self.respond(text)


class RecordingCache:
    def __init__(self, events, stored=None):
        self.events = events
        self.stored = dict(stored or {})

    def get(self, key):
        self.events.append(('cache_get', key.text, key.language_pair.source, key.language_pair.target))
        return self.stored.get(key.text)

    def set(self, key, translated):
        self.events.append(('cache_set', key.text, translated))
        self.stored[key.text] = translated


class RecordingMemory:
    def __init__(self, events, match=None):
        self.events = events
        self.match = match

    def lookup(self, text, pair):
        self.events.append(('memory', text))
        return self.match


def match(translated='memoria', *, applied=True, original='Hello'):
    from ayvu.translation_memory import TranslationMemoryMatch
    return TranslationMemoryMatch(original=original, translated=translated, score=99.0, applied=applied)


def run(text, events=None, *, cached=None, respond=None, memory=None, **options):
    events = Events() if events is None else events
    cache = RecordingCache(events, cached)
    memory_port = None if memory is None else RecordingMemory(events, memory if memory is not True else None)
    result = translate_text(text, RecordingTranslator(events, respond), cache, 'en', 'pt', memory=memory_port, **options)
    return result, events, cache


def test_provider_miss_order_is_lookup_memory_provider_then_write():
    result, events, cache = run('Hello there', memory=True)
    assert events == [('cache_get', 'Hello there', 'en', 'pt'), ('memory', 'Hello there'),
                      ('provider', 'Hello there', 'en', 'pt'), ('cache_set', 'Hello there', '[Hello there]')]
    assert result.text == '[Hello there]' and not result.from_cache and not result.from_memory and not result.missing


@pytest.mark.parametrize('text', [' Hello', 'Hello ', '  \n Hello\t ', ' Hello '], ids=repr)
def test_outer_whitespace_is_not_cached_or_sent_and_is_restored_last(text):
    result, events, cache = run(text, glossary={'[Hello]': '[Ola]'})
    assert ('provider', 'Hello', 'en', 'pt') in events and cache.stored == {'Hello': '[Hello]'}
    leading = text[:len(text) - len(text.lstrip())]
    trailing = text[len(text.rstrip()):]
    assert result.text == f'{leading}[Ola]{trailing}'


@pytest.mark.parametrize('text', ['', ' ', '\n\t  '], ids=repr)
def test_blank_text_touches_nothing_and_is_returned_as_is(text):
    result, events, _ = run(text, memory=True)
    assert events == [] and result.text == text
    assert not result.from_cache and not result.from_memory and not result.missing and result.memory_suggestion is None


def test_exact_cache_hit_short_circuits_memory_and_provider_and_writes_nothing():
    result, events, _ = run(' Hello ', cached={'Hello': 'Ola'}, memory=match())
    assert events == [('cache_get', 'Hello', 'en', 'pt')]
    assert result.text == ' Ola ' and result.from_cache and not result.from_memory


def test_glossary_applies_to_a_cache_hit_without_rewriting_the_cache():
    result, _, cache = run('Hello', cached={'Hello': 'Ola'}, glossary={'Ola': 'Oi'})
    assert result.text == 'Oi' and cache.stored == {'Hello': 'Ola'}
    assert result.glossary_usage.total_applied == 1


def test_damaged_cache_entry_is_ignored_and_replaced_by_a_new_translation():
    result, events, cache = run('Run uv sync now', cached={'Run uv sync now': 'Rode agora'},
                                respond=lambda text: text.replace('Run', 'Rode'))
    assert not result.from_cache and any(item[0] == 'provider' for item in events)
    assert cache.stored['Run uv sync now'] == 'Rode uv sync now'


def test_applied_memory_short_circuits_the_provider_and_is_not_promoted_to_the_cache():
    result, events, cache = run(' Hello ', memory=match(), glossary={'memoria': 'memória'})
    assert events == [('cache_get', 'Hello', 'en', 'pt'), ('memory', 'Hello')]
    assert result.text == ' memória ' and result.from_memory and not result.from_cache
    assert cache.stored == {} and result.memory_suggestion is None


def test_suggested_memory_is_returned_alongside_the_provider_translation():
    suggestion = match(applied=False)
    result, events, _ = run('Hello', memory=suggestion)
    assert any(item[0] == 'provider' for item in events)
    assert result.memory_suggestion == suggestion and result.text == '[Hello]' and not result.from_memory


def test_memory_is_skipped_for_inline_tag_placeholders():
    text = 'Hello __AYVU_TAG_0__ world'
    _, events, _ = run(text, memory=match())
    assert not any(item[0] == 'memory' for item in events)


@pytest.mark.parametrize('stored', [
    {'original': 'Hello __AYVU_TAG_0__', 'translated': 'Ola'}, {'original': 'Hello', 'translated': 'Ola __AYVU_TAG_0__'},
    {'original': 'Hello', 'translated': 'Ola AYVU PROTECTED 1'},
], ids=['original-tag', 'translation-tag', 'internal-marker'])
def test_memory_matches_carrying_markup_or_markers_are_rejected(stored):
    result, events, _ = run('Hello', memory=match(stored['translated'], original=stored['original']))
    assert not result.from_memory and result.memory_suggestion is None
    assert any(item[0] == 'provider' for item in events)


def test_cache_only_miss_returns_the_source_without_provider_or_write():
    result, events, cache = run(' Hello ', cache_only=True, memory=True)
    assert [item[0] for item in events] == ['cache_get', 'memory']
    assert result.text == ' Hello ' and result.missing and cache.stored == {}


def test_cache_only_keeps_a_memory_suggestion():
    suggestion = match(applied=False)
    result, _, _ = run('Hello', cache_only=True, memory=suggestion)
    assert result.missing and result.memory_suggestion == suggestion


def test_cache_only_still_uses_hits_and_applied_memory():
    assert run('Hello', cached={'Hello': 'Ola'}, cache_only=True)[0].from_cache
    assert run('Hello', cache_only=True, memory=match())[0].from_memory


def test_cache_only_takes_precedence_over_dry_run():
    result, _, _ = run('Hello', cache_only=True, dry_run=True)
    assert result.missing


def test_dry_run_miss_calls_no_provider_and_writes_nothing():
    suggestion = match(applied=False)
    result, events, cache = run(' Hello ', dry_run=True, memory=suggestion)
    assert [item[0] for item in events] == ['cache_get', 'memory']
    assert result.text == ' Hello ' and not result.missing and cache.stored == {}
    assert result.memory_suggestion == suggestion


def test_dry_run_still_uses_hits():
    assert run('Hello', cached={'Hello': 'Ola'}, dry_run=True)[0].from_cache


def test_raw_translation_is_cached_before_the_glossary_changes_it():
    result, _, cache = run('Hello', glossary={'[Hello]': 'Glossed'})
    assert cache.stored == {'Hello': '[Hello]'} and result.text == 'Glossed'


def test_chunks_are_sent_and_joined_in_order_and_cached_as_one():
    text = 'First sentence here. Second sentence here. Third sentence here.'
    result, events, cache = run(text, chunk_limit=25)
    sent = [item[1] for item in events if item[0] == 'provider']
    assert len(sent) > 1 and ''.join(sent) == text
    assert result.text == ''.join(f'[{chunk}]' for chunk in sent) and cache.stored == {text: result.text}


def test_protected_terms_are_replaced_by_placeholders_and_restored_before_the_cache_write():
    result, events, cache = run('Use foo_bar now', respond=lambda text: text.replace('Use', 'Usar').replace('now', 'agora'))
    assert events[1][:2] == ('provider', 'Use __AYVU_PROTECTED_0__ now')
    assert result.text == 'Usar foo_bar agora' and cache.stored['Use foo_bar now'] == 'Usar foo_bar agora'


def fallback(text):
    # The provider destroys placeholders but translates plain fragments.
    return text.replace('__AYVU_PROTECTED_0__', 'PLACEHOLDER GONE') if '__AYVU' in text else f'<{text}>'


def test_lost_placeholder_falls_back_to_fragments_that_never_contain_protected_terms():
    result, events, cache = run('Use foo_bar now', respond=fallback)
    sent = [item[1] for item in events if item[0] == 'provider']
    assert sent == ['Use __AYVU_PROTECTED_0__ now', 'Use', 'now']
    assert result.text == '<Use> foo_bar <now>' and cache.stored['Use foo_bar now'] == result.text


def test_chunk_limit_that_would_split_a_placeholder_goes_straight_to_fragments():
    _, events, _ = run('Use foo_bar now and later', chunk_limit=8)
    assert not any('__AYVU' in item[1] for item in events if item[0] == 'provider')


def test_empty_provider_response_falls_back_instead_of_being_cached():
    calls = []

    def respond(text):
        calls.append(text)
        return '' if len(calls) == 1 else 'Ola'

    result, _, cache = run('Hello', respond=respond)
    assert len(calls) == 2 and result.text == 'Ola' and cache.stored == {'Hello': 'Ola'}


@pytest.mark.parametrize('respond,message', [
    (lambda text: '', 'O tradutor devolveu texto vazio durante a recuperação.'),
    (lambda text: 'AYVU PROTECTED 1' if '__AYVU' not in text else 'x', 'O tradutor devolveu marcadores internos durante a recuperação.'),
])
def test_failed_recovery_raises_the_current_message_and_writes_nothing(respond, message):
    events = Events()
    cache = RecordingCache(events)
    with pytest.raises(ValueError, match=f'^{message}$'):
        translate_text('Hello', RecordingTranslator(events, respond), cache, 'en', 'pt')
    assert cache.stored == {}


def test_provider_echoing_a_protected_sentence_is_a_valid_result():
    result, _, cache = run('Use foo_bar now', respond=lambda text: text)
    assert result.text == 'Use foo_bar now' and cache.stored == {'Use foo_bar now': 'Use foo_bar now'}


def test_recovery_call_limit_is_enforced_through_the_existing_setting(monkeypatch):
    monkeypatch.setattr('ayvu.html_translate.MAX_PROTECTED_RECOVERY_CALLS', 1)
    events = Events()
    with pytest.raises(ValueError, match='^A recuperação excedeu o limite de chamadas ao tradutor.$'):
        translate_text('one foo_bar two foo_bar three', RecordingTranslator(events, fallback), RecordingCache(events), 'en', 'pt')


def test_provider_exception_propagates_unchanged_and_writes_nothing():
    class Exploding(Translator):
        def translate(self, text, source, target):
            raise KeyError('provider failure')

    events = Events()
    cache = RecordingCache(events)
    with pytest.raises(KeyError):
        translate_text('Hello', Exploding(), cache, 'en', 'pt')
    assert cache.stored == {}


def test_real_sqlite_cache_keeps_the_legacy_key_and_value(tmp_path):
    path = tmp_path / 'cache.sqlite'
    with TranslationCache(path) as cache:
        translate_text(' Hello ', RecordingTranslator(Events()), cache, 'en', 'pt')
        assert cache.get(CacheKey('Hello', PAIR)) == '[Hello]'
        assert cache.get(CacheKey(' Hello ', PAIR)) is None
    with TranslationCache(path) as cache:
        events = Events()
        result = translate_text('Hello', RecordingTranslator(events), cache, 'en', 'pt')
    assert result.from_cache and events == []


def test_real_translation_memory_is_still_consulted_after_a_cache_miss(tmp_path):
    with TranslationCache(tmp_path / 'cache.sqlite') as cache:
        cache.set(CacheKey('The quick brown fox jumps', PAIR), 'A raposa pula')
        memory = TranslationMemory(cache, TranslationMemoryOptions(apply_threshold=0.8, suggest_threshold=0.5))
        events = Events()
        result = translate_text('The quick brown fox jumped', RecordingTranslator(events), cache, 'en', 'pt', memory=memory)
    assert result.from_memory and events == [] and result.text == 'A raposa pula'


def test_recovery_that_adds_a_protected_term_raises_and_writes_nothing():
    events = Events()
    cache = RecordingCache(events)
    respond = lambda text: 'x __AYVU' if '__AYVU' in text else 'foo_bar'
    with pytest.raises(ValueError, match='^O tradutor devolveu marcadores internos ou perdeu termos protegidos.$'):
        translate_text('Use foo_bar now', RecordingTranslator(events, respond), cache, 'en', 'pt')
    assert cache.stored == {}
