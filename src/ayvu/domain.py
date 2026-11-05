from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from fnmatch import fnmatchcase
from pathlib import Path


class UserMode(Enum):
    COMMON = "common"
    DEVELOPER = "developer"


class LanguagePairError(ValueError):
    pass


class ChapterSelectionError(ValueError):
    pass


class TranslationMemoryError(ValueError):
    pass


@dataclass(frozen=True)
class LanguagePair:
    source: str
    target: str

    @property
    def target_label(self) -> str:
        return self.target.strip() or "translated"

    def validate_for_translation(self) -> None:
        if not self.source.strip():
            raise LanguagePairError("source language is required")
        if not self.target.strip():
            raise LanguagePairError("target language is required")


@dataclass(frozen=True)
class OutputPlan:
    path: Path
    dry_run: bool = False
    explicit_output: bool = False

    @classmethod
    def for_translation(
        cls,
        input_path: Path,
        explicit_output: Path | None,
        language_pair: LanguagePair,
        dry_run: bool = False,
        default_dir: Path | None = None,
    ) -> "OutputPlan":
        if explicit_output is not None:
            return cls(path=explicit_output, dry_run=dry_run, explicit_output=True)

        output_dir = default_dir or default_translated_books_dir()
        output_path = output_dir / f"{input_path.stem}-{language_pair.target_label}.epub"
        return cls(path=output_path, dry_run=dry_run)

    @classmethod
    def for_preview(
        cls,
        input_path: Path,
        explicit_output: Path | None = None,
        default_dir: Path | None = None,
    ) -> "OutputPlan":
        if explicit_output is not None:
            return cls(path=explicit_output, explicit_output=True)

        output_dir = default_dir or default_preview_books_dir()
        return cls(path=output_dir / f"{input_path.stem}-preview.epub")

    def with_path(self, path: Path) -> "OutputPlan":
        return OutputPlan(path=path, dry_run=self.dry_run, explicit_output=self.explicit_output)

    def blocks_existing_file(self, overwrite: bool) -> bool:
        if self.dry_run:
            return False
        if overwrite:
            return False
        return self.path.exists()


def automatic_output_candidate(directory: Path, stem: str, suffix: str, index: int) -> Path:
    """One candidate in the existing automatic report naming sequence."""
    if index < 1:
        raise ValueError("invalid_candidate_index")
    return directory / (f"{stem}{suffix}" if index == 1 else f"{stem}-{index}{suffix}")


def default_translated_books_dir() -> Path:
    return Path.home() / "Documentos" / "Livros" / "Traduzidos"


def default_preview_books_dir() -> Path:
    return Path.home() / "Documentos" / "Livros" / "Preview"


@dataclass(frozen=True)
class ChapterSelectionItem:
    raw: str
    start: int | None = None
    end: int | None = None
    pattern: str | None = None

    @property
    def is_pattern(self) -> bool:
        return self.pattern is not None

    def matches_index(self, index: int) -> bool:
        if self.is_pattern or self.start is None:
            return False
        end = self.end if self.end is not None else self.start
        return self.start <= index <= end

    def matches_text(self, values: tuple[str, ...]) -> bool:
        if self.pattern is None:
            return False

        pattern = self.pattern.casefold()
        uses_glob = any(char in pattern for char in "*?[")
        for value in values:
            candidate = value.casefold()
            if uses_glob and fnmatchcase(candidate, pattern):
                return True
            if not uses_glob and pattern in candidate:
                return True
        return False


@dataclass(frozen=True)
class ChapterSelection:
    source: str
    items: tuple[ChapterSelectionItem, ...]

    @classmethod
    def parse(cls, value: str) -> "ChapterSelection":
        raw_value = value.strip()
        if not raw_value:
            raise ChapterSelectionError("chapter selection cannot be empty")

        items = tuple(_parse_chapter_selection_item(part.strip()) for part in raw_value.split(","))
        return cls(source=raw_value, items=items)

    def matches(self, index: int, *values: str | None) -> bool:
        match_values = tuple(value for value in values if value)
        return any(item.matches_index(index) or item.matches_text(match_values) for item in self.items)


def _parse_chapter_selection_item(value: str) -> ChapterSelectionItem:
    if not value:
        raise ChapterSelectionError("chapter selection contains an empty item")

    if value.isdecimal():
        index = _parse_positive_chapter_index(value)
        return ChapterSelectionItem(raw=value, start=index)

    if "-" in value:
        start, separator, end = value.partition("-")
        start = start.strip()
        end = end.strip()
        if separator and start.isdecimal() and end.isdecimal():
            start_index = _parse_positive_chapter_index(start)
            end_index = _parse_positive_chapter_index(end)
            if start_index > end_index:
                raise ChapterSelectionError(f"chapter range is reversed: {value}")
            return ChapterSelectionItem(raw=value, start=start_index, end=end_index)

    return ChapterSelectionItem(raw=value, pattern=value)


