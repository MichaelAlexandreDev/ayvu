"""Reading an EPUB's container without trusting it, and copying it while changing only what was asked.

Everything an archive states about itself is checked against documented limits before any
member is read without a bound. Failures are bounded codes, because a member name or a
parser's message can quote the book. This module holds no translation and no HTML traversal.
"""
from __future__ import annotations

import copy
import posixpath
import re
import unicodedata
import zlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import BinaryIO
from urllib.parse import unquote
from zipfile import ZIP_DEFLATED, ZIP_STORED, BadZipFile, LargeZipFile, ZipFile, ZipInfo

from lxml import etree

MIMETYPE_MEMBER = "mimetype"
MIMETYPE_CONTENT = b"application/epub+zip"
CONTAINER_MEMBER = "META-INF/container.xml"
ENCRYPTION_MEMBER = "META-INF/encryption.xml"
XHTML_MEDIA_TYPE = "application/xhtml+xml"
_COPY_BYTES = 1024 * 1024
_MIMETYPE_DATE = (1980, 1, 1, 0, 0, 0)
_FONT_OBFUSCATION = frozenset({"http://www.idpf.org/2008/embedding", "http://ns.adobe.com/pdf/enc#RC"})
_ENTITY_DECLARATIONS = (b"<!ENTITY", "<!ENTITY".encode("utf-16-le"), "<!ENTITY".encode("utf-16-be"))
_DRIVE = re.compile(r"[A-Za-z]:")
_SCHEME = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*:")

# Which failures mean a resource ran out, as opposed to a file that is simply not valid.
RESOURCE_CODES = frozenset({"source_too_large", "too_many_members", "member_too_large", "archive_too_large",
                            "suspicious_compression", "document_too_large", "member_name_too_long"})


class EpubArchiveError(ValueError):
    """Its message is always a bounded code, never a member name or book text."""


@dataclass(frozen=True)
class EpubLimits:
    """Set well above a normal book, to stop an archive built to exhaust the machine."""

    max_source_bytes: int = 8 * 1024 ** 3
    max_uncompressed_bytes: int = 4 * 1024 ** 3
    max_member_bytes: int = 1024 ** 3
    max_members: int = 20000
    max_document_bytes: int = 64 * 1024 ** 2
    max_package_bytes: int = 16 * 1024 ** 2
    max_compression_ratio: int = 200
    ratio_applies_above_bytes: int = 1024 ** 2
    max_name_chars: int = 1024


DEFAULT_LIMITS = EpubLimits()


@dataclass(frozen=True, repr=False)
class ManifestItem:
    item_id: str
    href: str
    media_type: str
    properties: tuple[str, ...]
    archive_path: str | None

    def __repr__(self) -> str:
        return f"ManifestItem(media_type={self.media_type!r})"

    @property
    def is_document(self) -> bool:
        return self.media_type == XHTML_MEDIA_TYPE

    @property
    def is_navigation(self) -> bool:
        return self.is_document and "nav" in self.properties


@dataclass(frozen=True, repr=False)
class EpubPackage:
    archive_path: str
    version: str | None
    title: str | None
    creators: tuple[str, ...]
    language: str | None
    items: tuple[ManifestItem, ...]
    spine: tuple[str, ...]

    def __repr__(self) -> str:
        return f"EpubPackage(items={len(self.items)}, spine={len(self.spine)})"

    @property
    def base(self) -> str:
        return posixpath.dirname(self.archive_path)

    @property
    def documents(self) -> tuple[ManifestItem, ...]:
        """In manifest order, which is the order the current flow numbers chapters in."""
        return tuple(item for item in self.items if item.is_document and item.archive_path is not None)


def resolve_internal_path(reference: str, base_dir: str) -> str | None:
    """The member a link inside a document points at, or None when it leaves the book."""
    if not reference or reference.startswith("#"):
        return None
    if "://" in reference or reference.startswith(("mailto:", "tel:", "data:")):
        return None
    file_part = reference.split("#", 1)[0]
    if not file_part:
        return None
    joined = posixpath.join(base_dir, file_part) if base_dir else file_part
    return posixpath.normpath(joined)


