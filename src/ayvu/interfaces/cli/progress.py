from __future__ import annotations

from dataclasses import dataclass

from rich.progress import Progress


@dataclass(frozen=True)
class TranslationProgressSnapshot:
    chapters_processed: int
    total_chapters: int | None
    current_chapter: str | None
    texts_processed: int
    texts_translated: int
    texts_from_cache: int
    texts_from_memory: int
    texts_dry_run: int
    texts_missing: int
    text_errors: int


@dataclass
class TextProgressCounters:
    translated: int = 0
    cache: int = 0
    memory: int = 0
    dry_run: int = 0
    missing: int = 0
    error: int = 0

    def record(self, status: str) -> None:
        if status == "translated":
            self.translated += 1
            return
        if status == "cache":
            self.cache += 1
            return
        if status == "memory":
            self.memory += 1
            return
        if status == "dry_run":
            self.dry_run += 1
            return
        if status == "missing":
            self.missing += 1
            return
        if status == "error":
            self.error += 1
            return
        raise ValueError(f"Unknown text progress status: {status}")

    @property
    def processed(self) -> int:
        return self.translated + self.cache + self.memory + self.dry_run + self.missing + self.error

    def new_count(self, dry_run: bool) -> int:
        if dry_run:
            return self.dry_run
        return self.translated


class TranslationProgress:
    def __init__(self, progress: Progress, dry_run: bool, workers: int = 1) -> None:
        self._progress = progress
        self._dry_run = dry_run
        self._workers = workers
        self._counters = TextProgressCounters()
        self._chapters_processed = 0
        self._total_chapters: int | None = None
        self._current_chapter: str | None = None
        self._chapter_task = progress.add_task(self._chapter_label(), total=None)
        self._text_task = progress.add_task("Texts", total=None)

    def chapter_started(self, index: int, total: int, name: str) -> None:
        self._total_chapters = total
        self._current_chapter = name
        self._progress.update(
            self._chapter_task,
            total=total,
            description=self._chapter_description(index, total, name),
        )

    def chapter_done(self, index: int, total: int, name: str, _stats: object) -> None:
        self._chapters_processed += 1
        self._total_chapters = total
        self._current_chapter = name
        self._progress.advance(self._chapter_task)
        self._progress.update(self._chapter_task, description=self._chapter_description(index, total, name))

    def text_processed(self, status: str) -> None:
        self._counters.record(status)
        self._progress.advance(self._text_task)
        self._progress.update(self._text_task, description=self._text_description())

    def snapshot(self) -> TranslationProgressSnapshot:
        return TranslationProgressSnapshot(
            chapters_processed=self._chapters_processed,
            total_chapters=self._total_chapters,
            current_chapter=self._current_chapter,
            texts_processed=self._counters.processed,
            texts_translated=self._counters.translated,
            texts_from_cache=self._counters.cache,
            texts_from_memory=self._counters.memory,
            texts_dry_run=self._counters.dry_run,
            texts_missing=self._counters.missing,
            text_errors=self._counters.error,
        )

    def _chapter_description(self, index: int, total: int, name: str) -> str:
        return f"{self._chapter_label()} {index}/{total}: {_shorten(name)}"

    def _chapter_label(self) -> str:
        if self._workers > 1:
            return f"Chapters ({self._workers} workers, EPUB order)"
        return "Chapters"

    def _text_description(self) -> str:
        new_label = "would translate" if self._dry_run else "new"
        new_count = self._counters.new_count(self._dry_run)
        memory_part = f"memory {self._counters.memory} | " if self._counters.memory else ""
        return (
            f"Texts {self._counters.processed} | {new_label} {new_count} | "
            f"cache {self._counters.cache} | {memory_part}missing {self._counters.missing} | "
            f"errors {self._counters.error}"
        )


def _shorten(text: str, max_length: int = 50) -> str:
    if len(text) <= max_length:
        return text
    return text[: max_length - 3] + "..."
