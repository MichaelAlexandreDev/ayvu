import ast
import re



from tests.formats.format_adapter_support import CONTRACT_MODULES, REQUIRED_ROWS, ROOT
from tests.formats.format_adapter_support import map_rows
from tests.formats.format_adapter_support import resolve


def test_the_contract_imports_nothing_concrete():
    forbidden = {"ebooklib", "bs4", "lxml", "typer", "rich", "PySide6", "requests", "mcp", "sqlite3", "os", "shutil", "zipfile",
                 "ayvu.providers.libretranslate.client", "ayvu.providers", "ayvu.translation",
                 "ayvu.artifacts.store", "ayvu.jobs.store", "ayvu.knowledge.glossary", "ayvu.formats.epub.io", "ayvu.formats.local"}
    for name in CONTRACT_MODULES:
        tree = ast.parse((ROOT / "src" / "ayvu" / f"{name}.py").read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                found = {alias.name for alias in node.names}
            elif isinstance(node, ast.ImportFrom):
                module = f"ayvu.{node.module}" if node.level else node.module
                found = {module}
            else:
                continue
            assert not found & forbidden, (name, found)


def test_the_contract_never_branches_on_a_format_or_a_missing_method():
    for name in CONTRACT_MODULES:
        source = (ROOT / "src" / "ayvu" / f"{name}.py").read_text()
        assert "hasattr" not in source and "fake_" not in source
        assert not re.search(r"epub|srt|webvtt|\.txt|\.pdf", source, re.IGNORECASE), name


def test_getattr_in_the_service_only_reads_declared_port_fields():
    tree = ast.parse((ROOT / "src" / "ayvu" / "formats" / "service.py").read_text())
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "getattr"]
    assert [call.args[1].id for call in calls] == ["field"]


def test_the_map_covers_every_required_behavior():
    assert set(REQUIRED_ROWS) <= set(map_rows())


def test_every_symbol_in_the_map_exists():
    for name, (current, owner, _note) in map_rows().items():
        for reference in re.findall(r"`(ayvu\.[A-Za-z0-9_.]+)`", current + owner):
            assert resolve(reference) is not None, (name, reference)


def test_every_behavior_has_a_current_code_and_a_concrete_owner():
    for name, (current, owner, _note) in map_rows().items():
        assert re.search(r"`ayvu\.", current), name
        assert re.search(r"`ayvu\.|#\d+", owner), name


def test_jobs_and_resume_are_not_assigned_to_the_format_contract():
    rows = map_rows()
    for name in ("jobs", "resume", "output path", "translation"):
        assert "format_" not in rows[name][1], name
