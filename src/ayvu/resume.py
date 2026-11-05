from __future__ import annotations

import json
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .domain import TranslationMemoryOptions, TranslationOptions


RESUME_STATE_VERSION = 1
RUNNING_STATUS = "running"
COMPLETED_STATUS = "completed"
STATE_FILE_PATTERN = "*.ayvu-state.json"


class ResumeStateError(ValueError):
    pass


@dataclass(frozen=True)
class InvalidResumeState:
    path: Path
    message: str


@dataclass(frozen=True)
class ResumeStateScan:
    running: tuple[TranslationResumeState, ...]
    invalid: tuple[InvalidResumeState, ...]

    @property
    def has_findings(self) -> bool:
        return bool(self.running or self.invalid)


@dataclass(frozen=True)
class TranslationResumeState:
    version: int
    status: str
    input_path: Path
    output_path: Path
    cache_path: Path
    source: str
    target: str
    translator_name: str
    url: str
    glossary_path: Path | None
    fail_fast: bool
    overwrite: bool
    timeout: float
    retries: int
    chunk_limit: int
    workers: int
    translate_metadata: bool
    translate_alt_text: bool
    chapter_selection: str | None
    created_at: str
    updated_at: str
    translation_memory_enabled: bool = False
    translation_memory_apply_threshold: float | None = None
    translation_memory_suggest_threshold: float | None = None
    translation_memory_max_candidates: int | None = None
    requests_per_second: float | None = None
    retry_backoff: float = 0.5
    retry_backoff_max: float = 8.0
    total_chapters: int | None = None
    current_chapter: str | None = None
    completed_chapters: tuple[str, ...] = ()
    failed_chapters: tuple[str, ...] = ()
    failed_segment_count: int = 0
    # Part of the project's identity, and the only part this file did not hold. Old files have none.
    profile_name: str | None = None

    @classmethod
    def create(
        cls,
        input_path: Path,
        output_path: Path,
        cache_path: Path,
        translator_name: str,
        url: str,
        glossary_path: Path | None,
        options: TranslationOptions,
        overwrite: bool,
        timeout: float,
        retries: int,
        requests_per_second: float | None = None,
        retry_backoff: float = 0.5,
        retry_backoff_max: float = 8.0,
        profile_name: str | None = None,
    ) -> "TranslationResumeState":
        now = _utc_now()
        return cls(
            version=RESUME_STATE_VERSION,
            status=RUNNING_STATUS,
            input_path=_absolute_path(input_path),
            output_path=_absolute_path(output_path),
            cache_path=_absolute_path(cache_path),
            source=options.source,
            target=options.target,
            translator_name=translator_name,
            url=url,
            glossary_path=_absolute_path(glossary_path) if glossary_path else None,
            fail_fast=options.fail_fast,
            overwrite=overwrite,
            timeout=timeout,
            retries=retries,
            chunk_limit=options.chunk_limit,
            workers=options.workers,
            translate_metadata=options.translate_metadata,
            translate_alt_text=options.translate_alt_text,
            chapter_selection=options.chapter_selection.source if options.chapter_selection else None,
            created_at=now,
            updated_at=now,
            translation_memory_enabled=options.translation_memory is not None,
            translation_memory_apply_threshold=(
                options.translation_memory.apply_threshold if options.translation_memory else None
            ),
            translation_memory_suggest_threshold=(
                options.translation_memory.suggest_threshold if options.translation_memory else None
            ),
            translation_memory_max_candidates=(
                options.translation_memory.max_candidates if options.translation_memory else None
            ),
            requests_per_second=requests_per_second,
            retry_backoff=retry_backoff,
            retry_backoff_max=retry_backoff_max,
            profile_name=profile_name,
        )

    @classmethod
    def from_dict(cls, data: object) -> "TranslationResumeState":
        if not isinstance(data, dict):
            raise ResumeStateError("Resume state JSON must be an object.")

        version = _required_int(data, "version")
        if version != RESUME_STATE_VERSION:
            raise ResumeStateError(f"Unsupported resume state version: {version}.")

        status = _required_text(data, "status")
        if status not in (RUNNING_STATUS, COMPLETED_STATUS):
            raise ResumeStateError(f"Unsupported resume state status: {status}.")

        return cls(
            version=version,
            status=status,
            input_path=Path(_required_text(data, "input_path")),
            output_path=Path(_required_text(data, "output_path")),
            cache_path=Path(_required_text(data, "cache_path")),
            source=_required_text(data, "source"),
            target=_required_text(data, "target"),
            translator_name=_required_text(data, "translator_name"),
            url=_required_text(data, "url"),
            glossary_path=_optional_path(data, "glossary_path"),
            fail_fast=_required_bool(data, "fail_fast"),
            overwrite=_required_bool(data, "overwrite"),
            timeout=_required_number(data, "timeout"),
            retries=_required_int(data, "retries"),
            chunk_limit=_required_int(data, "chunk_limit"),
            workers=_optional_int_default(data, "workers", default=1),
            translate_metadata=_optional_bool(data, "translate_metadata", default=False),
            translate_alt_text=_optional_bool(data, "translate_alt_text", default=False),
            chapter_selection=_optional_text(data, "chapter_selection"),
            created_at=_required_text(data, "created_at"),
            updated_at=_required_text(data, "updated_at"),
            translation_memory_enabled=_optional_bool(data, "translation_memory_enabled", default=False),
            translation_memory_apply_threshold=_optional_number_or_none(
                data, "translation_memory_apply_threshold"
            ),
            translation_memory_suggest_threshold=_optional_number_or_none(
                data, "translation_memory_suggest_threshold"
            ),
            translation_memory_max_candidates=_optional_int_or_none(
                data, "translation_memory_max_candidates"
            ),
            requests_per_second=_optional_number_or_none(data, "requests_per_second"),
            retry_backoff=_optional_number(data, "retry_backoff", default=0.5),
            retry_backoff_max=_optional_number(data, "retry_backoff_max", default=8.0),
            total_chapters=_optional_int_or_none(data, "total_chapters"),
            current_chapter=_optional_text(data, "current_chapter"),
            completed_chapters=_optional_str_tuple(data, "completed_chapters"),
            failed_chapters=_optional_str_tuple(data, "failed_chapters"),
            failed_segment_count=_optional_int_default(data, "failed_segment_count", default=0),
            profile_name=_optional_text(data, "profile_name"),
        )

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        for key in ("input_path", "output_path", "cache_path", "glossary_path"):
            value = data[key]
            data[key] = str(value) if value is not None else None
        return data

    def translation_memory_options(self) -> TranslationMemoryOptions | None:
        if not self.translation_memory_enabled:
            return None
        defaults = TranslationMemoryOptions()
        apply_threshold = self.translation_memory_apply_threshold
        suggest_threshold = self.translation_memory_suggest_threshold
        max_candidates = self.translation_memory_max_candidates
        return TranslationMemoryOptions(
            apply_threshold=apply_threshold if apply_threshold is not None else defaults.apply_threshold,
            suggest_threshold=suggest_threshold if suggest_threshold is not None else defaults.suggest_threshold,
            max_candidates=max_candidates if max_candidates is not None else defaults.max_candidates,
        )

    def mark_completed(self) -> "TranslationResumeState":
        return replace(self, status=COMPLETED_STATUS, updated_at=_utc_now())

    def start_chapter(self, name: str, total: int) -> "TranslationResumeState":
        return replace(
            self,
            current_chapter=name,
            total_chapters=total,
            updated_at=_utc_now(),
        )

    def record_chapter(
        self,
        name: str,
        total: int,
        ok: bool,
        failed_segment_count: int = 0,
    ) -> "TranslationResumeState":
        completed = self.completed_chapters
        failed = self.failed_chapters
        failures = self.failed_segment_count
        if ok:
            completed = completed + (name,)
        else:
            failed = failed + (name,)
            failures = failures + failed_segment_count
        return replace(
            self,
            total_chapters=total,
            current_chapter=None,
            completed_chapters=completed,
            failed_chapters=failed,
            failed_segment_count=failures,
            updated_at=_utc_now(),
        )


