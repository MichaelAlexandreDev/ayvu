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

from tests.support.public_api_support import ADAPTER_MODULES, ALLOWED, PURE_API_EXPORTS, SOURCE, exported, violations, write


def test_pure_local_resolution_uses_package_exports():
    for package, names in PURE_API_EXPORTS.items():
        assert set(names) <= exported(SOURCE, package.replace(".", "/"))
        assert package not in ADAPTER_MODULES


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
