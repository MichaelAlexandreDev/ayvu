"""Local, bounded publication planning for a sequential translation batch.

Paths stay in this adapter; its objects deliberately do not render them in reprs.
No directories are created until an auxiliary file is actually written.
"""
from __future__ import annotations

from .cli_contracts import BatchOutputError

import os
import stat
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Literal

from .artifact import ArtifactConflictError
from .domain import OutputPlan, automatic_output_candidate

MAX_CANDIDATES = 10_000




@dataclass(frozen=True, repr=False)
class OutputIntent:
    source: Path
    output: OutputPlan
    report_dir: Path
    report_stem: str
    missing_stem: str | None = None
    overwrite: bool = False


@dataclass(frozen=True, repr=False)
class _Identity:
    canonical: str
    inode: tuple[int, int] | None
    revision: tuple[int, int, int] | None
    ancestor: tuple[str, int, int]
    case_insensitive: bool


def _case_insensitive(parent: Path, device: int) -> bool:
    # Probe existing directory names only: planning must not create anything.
    current = parent
    while current != current.parent:
        try:
            info = current.stat()
            if info.st_dev != device:
                break
            alternate = current.name.swapcase()
            if alternate != current.name:
                try:
                    other = (current.parent / alternate).stat()
                except FileNotFoundError:
                    return False
                return (info.st_dev, info.st_ino) == (other.st_dev, other.st_ino)
        except OSError:
            break
        current = current.parent
    return os.path.normcase("A") == os.path.normcase("a")


def _identity(path: Path) -> _Identity:
    try:
        canonical = os.path.normcase(os.path.realpath(os.path.abspath(os.path.expanduser(path))))
        try:
            st = os.stat(path)
            inode = (st.st_dev, st.st_ino)
            revision = (st.st_size, st.st_mtime_ns, st.st_ctime_ns)
        except FileNotFoundError:
            inode = None
            revision = None
        parent = Path(canonical).parent
        while True:
            try:
                st = parent.stat()
                break
            except FileNotFoundError:
                if parent == parent.parent:
                    raise
                parent = parent.parent
        return _Identity(canonical, inode, revision, (str(parent), st.st_dev, st.st_ino),
                         _case_insensitive(parent, st.st_dev))
    except (OSError, ValueError):
        raise BatchOutputError("output_identity_unavailable") from None


def _same(a: _Identity, b: _Identity) -> bool:
    return (a.canonical == b.canonical
            or ((a.case_insensitive or b.case_insensitive) and a.canonical.casefold() == b.canonical.casefold())
            or (a.inode is not None and a.inode == b.inode))


def _unchanged(path: Path, old: _Identity) -> bool:
    current = _identity(path)
    try:
        parent = os.stat(old.ancestor[0])
    except OSError:
        return False
    return (current.canonical == old.canonical and current.inode == old.inode and current.revision == old.revision
            and (parent.st_dev, parent.st_ino) == old.ancestor[1:])


def _valid_stem(value: str) -> bool:
    return bool(value) and value not in (".", "..") and Path(value).name == value and "\x00" not in value


@dataclass(repr=False)
class AcceptedItem:
    _batch: AcceptedBatchPlan = field(repr=False)
    _index: int

    @property
    def output(self) -> OutputPlan:
        return self._batch._intents[self._index].output

    @property
    def execution_output(self) -> OutputPlan:
        """The accepted canonical spelling used in project/resume fingerprints."""
        identity = self._batch._outputs[self._index]
        return self.output if identity is None else replace(self.output, path=Path(identity.canonical))

    @property
    def overwrite(self) -> bool:
        return self._batch._intents[self._index].overwrite

    @property
    def planning_failure(self) -> str | None:
        return self._batch._planning_failures.get(self._index)

    def revalidate(self) -> None:
        self._batch._revalidate(self._index)

    def publication_guard(self) -> None:
        try:
            self.revalidate()
        except BatchOutputError:
            raise ArtifactConflictError("destination_changed") from None

    def write_auxiliary(self, kind: Literal["report", "missing"], content: str) -> Path:
        return self._batch._write_auxiliary(self._index, kind, content)

    def mark_published(self) -> None:
        self._batch._published[self._index] = _identity(self.output.path)


