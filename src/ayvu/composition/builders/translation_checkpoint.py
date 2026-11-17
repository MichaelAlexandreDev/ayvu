"""The compatibility checkpoint a CLI translation keeps while it runs, written through the recovery store."""
from __future__ import annotations

from ...recovery.checkpoint import ResumeStateStore, TranslationResumeState


class TranslationCheckpoint:
    """Compatibility checkpoint writes stay with the storage adapter, never the presenter."""
    def __init__(self):
        self.store = None
        self.state = None

    def __repr__(self):
        return 'TranslationCheckpoint()'

    def start(self, *, processing_dir, epub_path, **values):
        self.store = ResumeStateStore(processing_dir)
        self.state = TranslationResumeState.create(input_path=epub_path, **values)
        self._save()

    def chapter_started(self, index, total, name):
        if self.state is not None:
            self.state = self.state.start_chapter(name, total)
            self._save()

    def chapter_done(self, index, total, name, stats):
        if self.state is not None:
            self.state = self.state.record_chapter(name=name, total=total,
                ok=not stats.errors and stats.missing == 0, failed_segment_count=len(stats.errors) + stats.missing)
            self._save()

    def completed(self):
        if self.state is not None:
            self.state = self.state.mark_completed()
            self._save()

    def _save(self):
        self.store.save(self.state)
