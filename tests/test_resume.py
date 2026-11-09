import json
from dataclasses import replace
from pathlib import Path

import pytest

from ayvu.domain import (
    ChapterSelection,
    LanguagePair,
    TranslationMemoryOptions,
    TranslationOptions,
)
from ayvu.resume import (
    COMPLETED_STATUS,
    RESUME_STATE_VERSION,
    RUNNING_STATUS,
    ResumeStateError,
    ResumeStateStore,
    TranslationResumeState,
)


def make_state(tmp_path: Path, stem: str = "book") -> TranslationResumeState:
    return TranslationResumeState.create(
        input_path=tmp_path / "Original" / f"{stem}.epub",
        output_path=tmp_path / "Traduzidos" / f"{stem}-pt.epub",
        cache_path=tmp_path / "cache.sqlite",
        translator_name="libretranslate",
        url="http://localhost:5000",
        glossary_path=tmp_path / "glossary.json",
        options=TranslationOptions(
            language_pair=LanguagePair(source="en", target="pt"),
            dry_run=False,
            fail_fast=True,
            chunk_limit=1200,
            workers=3,
            translate_metadata=True,
            translate_alt_text=True,
            chapter_selection=ChapterSelection.parse("1-2,*chapter3*"),
        ),
        overwrite=True,
        timeout=9.5,
        retries=3,
        requests_per_second=2.5,
        retry_backoff=0.25,
        retry_backoff_max=2.0,
    )


def test_resume_state_round_trip(tmp_path):
    store = ResumeStateStore(tmp_path / "Processando")
    state = make_state(tmp_path)

    path = store.save(state)
    loaded = store.load(path)

    assert path.name == "book-pt.ayvu-state.json"
    assert loaded == state
    assert loaded.status == RUNNING_STATUS
    assert loaded.version == RESUME_STATE_VERSION
    assert loaded.source == "en"
    assert loaded.target == "pt"
    assert loaded.fail_fast
    assert loaded.overwrite
    assert loaded.chunk_limit == 1200
    assert loaded.translate_metadata
    assert loaded.translate_alt_text
    assert loaded.chapter_selection == "1-2,*chapter3*"
    assert loaded.timeout == 9.5
    assert loaded.retries == 3
    assert loaded.workers == 3
    assert loaded.requests_per_second == 2.5
    assert loaded.retry_backoff == 0.25
    assert loaded.retry_backoff_max == 2.0


def test_resume_state_round_trips_translation_memory(tmp_path):
    store = ResumeStateStore(tmp_path / "Processando")
    state = TranslationResumeState.create(
        input_path=tmp_path / "Original" / "book.epub",
        output_path=tmp_path / "Traduzidos" / "book-pt.epub",
        cache_path=tmp_path / "cache.sqlite",
        translator_name="libretranslate",
        url="http://localhost:5000",
        glossary_path=None,
        options=TranslationOptions(
            language_pair=LanguagePair(source="en", target="pt"),
            translation_memory=TranslationMemoryOptions(
                apply_threshold=0.93, suggest_threshold=0.75, max_candidates=50
            ),
        ),
        overwrite=True,
        timeout=5.0,
        retries=1,
    )

    loaded = store.load(store.save(state))

    assert loaded.translation_memory_enabled is True
    assert loaded.translation_memory_apply_threshold == 0.93
    assert loaded.translation_memory_suggest_threshold == 0.75
    assert loaded.translation_memory_max_candidates == 50
    assert loaded.translation_memory_options() == TranslationMemoryOptions(
        apply_threshold=0.93, suggest_threshold=0.75, max_candidates=50
    )


def test_resume_state_defaults_to_disabled_translation_memory(tmp_path):
    state = make_state(tmp_path)

    assert state.translation_memory_enabled is False
    assert state.translation_memory_options() is None


def test_resume_state_loads_legacy_file_without_translation_memory(tmp_path):
    state = make_state(tmp_path)
    data = state.to_dict()
    for key in (
        "translation_memory_enabled",
        "translation_memory_apply_threshold",
        "translation_memory_suggest_threshold",
        "translation_memory_max_candidates",
        "workers",
        "requests_per_second",
        "retry_backoff",
        "retry_backoff_max",
    ):
        data.pop(key, None)
    path = tmp_path / "legacy.ayvu-state.json"
    path.write_text(json.dumps(data), encoding="utf-8")

    loaded = ResumeStateStore(tmp_path).load(path)

    assert loaded.translation_memory_enabled is False
    assert loaded.translation_memory_options() is None
    assert loaded.workers == 1
    assert loaded.requests_per_second is None
    assert loaded.retry_backoff == 0.5
    assert loaded.retry_backoff_max == 8.0


def test_resume_state_can_be_marked_completed(tmp_path):
    state = make_state(tmp_path)

    completed = state.mark_completed()

    assert state.status == RUNNING_STATUS
    assert completed.status == COMPLETED_STATUS
    assert completed.created_at == state.created_at


def test_resume_state_load_reports_invalid_json(tmp_path):
    path = tmp_path / "bad.ayvu-state.json"
    path.write_text("{bad", encoding="utf-8")
    store = ResumeStateStore(tmp_path)

    with pytest.raises(ResumeStateError) as error:
        store.load(path)

    assert "Resume state file is not valid JSON" in str(error.value)
    assert str(path) in str(error.value)


