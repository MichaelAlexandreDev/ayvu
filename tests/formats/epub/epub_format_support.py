import io
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile
import pytest
from bs4 import BeautifulSoup
from lxml import etree
from ayvu.formats.epub import adapter as epub_format, io as epub_io
from ayvu.v1 import epub as v1_epub
from ayvu.artifacts.model import ArtifactStoreError
from ayvu.v1.cache import TranslationCache
from ayvu.formats.epub.archive import EpubArchiveError
from ayvu.formats.epub.adapter import EPUB_REGISTRATION, epub_format_adapter
from ayvu.formats.contracts import (
    ContentScope, DetectionOutcome, ExtractionRequest, FidelityAspect, FidelityLevel, FindingSeverity, FormatOperation, OperationSupport, PreviewRequest,
    ReconstructionRequest, UnitTranslation,
)
from ayvu.formats.local import LocalFormatSource, LocalOutputGuard
from ayvu.formats.service import FormatRegistry, FormatService, format_safe_points
from ayvu.formats.epub.html_structure import html_unit_template
from tests.support.epub_fakes import OPF, TWO, XHTML, epub_bytes, members, opf_with, zip_bytes
from tests.support.format_contract_env import PROJECT, FormatEnv, FormatHarness, TriggerToken
from tests.support.format_contract import FormatAdapterContract
from ayvu.kernel import LanguagePair, TranslationOptions, JobFailureFamily

def translation_of(unit, index):
    return f"{html_unit_template(unit)} translated {index}"


def readable(data):
    with ZipFile(io.BytesIO(data)) as archive:
        return b"\n".join(archive.read(info) for info in archive.infolist())


ALL_SCOPES = frozenset(ContentScope)


@pytest.fixture
def service():
    registry = FormatRegistry()
    registry.register(EPUB_REGISTRATION, epub_format_adapter())
    return FormatService(registry)


def source_of(tmp_path, data=None, name="book.epub"):
    path = tmp_path / name
    path.write_bytes(epub_bytes() if data is None else data)
    return path, LocalFormatSource(path)


def extract(service, source, scopes=ALL_SCOPES, **options):
    return service.extract("epub", source, ExtractionRequest(PROJECT, scopes, **options))


def all_units(documents):
    return [unit for document in documents for unit in document.units]


def done(documents, change=lambda text: text):
    return tuple(UnitTranslation(unit.unit_id, change(html_unit_template(unit))) for unit in all_units(documents))


def members_of(data):
    with ZipFile(io.BytesIO(data)) as archive:
        return {info.filename: archive.read(info) for info in archive.infolist()}


def rebuild(service, env, source, documents, translations=None, *, kind=ReconstructionRequest, guard=None):
    publication = env.publication(guard)
    request = kind(documents, done(documents) if translations is None else translations)
    method = service.reconstruct if kind is ReconstructionRequest else service.preview
    result = method("epub", source, request, publication)
    return result, publication


def crash_at(env, stage):
    """Makes one step of publication fail, as a process dying at that moment would leave things."""
    service = env.artifacts
    if stage == "rename":
        original = service._payloads.publish

        def failing(*_args):
            raise ArtifactStoreError("crashed")
        service._payloads.publish = failing
        return lambda: setattr(service._payloads, "publish", original)
    original = service._repository.mark_ready

    def failing(*_args):
        raise ArtifactStoreError("crashed")
    service._repository.mark_ready = failing
    return lambda: setattr(service._repository, "mark_ready", original)
