"""Launch the Qt spike with a scoped Accerciser API-view correction.

Run using the system Python that supplies GI, pyatspi and Accerciser. The Qt
interpreter is passed separately; no system or user plugins are overwritten.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys
import time
from types import MethodType
import uuid


APPLICATION_PROPERTIES = {"id", "toolkitName", "toolkitVersion", "atspiVersion"}


class AccessibleProperties:
    """Avoid invoking Application-only getters on an Accessible child."""

    def __init__(self, accessible, application: bool) -> None:
        self.accessible = accessible
        self.application = application

    def __dir__(self):
        return sorted(
            name for name in dir(self.accessible)
            if self.application or name not in APPLICATION_PROPERTIES
        )

    def __getattr__(self, name):
        return getattr(self.accessible, name)


def configure_api_browser(browser, pyatspi) -> None:
    original_refresh = browser._refreshAttribs
    original_populate = browser._popAttribViews

    def refresh(self, widget):
        if self.iface_combo.get_active_text() == "Accessible":
            self.curr_iface = self.acc
            self._popAttribViews()
        else:
            original_refresh(widget)

    def populate(self):
        original = self.curr_iface
        if isinstance(original, pyatspi.Accessible):
            self.curr_iface = AccessibleProperties(
                original, "Application" in pyatspi.listInterfaces(original)
            )
        try:
            original_populate()
        finally:
            self.curr_iface = original

    # Replace the callbacks already bound during plugin construction.
    browser.iface_combo.disconnect_by_func(original_refresh)
    browser.private_toggle.disconnect_by_func(original_refresh)
    browser._refreshAttribs = MethodType(refresh, browser)
    browser._popAttribViews = MethodType(populate, browser)
    browser.iface_combo.connect("changed", browser._refreshAttribs)
    browser.private_toggle.connect("toggled", browser._refreshAttribs)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--qt-python", required=True, type=Path)
    parser.add_argument("--verify", action="store_true", help="exercise AT-SPI and close both windows")
    args = parser.parse_args()
    # Keep the venv interpreter path: resolving its symlink loses the venv.
    interpreter = args.qt_python.absolute()
    if not interpreter.is_file():
        parser.error("Qt Python interpreter does not exist")

    os.environ["GDK_BACKEND"] = "x11"
    import gi

    gi.require_version("Gtk", "3.0")
    gi.require_version("Gdk", "3.0")
    gi.require_version("Wnck", "3.0")
    from gi.repository import GLib, Gio
    import pyatspi
    from accerciser.accerciser import Main

    app_path = Path(__file__).parent / "apps" / "qt_widgets.py"
    name = "Ayvu-Qt-inspect-" + uuid.uuid4().hex[:8]
    output = sys.stdout
    child = None
    passed = False
    failed = False
    deadline = 0.0
    phase = "start"

    def press(node):
        action = node.queryAction()
        index = next(i for i in range(action.nActions) if action.getName(i).lower() in ("press", "click"))
        assert action.doAction(index), "AT-SPI rejected the button action"

    def find(node, target):
        if node.name == target:
            return node
        for item in node:
            result = find(item, target)
            if result is not None:
                return result
        return None

    class Inspector(Main):
        def do_activate(self):
            if self.window is None:
                # Accerciser enters the AT-SPI event loop inside do_activate;
                # prepare the probe from that loop after plugins are loaded.
                GLib.idle_add(self.prepare_probe)
            super().do_activate()

        def prepare_probe(self):
            nonlocal failed
            try:
                self.activate_probe()
            except Exception as exc:
                failed = True
                print(f"FALHA NA ABERTURA: {exc!r}", file=output, flush=True)
                self._onQuit(None)
            return GLib.SOURCE_REMOVE

        def activate_probe(self):
            nonlocal child, deadline
            browser = next(
                row[self.plugin_manager.COL_INSTANCE] for row in self.plugin_manager
                if row[self.plugin_manager.COL_CLASS].__name__ == "APIBrowser"
            )
            if browser is None:
                raise RuntimeError("Accerciser API Browser plugin is unavailable")
            configure_api_browser(browser, pyatspi)
            self.browser = browser
            env = os.environ.copy()
            env["QT_LINUX_ACCESSIBILITY_ALWAYS_ON"] = "1"
            env["QT_QPA_PLATFORM"] = "xcb"
            code = (
                "import sys; path, name = sys.argv[1:3]; "
                "sys.path.insert(0, str(__import__('pathlib').Path(path).parent)); "
                "import qt_widgets; sys.argv = [name, '--atspi-compat']; "
                "raise SystemExit(qt_widgets.main())"
            )
            child = subprocess.Popen([str(interpreter), "-I", "-c", code, str(app_path), name], env=env)
            deadline = time.monotonic() + 15
            GLib.timeout_add(200, inspect)

    app = Inspector(flags=Gio.ApplicationFlags.NON_UNIQUE)
    app.hold()

    def inspect():
        nonlocal phase, passed, failed
        try:
            if child.poll() is not None:
                raise RuntimeError(f"Qt exited with status {child.returncode}")
            if time.monotonic() > deadline:
                raise RuntimeError("AT-SPI inspection timed out")
            root = next((a for a in pyatspi.Registry.getDesktop(0) if a.name == name), None)
            if root is None:
                return GLib.SOURCE_CONTINUE
            start = find(root, "Iniciar demonstração")
            if start is None:
                return GLib.SOURCE_CONTINUE
            progress = find(root, "Progresso da tarefa de demonstração")
            cancel = find(root, "Cancelar demonstração")
            if phase == "start":
                app.node.update(start)
                properties = {row[0] for row in app.browser.property_tree.get_model()}
                assert "name" in properties and not properties & APPLICATION_PROPERTIES
                # Application properties remain available on the root.
                app.node.update(root)
                root_properties = {row[0] for row in app.browser.property_tree.get_model()}
                assert APPLICATION_PROPERTIES <= root_properties
                assert root.toolkitName == "Qt"
                app.node.update(start)
                print("PRONTO: Qt selecionado; API consultada sem propriedades inválidas.", file=output, flush=True)
                if not args.verify:
                    return GLib.SOURCE_REMOVE
                press(start)
                phase = "cancel"
            elif phase == "cancel" and progress.queryValue().currentValue > 0:
                assert cancel.getState().contains(pyatspi.STATE_ENABLED)
                press(cancel)
                phase = "restart"
            elif phase == "restart" and start.getState().contains(pyatspi.STATE_ENABLED):
                assert 0 < progress.queryValue().currentValue < 100
                assert not cancel.getState().contains(pyatspi.STATE_ENABLED)
                press(start)
                phase = "complete"
            elif phase == "complete" and start.getState().contains(pyatspi.STATE_ENABLED):
                assert progress.queryValue().currentValue == 100
                assert not cancel.getState().contains(pyatspi.STATE_ENABLED)
                passed = True
                print("OK: API, início, progresso, cancelamento, reinício e conclusão via AT-SPI.", file=output, flush=True)
                app._onQuit(None)
                return GLib.SOURCE_REMOVE
            return GLib.SOURCE_CONTINUE
        except Exception as exc:
            failed = True
            print(f"FALHA: {exc!r}", file=output, flush=True)
            app._onQuit(None)
            return GLib.SOURCE_REMOVE

    try:
        result = app.run(["accerciser-ayvu-qt"])
    finally:
        if child is not None and child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
    if failed or (args.verify and not passed):
        return 1
    return result


if __name__ == "__main__":
    raise SystemExit(main())