def _parse_positive_chapter_index(value: str) -> int:
    index = int(value)
    if index < 1:
        raise ChapterSelectionError(f"chapter index must be 1 or greater: {value}")
    return index


@dataclass(frozen=True)
class TranslationMemoryOptions:
    """Configuration for the fuzzy translation memory.

    The presence of this object means the memory is enabled; ``None`` on
    :class:`TranslationOptions` keeps it disabled. Thresholds are normalized
    similarity scores in the ``(0, 1]`` range. A score at or above
    ``apply_threshold`` reuses the stored translation directly; a score in
    ``[suggest_threshold, apply_threshold)`` is only recorded as a suggestion.
    """

    apply_threshold: float = 0.95
    suggest_threshold: float = 0.80
    max_candidates: int = 200

    def validate(self) -> None:
        if not 0.0 < self.suggest_threshold <= 1.0:
            raise TranslationMemoryError("suggest threshold must be between 0 and 1")
        if not 0.0 < self.apply_threshold <= 1.0:
            raise TranslationMemoryError("apply threshold must be between 0 and 1")
        if self.suggest_threshold > self.apply_threshold:
            raise TranslationMemoryError("suggest threshold cannot be greater than apply threshold")
        if self.max_candidates < 1:
            raise TranslationMemoryError("max candidates must be 1 or greater")

    def applies(self, score: float) -> bool:
        return score >= self.apply_threshold

    def suggests(self, score: float) -> bool:
        return self.suggest_threshold <= score < self.apply_threshold


@dataclass(frozen=True)
class PerformanceProfile:
    name: str
    workers: int
    requests_per_second: float | None
    retries: int
    retry_backoff: float
    retry_backoff_max: float
    chunk_limit: int
    description: str


PERFORMANCE_PROFILE_LOW = "low"
PERFORMANCE_PROFILE_STANDARD = "standard"
PERFORMANCE_PROFILE_HIGH = "high"
PERFORMANCE_PROFILE_CUSTOM = "custom"
DEFAULT_PERFORMANCE_PROFILE = PERFORMANCE_PROFILE_STANDARD

PERFORMANCE_PROFILES: dict[str, PerformanceProfile] = {
    PERFORMANCE_PROFILE_LOW: PerformanceProfile(
        name=PERFORMANCE_PROFILE_LOW,
        workers=1,
        requests_per_second=1.0,
        retries=4,
        retry_backoff=1.0,
        retry_backoff_max=12.0,
        chunk_limit=1500,
        description="Conservative settings for weak machines or fragile translator servers.",
    ),
    PERFORMANCE_PROFILE_STANDARD: PerformanceProfile(
        name=PERFORMANCE_PROFILE_STANDARD,
        workers=1,
        requests_per_second=None,
        retries=2,
        retry_backoff=0.5,
        retry_backoff_max=8.0,
        chunk_limit=3000,
        description="Default sequential settings for predictable local translation.",
    ),
    PERFORMANCE_PROFILE_HIGH: PerformanceProfile(
        name=PERFORMANCE_PROFILE_HIGH,
        workers=4,
        requests_per_second=None,
        retries=2,
        retry_backoff=0.5,
        retry_backoff_max=8.0,
        chunk_limit=4000,
        description="Opt-in parallel settings for stronger machines and stable translator servers.",
    ),
    PERFORMANCE_PROFILE_CUSTOM: PerformanceProfile(
        name=PERFORMANCE_PROFILE_CUSTOM,
        workers=1,
        requests_per_second=None,
        retries=2,
        retry_backoff=0.5,
        retry_backoff_max=8.0,
        chunk_limit=3000,
        description="Start from standard settings and rely on explicit CLI overrides.",
    ),
}


@dataclass(frozen=True)
class TranslationOptions:
    language_pair: LanguagePair
    dry_run: bool = False
    fail_fast: bool = False
    chunk_limit: int = 3000
    workers: int = 1
    max_documents: int | None = None
    translate_metadata: bool = False
    translate_alt_text: bool = False
    chapter_selection: ChapterSelection | None = None
    cache_only: bool = False
    require_full_cache: bool = False
    translation_memory: TranslationMemoryOptions | None = None

    @property
    def source(self) -> str:
        return self.language_pair.source

    @property
    def target(self) -> str:
        return self.language_pair.target
