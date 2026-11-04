"""Two invented formats, so nothing in the contract can lean on a real one.

``fake_notes`` is read-only and has no signature. ``fake_cards`` round-trips,
has a signature and several documents. Their descriptors differ because their
abilities do, and the contract suite reads only the descriptors.
"""
from __future__ import annotations

from ayvu.content import AdapterIdentity, Locator, SemanticKind, UnitContent, build_document
from ayvu.format_adapter import (
    ContentScope, FidelityAspect, FidelityClaim, FidelityLevel, FindingSeverity, FormatDescriptor, FormatInventory, FormatLimits,
    FormatMaturity, FormatOperation, FormatSignature, OperationClaim, OperationSupport, ValidationFinding,
)
from ayvu.format_service import FormatAdapter, FormatRegistration

EVIDENCE = ("contract:format_adapter_v1",)
CARDS_HEADER = b"CARDS1\n"
KIND = SemanticKind("paragraph", 1)


def unsupported(*operations):
    return tuple(OperationClaim(operation, OperationSupport.UNSUPPORTED) for operation in operations)


def all_fidelity(level, evidence=()):
    return tuple(FidelityClaim(aspect, level, evidence) for aspect in FidelityAspect)


def notes_descriptor(**changes):
    values = dict(
        adapter=AdapterIdentity("fake_notes", 1, 1), locator_schema="fake_line", locator_version=1,
        operations=(OperationClaim(FormatOperation.INSPECT, OperationSupport.SUPPORTED, EVIDENCE),
                    OperationClaim(FormatOperation.EXTRACT, OperationSupport.SUPPORTED, EVIDENCE, ("notes_between_lines",)),
                    *unsupported(FormatOperation.PREVIEW, FormatOperation.RECONSTRUCT, FormatOperation.VALIDATE)),
        fidelity=all_fidelity(FidelityLevel.NOT_APPLICABLE), extensions=(".notes",), media_types=("text/x-notes",),
        format_versions=("1",), content_scopes=frozenset({ContentScope.BODY}), limits=FormatLimits(max_source_bytes=1024 * 1024),
        maturity=FormatMaturity.EXPERIMENTAL)
    return FormatDescriptor(**{**values, **changes})


def _lines(context):
    with context.source.open() as stream:
        return stream.read().decode("utf-8").split("\n")


class NotesPorts:
    def inspect(self, context):
        lines = [line for line in _lines(context) if line]
        return FormatInventory(document_count=1, resource_count=0, format_version="1", title=lines[0] if lines else None)

    def extract(self, context, request):
        units = []
        for index, line in enumerate(_lines(context)):
            context.checkpoint("notes_between_lines")
            if line:
                units.append(UnitContent(Locator("fake_line", 1, str(index)), line, KIND, (index,)))
        if request.selection is not None and "notes" not in request.selection:
            return ()
        return (build_document(request.project_id, "notes", notes_descriptor().adapter, request.source_locale, units,
                               source_fingerprint=context.source_fingerprint),)


def notes_adapter():
    ports = NotesPorts()
    return FormatAdapter(notes_descriptor(), inspector=ports, extractor=ports)


NOTES_REGISTRATION = FormatRegistration("fake_notes", 1, "fake_line", 1)


def cards_descriptor(**changes):
    fidelity = {aspect: FidelityClaim(aspect, FidelityLevel.NOT_APPLICABLE) for aspect in FidelityAspect}
    fidelity[FidelityAspect.STRUCTURE] = FidelityClaim(FidelityAspect.STRUCTURE, FidelityLevel.PRESERVED, EVIDENCE)
    fidelity[FidelityAspect.METADATA] = FidelityClaim(FidelityAspect.METADATA, FidelityLevel.BEST_EFFORT, EVIDENCE)
    fidelity[FidelityAspect.STYLES] = FidelityClaim(FidelityAspect.STYLES, FidelityLevel.LOST, EVIDENCE)
    fidelity[FidelityAspect.LINKS] = FidelityClaim(FidelityAspect.LINKS, FidelityLevel.UNKNOWN)
    values = dict(
        adapter=AdapterIdentity("fake_cards", 2, 1), locator_schema="fake_card", locator_version=3,
        operations=(OperationClaim(FormatOperation.INSPECT, OperationSupport.SUPPORTED, EVIDENCE),
                    OperationClaim(FormatOperation.EXTRACT, OperationSupport.SUPPORTED, EVIDENCE, ("cards_between_documents",)),
                    OperationClaim(FormatOperation.PREVIEW, OperationSupport.EXPERIMENTAL, (), ("cards_before_render",)),
                    OperationClaim(FormatOperation.RECONSTRUCT, OperationSupport.SUPPORTED, EVIDENCE, ("cards_before_render", "cards_after_render")),
                    OperationClaim(FormatOperation.VALIDATE, OperationSupport.SUPPORTED, EVIDENCE, ("cards_before_validate",))),
        fidelity=tuple(fidelity.values()), extensions=(".cards",), signatures=(FormatSignature(((0, CARDS_HEADER),)),),
        format_versions=("1", "2"), content_scopes=frozenset({ContentScope.BODY, ContentScope.METADATA}), maturity=FormatMaturity.STABLE)
    return FormatDescriptor(**{**values, **changes})


def _sections(context):
    """Returns the lines of the file, and for each card line its document and locator."""
    lines = _lines(context)
    if not lines or f"{lines[0]}\n".encode() != CARDS_HEADER:
        return None, {}
    placed, document = {}, None
    for index, line in enumerate(lines[1:], start=1):
        if line.startswith("#"):
            document = line[1:]
        elif line and document:
            placed[index] = document
    return lines, placed


class CardsPorts:
    def inspect(self, context):
        lines, placed = _sections(context)
        return FormatInventory(document_count=len(set(placed.values())), resource_count=0, format_version="2", title="A card title")

    def extract(self, context, request):
        lines, placed = _sections(context)
        documents = []
        for name in dict.fromkeys(placed.values()):
            context.checkpoint("cards_between_documents")
            if request.selection is not None and name not in request.selection:
                continue
            units = [UnitContent(Locator("fake_card", 3, str(index)), lines[index], KIND, (index,))
                     for index, owner in placed.items() if owner == name]
            documents.append(build_document(request.project_id, name, cards_descriptor().adapter, request.source_locale, units,
                                            source_fingerprint=context.source_fingerprint))
        return tuple(documents)

    def _render(self, context, request):
        lines, _ = _sections(context)
        translated = {item.unit_id: item.text for item in request.translations}
        for document in request.documents:
            for unit in document.units:
                if unit.unit_id in translated:
                    lines[int(unit.content.locator.value)] = translated[unit.unit_id]
        return "\n".join(lines).encode("utf-8")

    def preview(self, context, request):
        context.checkpoint("cards_before_render")
        yield self._render(context, request)

    def reconstruct(self, context, request):
        context.checkpoint("cards_before_render")
        rendered = self._render(context, request)
        yield rendered[:4]
        context.checkpoint("cards_after_render")
        yield rendered[4:]

    def validate(self, context):
        context.checkpoint("cards_before_validate")
        lines, placed = _sections(context)
        if lines is None:
            return (ValidationFinding(FindingSeverity.ERROR, "missing_header"),)
        return () if placed else (ValidationFinding(FindingSeverity.WARNING, "no_cards"),)


def cards_adapter():
    ports = CardsPorts()
    return FormatAdapter(cards_descriptor(), inspector=ports, extractor=ports, previewer=ports, reconstructor=ports, validator=ports)


CARDS_REGISTRATION = FormatRegistration("fake_cards", 2, "fake_card", 3)