@dataclass(frozen=True)
class ResumeStateStore:
    processing_dir: Path

    def state_path_for(self, state: TranslationResumeState) -> Path:
        filename = f"{_safe_filename_part(state.input_path.stem)}-{_safe_filename_part(state.target)}.ayvu-state.json"
        return self.processing_dir / filename

    def save(self, state: TranslationResumeState) -> Path:
        self.processing_dir.mkdir(parents=True, exist_ok=True)
        path = self.state_path_for(state)
        path.write_text(json.dumps(state.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return path

    def load(self, path: Path) -> TranslationResumeState:
        try:
            raw = path.read_text(encoding="utf-8")
            data = json.loads(raw)
        except FileNotFoundError as exc:
            raise ResumeStateError(f"Resume state file not found: {path}") from exc
        except json.JSONDecodeError as exc:
            raise ResumeStateError(f"Resume state file is not valid JSON: {path}") from exc

        try:
            return TranslationResumeState.from_dict(data)
        except ResumeStateError as exc:
            raise ResumeStateError(f"Invalid resume state file {path}: {exc}") from exc

    def scan(self) -> ResumeStateScan:
        if not self.processing_dir.exists():
            return ResumeStateScan(running=(), invalid=())

        running: list[TranslationResumeState] = []
        invalid: list[InvalidResumeState] = []
        for path in sorted(self.processing_dir.glob(STATE_FILE_PATTERN)):
            try:
                state = self.load(path)
            except ResumeStateError as exc:
                invalid.append(InvalidResumeState(path=path, message=str(exc)))
                continue

            if state.status == RUNNING_STATUS:
                running.append(state)

        return ResumeStateScan(running=tuple(running), invalid=tuple(invalid))


def default_processing_dir() -> Path:
    return Path.home() / "Documentos" / "Livros" / "Processando"


def _required_text(data: dict[str, object], key: str) -> str:
    value = _required_value(data, key)
    if not isinstance(value, str) or not value.strip():
        raise ResumeStateError(f"Resume state field {key} must be a non-empty string.")
    return value


def _required_int(data: dict[str, object], key: str) -> int:
    value = _required_value(data, key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ResumeStateError(f"Resume state field {key} must be an integer.")
    return value


def _required_bool(data: dict[str, object], key: str) -> bool:
    value = _required_value(data, key)
    if not isinstance(value, bool):
        raise ResumeStateError(f"Resume state field {key} must be true or false.")
    return value


def _optional_bool(data: dict[str, object], key: str, default: bool) -> bool:
    if key not in data:
        return default
    value = data[key]
    if not isinstance(value, bool):
        raise ResumeStateError(f"Resume state field {key} must be true or false.")
    return value


def _optional_text(data: dict[str, object], key: str) -> str | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ResumeStateError(f"Resume state field {key} must be a non-empty string or null.")
    return value


def _optional_int_or_none(data: dict[str, object], key: str) -> int | None:
    if key not in data or data[key] is None:
        return None
    value = data[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise ResumeStateError(f"Resume state field {key} must be an integer or null.")
    return value


def _optional_number_or_none(data: dict[str, object], key: str) -> float | None:
    if key not in data or data[key] is None:
        return None
    value = data[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ResumeStateError(f"Resume state field {key} must be a number or null.")
    return float(value)


def _optional_number(data: dict[str, object], key: str, default: float) -> float:
    if key not in data:
        return default
    value = data[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ResumeStateError(f"Resume state field {key} must be a number.")
    return float(value)


def _optional_int_default(data: dict[str, object], key: str, default: int) -> int:
    if key not in data:
        return default
    value = data[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise ResumeStateError(f"Resume state field {key} must be an integer.")
    return value


def _optional_str_tuple(data: dict[str, object], key: str) -> tuple[str, ...]:
    if key not in data:
        return ()
    value = data[key]
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        raise ResumeStateError(f"Resume state field {key} must be a list of non-empty strings.")
    return tuple(value)


def _required_number(data: dict[str, object], key: str) -> float:
    value = _required_value(data, key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ResumeStateError(f"Resume state field {key} must be a number.")
    return float(value)


def _required_value(data: dict[str, object], key: str) -> object:
    if key not in data:
        raise ResumeStateError(f"Resume state field {key} is required.")
    return data[key]


def _optional_path(data: dict[str, object], key: str) -> Path | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ResumeStateError(f"Resume state field {key} must be a string or null.")
    return Path(value)


def _safe_filename_part(value: str) -> str:
    clean = []
    for char in value.strip():
        if char.isalnum() or char in ("-", "_"):
            clean.append(char)
            continue
        if char in (" ", "."):
            clean.append("-")

    filename = "".join(clean).strip("-_")
    return filename or "translation"


def _absolute_path(path: Path) -> Path:
    return path.expanduser().resolve()


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()