@dataclass(repr=False)
class AcceptedBatchPlan:
    _intents: tuple[OutputIntent, ...]
    _sources: tuple[_Identity, ...]
    _outputs: tuple[_Identity | None, ...]
    _report_dirs: list[_Identity]
    _auxiliary: list[_Identity] = field(default_factory=list)
    _published: dict[int, _Identity] = field(default_factory=dict)
    _planned_aux: dict[tuple[int, str], _Identity] = field(default_factory=dict)
    _planning_failures: dict[int, str] = field(default_factory=dict)

    @property
    def items(self) -> tuple[AcceptedItem, ...]:
        return tuple(AcceptedItem(self, i) for i in range(len(self._intents)))

    def _revalidate(self, index: int) -> None:
        for intent, original in zip(self._intents, self._sources):
            if _identity(intent.source) != original:
                raise BatchOutputError("source_changed")
        seen: list[_Identity] = []
        for i, (intent, original) in enumerate(zip(self._intents, self._outputs)):
            if original is None:
                continue
            current = _identity(intent.output.path)
            # A completed earlier item may now occupy its reserved name.
            expected = self._published.get(i, original)
            if not _unchanged(intent.output.path, expected):
                raise BatchOutputError("destination_changed")
            if any(_same(current, source) for source in self._sources):
                raise BatchOutputError("source_collision")
            if any(_same(current, aux) for aux in self._auxiliary):
                raise BatchOutputError("destination_collision")
            if any(_same(current, previous) for previous in seen):
                raise BatchOutputError("destination_collision")
            seen.append(current)

    def _check_report_dir(self, index: int) -> None:
        old = self._report_dirs[index]
        current = _identity(self._intents[index].report_dir)
        try:
            ancestor = os.stat(old.ancestor[0])
        except OSError:
            raise BatchOutputError("report_directory_changed") from None
        if (current.canonical != old.canonical
                or (ancestor.st_dev, ancestor.st_ino) != old.ancestor[1:]):
            raise BatchOutputError("report_directory_changed")
        if old.inode is None and current.inode is not None:
            try:
                if not stat.S_ISDIR(os.stat(self._intents[index].report_dir).st_mode):
                    raise BatchOutputError("report_directory_changed")
            except OSError:
                raise BatchOutputError("report_directory_changed") from None
            for i, previous in enumerate(self._report_dirs):
                if previous.inode is None and previous.canonical == old.canonical:
                    self._report_dirs[i] = current
        elif current.inode != old.inode:
            raise BatchOutputError("report_directory_changed")

    def _write_auxiliary(self, index: int, kind: str, content: str) -> Path:
        intent = self._intents[index]
        stem, suffix = ((intent.report_stem, ".md") if kind == "report" else (intent.missing_stem, ".txt"))
        if stem is None or kind not in ("report", "missing"):
            raise BatchOutputError("unplanned_publication")
        self._revalidate(index)
        self._check_report_dir(index)
        try:
            intent.report_dir.mkdir(parents=True, exist_ok=True)
            self._check_report_dir(index)
            for candidate_index in range(1, MAX_CANDIDATES + 1):
                self._revalidate(index)
                path = automatic_output_candidate(intent.report_dir, stem, suffix, candidate_index)
                identity = _identity(path)
                reserved = (*self._sources, *(x for x in self._outputs if x is not None), *self._auxiliary,
                            *(value for key, value in self._planned_aux.items() if key != (index, kind)))
                if any(_same(identity, other) for other in reserved):
                    continue
                try:
                    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
                    self._revalidate(index)
                    self._check_report_dir(index)
                    fd = os.open(path, flags, 0o666)
                except FileExistsError:
                    continue
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    stream.write(content)
                self._auxiliary.append(_identity(path))
                self._planned_aux.pop((index, kind), None)
                return path
        except OSError:
            raise BatchOutputError("auxiliary_write_failed") from None
        raise BatchOutputError("auxiliary_allocation_exhausted")


class BatchOutputPolicy:
    """Resolve the complete output set without mutating any local path."""

    def plan(self, items: tuple[OutputIntent, ...]) -> AcceptedBatchPlan:
        if not 1 <= len(items) <= 100:
            raise BatchOutputError("invalid_batch_size")
        for intent in items:
            if not _valid_stem(intent.report_stem) or (intent.missing_stem is not None and not _valid_stem(intent.missing_stem)):
                raise BatchOutputError("invalid_auxiliary_name")
        sources = tuple(_identity(item.source) for item in items)
        outputs = tuple(None if item.output.dry_run else _identity(item.output.path) for item in items)
        for i, (intent, identity) in enumerate(zip(items, outputs)):
            if identity is None:
                continue
            if any(_same(identity, source) for source in sources):
                raise BatchOutputError("source_collision")
            if any(other is not None and _same(identity, other) for other in outputs[:i]):
                raise BatchOutputError("destination_collision")
            if identity.inode is not None and not intent.overwrite:
                raise BatchOutputError("output_exists")
        report_dirs = [_identity(item.report_dir) for item in items]
        result = AcceptedBatchPlan(items, sources, outputs, report_dirs)
        for index, intent in enumerate(items):
            for kind, stem, suffix in (("report", intent.report_stem, ".md"),
                                       ("missing", intent.missing_stem, ".txt")):
                if stem is None:
                    continue
                for candidate_index in range(1, MAX_CANDIDATES + 1):
                    identity = _identity(automatic_output_candidate(intent.report_dir, stem, suffix, candidate_index))
                    reserved = (*sources, *(x for x in outputs if x is not None), *result._planned_aux.values())
                    if identity.inode is None and not any(_same(identity, other) for other in reserved):
                        result._planned_aux[(index, kind)] = identity
                        break
                else:
                    result._planning_failures[index] = "auxiliary_allocation_exhausted"
        return result
