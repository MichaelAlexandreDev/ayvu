"""Format-neutral content documents and translation units.

Immutable values with deterministic identity. The core never interprets a
locator: adapters own its meaning. Target locale and translation or review state
are absent on purpose, since they belong to execution and review, not to the source.

IDs are domain-separated hashes that include the project UUID. They contain no
text, but the UUID is a salt and not a secret key, so an ID is not safe to publish
outside the project: someone knowing the UUID, the document key and the locator
could confirm a guess about a very short document.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Callable, Iterable
from uuid import UUID

CONTENT_SCHEMA_VERSION = 1
IDENTITY_VERSION = 1
MAX_UNITS = 262144
MAX_SOURCE_CHARS = 1024 * 1024
MAX_DOCUMENT_BYTES = 256 * 1024 * 1024
MAX_REFERENCE_CHARS = 4096
MAX_EXTENSION_BYTES = 64 * 1024
MAX_LIST_ITEMS = 64
MAX_ORDER_DEPTH = 8
_SLUG = re.compile(r"[a-z][a-z0-9_]{0,63}")
_LOCALE = re.compile(r"[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*")
_FINGERPRINT = re.compile(r"sha256:[0-9a-f]{64}")
_ABSOLUTE_PATH = re.compile(r"(?:[/\\]|[A-Za-z]:[/\\])")

SEMANTIC_KINDS = {name: frozenset({1}) for name in (
    "paragraph", "heading", "title", "alt_text", "metadata", "note", "table_cell", "subtitle_cue", "layout_region",
)}
PROTECTED_KINDS = frozenset({"markup", "placeholder", "url", "identifier", "formula"})
CONTEXT_RELATIONS = frozenset({"previous", "next", "parent", "caption_of", "note_of"})


class ContentError(ValueError):
    """A bounded error: messages never include caller data."""

    def __init__(self, code: str, *, unit_index: int | None = None) -> None:
        self.code = code
        self.unit_index = unit_index
        super().__init__(code)


class ContentValidationError(ContentError):
    pass


class ContentIdentityError(ContentError):
    pass


class ContentVersionError(ContentError):
    """Something required is at a version this code does not understand."""


class ContentSerializationError(ContentError):
    pass


def _is_slug(value: object) -> bool:
    return isinstance(value, str) and _SLUG.fullmatch(value) is not None


def _encodable(value: str) -> bool:
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _is_version(value: object) -> bool:
    return type(value) is int and 1 <= value < 2**31


def _valid_reference(value: object) -> bool:
    """Opaque to the core, but never an absolute path, a URL or free of bounds."""
    return (isinstance(value, str) and 1 <= len(value) <= MAX_REFERENCE_CHARS and _encodable(value)
            and not any(ord(character) < 32 or ord(character) == 127 for character in value)
            and not _ABSOLUTE_PATH.match(value) and "://" not in value)


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _hash(kind: str, *parts: object) -> str:
    # A JSON array frames every part, so no two different part lists hash the same text.
    text = _canonical(["ayvu.content", IDENTITY_VERSION, kind, *parts])
    return hashlib.sha256(text.encode("ascii")).hexdigest()


@dataclass(frozen=True, repr=False)
class AdapterIdentity:
    namespace: str
    adapter_version: int
    segmenter_version: int

    def __post_init__(self) -> None:
        if (not _is_slug(self.namespace) or not _is_version(self.adapter_version)
                or not _is_version(self.segmenter_version)):
            raise ContentValidationError("invalid_adapter")

    def __repr__(self) -> str:
        return (f"AdapterIdentity(namespace={self.namespace!r}, adapter_version={self.adapter_version}, "
                f"segmenter_version={self.segmenter_version})")


@dataclass(frozen=True, repr=False)
class Locator:
    """A structural position only its adapter can interpret."""

    schema: str
    schema_version: int
    value: str

    def __post_init__(self) -> None:
        if not _is_slug(self.schema) or not _is_version(self.schema_version) or not _valid_reference(self.value):
            raise ContentValidationError("invalid_locator")

    def __repr__(self) -> str:
        return f"Locator(schema={self.schema!r}, schema_version={self.schema_version})"

    def _fields(self) -> list:
        return [self.schema, self.schema_version, self.value]


@dataclass(frozen=True)
class SemanticKind:
    name: str
    version: int = 1

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or type(self.version) is not int:
            raise ContentValidationError("invalid_semantic_kind")
        if self.version not in SEMANTIC_KINDS.get(self.name, ()):
            raise ContentVersionError("unsupported_semantic_kind")


class LocaleState(StrEnum):
    EXPLICIT = "explicit"
    INHERITED = "inherited"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class SourceLocale:
    state: LocaleState
    value: str | None = None

    def __post_init__(self) -> None:
        if type(self.state) is not LocaleState:
            raise ContentValidationError("invalid_locale")
        if self.state is LocaleState.EXPLICIT:
            if not isinstance(self.value, str) or not _LOCALE.fullmatch(self.value):
                raise ContentValidationError("invalid_locale")
        elif self.value is not None:
            raise ContentValidationError("invalid_locale")

    def _fields(self) -> list:
        return [self.state.value, self.value]


INHERITED_LOCALE = SourceLocale(LocaleState.INHERITED)
UNKNOWN_LOCALE = SourceLocale(LocaleState.UNKNOWN)


@dataclass(frozen=True)
class ProtectedPart:
    """A half-open span of code points in the source text. It knows nothing of provider chunks."""

    start: int
    end: int
    kind: str

    def __post_init__(self) -> None:
        if type(self.start) is not int or type(self.end) is not int or not 0 <= self.start < self.end:
            raise ContentValidationError("invalid_protected_part")
        if not isinstance(self.kind, str) or self.kind not in PROTECTED_KINDS:
            raise ContentVersionError("unsupported_protected_kind")


def _positive_int(value: object) -> bool:
    return type(value) is int and 1 <= value < 2**63


def _boolean(value: object) -> bool:
    return type(value) is bool


CONSTRAINTS: dict[tuple[str, int], Callable[[object], bool]] = {
    ("max_chars", 1): _positive_int,
    ("max_lines", 1): _positive_int,
    ("max_duration_ms", 1): _positive_int,
    ("preserve_line_breaks", 1): _boolean,
}


@dataclass(frozen=True)
class Constraint:
    name: str
    value: int | bool
    version: int = 1

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or type(self.version) is not int:
            raise ContentValidationError("invalid_constraint")
        validator = CONSTRAINTS.get((self.name, self.version))
        if validator is None:
            raise ContentVersionError("unsupported_constraint")
        if not validator(self.value):
            raise ContentValidationError("invalid_constraint")


@dataclass(frozen=True, repr=False)
class ContextReference:
    """A relation to another unit of the same document, named by its locator while building."""

    relation: str
    target: Locator

    def __post_init__(self) -> None:
        if type(self.target) is not Locator or not isinstance(self.relation, str):
            raise ContentValidationError("invalid_context_reference")
        if self.relation not in CONTEXT_RELATIONS:
            raise ContentVersionError("unsupported_context_relation")

    def __repr__(self) -> str:
        return f"ContextReference(relation={self.relation!r})"


@dataclass(frozen=True, repr=False)
class Extension:
    """Stored and returned verbatim. It is outside identity, and no core code reads it."""

    namespace: str
    version: int
    payload_json: str
    required: bool = False

    def __post_init__(self) -> None:
        if not _is_slug(self.namespace) or not _is_version(self.version) or type(self.required) is not bool:
            raise ContentValidationError("invalid_extension")
        if self.required:
            # No required extension exists yet, so none can be honored.
            raise ContentVersionError("unsupported_required_extension")
        if not isinstance(self.payload_json, str) or len(self.payload_json) > MAX_EXTENSION_BYTES:
            raise ContentValidationError("invalid_extension")
        try:
            canonical = _canonical(json.loads(self.payload_json))
        except (ValueError, RecursionError):
            raise ContentValidationError("invalid_extension") from None
        if canonical != self.payload_json:
            raise ContentValidationError("invalid_extension")

    def __repr__(self) -> str:
        return f"Extension(namespace={self.namespace!r}, version={self.version})"

    @classmethod
    def of(cls, namespace: str, version: int, value: object) -> Extension:
        try:
            payload = _canonical(value)
        except (TypeError, ValueError, RecursionError):
            raise ContentValidationError("invalid_extension") from None
        return cls(namespace, version, payload)


def _sorted_unique(items: tuple, kind: type, key: Callable[[object], object], code: str) -> tuple:
    if type(items) is not tuple or len(items) > MAX_LIST_ITEMS or any(type(item) is not kind for item in items):
        raise ContentValidationError(code)
    ordered = tuple(sorted(items, key=key))
    if len({key(item) for item in ordered}) != len(ordered):
        raise ContentValidationError(code)
    return ordered


def _validate_order_key(value: object) -> None:
    if (type(value) is not tuple or not 1 <= len(value) <= MAX_ORDER_DEPTH
            or any(type(part) is not int or not 0 <= part < 2**63 for part in value)):
        raise ContentValidationError("invalid_order_key")


def _validate_protected_parts(parts: object, text_length: int) -> None:
    if type(parts) is not tuple or any(type(part) is not ProtectedPart for part in parts):
        raise ContentValidationError("invalid_protected_part")
    previous_end = 0
    for part in parts:
        if part.end > text_length:
            raise ContentValidationError("invalid_protected_part")
        if part.start < previous_end:
            raise ContentValidationError("overlapping_protected_parts")
        previous_end = part.end


@dataclass(frozen=True, repr=False)
class UnitContent:
    """What an adapter states about one unit. Identity is computed when a document is built."""

    locator: Locator
    source_text: str
    kind: SemanticKind
    order_key: tuple[int, ...]
    source_locale: SourceLocale = INHERITED_LOCALE
    protected_parts: tuple[ProtectedPart, ...] = ()
    constraints: tuple[Constraint, ...] = ()
    context: tuple[ContextReference, ...] = ()
    extensions: tuple[Extension, ...] = ()

    def __post_init__(self) -> None:
        if type(self.locator) is not Locator:
            raise ContentValidationError("invalid_locator")
        if (not isinstance(self.source_text, str) or not self.source_text
                or len(self.source_text) > MAX_SOURCE_CHARS or not _encodable(self.source_text)):
            raise ContentValidationError("invalid_source_text")
        if type(self.kind) is not SemanticKind:
            raise ContentValidationError("invalid_semantic_kind")
        _validate_order_key(self.order_key)
        if type(self.source_locale) is not SourceLocale:
            raise ContentValidationError("invalid_locale")
        _validate_protected_parts(self.protected_parts, len(self.source_text))
        object.__setattr__(self, "constraints", _sorted_unique(
            self.constraints, Constraint, lambda item: (item.name, item.version), "invalid_constraint"))
        object.__setattr__(self, "context", _sorted_unique(
            self.context, ContextReference, lambda item: (item.relation, *item.target._fields()),
            "invalid_context_reference"))
        object.__setattr__(self, "extensions", _sorted_unique(
            self.extensions, Extension, lambda item: (item.namespace, item.version), "invalid_extension"))

    def __repr__(self) -> str:
        return f"UnitContent(kind={self.kind.name!r}, order_key={self.order_key!r})"


@dataclass(frozen=True)
class ContextLink:
    relation: str
    target_unit_id: str


@dataclass(frozen=True, repr=False)
class TranslationUnit:
    unit_id: str
    source_digest: str
    content: UnitContent
    context_links: tuple[ContextLink, ...] = ()

    def __repr__(self) -> str:
        return f"TranslationUnit(unit_id={self.unit_id!r}, kind={self.content.kind.name!r})"

    @property
    def source_text(self) -> str:
        return self.content.source_text

    @property
    def order_key(self) -> tuple[int, ...]:
        return self.content.order_key

    @property
    def locator(self) -> Locator:
        return self.content.locator


def source_digest(project_id: str, text: str) -> str:
    """For divergence and integrity checks only: not a public ID, and no proof of safety to log."""
    return f"scd:v{IDENTITY_VERSION}:{_hash('source', project_id, text)}"


def _document_id(project_id: str, namespace: str, logical_key: str) -> str:
    # Adapter versions are left out: upgrading an adapter is a new revision of the same document.
    return f"doc:v{IDENTITY_VERSION}:{_hash('document', project_id, namespace, logical_key)}"


def _unit_fingerprint(content: UnitContent, digest: str) -> dict:
    """Everything that makes a revision different. Extensions are deliberately absent."""
    return {
        "constraints": [[item.name, item.version, item.value] for item in content.constraints],
        "context": [[item.relation, *item.target._fields()] for item in content.context],
        "digest": digest,
        "kind": [content.kind.name, content.kind.version],
        "locale": content.source_locale._fields(),
        "locator": content.locator._fields(),
        "order_key": list(content.order_key),
        "protected_parts": [[item.start, item.end, item.kind] for item in content.protected_parts],
    }


def _revision_id(document_id: str, adapter: AdapterIdentity, source_locale: SourceLocale,
                 fingerprints: list[dict]) -> str:
    return f"rev:v{IDENTITY_VERSION}:" + _hash("revision", document_id, adapter.adapter_version,
                                               adapter.segmenter_version, source_locale._fields(), fingerprints)


def _unit_id(document_id: str, revision_id: str, adapter: AdapterIdentity, locator: Locator) -> str:
    return f"unit:v{IDENTITY_VERSION}:" + _hash("unit", document_id, revision_id, adapter.namespace,
                                                adapter.adapter_version, adapter.segmenter_version, locator._fields())


def _resolve_units(project_id: str, document_id: str, adapter: AdapterIdentity, source_locale: SourceLocale,
                   contents: tuple[UnitContent, ...]) -> tuple[str, tuple[TranslationUnit, ...]]:
    """The only place identity is computed. Duplicates and collisions fail: no suffix repairs them."""
    if len(contents) > MAX_UNITS:
        raise ContentValidationError("too_many_units")
    locators: set[tuple] = set()
    orders: set[tuple[int, ...]] = set()
    for index, content in enumerate(contents):
        if content.order_key in orders:
            raise ContentValidationError("duplicate_order_key", unit_index=index)
        key = tuple(content.locator._fields())
        if key in locators:
            raise ContentValidationError("duplicate_locator", unit_index=index)
        orders.add(content.order_key)
        locators.add(key)
    digests = [source_digest(project_id, content.source_text) for content in contents]
    revision_id = _revision_id(document_id, adapter, source_locale,
                               [_unit_fingerprint(content, digest) for content, digest in zip(contents, digests)])
    ids = {tuple(content.locator._fields()): _unit_id(document_id, revision_id, adapter, content.locator)
           for content in contents}
    if len(set(ids.values())) != len(contents):
        raise ContentIdentityError("unit_id_collision")
    units = []
    for index, (content, digest) in enumerate(zip(contents, digests)):
        links = []
        for reference in content.context:
            target = tuple(reference.target._fields())
            if target not in ids:
                raise ContentValidationError("missing_context_reference", unit_index=index)
            if target == tuple(content.locator._fields()):
                raise ContentValidationError("self_context_reference", unit_index=index)
            links.append(ContextLink(reference.relation, ids[target]))
        units.append(TranslationUnit(ids[tuple(content.locator._fields())], digest, content, tuple(links)))
    return revision_id, tuple(units)


@dataclass(frozen=True, repr=False)
class ContentDocument:
    """One immutable extracted revision. Constructing one recomputes and checks every ID."""

    project_id: str
    logical_key: str
    adapter: AdapterIdentity
    source_locale: SourceLocale
    document_id: str
    revision_id: str
    units: tuple[TranslationUnit, ...]
    source_fingerprint: str | None = None
    extensions: tuple[Extension, ...] = ()
    identity_version: int = IDENTITY_VERSION
    schema_version: int = CONTENT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != CONTENT_SCHEMA_VERSION:
            raise ContentVersionError("unsupported_content_schema")
        if type(self.identity_version) is not int or self.identity_version != IDENTITY_VERSION:
            raise ContentVersionError("unsupported_identity_version")
        _validate_document_inputs(self.project_id, self.logical_key, self.adapter, self.source_locale,
                                  self.source_fingerprint)
        if type(self.units) is not tuple or any(type(unit) is not TranslationUnit for unit in self.units):
            raise ContentValidationError("invalid_units")
        object.__setattr__(self, "extensions", _sorted_unique(
            self.extensions, Extension, lambda item: (item.namespace, item.version), "invalid_extension"))
        contents = tuple(unit.content for unit in self.units)
        if any(type(content) is not UnitContent for content in contents):
            raise ContentValidationError("invalid_units")
        if any(left.order_key >= right.order_key for left, right in zip(contents, contents[1:])):
            raise ContentValidationError("invalid_unit_order")
        document_id = _document_id(self.project_id, self.adapter.namespace, self.logical_key)
        revision_id, units = _resolve_units(self.project_id, document_id, self.adapter, self.source_locale, contents)
        if (self.document_id, self.revision_id, self.units) != (document_id, revision_id, units):
            raise ContentIdentityError("identity_mismatch")

    def __repr__(self) -> str:
        return f"ContentDocument(document_id={self.document_id!r}, revision_id={self.revision_id!r}, units={len(self.units)})"


def _validate_document_inputs(project_id: object, logical_key: object, adapter: object, source_locale: object,
                              source_fingerprint: object) -> None:
    try:
        if not isinstance(project_id, str) or str(UUID(project_id)) != project_id:
            raise ValueError
    except (ValueError, AttributeError, TypeError):
        raise ContentValidationError("invalid_project_identity") from None
    if not _valid_reference(logical_key):
        raise ContentValidationError("invalid_logical_key")
    if type(adapter) is not AdapterIdentity:
        raise ContentValidationError("invalid_adapter")
    # A document has no parent to inherit a locale from.
    if type(source_locale) is not SourceLocale or source_locale.state is LocaleState.INHERITED:
        raise ContentValidationError("invalid_locale")
    if source_fingerprint is not None and (not isinstance(source_fingerprint, str)
                                           or not _FINGERPRINT.fullmatch(source_fingerprint)):
        raise ContentValidationError("invalid_source_fingerprint")


def build_document(project_id: str, logical_key: str, adapter: AdapterIdentity, source_locale: SourceLocale,
                   units: Iterable[UnitContent], *, source_fingerprint: str | None = None,
                   extensions: tuple[Extension, ...] = ()) -> ContentDocument:
    """All or nothing. Input order is irrelevant: ``order_key`` alone decides reading order.

    ``source_fingerprint`` and ``extensions`` are recorded outside identity, so
    rewriting a container without changing what was extracted keeps every ID.
    """
    _validate_document_inputs(project_id, logical_key, adapter, source_locale, source_fingerprint)
    contents = tuple(units)
    for index, content in enumerate(contents):
        if type(content) is not UnitContent:
            raise ContentValidationError("invalid_units", unit_index=index)
    contents = tuple(sorted(contents, key=lambda content: content.order_key))
    document_id = _document_id(project_id, adapter.namespace, logical_key)
    revision_id, resolved = _resolve_units(project_id, document_id, adapter, source_locale, contents)
    return ContentDocument(project_id, logical_key, adapter, source_locale, document_id, revision_id, resolved,
                           source_fingerprint, extensions)


def _encode_extensions(extensions: tuple[Extension, ...]) -> list:
    return [{"namespace": item.namespace, "payload_json": item.payload_json, "required": item.required,
             "version": item.version} for item in extensions]


def _encode_locator(locator: Locator) -> dict:
    return {"schema": locator.schema, "schema_version": locator.schema_version, "value": locator.value}


def _encode_unit(unit: TranslationUnit) -> dict:
    content = unit.content
    return {
        "constraints": [{"name": item.name, "value": item.value, "version": item.version}
                        for item in content.constraints],
        "context": [{"relation": item.relation, "target": _encode_locator(item.target)} for item in content.context],
        "extensions": _encode_extensions(content.extensions),
        "kind": {"name": content.kind.name, "version": content.kind.version},
        "locator": _encode_locator(content.locator),
        "order_key": list(content.order_key),
        "protected_parts": [{"end": item.end, "kind": item.kind, "start": item.start}
                            for item in content.protected_parts],
        "source_digest": unit.source_digest,
        "source_locale": {"state": content.source_locale.state.value, "value": content.source_locale.value},
        "source_text": content.source_text,
        "unit_id": unit.unit_id,
    }


def encode_document(document: ContentDocument) -> bytes:
    """One canonical byte string per document. This is a private artifact: it holds the book's text."""
    payload = _canonical({
        "adapter": {"adapter_version": document.adapter.adapter_version, "namespace": document.adapter.namespace,
                    "segmenter_version": document.adapter.segmenter_version},
        "document_id": document.document_id,
        "extensions": _encode_extensions(document.extensions),
        "identity_version": document.identity_version,
        "logical_key": document.logical_key,
        "project_id": document.project_id,
        "revision_id": document.revision_id,
        "schema_version": document.schema_version,
        "source_fingerprint": document.source_fingerprint,
        "source_locale": {"state": document.source_locale.state.value, "value": document.source_locale.value},
        "units": [_encode_unit(unit) for unit in document.units],
    }).encode("ascii")
    if len(payload) > MAX_DOCUMENT_BYTES:
        raise ContentSerializationError("document_too_large")
    return payload