def test_resume_state_load_reports_missing_required_field(tmp_path):
    path = tmp_path / "missing.ayvu-state.json"
    data = make_state(tmp_path).to_dict()
    data.pop("cache_path")
    path.write_text(json.dumps(data), encoding="utf-8")
    store = ResumeStateStore(tmp_path)

    with pytest.raises(ResumeStateError) as error:
        store.load(path)

    assert "Invalid resume state file" in str(error.value)
    assert "cache_path is required" in str(error.value)


def test_resume_state_load_defaults_missing_translate_metadata_to_false(tmp_path):
    path = tmp_path / "old.ayvu-state.json"
    data = make_state(tmp_path).to_dict()
    data.pop("translate_metadata")
    path.write_text(json.dumps(data), encoding="utf-8")
    store = ResumeStateStore(tmp_path)

    loaded = store.load(path)

    assert not loaded.translate_metadata


def test_resume_state_load_defaults_missing_translate_alt_text_to_false(tmp_path):
    path = tmp_path / "old.ayvu-state.json"
    data = make_state(tmp_path).to_dict()
    data.pop("translate_alt_text")
    path.write_text(json.dumps(data), encoding="utf-8")
    store = ResumeStateStore(tmp_path)

    loaded = store.load(path)

    assert not loaded.translate_alt_text


def test_resume_state_load_defaults_missing_chapter_selection_to_none(tmp_path):
    path = tmp_path / "old.ayvu-state.json"
    data = make_state(tmp_path).to_dict()
    data.pop("chapter_selection")
    path.write_text(json.dumps(data), encoding="utf-8")
    store = ResumeStateStore(tmp_path)

    loaded = store.load(path)

    assert loaded.chapter_selection is None


def test_resume_state_starts_with_empty_progress(tmp_path):
    state = make_state(tmp_path)

    assert state.total_chapters is None
    assert state.current_chapter is None
    assert state.completed_chapters == ()
    assert state.failed_chapters == ()
    assert state.failed_segment_count == 0


def test_resume_state_start_chapter_sets_current_and_total(tmp_path):
    state = make_state(tmp_path)

    started = state.start_chapter("chapter-one.xhtml", 12)

    assert started.current_chapter == "chapter-one.xhtml"
    assert started.total_chapters == 12
    assert started.completed_chapters == ()


def test_resume_state_record_chapter_accumulates_completed_and_failed(tmp_path):
    state = make_state(tmp_path).start_chapter("chapter-one.xhtml", 3)

    after_first = state.record_chapter("chapter-one.xhtml", 3, ok=True)
    after_second = after_first.record_chapter(
        "chapter-two.xhtml", 3, ok=False, failed_segment_count=2
    )

    assert after_first.completed_chapters == ("chapter-one.xhtml",)
    assert after_first.current_chapter is None
    assert after_second.completed_chapters == ("chapter-one.xhtml",)
    assert after_second.failed_chapters == ("chapter-two.xhtml",)
    assert after_second.failed_segment_count == 2
    assert after_second.total_chapters == 3


def test_resume_state_progress_round_trip(tmp_path):
    store = ResumeStateStore(tmp_path / "Processando")
    state = (
        make_state(tmp_path)
        .record_chapter("chapter-one.xhtml", 4, ok=True)
        .record_chapter("chapter-two.xhtml", 4, ok=False, failed_segment_count=3)
        .start_chapter("chapter-three.xhtml", 4)
    )

    path = store.save(state)
    loaded = store.load(path)

    assert loaded == state
    assert loaded.completed_chapters == ("chapter-one.xhtml",)
    assert loaded.failed_chapters == ("chapter-two.xhtml",)
    assert loaded.failed_segment_count == 3
    assert loaded.current_chapter == "chapter-three.xhtml"
    assert loaded.total_chapters == 4


def test_resume_state_load_defaults_missing_progress_fields(tmp_path):
    path = tmp_path / "old.ayvu-state.json"
    data = make_state(tmp_path).to_dict()
    for key in (
        "total_chapters",
        "current_chapter",
        "completed_chapters",
        "failed_chapters",
        "failed_segment_count",
    ):
        data.pop(key, None)
    path.write_text(json.dumps(data), encoding="utf-8")
    store = ResumeStateStore(tmp_path)

    loaded = store.load(path)

    assert loaded.total_chapters is None
    assert loaded.current_chapter is None
    assert loaded.completed_chapters == ()
    assert loaded.failed_chapters == ()
    assert loaded.failed_segment_count == 0


def test_resume_state_scan_finds_running_and_invalid_states(tmp_path):
    store = ResumeStateStore(tmp_path / "Processando")
    running = make_state(tmp_path, "running-book")
    completed = make_state(tmp_path, "completed-book").mark_completed()
    store.save(running)
    store.save(completed)
    bad_path = store.processing_dir / "bad.ayvu-state.json"
    bad_path.write_text("{bad", encoding="utf-8")

    scan = store.scan()

    assert scan.running == (running,)
    assert len(scan.invalid) == 1
    assert scan.invalid[0].path == bad_path
    assert "not valid JSON" in scan.invalid[0].message
    assert scan.has_findings


def test_resume_state_scan_ignores_missing_processing_dir(tmp_path):
    scan = ResumeStateStore(tmp_path / "missing").scan()

    assert scan.running == ()
    assert scan.invalid == ()
    assert not scan.has_findings


def test_resume_state_records_the_profile_and_an_old_file_has_none(tmp_path):
    state = replace(make_state(tmp_path), profile_name="tec")
    data = json.loads(json.dumps(state.to_dict()))
    assert TranslationResumeState.from_dict(data).profile_name == "tec"
    del data["profile_name"]
    assert TranslationResumeState.from_dict(data).profile_name is None