def _name_is_safe(name: str) -> bool:
    """Refuses what could escape a directory or mean different things to different readers."""
    if not name or "\\" in name or _DRIVE.match(name):
        return False
    if any(ord(character) < 32 or ord(character) == 127 for character in name):
        return False
    # An absolute name splits into an empty first part, so it is refused here as well.
    parts = name.rstrip("/").split("/")
    return all(part not in ("", ".", "..") for part in parts)


def _canonical(name: str) -> str:
    """The OCF forbids names that differ only by case or Unicode form."""
    return unicodedata.normalize("NFC", name.rstrip("/")).casefold()


def declares_entities(data: bytes) -> bool:
    return any(marker in data for marker in _ENTITY_DECLARATIONS)


def _parse_xml(data: bytes, code: str) -> etree._Element:
    if declares_entities(data):
        raise EpubArchiveError("entity_declaration")
    parser = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False, huge_tree=False)
    try:
        return etree.fromstring(data, parser)
    except (etree.XMLSyntaxError, ValueError):
        raise EpubArchiveError(code) from None


def _local(element: etree._Element) -> str:
    tag = element.tag
    return etree.QName(tag).localname if isinstance(tag, str) else ""


def _child(parent: etree._Element, name: str) -> etree._Element | None:
    return next((child for child in parent if _local(child) == name), None)


def parse_package_for_edit(data: bytes) -> etree._Element:
    """Entities stay references and CDATA stays CDATA, so writing it back changes as little as a parser can."""
    parser = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False, huge_tree=False, strip_cdata=False)
    return etree.fromstring(data, parser)


def package_title(root: etree._Element) -> etree._Element | None:
    metadata = next((element for element in root.iter() if _local(element) == "metadata"), None)
    return None if metadata is None else _child(metadata, "title")


def serialize_package(root: etree._Element, source: bytes) -> bytes:
    """Declares what the source declared. Outside the edited text, only what a parser normalizes differs."""
    return etree.tostring(root.getroottree(), encoding="utf-8", xml_declaration=source.lstrip().startswith(b"<?xml"))


def _manifest_path(base: str, href: str) -> str | None:
    """The member a manifest href names. None means it is outside the book, such as a remote resource."""
    clean = href.split("#", 1)[0].split("?", 1)[0]
    if _SCHEME.match(clean):
        return None
    path = posixpath.normpath(posixpath.join(base, unquote(clean)) if base else unquote(clean))
    if path == ".." or path.startswith("../") or not _name_is_safe(path):
        raise EpubArchiveError("unsafe_manifest_href")
    return path


