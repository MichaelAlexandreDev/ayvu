"""Framework-neutral cache administration contracts."""

import ast
from pathlib import Path


def test_application_module_has_no_infrastructure_or_presentation_imports():
    module = Path(__file__).parents[1] / "src" / "ayvu" / "cache_admin.py"
    tree = ast.parse(module.read_text(encoding="utf-8"))
    forbidden = {"sqlite3", "typer", "rich", "PyQt5", "PyQt6", "PySide2", "PySide6", "pathlib", "os", "tempfile"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [node.module or ""]
        else:
            continue
        assert not any(name.split(".")[0] in forbidden for name in names)

import hashlib
import json

import pytest

from ayvu import cache_admin as admin


def payload(original="secret original", translated="secret translation"):
    return json.dumps({"version": 1, "entries": [{
        "source_lang": "en", "target_lang": "pt", "original_text": original,
        "translated_text": translated, "original_text_hash": hashlib.sha256(original.encode()).hexdigest(),
        "created_at": "2024-01-01 00:00:00",
    }]}).encode()


class FakeRepository:
    def __init__(self):
        self.calls = []
        self.data = payload()

    def inspect(self, request):
        self.calls.append(("inspect", request))
        return admin.CacheSummary(True, 1, 42, (admin.CachePair("en", "pt", 1, "2024", "2024"),))

    def preview_clean(self, request):
        self.calls.append(("preview", request))
        return admin.CleanSelection(request.cache, request.filters, 1, "a" * 64)

    def execute_clean(self, request):
        self.calls.append(("execute", request))
        return request.selection.count

    def export_payload(self, request):
        self.calls.append(("export", request))
        return self.data

    def import_payload(self, request, data):
        self.calls.append(("import", request, data))
        return admin.ImportCounts(0, 0 if request.replace else 1, 1 if request.replace else 0)


class FakeFiles:
    def __init__(self, data=None):
        self.data = payload() if data is None else data
        self.published = []

    def read(self, request):
        return self.data

    def publish(self, request, data):
        self.published.append(data)


def test_services_pass_resolved_selection_and_leave_confirmation_to_caller():
    repo = FakeRepository()
    ref = admin.CacheRef("private/path")
    inspected = admin.InspectCache(repo).execute(admin.InspectRequest(ref))
    preview = admin.PreviewClean(repo).execute(admin.PreviewCleanRequest(ref, admin.CacheFilter(source_lang="en")))
    assert inspected.value.count == 1
    assert [call[0] for call in repo.calls] == ["inspect", "preview"]
    result = admin.ExecuteClean(repo).execute(admin.ExecuteCleanRequest(preview.value))
    assert result.value == 1
    assert repo.calls[-1][1].selection is preview.value


@pytest.mark.parametrize("filters", [admin.CacheFilter(all_entries=True), admin.CacheFilter(source_lang="en"),
    admin.CacheFilter(target_lang="pt"), admin.CacheFilter(before="2024-01-01"),
    admin.CacheFilter(source_lang="en", target_lang="pt", before="2024-01-01")])
def test_explicit_clean_scopes(filters):
    assert admin.PreviewCleanRequest(admin.CacheRef("cache"), filters).filters == filters


@pytest.mark.parametrize("kwargs", [{}, {"source_lang": ""}, {"before": "not-a-date"},
    {"all_entries": True, "source_lang": "en"}])
def test_invalid_clean_scopes_rejected(kwargs):
    with pytest.raises(admin.CacheAdminError):
        admin.PreviewCleanRequest(admin.CacheRef("cache"), admin.CacheFilter(**kwargs))


def test_references_requests_and_results_do_not_represent_sensitive_data():
    ref = admin.CacheRef("private/path")
    request = admin.ExportRequest(ref, admin.FileRef("secret/destination"), admin.CacheFilter(source_lang="secret"))
    for item in (ref, request, admin.CacheResult(value=payload())):
        assert "secret" not in repr(item)
        assert "private" not in repr(item)


def test_export_publishes_v1_payload_and_import_passes_replace_intent():
    repo, files = FakeRepository(), FakeFiles()
    ref = admin.CacheRef("cache")
    out = admin.ExportCache(repo, files).execute(admin.ExportRequest(ref, admin.FileRef("out")))
    assert out.value == 1
    assert files.published == [payload()]
    for replace in (False, True):
        result = admin.ImportCache(repo, files).execute(admin.ImportRequest(ref, admin.FileRef("in"), replace=replace))
        assert result.value == admin.ImportCounts(0, int(not replace), int(replace))


def test_fake_port_preserves_v1_conflict_policy():
    class MemoryRepository(FakeRepository):
        def __init__(self): self.rows = {}
        def import_payload(self, request, data):
            imported = skipped = replaced = 0
            for entry in json.loads(data)["entries"]:
                key = (entry["source_lang"], entry["target_lang"], entry["original_text_hash"])
                if key not in self.rows:
                    self.rows[key] = entry
                    imported += 1
                elif request.replace:
                    self.rows[key] = entry
                    replaced += 1
                else:
                    skipped += 1
            return admin.ImportCounts(imported, skipped, replaced)
    repo, files = MemoryRepository(), FakeFiles(payload(translated="first"))
    service = admin.ImportCache(repo, files)
    ref, file_ref = admin.CacheRef("cache"), admin.FileRef("source")
    assert service.execute(admin.ImportRequest(ref, file_ref)).value == admin.ImportCounts(1, 0, 0)
    files.data = payload(translated="second")
    assert service.execute(admin.ImportRequest(ref, file_ref)).value == admin.ImportCounts(0, 1, 0)
    assert next(iter(repo.rows.values()))["translated_text"] == "first"
    assert service.execute(admin.ImportRequest(ref, file_ref, replace=True)).value == admin.ImportCounts(0, 0, 1)
    assert next(iter(repo.rows.values()))["translated_text"] == "second"


@pytest.mark.parametrize("data,code", [(b"{", "malformed_json"), (b'{"version":2,"entries":[]}', "unsupported_version"),
    (b'{"version":1,"entries":[{}]}', "malformed_entry")])
def test_invalid_import_never_calls_mutation(data, code):
    repo, files = FakeRepository(), FakeFiles(data)
    result = admin.ImportCache(repo, files).execute(admin.ImportRequest(admin.CacheRef("cache"), admin.FileRef("in")))
    assert result.failure.code == code
    assert repo.calls == []


def test_hash_mismatch_and_limit_fail_before_mutation(monkeypatch):
    bad = json.loads(payload())
    bad["entries"][0]["original_text_hash"] = "0" * 64
    repo = FakeRepository()
    request = admin.ImportRequest(admin.CacheRef("cache"), admin.FileRef("in"))
    result = admin.ImportCache(repo, FakeFiles(json.dumps(bad).encode())).execute(request)
    assert result.failure.code == "invalid_text_hash"
    monkeypatch.setattr(admin, "MAX_FILE_BYTES", 1)
    result = admin.ImportCache(repo, FakeFiles()).execute(request)
    assert result.failure.code == "file_too_large"
    assert repo.calls == []


def test_cancellation_before_read_prevents_access():
    class Cancel:
        def checkpoint(self, point):
            assert point == "cache_before_read"
            return object()
    repo, files = FakeRepository(), FakeFiles()
    request = admin.ExportRequest(admin.CacheRef("cache"), admin.FileRef("out"), cancellation=Cancel())
    result = admin.ExportCache(repo, files).execute(request)
    assert result.failure.code == "cancelled"
    assert repo.calls == [] and files.published == []


def test_unknown_errors_do_not_expose_content():
    class Broken(FakeRepository):
        def inspect(self, request):
            raise ValueError("secret book text and private path")
    result = admin.InspectCache(Broken()).execute(admin.InspectRequest(admin.CacheRef("path")))
    assert result.failure.code == "unexpected_error"
    assert "secret" not in repr(result)


def test_entry_and_text_limits_fail_before_mutation(monkeypatch):
    repo = FakeRepository()
    request = admin.ImportRequest(admin.CacheRef("cache"), admin.FileRef("in"))
    monkeypatch.setattr(admin, "MAX_ENTRIES", 0)
    result = admin.ImportCache(repo, FakeFiles()).execute(request)
    assert result.failure.code == "too_many_entries"
    monkeypatch.setattr(admin, "MAX_ENTRIES", 1)
    monkeypatch.setattr(admin, "MAX_TEXT_LENGTH", 1)
    result = admin.ImportCache(repo, FakeFiles()).execute(request)
    assert result.failure.code == "text_too_large"
    assert repo.calls == []


def test_progress_validation_and_sink_failure_are_bounded():
    class BrokenSink:
        def report(self, progress):
            raise ValueError("private text")
    request = admin.InspectRequest(admin.CacheRef("cache"), progress=BrokenSink())
    with pytest.raises(admin.CacheAdminError, match="progress_sink_failed"):
        admin.report(request, "reading", 1, 1)
    with pytest.raises(admin.CacheAdminError, match="invalid_progress"):
        admin.CacheProgress("reading", 2, 1)


def test_undeclared_checkpoint_is_rejected():
    with pytest.raises(admin.CacheAdminError, match="undeclared_safe_point"):
        admin.checkpoint(admin.InspectRequest(admin.CacheRef("cache")), "unexpected")
