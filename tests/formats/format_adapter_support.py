import ast
import importlib
import logging
import os
import re
from dataclasses import replace
from pathlib import Path
import pytest
from ayvu.content.model import AdapterIdentity, Locator, UnitContent, build_document
from ayvu.formats.contracts import (
    MAX_DETECTION_BYTES, AliasVerdict, ContentScope, DetectionEvidence, DetectionOutcome, ExtractionRequest, ExtractionResult, FidelityAspect,
    FidelityClaim, FidelityLevel, FidelityRequirement, FindingSeverity, FormatCancelledError, FormatError, FormatInventory,
    FormatLimits, FormatMaturity, FormatOperation, FormatOperationError, InventoryDocument, FormatRequirements, FormatSignature, InspectionResult, MaterializationResult,
    OperationClaim, OperationSupport, PreviewRequest, ReconstructionRequest, UnitTranslation, ValidationFinding, ValidationResult, negotiate_format,
)
from ayvu.formats.local import LocalFormatSource, LocalOutputGuard
from ayvu.formats.service import FormatAdapter, FormatRegistration, FormatRegistry, FormatService
from tests.support.format_contract_env import PROJECT, CountingSource, FixedGuard, FormatEnv, FormatHarness
from tests.support.format_contract import FormatAdapterContract
from tests.support.format_fakes import CARDS_HEADER, CARDS_REGISTRATION, EVIDENCE, KIND, NOTES_REGISTRATION, CardsPorts, NotesPorts, all_fidelity, cards_adapter, cards_descriptor, notes_adapter, notes_descriptor, unsupported
from ayvu.kernel import JobFailure, JobFailureFamily
from tests.support.paths import PROJECT_ROOT

CARDS = CARDS_HEADER + b"#one\nfirst card\nsecond card\n#two\nthird card\n"


ROOT = PROJECT_ROOT


def registry_with(*pairs):
    registry = FormatRegistry()
    for registration, adapter in pairs:
        registry.register(registration, adapter)
    return registry


def service_for(adapter=None, registration=CARDS_REGISTRATION):
    return FormatService(registry_with((registration, adapter or cards_adapter())))


@pytest.fixture
def cards(tmp_path):
    path = tmp_path / "book.cards"
    path.write_bytes(CARDS)
    return path


def documents_of(path):
    return service_for().extract("fake_cards", LocalFormatSource(path), ExtractionRequest(PROJECT)).documents


def complete(documents):
    return tuple(UnitTranslation(unit.unit_id, "text") for document in documents for unit in document.units)


# Negotiation

def requirements(*fidelity, operations=(), scopes=frozenset(), experimental=False):
    return FormatRequirements(operations, tuple(FidelityRequirement(aspect, level) for aspect, level in fidelity), scopes, experimental)


def with_method(name, method):
    class Ports(CardsPorts):
        pass
    setattr(Ports, name, method)
    return Ports()


def extractor_returning(make):
    base = CardsPorts()
    return FormatAdapter(cards_descriptor(), inspector=base, extractor=with_method("extract", lambda self, context, request: make(context, request)),
                         previewer=base, reconstructor=base, validator=base)


def fake_document(context, request, **changes):
    values = dict(project_id=request.project_id, logical_key="one", adapter=cards_descriptor().adapter, schema="fake_card", version=3,
                  fingerprint=context.source_fingerprint)
    values.update(changes)
    return build_document(values["project_id"], values["logical_key"], values["adapter"], request.source_locale,
                          [UnitContent(Locator(values["schema"], values["version"], "1"), "text", KIND, (1,))], source_fingerprint=values["fingerprint"])


# Materializing

def materialize(tmp_path, cards, adapter=None, *, kind=ReconstructionRequest, guard=None, translations=None, requirements_=None, source=None):
    env = FormatEnv(tmp_path / "env")
    documents = documents_of(cards)
    request = kind(documents, complete(documents) if translations is None else translations, requirements_ or FormatRequirements(allow_experimental=True))
    publication = env.publication(guard)
    method = service_for(adapter).reconstruct if kind is ReconstructionRequest else service_for(adapter).preview
    return method("fake_cards", source or LocalFormatSource(cards), request, publication), env, publication


@pytest.fixture
def envdir(tmp_path):
    return tmp_path


class BrokenPublisher:
    def __init__(self, returned=None, error=None):
        self.returned, self.error = returned, error

    def publish(self, request, chunks, **options):
        list(chunks)
        if self.error:
            raise self.error
        return self.returned


def publish_with(tmp_path, cards, publisher):
    env = FormatEnv(tmp_path / "env")
    documents = documents_of(cards)
    return service_for().reconstruct("fake_cards", LocalFormatSource(cards), ReconstructionRequest(documents, complete(documents)),
                                     replace(env.publication(), artifacts=publisher))


# Detection

def detector(*pairs):
    return registry_with(*pairs)


def signed(name, signature=b"SAME", extensions=(".signed",)):
    base = CardsPorts()
    return (FormatRegistration(name, 2, (("fake_card", 3),)),
            FormatAdapter(cards_descriptor(adapter=AdapterIdentity(name, 2, 1), extensions=extensions, signatures=(FormatSignature(((0, signature),)),)),
                          inspector=base, extractor=base, previewer=base, reconstructor=base, validator=base))


def named(name, extensions=(".plain",), media_types=()):
    base = NotesPorts()
    return (FormatRegistration(name, 1, (("fake_line", 1),)),
            FormatAdapter(notes_descriptor(adapter=AdapterIdentity(name, 1, 1), extensions=extensions, media_types=media_types), inspector=base, extractor=base))


# Boundaries

CONTRACT_MODULES = ("formats/contracts", "formats/service")


# The compatibility map

MAP = ROOT / "docs" / "architecture" / "modules" / "formats-compatibility.md"


REQUIRED_ROWS = ("inspect", "language detection", "extract", "preview", "chapter selection", "metadata", "navigation", "image alt text", "review export", "review apply",
                 "rebuild", "validation", "archive preservation", "mimetype storage", "visible text selection", "html reconstruction", "jobs", "resume", "output path", "translation")


def map_rows():
    rows = {}
    for line in MAP.read_text().splitlines():
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if line.startswith("|") and len(cells) == 4 and not set(cells[0]) <= {"-", " "} and cells[0] != "Behavior":
            rows[cells[0]] = cells[1:]
    return rows


def resolve(reference):
    parts = reference.split(".")
    for split in range(len(parts), 1, -1):
        try:
            value = importlib.import_module(".".join(parts[:split]))
        except ImportError:
            continue
        for part in parts[split:]:
            value = getattr(value, part)
        return value
    raise ImportError(reference)
