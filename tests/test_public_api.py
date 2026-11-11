"""D2 of docs/architecture/decisions/package-structure.md (REF-03, #271): one public API per module.

Code in one top-level package of ``ayvu`` may use another top-level package only through the names that
package's ``__init__.py`` exports, or that one of its subpackages' ``__init__.py`` exports (such as
``ayvu.kernel.sqlite``). It may not import a plain submodule. Imports inside a package are free. Top-level flat modules are not packages yet, so they are outside this check until they move.

The composition roots are an exception (D5): each is a public module of its own, so an interface loads only
its root and not every edition's wiring. The quarantined v1 modules are the other (D7).
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1] / "src" / "ayvu"

# Violations that exist today, each with the issue that removes it. An entry that no longer matches fails,
# so this list can only shrink.
# Empty at the start: the only package today, desktop/, is not reached into.
ALLOWED: dict[str, str] = {}

# Plain modules that are a public API by themselves: the composition roots (D5); the v1 modules, quarantined
# until REF-18 retires them (D7); and the adapters, which do I/O, so that importing a package's model and use
# cases never loads SQLite, HTTP or the filesystem code behind them (D2, like ayvu.kernel.sqlite).
ADAPTER_MODULES = frozenset({
    "network.http", "preferences.store", "preferences.config_facade", "provenance.store", "jobs.store",
    "artifacts.store", "artifacts.destination", "projects.store", "projects.schema", "projects.profiles.store",
    "projects.profiles.glossary_store", "library.adapters", "recovery.store", "knowledge.cache.schema",
    "knowledge.cache.repository", "knowledge.cache.interchange", "knowledge.cache.admin_store",
    "knowledge.cache.admin_v2_store", "knowledge.memory.approved.store", "knowledge.glossary.catalog_store"})
PUBLIC_MODULES = frozenset({"composition.cli", "composition.desktop"} | {f"v1.{name}" for name in (
    "translation", "html_translate", "translator", "cache", "cache_format", "cache_stream", "cache_admin_store",
    "resume", "resume_scan")} | ADAPTER_MODULES)


def packages(root: Path = SOURCE) -> set[str]:
    return {path.name for path in root.iterdir() if (path / "__init__.py").is_file()}


def exported(root: Path, package: str) -> set[str]:
    return exported_names(ast.parse((root / package / "__init__.py").read_text(encoding="utf-8")))


def exported_names(tree: ast.Module) -> set[str]:
    """The names a package's __init__.py exports: its __all__, or else every top-level binding."""
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", None) == "__all__" for t in node.targets):
            return {element.value for element in node.value.elts}
    names = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.ImportFrom):
            names.update(alias.asname or alias.name for alias in node.names)
    return names


def violations(root: Path = SOURCE, public_modules: frozenset[str] = PUBLIC_MODULES) -> dict[str, str]:
    """Maps "importer -> target" to a short reason, for every import that skips a package's public API."""
    roots = packages(root)
    found: dict[str, str] = {}
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        parts = path.relative_to(root).with_suffix("").parts
        module = parts[:-1] if parts[-1] == "__init__" else parts
        base = module if parts[-1] == "__init__" else module[:-1]
        importer = ".".join(module)
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            targets: list[tuple[tuple[str, ...], list[str]]] = []
            if isinstance(node, ast.ImportFrom):
                if node.level:
                    anchor = base[:len(base) - node.level + 1] if node.level > 1 else base
                    target = anchor + (tuple(node.module.split(".")) if node.module else ())
                elif node.module and node.module.split(".")[0] == root.name:
                    target = tuple(node.module.split(".")[1:])
                else:
                    continue
                targets.append((target, [alias.name for alias in node.names]))
            elif isinstance(node, ast.Import):
                targets.extend((tuple(a.name.split(".")[1:]), []) for a in node.names if a.name.split(".")[0] == root.name)
            for target, names in targets:
                if not target or target[0] not in roots or (module and module[0] == target[0]):
                    continue
                key = f"{importer} -> {'.'.join(target)}"
                api = Path(root, *target)
                if ".".join(target) in public_modules:
                    tree = ast.parse(api.with_suffix(".py").read_text(encoding="utf-8"))
                    missing = sorted(set(names) - exported_names(tree) - {"*"})
                    if missing:
                        found[key] = f"imports names the module does not define: {', '.join(missing)}"
                elif not (api / "__init__.py").is_file():
                    found[key] = "imports a submodule of another package"
                else:
                    modules = {name for name in names if ".".join((*target, name)) in public_modules}
                    missing = sorted(set(names) - exported(root, "/".join(target)) - {"*"} - modules)
                    if missing:
                        found[key] = f"imports names the package does not export: {', '.join(missing)}"
    return found


