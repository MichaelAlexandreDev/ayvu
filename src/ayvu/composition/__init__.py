"""Composition roots (D5 of docs/architecture/decisions/package-structure.md).

The one place that knows both the use cases and the concrete adapters behind them. Each root is a public
module of its own, so an interface loads only its root: the CLI imports ``composition.cli`` and the desktop
``composition.desktop``. The ``builders`` assemble one capability each and are used only by the roots.
"""