def _fields(data: object, *names: str) -> tuple:
    if type(data) is not dict or set(data) != set(names):
        raise ValueError
    return tuple(data[name] for name in names)


def _list(data: object) -> list:
    if type(data) is not list:
        raise ValueError
    return data


def _decode_locator(data: object) -> Locator:
    return Locator(*_fields(data, "schema", "schema_version", "value"))


def _decode_extensions(data: object) -> tuple[Extension, ...]:
    extensions = []
    for item in _list(data):
        namespace, payload_json, required, version = _fields(item, "namespace", "payload_json", "required", "version")
        extensions.append(Extension(namespace, version, payload_json, required))
    return tuple(extensions)


def _decode_locale(data: object) -> SourceLocale:
    state, value = _fields(data, "state", "value")
    return SourceLocale(LocaleState(state), value)


def _decode_unit(data: object) -> UnitContent:
    (constraints, context, extensions, kind, locator, order_key, protected_parts, _digest, source_locale, source_text,
     _unit) = _fields(data, "constraints", "context", "extensions", "kind", "locator", "order_key", "protected_parts",
                      "source_digest", "source_locale", "source_text", "unit_id")
    return UnitContent(
        locator=_decode_locator(locator), source_text=source_text, kind=SemanticKind(*_fields(kind, "name", "version")),
        order_key=tuple(_list(order_key)), source_locale=_decode_locale(source_locale),
        protected_parts=tuple(ProtectedPart(*_fields(item, "start", "end", "kind")) for item in _list(protected_parts)),
        constraints=tuple(Constraint(*_fields(item, "name", "value", "version")) for item in _list(constraints)),
        context=tuple(ContextReference(item_relation, _decode_locator(item_target)) for item_relation, item_target in
                      (_fields(item, "relation", "target") for item in _list(context))),
        extensions=_decode_extensions(extensions))


