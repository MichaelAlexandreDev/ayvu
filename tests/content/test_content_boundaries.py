import ast
from dataclasses import FrozenInstanceError, fields
import logging
import sys

import pytest

from ayvu.content.model import AdapterIdentity, Constraint, ContentValidationError, ContextReference, Extension, LocaleState, Locator, ProtectedPart, SemanticKind, SourceLocale, UnitContent, decode_document
from tests.support.content_fixtures import FINGERPRINT, PROJECT, epub_document, epub_locator, unit

from tests.content.content_support import CANARIES, DOCUMENTS
from tests.content.content_support import build, single
from tests.support.paths import PROJECT_ROOT


# --- Determinism -----------------------------------------------------------------------------------------------------

@pytest.mark.parametrize('canary', CANARIES)
def test_text_paths_and_credentials_never_reach_ids_repr_or_errors(canary, caplog):
    caplog.set_level(logging.DEBUG)
    document = build(unit('body/p[1]', canary, protected_parts=(ProtectedPart(0, 4, 'identifier'),),
                          constraints=(Constraint('max_chars', 999),), extensions=(Extension.of('note', 1, canary),),
                          order=(1,)),
                     unit('body/p[2]', 'other', order=(2,), context=(ContextReference('previous', epub_locator('body/p[1]')),)))
    first, second = document.units
    values = [document, first, second, first.content, first.locator, first.content.extensions[0], first.content.protected_parts[0],
              first.content.constraints[0], second.content.context[0], second.context_links[0], document.adapter]
    texts = [repr(value) for value in values] + [str(value) for value in values]
    texts += [document.document_id, document.revision_id, first.unit_id, second.unit_id, first.source_digest]
    for attempt in (lambda: Locator('schema', 1, canary + '\n'), lambda: SemanticKind(canary),
                    lambda: Constraint(canary, 1), lambda: ContextReference(canary, epub_locator('a')),
                    lambda: Extension(canary, 1, '{}'), lambda: AdapterIdentity(canary, 1, 1),
                    lambda: SourceLocale(LocaleState.EXPLICIT, canary + ' '), lambda: build(single(), project_id=canary),
                    lambda: build(single(), source_fingerprint=canary), lambda: decode_document(canary.encode())):
        with pytest.raises(ValueError) as caught:
            attempt()
        texts += [str(caught.value), repr(caught.value)]
    texts.append(caplog.text)
    assert not any(canary in text for text in texts)


def test_repr_hides_locators_digests_and_keys():
    document = epub_document()
    text = repr(document) + repr(document.units[1]) + repr(document.units[1].locator)
    for secret in ('OEBPS/ch1.xhtml', document.units[1].source_digest, FINGERPRINT, PROJECT):
        assert secret not in text


def test_errors_carry_only_a_code_and_a_position():
    with pytest.raises(ContentValidationError) as caught:
        build(unit('a', 'secret text', order=(1,)), unit('a', 'secret text', order=(2,)))
    assert vars(caught.value) == {'code': 'duplicate_locator', 'unit_index': 1}


def test_values_are_frozen():
    document = epub_document()
    with pytest.raises(FrozenInstanceError):
        document.revision_id = 'x'
    with pytest.raises(FrozenInstanceError):
        document.units[0].unit_id = 'x'
    with pytest.raises(FrozenInstanceError):
        document.units[0].content.source_text = 'x'
    assert type(document.units) is tuple and type(document.units[1].content.protected_parts) is tuple


def test_model_imports_only_the_standard_library():
    tree = ast.parse((PROJECT_ROOT / 'src' / 'ayvu' / 'content' / 'model.py').read_text())
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split('.')[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert not node.level, 'the content model imports no other Ayvu module'
            roots.add(node.module.split('.')[0])
    assert roots <= sys.stdlib_module_names
    assert not roots & {'os', 'pathlib', 'sqlite3', 'html', 'xml', 'zipfile'}


def test_unlike_formats_share_the_model_without_format_fields():
    kinds = {name: {item.content.kind.name for item in factory().units} for name, factory in DOCUMENTS.items()}
    assert kinds == {'epub': {'title', 'paragraph', 'alt_text'}, 'subtitle': {'subtitle_cue'}, 'layout': {'layout_region'}}
    names = {field.name for field in fields(UnitContent)}
    assert not {'html', 'tag', 'dom', 'node', 'cue', 'timestamp', 'bbox', 'page'} & names
