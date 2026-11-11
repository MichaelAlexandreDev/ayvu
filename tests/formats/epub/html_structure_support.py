import ast
import random
from pathlib import Path
import pytest
from bs4 import BeautifulSoup
from ayvu.content.model import AdapterIdentity, ContentDocument, LocaleState, SourceLocale
from ayvu.formats.contracts import UnitTranslation
from ayvu.formats.epub.html_structure import (
    HTML_ADAPTER, HTML_LOCATOR_SCHEMA, HTML_LOCATOR_VERSION, HtmlStructureError, extract_html_document, extract_visible_text, html_unit_template,
    reconstruct_html,
)

PROJECT = "00000000-0000-4000-8000-000000000143"


XHTML = '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" xmlns:xlink="http://www.w3.org/1999/xlink">'


FIXTURES = {
    "plain": "<html><body><p>Hello world</p><p> spaced </p></body></html>",
    "nested": '<html><body><p>a <b>bold <i class="x">deep</i></b> <a href="u?a=1&amp;b=2" id="l">link</a> end</p></body></html>',
    "mixed": "<html><body>loose<p>inside</p>tail<div>more<p>x</p>after</div></body></html>",
    "ignored_inline": "<html><body><p>a <code>code()</code> b <kbd>K</kbd> <samp>s</samp></p></body></html>",
    "ignored_blocks": "<html><body><pre>keep</pre><script>x()</script><style>y{}</style><svg><text>t</text></svg><math><mi>m</mi></math><p>ok</p></body></html>",
    "namespaced": f'{XHTML}<body><section epub:type="chapter"><p><span epub:type="pagebreak" id="p1">one</span> <a xlink:href="#n" epub:type="noteref">two</a></p></section></body></html>',
    "comments": "<html><body><!-- out --><p>a<!-- split -->b</p><p>c <b>d<!-- in -->e<?pi data?></b></p></body></html>",
    "declarations": '<?xml version="1.0" encoding="utf-8"?><!DOCTYPE html><html><head><title>t</title></head><body><p>x</p></body></html>',
    "entities": "<html><body><p>a &amp; b &lt;c&gt;   café \U0001F600</p></body></html>",
    "voids": '<html><body><p>a<br/>b<span/><a id="z"/><wbr/></p><img src="i.png" alt="pic"/></body></html>',
    "malformed": "<html><body><p>a<b>b</p></body>",
    "whitespace": "<html><body><p>   </p>\n\n<p> x </p> <b> </b></body></html>",
    "inline_with_block": '<html><body><a href="u"><div>x</div></a></body></html>',
    "literal_token": "<html><body><p>__AYVU_TAG_0__ x <b>y</b> __AYVU_TAG_1__</p></body></html>",
    "equal_siblings": "<html><body><p>same</p><p>same</p><p>same</p></body></html>",
}


def extract(html, **options):
    return extract_html_document(html, PROJECT, "chapter.xhtml", **options)


def tokens_of(document):
    return [UnitTranslation(unit.unit_id, html_unit_template(unit)) for unit in document.units]


def serialized(html):
    return BeautifulSoup(html, "lxml-xml").encode(formatter="minimal")


def generated(seed):
    rng = random.Random(seed)
    words = ["world", "a &amp; b", "&lt;x&gt;", " pad ", "café", "__AYVU_TAG_1__", " ", "line\nbreak"]
    attributes = ['', ' id="a"', ' epub:type="noteref"', ' xlink:href="#n"', ' xml:lang="pt"', ' title="a &amp; b"']

    def inline(depth=0):
        roll = rng.random()
        if roll < .5 or depth > 3:
            return rng.choice(words)
        if roll < .6:
            return rng.choice(["<br/>", "<!-- c -->", "<?pi x?>", "<code>k()</code>", "<span/>", " ", "\n"])
        tag = rng.choice(["b", "i", "a", "span", "em", "sub"])
        return f"<{tag}{rng.choice(attributes)}>{''.join(inline(depth + 1) for _ in range(rng.randint(1, 3)))}</{tag}>"

    def block(depth=0):
        roll = rng.random()
        if roll < .1:
            tag = rng.choice(["pre", "script", "svg"])
            return f"<{tag}>keep</{tag}>"
        if roll < .2:
            return '<img src="i" alt="an image"/>'
        if roll < .3 and depth < 2:
            return f"<div>{''.join(block(depth + 1) for _ in range(2))}</div>"
        return f"<p>{''.join(inline() for _ in range(rng.randint(1, 4)))}</p>"

    return f"{XHTML}<body>{''.join(block() for _ in range(rng.randint(1, 5)))}</body></html>"


# Reconstruction

NESTED = '<html><body><p>a <b id="k">b <i>c</i></b> d</p><p>second <b>x</b></p></body></html>'


def reconstruct(texts, html=NESTED, **options):
    document = extract(html)
    return reconstruct_html(html, document, [UnitTranslation(unit.unit_id, text) for unit, text in zip(document.units, texts)], **options)


def template(index=0, html=NESTED):
    return html_unit_template(extract(html).units[index])