def decode_document(payload: bytes) -> ContentDocument:
    """Strict inverse of ``encode_document``: rebuilds everything and accepts only its exact output."""
    if type(payload) is not bytes or len(payload) > MAX_DOCUMENT_BYTES:
        raise ContentSerializationError("invalid_document")
    try:
        data = json.loads(payload.decode("ascii"))
        if type(data) is dict:
            if data.get("schema_version", CONTENT_SCHEMA_VERSION) != CONTENT_SCHEMA_VERSION:
                raise ContentVersionError("unsupported_content_schema")
            if data.get("identity_version", IDENTITY_VERSION) != IDENTITY_VERSION:
                raise ContentVersionError("unsupported_identity_version")
        (adapter, _document, extensions, _identity, logical_key, project_id, _revision, _schema, source_fingerprint,
         source_locale, units) = _fields(
            data, "adapter", "document_id", "extensions", "identity_version", "logical_key", "project_id",
            "revision_id", "schema_version", "source_fingerprint", "source_locale", "units")
        document = build_document(
            project_id, logical_key,
            AdapterIdentity(*_fields(adapter, "namespace", "adapter_version", "segmenter_version")),
            _decode_locale(source_locale), [_decode_unit(item) for item in _list(units)],
            source_fingerprint=source_fingerprint, extensions=_decode_extensions(extensions))
        if encode_document(document) != payload:
            raise ValueError
        return document
    except ContentVersionError:
        raise
    except (ValueError, TypeError, RecursionError):
        raise ContentSerializationError("invalid_document") from None
