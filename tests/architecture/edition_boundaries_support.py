"""Static guard against product policy escaping composition.

Resolve imports with the existing boundary reader. Check names, string constants
and comments as well as imports, including dynamic import paths. This does not
claim to detect arbitrary computed Python programs or encoded product identities.
"""
from __future__ import annotations

import ast
import io
import re
import tokenize

from tests.architecture.architecture_boundaries_support import BoundaryError, imports


PRIVATE_MODULES = frozenset({
    "ayvu.composition.editions", "ayvu.composition.feature_catalog", "ayvu.composition.surface_rules",
})
PRODUCT_IDS = frozenset({"lite", "free", "ai", "cli_free", "cli_pro"})
PRODUCT_NAMES = re.compile(r"\b(?:Lite|Free|AI|CLI[ _-](?:Free|Pro|FREE|PRO)|CLI_FREE|CLI_PRO|LITE|FREE)\b")
SELECTOR = re.compile(r"\b[a-zA-Z_]*edition[a-zA-Z_]*\b", re.IGNORECASE)


def _identity(value: str, *, comment=False) -> bool:
    if value.strip().lower() in PRODUCT_IDS:
        return True
    if PRODUCT_NAMES.search(value):
        return True
    if SELECTOR.search(value):
        return True
    if not comment and any(module in value for module in PRIVATE_MODULES):
        return True
    return value.lower() in {"editions", "feature_catalog", "surface_rules"}


def _technical_ai_nodes(source, tree):
    # D9 freezes these two provenance tags. Exempt only their exact enum member,
    # not the module, class, AI attributes elsewhere or commercial selectors.
    allowed = set()
    if source == "ayvu.provenance.model":
        for node in tree.body:
            if not isinstance(node, ast.ClassDef) or node.name not in {"ActorKind", "SourceKind"}:
                continue
            for member in node.body:
                if (isinstance(member, ast.Assign) and len(member.targets) == 1
                        and isinstance(member.targets[0], ast.Name) and member.targets[0].id == "AI"
                        and isinstance(member.value, ast.Constant) and member.value.value == "ai"):
                    allowed.update(ast.walk(member))
    return allowed


def check_edition_boundaries(modules):
    for source, (path, package, tree) in modules.items():
        if source == "ayvu.composition" or source.startswith("ayvu.composition."):
            continue
        targets = imports(source, package, tree, modules)
        if PRIVATE_MODULES.intersection(targets):
            raise BoundaryError(f"Edition data import outside composition: {source}")
        _check_root_imports(source, tree, modules)
        technical = _technical_ai_nodes(source, tree)
        for node in ast.walk(tree):
            if node in technical:
                continue
            values = ()
            if isinstance(node, ast.Name):
                values = (node.id,)
            elif isinstance(node, ast.Attribute):
                values = (node.attr,)
            elif isinstance(node, ast.arg):
                values = (node.arg,)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                values = (node.name,)
            elif isinstance(node, ast.alias):
                values = (node.name, node.asname or "")
            if any(_identity(value) for value in values):
                raise BoundaryError(f"Edition identifier outside composition: {source}")
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and _identity(node.value):
                raise BoundaryError(f"Edition string outside composition: {source}")
        for token in tokenize.generate_tokens(io.StringIO(path.read_text(encoding="utf-8")).readline):
            if token.type == tokenize.COMMENT and _identity(token.string, comment=True):
                raise BoundaryError(f"Edition comment outside composition: {source}")


def _check_root_imports(source, tree, modules):
    """A root exposes locally defined factories/data types, never imported identities."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or not node.module:
            continue
        targets = imports(source, modules[source][1], ast.Module(body=[node], type_ignores=[]), modules)
        for target in targets:
            if target not in {"ayvu.composition.cli", "ayvu.composition.desktop"}:
                continue
            defined = {part.name for part in modules[target][2].body
                       if isinstance(part, (ast.FunctionDef, ast.ClassDef))}
            if any(alias.name not in defined for alias in node.names):
                raise BoundaryError(f"Edition reexport through root: {source}")
