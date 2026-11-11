from __future__ import annotations
import ast
from dataclasses import dataclass
from pathlib import Path
import sys
import pytest
from ayvu.knowledge.cache.cache_only import CacheOnlyRequest
from ayvu.knowledge.memory.fuzzy import TranslationMemoryMatch
from ayvu.translation.pipeline import (
    DEFAULT_MAX_RECOVERY_CALLS, LEGACY_TEXT_STAGES, LegacyTextPipelineFacade, LinguisticSource,
)
from ayvu.content.protection import protect_special_terms, protected_content_is_intact
from ayvu.kernel import LanguagePair
from tests.support.paths import PROJECT_ROOT

PAIR = LanguagePair(source='en', target='pt')


ROOT = PROJECT_ROOT / 'src' / 'ayvu'


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


CORRUPTIONS = {
    'dropped': lambda text: text.replace('__AYVU_PROTECTED_0__', ''),
    'duplicated': lambda text: text.replace('__AYVU_PROTECTED_0__', '__AYVU_PROTECTED_0__ __AYVU_PROTECTED_0__'),
    'invented': lambda text: text + ' __AYVU_PROTECTED_9__',
    'rewritten': lambda text: text.replace('__AYVU_PROTECTED_', 'AYVU PROTECTED ').replace('__', ''),
    'renumbered': lambda text: text.replace('_0__', '_1__'),
}


def imported_roots(name):
    roots = set()
    for node in ast.walk(ast.parse((ROOT / name).read_text())):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split('.')[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            roots.add(node.module.split('.')[0] if not node.level else f'.{node.module}')
    return roots
