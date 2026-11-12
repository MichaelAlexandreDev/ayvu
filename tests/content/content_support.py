import ast
from dataclasses import FrozenInstanceError, fields, replace
import hashlib
import json
import logging
import os
from pathlib import Path
import random
import re
import subprocess
import sys
from uuid import UUID
import pytest
import ayvu.content.model as content_module
from ayvu.content.model import (
    CONTENT_SCHEMA_VERSION, IDENTITY_VERSION, INHERITED_LOCALE, MAX_ORDER_DEPTH, MAX_REFERENCE_CHARS, UNKNOWN_LOCALE,
    AdapterIdentity, Constraint, ContentDocument, ContentIdentityError, ContentSerializationError,
    ContentValidationError, ContentVersionError, ContextLink, ContextReference, Extension, LocaleState, Locator,
    ProtectedPart, SemanticKind, SourceLocale, TranslationUnit, UnitContent, build_document, decode_document,
    encode_document, source_digest,
)
from ayvu.provenance.model import SubjectRef
from tests.support.content_fixtures import ENGLISH, EPUB, FINGERPRINT, PROJECT, epub_document, epub_locator, epub_units, layout_document, subtitle_document, unit
from tests.support.paths import TESTS_ROOT

FIXTURES = TESTS_ROOT / 'fixtures' / 'content'


OTHER_PROJECT = str(UUID(int=0x9999))


DOCUMENTS = {'epub': epub_document, 'subtitle': subtitle_document, 'layout': layout_document}


ID_FORMAT = re.compile(r'unit:v1:[0-9a-f]{64}')


def ids(document):
    return [item.unit_id for item in document.units]


def single(text='Hello', **changes):
    return unit('body/p[1]', text, **changes)


def build(*units, **changes):
    return build_document(changes.pop('project_id', PROJECT), changes.pop('logical_key', 'chapter-1'),
                          changes.pop('adapter', EPUB), changes.pop('source_locale', ENGLISH), units, **changes)


def base_units(**changes):
    return [unit('title', 'Title', 'title', (1,)), unit('body', 'Text body', 'paragraph', (2,), **changes)]


def _tampered(mutate):
    data = json.loads(encode_document(epub_document()))
    mutate(data)
    return json.dumps(data, sort_keys=True, separators=(',', ':')).encode()


# --- Privacy and boundaries ------------------------------------------------------------------------------------------

CANARIES = ('Call me Ishmael.', 'Me chame de Ishmael.', '/home/reader/private/book.epub',
            'https://user:hunter2@example.test/x', 'sk-secret-token', 'C:\\Users\\reader\\book.epub')