class EpubArchive:
    """A validated EPUB. The caller owns the stream, which is only ever read."""

    def __init__(self, stream: BinaryIO, *, limits: EpubLimits = DEFAULT_LIMITS,
                 checkpoint: Callable[[], None] | None = None) -> None:
        self._limits = limits
        self._checkpoint = checkpoint or (lambda: None)
        try:
            size = stream.seek(0, 2)
            stream.seek(0)
            if size > limits.max_source_bytes:
                raise EpubArchiveError("source_too_large")
            self._zip = ZipFile(stream, "r")
        except (BadZipFile, LargeZipFile, OSError, ValueError, EOFError) as error:
            if isinstance(error, EpubArchiveError):
                raise
            raise EpubArchiveError("corrupt_archive") from None
        try:
            self._members = {info.filename: info for info in self._check_members()}
            self.mimetype_is_valid = self._mimetype_is_valid()
            self.package = self._read_package(self._read_rootfile())
            self._check_encryption()
        except BaseException:
            self._zip.close()
            raise

    def __repr__(self) -> str:
        return f"EpubArchive(members={len(self._members)})"

    def __enter__(self) -> "EpubArchive":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._zip.close()

    @property
    def comment(self) -> bytes:
        return self._zip.comment

    def names(self) -> tuple[str, ...]:
        return tuple(info.filename for info in self._zip.infolist())

    def infos(self) -> tuple[ZipInfo, ...]:
        """Copies, in source order, so that nothing a caller does reaches the open archive."""
        return tuple(copy.copy(info) for info in self._zip.infolist())

    def _check_members(self) -> list[ZipInfo]:
        limits = self._limits
        infos = self._zip.infolist()
        if len(infos) > limits.max_members:
            raise EpubArchiveError("too_many_members")
        seen: set[str] = set()
        total = 0
        for info in infos:
            self._checkpoint()
            # ``filename`` is cut at a NUL by ``zipfile``, so the original is what must be judged.
            original = info.orig_filename
            if len(original) > limits.max_name_chars:
                raise EpubArchiveError("member_name_too_long")
            if not _name_is_safe(original):
                raise EpubArchiveError("unsafe_member_name")
            canonical = _canonical(original)
            if canonical in seen:
                raise EpubArchiveError("duplicate_member")
            seen.add(canonical)
            if info.flag_bits & 0x1:
                raise EpubArchiveError("encrypted_member")
            if info.compress_type not in (ZIP_STORED, ZIP_DEFLATED):
                raise EpubArchiveError("unsupported_compression")
            if info.file_size > limits.max_member_bytes:
                raise EpubArchiveError("member_too_large")
            total += info.file_size
            if total > limits.max_uncompressed_bytes:
                raise EpubArchiveError("archive_too_large")
            if (info.file_size > limits.ratio_applies_above_bytes
                    and info.file_size > limits.max_compression_ratio * max(info.compress_size, 1)):
                raise EpubArchiveError("suspicious_compression")
        return infos

    def read(self, name: str, limit: int | None = None) -> bytes:
        """One member, never past the bound, whatever its header claimed."""
        info = self._members.get(name)
        if info is None:
            raise EpubArchiveError("missing_member")
        bound = self._limits.max_document_bytes if limit is None else limit
        if info.file_size > bound:
            raise EpubArchiveError("document_too_large")
        try:
            with self._zip.open(info) as member:
                # The reader stops at the size the header declared, which was just checked.
                return member.read()
        except (BadZipFile, OSError, ValueError, EOFError, RuntimeError, NotImplementedError, zlib.error):
            raise EpubArchiveError("corrupt_member") from None

    def read_document(self, name: str) -> bytes:
        data = self.read(name)
        if declares_entities(data):
            raise EpubArchiveError("entity_declaration")
        return data

    def _mimetype_is_valid(self) -> bool:
        """A defect here carries no risk and books do have it, so a copy corrects it instead of refusing the book."""
        infos = self._zip.infolist()
        return (MIMETYPE_MEMBER in self._members and infos[0].filename == MIMETYPE_MEMBER and infos[0].compress_type == ZIP_STORED
                and infos[0].file_size == len(MIMETYPE_CONTENT)
                and self.read(MIMETYPE_MEMBER, len(MIMETYPE_CONTENT)) == MIMETYPE_CONTENT)

    def _read_rootfile(self) -> str:
        if CONTAINER_MEMBER not in self._members:
            raise EpubArchiveError("missing_container")
        root = _parse_xml(self.read(CONTAINER_MEMBER, self._limits.max_package_bytes), "malformed_container")
        rootfile = next((element for element in root.iter() if _local(element) == "rootfile"), None)
        path = (rootfile.get("full-path", "") if rootfile is not None else "").strip()
        if not path or not _name_is_safe(path):
            raise EpubArchiveError("malformed_container")
        if path not in self._members:
            raise EpubArchiveError("missing_package")
        return path

    def _read_package(self, path: str) -> EpubPackage:
        root = _parse_xml(self.read(path, self._limits.max_package_bytes), "malformed_package")
        metadata, manifest, spine = _child(root, "metadata"), _child(root, "manifest"), _child(root, "spine")
        if _local(root) != "package" or metadata is None or manifest is None or spine is None:
            raise EpubArchiveError("malformed_package")
        base = posixpath.dirname(path)
        items: list[ManifestItem] = []
        ids: set[str] = set()
        targets: set[str] = set()
        for element in manifest:
            if _local(element) != "item":
                continue
            item_id, href, media_type = element.get("id", ""), element.get("href", "").strip(), element.get("media-type", "")
            if not item_id or not href or not media_type or item_id in ids:
                raise EpubArchiveError("malformed_package")
            ids.add(item_id)
            target = _manifest_path(base, href)
            if target is not None:
                if target in targets:
                    raise EpubArchiveError("malformed_package")
                targets.add(target)
                if target not in self._members:
                    raise EpubArchiveError("manifest_member_missing")
            items.append(ManifestItem(item_id, href, media_type, tuple(element.get("properties", "").split()), target))
        idrefs = tuple(element.get("idref", "") for element in spine if _local(element) == "itemref")
        if any(idref not in ids for idref in idrefs):
            raise EpubArchiveError("malformed_package")
        title = next((child.text for child in metadata if _local(child) == "title"), None)
        creators = tuple(child.text for child in metadata if _local(child) == "creator" and child.text)
        language = next((child.text for child in metadata if _local(child) == "language"), None)
        return EpubPackage(path, root.get("version"), title, creators, language, tuple(items), idrefs)

    def _check_encryption(self) -> None:
        """Ayvu does not remove DRM. Obfuscated fonts are not protected content, and are left alone."""
        if ENCRYPTION_MEMBER not in self._members:
            return
        root = _parse_xml(self.read(ENCRYPTION_MEMBER, self._limits.max_package_bytes), "malformed_encryption")
        documents = {item.archive_path for item in self.package.documents} | {self.package.archive_path}
        for data in (element for element in root.iter() if _local(element) == "EncryptedData"):
            method = next((element for element in data.iter() if _local(element) == "EncryptionMethod"), None)
            reference = next((element for element in data.iter() if _local(element) == "CipherReference"), None)
            if method is None or reference is None:
                raise EpubArchiveError("malformed_encryption")
            target = posixpath.normpath(unquote(reference.get("URI", "")))
            if target in documents or method.get("Algorithm") not in _FONT_OBFUSCATION:
                raise EpubArchiveError("encrypted_content")

    def copy_to(self, output: BinaryIO, replacements: Mapping[str, bytes],
                checkpoint: Callable[[], None] | None = None) -> None:
        """Every member in source order, changing only the content of the ones named.

        Name, order, timestamp, attributes and compression stay as they are, except that a defective
        ``mimetype`` comes out correct and first. The stream must be seekable: writing a ZIP to one
        that is not sets flags on ``mimetype`` that readers refuse.
        """
        between = checkpoint or (lambda: None)
        if replacements.keys() - self._members.keys():
            raise EpubArchiveError("unknown_replacement")
        if replacements.keys() & {MIMETYPE_MEMBER, CONTAINER_MEMBER, ENCRYPTION_MEMBER}:
            raise EpubArchiveError("protected_member")
        with ZipFile(output, "w", allowZip64=True) as archive:
            archive.comment = self._zip.comment
            if not self.mimetype_is_valid:
                archive.writestr(ZipInfo(MIMETYPE_MEMBER, date_time=_MIMETYPE_DATE), MIMETYPE_CONTENT, ZIP_STORED)
            for info in self.infos():
                between()
                if not self.mimetype_is_valid and info.filename == MIMETYPE_MEMBER:
                    continue
                if info.filename in replacements:
                    archive.writestr(info, replacements[info.filename])
                    continue
                self._copy_member(archive, info)

    def _copy_member(self, archive: ZipFile, info: ZipInfo) -> None:
        source = self._members[info.filename]
        try:
            with self._zip.open(source) as reader, archive.open(info, "w", force_zip64=info.file_size >= 2 ** 31) as writer:
                while chunk := reader.read(_COPY_BYTES):
                    writer.write(chunk)
        except (BadZipFile, EOFError, RuntimeError, NotImplementedError, zlib.error):
            raise EpubArchiveError("corrupt_member") from None
