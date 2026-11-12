from pathlib import Path

import pytest

from ayvu.v1.cache import TranslationCache
from ayvu.formats.epub.io import inspect_epub
from ayvu.v1.epub import apply_reviewed_epub, translate_epub
from ayvu.review.csv_import import ReviewImportData, ReviewImportError
from ayvu.kernel import LanguagePair, TranslationOptions

from tests.support.epub_io_support import FixedTranslator, OPENABLE_OPF, PrefixTranslator
from tests.support.epub_io_support import RICH_OPF, TITLE_BYTES, _book_with_opf
from tests.support.epub_io_support import _opf_of, _reviewed_title
from tests.support.epub_io_support import _translate_title, _without_declaration


def test_translating_the_title_changes_only_its_text(tmp_path: Path):
    output, report, _ = _translate_title(tmp_path, RICH_OPF)

    assert report.errors == []
    expected = RICH_OPF.replace(TITLE_BYTES, b'<dc:title id="t">PT:The Title</dc:title>')
    assert _without_declaration(_opf_of(output)) == _without_declaration(expected)


def test_translating_the_title_normalizes_only_what_the_parser_normalizes(tmp_path: Path):
    source = (
        b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n\n'
        b'<!-- top -->\n'
        b'<package xmlns="http://www.idpf.org/2007/opf" xmlns:dc="http://purl.org/dc/elements/1.1/" version="3.0" unique-identifier="id">'
        b"<metadata><dc:title>The Title</dc:title><dc:identifier id='id'>Caf&#233;</dc:identifier><dc:language>en</dc:language><dc:rights></dc:rights></metadata>"
        b'<manifest><item id="c1" href="text/one.xhtml" media-type="application/xhtml+xml"/></manifest>'
        b'<spine><itemref idref="c1"/></spine></package>\n'
    )
    output, report, _ = _translate_title(tmp_path, source)

    assert report.errors == []
    assert _opf_of(output) == (
        b"<?xml version='1.0' encoding='utf-8'?>\n"
        b'<!-- top --><package xmlns="http://www.idpf.org/2007/opf" xmlns:dc="http://purl.org/dc/elements/1.1/" version="3.0" unique-identifier="id">'
        b'<metadata><dc:title>PT:The Title</dc:title><dc:identifier id="id">Caf\xc3\xa9</dc:identifier><dc:language>en</dc:language><dc:rights/></metadata>'
        b'<manifest><item id="c1" href="text/one.xhtml" media-type="application/xhtml+xml"/></manifest>'
        b'<spine><itemref idref="c1"/></spine></package>'
    )


def test_translating_the_title_writes_an_opf_in_another_encoding_as_utf8(tmp_path: Path):
    source = OPENABLE_OPF.replace(b'encoding="utf-8"', b'encoding="iso-8859-1"').replace(b"An Author", b"Caf\xe9")
    output, report, _ = _translate_title(tmp_path, source)

    written = _opf_of(output)
    assert report.errors == []
    assert written.startswith(b"<?xml version='1.0' encoding='utf-8'?>") and "Café".encode() in written


def test_translating_the_title_adds_no_declaration_the_source_did_not_have(tmp_path: Path):
    source = _without_declaration(RICH_OPF)
    output, report, _ = _translate_title(tmp_path, source)

    assert report.errors == []
    assert _opf_of(output) == source.replace(TITLE_BYTES, b'<dc:title id="t">PT:The Title</dc:title>')


def test_translating_the_title_escapes_the_translation(tmp_path: Path):
    output, _, _ = _translate_title(tmp_path, RICH_OPF, change=lambda text: "O <Titulo> & cia")

    assert b"O &lt;Titulo&gt; &amp; cia</dc:title>" in _opf_of(output)
    assert inspect_epub(output).title == "O <Titulo> & cia"


def test_translating_the_title_accepts_a_comment_before_the_metadata(tmp_path: Path):
    source = RICH_OPF.replace(b"<metadata>", b"<!-- before --><metadata>", 1)
    output, report, _ = _translate_title(tmp_path, source)

    assert report.errors == []
    assert b"<!-- before --><metadata>" in _opf_of(output) and b"PT:The Title" in _opf_of(output)


