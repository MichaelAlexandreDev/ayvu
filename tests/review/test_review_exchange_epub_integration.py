from __future__ import annotations

import hashlib
import io
import socket
import sqlite3
from pathlib import Path
from zipfile import ZipFile

from bs4 import BeautifulSoup

from ayvu.artifacts.model import Artifact, ArtifactRequest, ArtifactState
from ayvu.formats.epub.adapter import EPUB_REGISTRATION, epub_format_adapter
from ayvu.formats.contracts import ContentScope, ExtractionRequest
from ayvu.formats.service import FormatRegistry, FormatService
from ayvu.formats.epub.html_structure import html_unit_template, reconstruct_html
from ayvu.projects.model import ProjectManifest, ProjectRequest
from ayvu.review.exchange_codec import decode_review_package, encode_review_package
from ayvu.review.exchange import export_review_package
from ayvu.review.exchange_mapping import map_review_package
from tests.support.epub_fakes import epub_bytes

from tests.review.review_exchange_support import ARTIFACT_ID, DESCRIPTOR, JOB_ID, MemorySource, NOW
from tests.review.review_exchange_support import PROJECT_ID
from tests.review.review_exchange_support import mark_reviewed


def test_production_epub_extraction_review_mapping_and_staged_html_reconstruction(monkeypatch):
    xhtml = (
        b'<?xml version="1.0"?><html xmlns="http://www.w3.org/1999/xhtml" '
        b'xmlns:epub="http://www.idpf.org/2007/ops"><head><title>t</title></head><body>'
        b'<p>Hello <strong>bold <em>nested</em></strong> and <code>Ctrl+S</code>.</p>'
        b'<div>Loose <p>mixed <a epub:type="noteref" href="#n">content</a></p> tail</div>'
        b'</body></html>')
    source_bytes = epub_bytes(replace={"OEBPS/text/one.xhtml": xhtml})
    source = MemorySource(source_bytes)
    project_sha = hashlib.sha256(source_bytes).hexdigest()
    project = ProjectManifest.create(ProjectRequest("/secret/book.epub", len(source_bytes), project_sha, "en", "pt"), PROJECT_ID)
    registry = FormatRegistry()
    registry.register(EPUB_REGISTRATION, epub_format_adapter())
    service = FormatService(registry)
    result = service.extract("epub", source, ExtractionRequest(PROJECT_ID, frozenset({ContentScope.BODY})))
    assert result.failure is None
    documents = result.documents
    artifact = Artifact(ARTIFACT_ID, ArtifactRequest(
        PROJECT_ID, JOB_ID, "raw_results", "translate", 1, "sha256:" + "e" * 64, "application/json"),
        ArtifactState.READY, 3, NOW, NOW, 321, "sha256:" + "f" * 64)
    machine = {unit.unit_id: html_unit_template(unit) + " machine" for doc in documents for unit in doc.units}
    package = export_review_package(project=project, machine_artifact=artifact, descriptor=DESCRIPTOR,
                                    format_version="3.0", documents=documents, machine_translations=machine)
    imported = decode_review_package(encode_review_package(package))
    edited = mark_reviewed(imported, lambda text: text.replace("Hello", "Olá"))

    # Instrument real effect ports and the reconstructor. The mapper must stop
    # before any of them; only the explicit staged step below reconstructs HTML.
    transport_calls = []
    socket_calls = []
    publisher_calls = []
    provenance_calls = []
    event_calls = []
    reconstruction_calls = []
    real_reconstruct = reconstruct_html

    def instrumented_reconstruct(*args, **kwargs):
        reconstruction_calls.append(1)
        return real_reconstruct(*args, **kwargs)

    monkeypatch.setattr("requests.Session.request", lambda *args, **kwargs: transport_calls.append((args, kwargs)))
    monkeypatch.setattr(socket.socket, "connect", lambda *args, **kwargs: socket_calls.append((args, kwargs)))
    monkeypatch.setattr("ayvu.artifacts.service.ArtifactService.publish",
                        lambda *args, **kwargs: publisher_calls.append((args, kwargs)))
    monkeypatch.setattr("ayvu.provenance.store.SqliteProvenanceRepository.append",
                        lambda *args, **kwargs: provenance_calls.append((args, kwargs)))
    monkeypatch.setattr("ayvu.jobs.execution.InMemoryJobEventLog.emit",
                        lambda *args, **kwargs: event_calls.append((args, kwargs)))
    monkeypatch.setattr("ayvu.formats.epub.html_structure.reconstruct_html", instrumented_reconstruct)
    monkeypatch.setattr(Path, "open", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("unexpected file IO")))
    monkeypatch.setattr(sqlite3, "connect", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("unexpected persistence")))
    plan = map_review_package(edited, project=project, machine_artifact=artifact, descriptor=DESCRIPTOR,
                              format_version="3.0", documents=documents, machine_translations=machine)
    assert reconstruction_calls == []
    assert publisher_calls == [] and transport_calls == [] and socket_calls == []
    assert provenance_calls == [] and event_calls == []
    with ZipFile(io.BytesIO(source_bytes)) as archive:
        original_html = archive.read(documents[0].logical_key)
    staged_html = instrumented_reconstruct(original_html, documents[0], tuple(
        item for item in plan.translations if item.unit_id in {unit.unit_id for unit in documents[0].units}))

    assert reconstruction_calls == [1]
    assert publisher_calls == [] and transport_calls == [] and socket_calls == []
    assert provenance_calls == [] and event_calls == []
    parsed = BeautifulSoup(staged_html, "lxml-xml")
    assert parsed.find("script") is None
    assert parsed.code.get_text() == "Ctrl+S"
    assert parsed.strong.find("em") is not None
    assert parsed.a.get("epub:type") == "noteref" and parsed.a.get("href") == "#n"
    assert "Olá" in parsed.get_text() and "tail" in parsed.get_text()
