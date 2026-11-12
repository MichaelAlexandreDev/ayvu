from __future__ import annotations
import pytest
from ayvu.content.model import ProtectedPart
from ayvu.translation.document_contracts import TranslationExecutionError, UnitSource
from ayvu.kernel import JobFailureFamily
from tests.support.translation_document_support import CORRUPTIONS, ENGLISH
from tests.support.translation_document_support import Glossed
from tests.support.translation_document_support import PROJECT, PlanCache, Provider
from tests.support.translation_document_support import cue, document, env
from tests.support.translation_document_support import tags


def test_protected_parts_never_reach_the_provider_and_are_restored(env):
    provider = Provider()
    subject = document(cue('a', 'Say <b>hi</b> now', 1, protected_parts=(ProtectedPart(4, 7, 'markup'), ProtectedPart(9, 13, 'markup'))))
    result = env.translate(subject, provider, glossary=Glossed)
    assert '<b>' not in provider.requests[0].text and '__AYVU_TAG' not in provider.requests[0].text
    assert tags(0) in provider.requests[0].text and tags(1) in provider.requests[0].text
    assert result.units[0].text == '[Say <b>hi</b> now]'


@pytest.mark.parametrize('kind', CORRUPTIONS)
def test_any_protected_part_corruption_fails_before_cache_artifact_and_output(env, kind):
    # The recovery keeps protected spans verbatim and translates around them, so damage is repaired or refused,
    # and a provider that also damages plain fragments cannot pass.
    def respond(text):
        return CORRUPTIONS[kind](text) if '__AYVU' in text else 'AYVU TAG 9'

    cache = PlanCache()
    subject = document(cue('a', 'Say <b>hi</b> now', 1, protected_parts=(ProtectedPart(4, 7, 'markup'), ProtectedPart(9, 13, 'markup'))))
    with pytest.raises(TranslationExecutionError, match='^protected_content_not_preserved$') as caught:
        env.translate(subject, Provider(respond), translator_options={'exact_cache': cache})
    assert caught.value.failure.family is JobFailureFamily.INTEGRITY and 'put_if_absent' not in [call[0] for call in cache.calls]
    assert not (env.root / 'ready').exists() and env.records() == ()


def test_recovery_from_a_corrupted_part_is_a_valid_translation(env):
    def respond(text):
        return text.replace(tags(0), '') if '__AYVU' in text else f'<{text}>'

    subject = document(cue('a', 'Say <b> now', 1, protected_parts=(ProtectedPart(4, 7, 'markup'),)))
    assert env.translate(subject, Provider(respond), glossary=Glossed).units[0].text == '<Say> <b> <now>'


def test_a_token_inside_a_protected_part_is_safe_and_comes_back_unchanged(env):
    literal = '__AYVU_TAG_0__'
    subject = document(cue('a', f'{literal} world', 1, protected_parts=(ProtectedPart(0, len(literal), 'placeholder'),)))
    result = env.translate(subject, Provider(lambda text: text.replace('world', 'mundo')), glossary=lambda text: Glossed(text))
    assert result.units[0].text == f'{literal} mundo'
    assert result.units[0].tokenized_text == '__AYVU_TAG_0__ mundo'


def test_replay_gives_the_same_tokenized_text(env):
    literal = '<b>'
    subject = document(cue('a', f'{literal}world', 1, protected_parts=(ProtectedPart(0, len(literal), 'markup'),)))
    first = env.translate(subject, Provider(lambda text: text.replace('world', 'mundo')), glossary=lambda text: Glossed(text))
    second = env.translate(subject, Provider(), glossary=lambda text: Glossed(text), context=env.context())
    assert second.units[0].source is UnitSource.ARTIFACT_REPLAY
    assert second.units[0].tokenized_text == first.units[0].tokenized_text == '__AYVU_TAG_0__mundo'


def test_tokenized_text_of_a_blank_unit_is_its_source(env):
    result = env.translate(document(cue('a', '   ', 1), cue('b', 'x', 2)), glossary=lambda text: Glossed(text))
    assert result.units[0].tokenized_text == '   '
    replayed = env.translate(document(cue('a', '   ', 1), cue('b', 'x', 2)), glossary=lambda text: Glossed(text), context=env.context())
    assert replayed.units[0].source is UnitSource.BLANK and replayed.units[0].tokenized_text == '   '


def test_tokenized_text_stays_out_of_the_representation(env):
    result = env.translate(document(cue('a', 'Call me Ishmael.', 1)), glossary=lambda text: Glossed(text))
    assert 'Ishmael' not in repr(result.units[0])


def test_an_html_document_survives_the_rich_path_and_reconstruction(env):
    from ayvu.formats.contracts import UnitTranslation
    from ayvu.formats.epub.html_structure import _parse, extract_html_document, reconstruct_html

    html = ('<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="urn:epub"><body>'
            '<p>The <a epub:type="noteref" href="#n">world<!-- keep --></a> and <code>world</code>.</p><p>__AYVU_TAG_0__ world</p>'
            '<img src="a.png" alt="world"/></body></html>')
    subject = extract_html_document(html, PROJECT, 'chapter.xhtml', source_locale=ENGLISH, include_alt_text=True)
    hostile = Provider(lambda text: text.replace('world', 'mundo <script>x()</script>'))
    result = env.translate(subject, hostile, glossary=lambda text: Glossed(text))
    assert all('<code>' not in request.text and '<a ' not in request.text for request in hostile.requests)
    output = reconstruct_html(html, subject, [UnitTranslation(unit.unit_id, unit.tokenized_text) for unit in result.units])
    parsed = _parse(output)
    assert parsed.find('script') is None and 'mundo &lt;script&gt;' in output.decode()
    link = parsed.find('a')
    assert link['epub:type'] == 'noteref' and link['href'] == '#n' and '<!-- keep -->' in str(link)
    assert parsed.find('code').string == 'world' and '__AYVU_TAG_0__ mundo' in output.decode()
    assert parsed.find('img')['alt'].startswith('mundo') and parsed.find('img')['src'] == 'a.png'


def test_source_holding_a_reserved_placeholder_is_refused(env):
    with pytest.raises(TranslationExecutionError, match='^reserved_placeholder_in_source$'):
        env.translate(document(cue('a', 'Odd __AYVU_TAG_0__ text', 1)))