@pytest.mark.parametrize(
    ("opf", "change", "options"),
    [
        pytest.param(RICH_OPF.replace(TITLE_BYTES, b""), lambda text: f"PT:{text}", {}, id="no-title"),
        pytest.param(RICH_OPF.replace(TITLE_BYTES, b'<dc:title id="t"/>'), lambda text: f"PT:{text}", {}, id="empty-title"),
        pytest.param(RICH_OPF.replace(TITLE_BYTES, b'<dc:title id="t">  </dc:title>'), lambda text: f"PT:{text}", {}, id="blank-title"),
        pytest.param(RICH_OPF, lambda text: f"PT:{text}", {"cache_only": True}, id="missing-translation"),
        pytest.param(RICH_OPF, lambda text: text, {}, id="same-translation"),
    ],
)
def test_the_opf_keeps_its_bytes_when_the_title_does_not_change(tmp_path: Path, opf, change, options):
    output, report, _ = _translate_title(tmp_path, opf, change=change, **options)

    assert report.errors == []
    assert _opf_of(output) == opf


def test_the_opf_keeps_its_bytes_without_translate_metadata(tmp_path: Path):
    source = _book_with_opf(tmp_path, RICH_OPF)
    output = tmp_path / "out.epub"
    options = TranslationOptions(language_pair=LanguagePair(source="en", target="pt"))
    with TranslationCache(tmp_path / "cache.sqlite") as cache:
        translate_epub(source, output, translator=PrefixTranslator(), cache=cache, options=options)

    assert _opf_of(output) == RICH_OPF


@pytest.mark.parametrize(
    ("title", "prolog"),
    [
        pytest.param(b"A<!-- c -->B", b"", id="comment"),
        pytest.param(b"<!-- c -->B", b"", id="leading-comment"),
        pytest.param(b"A<span>B</span>C", b"", id="element"),
        pytest.param(b"A<?keep this?>B", b"", id="processing-instruction"),
        pytest.param(b"A &e; B", b'<!DOCTYPE package [<!ENTITY e "E">]>\n', id="entity"),
    ],
)
def test_a_title_holding_other_nodes_stays_untouched_and_is_reported(tmp_path: Path, title, prolog):
    source = RICH_OPF.replace(TITLE_BYTES, b'<dc:title id="t">' + title + b"</dc:title>").replace(b"<package ", prolog + b"<package ", 1)
    output, report, translator = _translate_title(tmp_path, source)

    assert _opf_of(output) == source
    assert [error for error in report.errors if error.startswith("metadata title: ")]
    assert not [call for call in translator.calls if call.startswith(("A", "B"))]


def test_a_title_holding_other_nodes_stops_a_fail_fast_run(tmp_path: Path):
    source = RICH_OPF.replace(TITLE_BYTES, b'<dc:title id="t">A<!-- c -->B</dc:title>')
    with pytest.raises(ValueError, match="stays untranslated"):
        _translate_title(tmp_path, source, fail_fast=True)


def test_an_external_entity_in_the_title_is_never_read(tmp_path: Path):
    secret = tmp_path / "secret.txt"
    secret.write_text("canary-value", encoding="utf-8")
    prolog = b'<!DOCTYPE package [<!ENTITY e SYSTEM "' + secret.as_uri().encode() + b'">]>\n'
    source = RICH_OPF.replace(TITLE_BYTES, b'<dc:title id="t">A &e; B</dc:title>').replace(b"<package ", prolog + b"<package ", 1)
    output, report, translator = _translate_title(tmp_path, source)

    assert _opf_of(output) == source
    assert report.errors
    assert not [call for call in translator.calls if "canary-value" in call]


def test_a_translation_that_xml_cannot_hold_leaves_the_opf_valid_and_counts_once(tmp_path: Path):
    source = _book_with_opf(tmp_path, RICH_OPF)
    output = tmp_path / "out.epub"
    settings = TranslationOptions(language_pair=LanguagePair(source="en", target="pt"), translate_metadata=True)
    statuses: list[str] = []
    translator = FixedTranslator(lambda text: "a\x0bb" if text == "The Title" else text)
    with TranslationCache(tmp_path / "cache.sqlite") as cache:
        report = translate_epub(source, output, translator=translator, cache=cache, options=settings, on_text_processed=statuses.append)

    assert _opf_of(output) == RICH_OPF
    assert [error for error in report.errors if error.startswith("metadata title: ")]
    assert statuses.count("error") == 1
    assert statuses.count("translated") == len(translator.calls) - 1


@pytest.mark.parametrize("review", [
    _reviewed_title("Titulo <Revisado>"),
    _reviewed_title("The Title"),
    _reviewed_title("a\x0bb"),
])
def test_opf_title_v1_review_is_rejected_before_reconstruction(tmp_path: Path, review: ReviewImportData):
    source = _book_with_opf(tmp_path, RICH_OPF)
    output = tmp_path / "out.epub"
    with pytest.raises(ReviewImportError, match="^legacy_reexport_required$"):
        apply_reviewed_epub(source, output, review)

    assert not output.exists()
