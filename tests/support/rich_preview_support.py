"""A thirteen-document EPUB and real transient wiring with observable effects."""
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
import re
import sqlite3
import socket
from zipfile import ZipFile, ZipInfo

import pytest

from ayvu.composition.cli import _BookHooks, _BookRequest, prepare_preview, run_translation_preflight
from ayvu.composition.builders.translate_project import epub_format_service, v2_cache_path
from ayvu.formats import ContentScope, ExtractionRequest
from ayvu.formats.local import LocalFormatSource
from ayvu.kernel import LanguagePair, OutputPlan, TranslationOptions
from ayvu.knowledge.cache.repository import SQLiteExactCache
from ayvu.projects.store import ProjectJsonRepository
from ayvu.providers import Capability, ProviderRequirements, ProviderRoute
from ayvu.providers.libretranslate.adapter import LIBRETRANSLATE_PRODUCER, libretranslate_snapshot
from ayvu.translation import resolve_plan
from ayvu.translation.document_contracts import _template
from tests.support.cli_translate_project_support import _book, _document
from tests.support.cli_rich_runtime_support import Transport
from tests.support.transient_translation_support import deny_runtime, exact_entry
from tests.knowledge.cache.read_only_support import snapshot

ORDER = (7, 1, 13, 2, 12, 3, 11, 4, 10, 5, 9, 6, 8)
KEYS = tuple(f"OEBPS/c{index:02}.xhtml" for index in ORDER)
MODELS = {"libretranslate": "local-model-2026"}
HOOKS = _BookHooks(*([lambda *args: None] * 4))


def preview_book(path, *, repeated=False, distinct_first=False):
    """Manifest, spine and ZIP order deliberately disagree; navigation is interleaved."""
    _book(path)
    with ZipFile(path) as archive:
        members = {item.filename: archive.read(item) for item in archive.infolist()}
    opf = members["OEBPS/content.opf"].decode()
    manifest = ''.join(f'<item id="c{i}" href="c{i:02}.xhtml" media-type="application/xhtml+xml"/>' for i in ORDER)
    nav = '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>'
    opf = re.sub(r'<manifest>.*?</manifest>', f'<manifest>{manifest}{nav}</manifest>', opf)
    spine = ''.join(f'<itemref idref="c{i}"/>' for i in reversed(ORDER))
    opf = re.sub(r'<spine>.*?</spine>', f'<spine>{spine}</spine>', opf)
    members = {key: value for key, value in members.items() if key not in {"OEBPS/c1.xhtml", "OEBPS/c2.xhtml"}}
    members["OEBPS/content.opf"] = opf.encode()
    members["OEBPS/nav.xhtml"] = members["OEBPS/nav.xhtml"].replace(b'c1.xhtml', b'c07.xhtml')
    for i in reversed(ORDER):
        text = "Repeated reader." if repeated else f"Document {i:02}."
        if distinct_first and i == ORDER[0]:
            text = "Cached reader."
        members[f"OEBPS/c{i:02}.xhtml"] = _document(f'<p>{text}</p>').replace('<title>T</title>', '<title/>').encode()
    with ZipFile(path, "w") as archive:
        for name, value in members.items():
            archive.writestr(ZipInfo(name, (2024, 1, 1, 0, 0, 0)), value)
    return path


def members(path):
    with ZipFile(path) as archive:
        return {item.filename: archive.read(item) for item in archive.infolist()}


class PreviewTransport(Transport):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.closed = 0

    def close(self):
        self.closed += 1


def book_request(root, source, *, options=None, cancellation=None, overwrite=False, glossary=None,
                 requests_per_second=None):
    options = options or TranslationOptions(LanguagePair("en", "pt"), max_documents=12)
    def prepare():
        return prepare_preview(translator_models=MODELS, translator_name="libretranslate", options=options,
            epub_path=source, cache_path=root / "cache.sqlite", glossary_path=glossary,
            url="http://localhost:5000", timeout=30, retries=0, requests_per_second=requests_per_second,
            retry_backoff=0.5, retry_backoff_max=8, language_pair=options.language_pair, dry_run=False,
            processing_dir=root / "processing", preflight=run_translation_preflight, on_route=lambda _: None)
    return _BookRequest(source, OutputPlan.for_preview(source, default_dir=root / "Preview"), overwrite,
        options, root / "cache.sqlite", prepare, translator_models=MODELS, preview=True, cancellation=cancellation)


def seed_preview(book, *, first_only=False):
    prepared = book.prepare()
    project = ProjectJsonRepository(prepared.processing_dir).create(prepared.request)
    base = prepared.online.translator
    plan = resolve_plan(libretranslate_snapshot(base, datetime.now(UTC)),
        ProviderRequirements((Capability.PLAIN_TEXT_INPUT, Capability.PLAIN_TEXT_OUTPUT)),
        replace(LIBRETRANSLATE_PRODUCER, model="declared:local-model-2026"), ProviderRoute("en", "pt"),
        chunk_limit=book.options.chunk_limit)
    base.close()
    writer = SQLiteExactCache(v2_cache_path(book.cache_path))
    documents = epub_format_service().extract("epub", LocalFormatSource(book.epub_path),
        ExtractionRequest(project.project_id, frozenset({ContentScope.BODY}))).documents
    for document in documents[:1] if first_only else documents:
        for unit in document.units:
            template, _ = _template(unit)
            writer.put_if_absent(exact_entry(project, plan, unit.source_text, "PT:" + template))
    return project, plan


@contextmanager
def preview_guard(monkeypatch, root, destination, *, published=True):
    """Deny durable constructors, capture all SQL and permit only the preview output."""
    allowed = (
        "ayvu.providers.libretranslate.client.LibreTranslateTranslator.__init__",
        "ayvu.providers.libretranslate.client.create_translator", "ayvu.translation.preflight.create_translator",
        "ayvu.translation.preflight.CliProviderSession", "ayvu.providers.session.CliProviderSession.__init__",
        "ayvu.providers.libretranslate.adapter.LibreTranslateProvider.__init__", "requests.Session")
    from _pytest.monkeypatch import derive_importpath
    originals = [(target, getattr(*derive_importpath(target, raising=True)[::-1])) for target in allowed]
    before, statements, connections = snapshot(root), [], []
    original_connect = sqlite3.connect
    def connect(database, *args, **kwargs):
        assert kwargs.get("uri") and str(database).endswith("?mode=ro")
        connection = original_connect(database, *args, **kwargs)
        connection.set_trace_callback(statements.append)
        connections.append(connection)
        return connection
    with monkeypatch.context() as patch:
        deny_runtime(patch)
        for target, original in originals:
            patch.setattr(target, original)
        patch.setattr(socket, "getaddrinfo", lambda *args: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 5000))])
        patch.setattr(sqlite3, "connect", connect)
        yield statements
    after = snapshot(root)
    output = str(destination.relative_to(root))
    if not published:
        assert after == before
    else:
        for name, value in before.items():
            if name != output and value[-1] is not None:
                assert after[name] == value, name
        added = after.keys() - before.keys()
        assert all(name == output or (after[name][-1] is None and Path(name) in destination.relative_to(root).parents)
                   for name in added), added
    assert not any(re.search(r"\b(INSERT|UPDATE|DELETE|CREATE|REPLACE|VACUUM|ATTACH|DETACH)\b|PRAGMA\s+\w+\s*=|BEGIN\s+IMMEDIATE", sql, re.I)
                   for sql in statements), statements
    for connection in connections:
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            connection.execute("SELECT 1")