def test_no_module_reaches_into_another_package():
    current = violations()
    new = {key: why for key, why in current.items() if key not in ALLOWED}
    assert not new, "D2: use the package's __init__.py exports instead:\n" + "\n".join(f"{k}: {v}" for k, v in new.items())


def test_a_package_api_loads_no_adapter_or_io_library():
    """Importing a package's __init__ must not pull in an adapter module, SQLite or HTTP."""
    names = sorted(".".join(path.relative_to(SOURCE).parts[:-1]) for path in SOURCE.rglob("__init__.py")
                   if path.parent != SOURCE and path.relative_to(SOURCE).parts[0] not in {"composition", "v1", "desktop"}
                   and "sqlite" not in path.relative_to(SOURCE).parts)
    adapters = sorted(f"ayvu.{name}" for name in ADAPTER_MODULES)
    code = ("import importlib, sys\nfor name in sys.argv[2:]:\n    importlib.import_module('ayvu.' + name)\n"
            "bad = sorted(m for m in sys.modules if m in ('sqlite3', 'requests') or m in sys.argv[1].split(','))\n"
            "print(','.join(bad))")
    result = subprocess.run([sys.executable, "-c", code, ",".join(adapters), *names], capture_output=True, text=True,
                            env={**os.environ, "PYTHONPATH": str(SOURCE.parent)}, timeout=60, check=True)
    assert names and result.stdout.strip() == ""


def test_the_allowed_list_only_shrinks():
    stale = sorted(ALLOWED.keys() - violations().keys())
    assert not stale, f"these violations are gone; remove them from ALLOWED: {stale}"


def write(root: Path, relative: str, text: str = "") -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_the_check_on_a_synthetic_tree(tmp_path):
    root = tmp_path / "ayvu"
    write(root, "__init__.py")
    write(root, "alpha/__init__.py", "from .inner import Public\n__all__ = ['Public']\n")
    write(root, "alpha/inner.py", "class Public: ...\nclass Private: ...\n")
    write(root, "beta/__init__.py")
    write(root, "beta/ok.py", "from ..alpha import Public\nfrom . import ok\n")
    write(root, "beta/deep.py", "from ..alpha.inner import Public\n")
    write(root, "beta/private.py", "from ayvu.alpha import Private\n")
    write(root, "flat.py", "from .alpha.inner import Private\n")
    write(root, "alpha/sub/__init__.py", "from .impl import Tool\n__all__ = ['Tool']\n")
    write(root, "alpha/sub/impl.py", "class Tool: ...\n")
    write(root, "beta/sub_ok.py", "from ..alpha.sub import Tool\n")
    write(root, "alpha/entry.py", "def start(): ...\n")
    write(root, "beta/entry_ok.py", "from ..alpha.entry import start\n")
    write(root, "beta/entry_bad.py", "from ..alpha.entry import stop\n")
    assert violations(root, frozenset({"alpha.entry"})) == {
        "beta.entry_bad -> alpha.entry": "imports names the module does not define: stop",
        "beta.deep -> alpha.inner": "imports a submodule of another package",
        "beta.private -> alpha": "imports names the package does not export: Private",
        "flat -> alpha.inner": "imports a submodule of another package",
    }
    assert violations(root)["beta.entry_ok -> alpha.entry"] == "imports a submodule of another package"
