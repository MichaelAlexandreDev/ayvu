"""Automated native-widget checks; not a manual screen-reader assessment."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import time


def qt_probe(output: Path, scale: str, native_dpr: float = 1) -> dict:
    os.environ["QT_QPA_PLATFORM"] = "xcb"
    os.environ["QT_SCALE_FACTOR"] = str(float(scale) / native_dpr)
    from PySide6.QtCore import Qt, qVersion
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication, QLabel, QPushButton
    sys.path.insert(0, str(Path(__file__).parent / "apps"))
    from qt_widgets import SpikeWindow

    app = QApplication(["ayvu-qt-native-probe"])
    def pump(seconds):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            app.processEvents()
            # Give the Python QThread worker the GIL between event batches.
            time.sleep(0.01)

    window = SpikeWindow()
    window.resize(420, 180)
    window.show()
    window.activateWindow()
    window.start_button.setFocus()
    pump(0.2)
    checks = {}
    checks["initial_focus"] = window.start_button.hasFocus()
    checks["disabled_cancel_skipped"] = not window.cancel_button.isEnabled()
    QTest.keyClick(window.start_button, Qt.Key_Tab)
    checks["tab_skips_disabled_cancel"] = window.start_button.hasFocus()
    QTest.keyClick(window.start_button, Qt.Key_Tab, Qt.ShiftModifier)
    checks["shift_tab_skips_disabled_cancel"] = window.start_button.hasFocus()

    QTest.keyClick(window.start_button, Qt.Key_Space)
    pump(0.3)
    checks["keyboard_starts"] = window._worker is not None and window.progress.value() > 0
    checks["cancel_receives_focus"] = window.cancel_button.hasFocus()
    QTest.keyClick(window.cancel_button, Qt.Key_Space)
    pump(0.25)
    checks["space_cancels"] = window._worker is None and "cancelada" in window.status.text()
    checks["focus_returns_to_start"] = window.start_button.hasFocus()
    QTest.keyClick(window.start_button, Qt.Key_Space)
    pump(4.4)
    checks["space_restarts_and_completes"] = window._worker is None and window.progress.value() == 100
    checks["completed_state"] = "concluída" in window.status.text()
    window.start_button.setFocus()
    QTest.keyClick(window.start_button, Qt.Key_Return)
    pump(0.3)
    checks["return_starts"] = window._worker is not None and window.progress.value() > 0
    if window._worker is not None:
        window.cancel_work()
        pump(0.2)
    layout = []
    for widget in window.findChildren(QLabel) + window.findChildren(QPushButton):
        text_width = widget.fontMetrics().horizontalAdvance(widget.text())
        layout.append({
            "text": widget.text(), "width": widget.width(),
            "text_width": text_width,
            "text_fits": text_width <= widget.contentsRect().width(),
            "geometry_fits": window.rect().contains(widget.mapTo(window, widget.rect().topLeft()))
                and window.rect().contains(widget.mapTo(window, widget.rect().bottomRight())),
        })
    output.mkdir(parents=True, exist_ok=True)
    snapshot = output / f"qt-scale-{scale}.png"
    if not window.grab().save(str(snapshot)):
        raise RuntimeError("could not save the synthetic window snapshot")
    result = {
        "candidate": "qt", "qt": qVersion(), "python": sys.version.split()[0],
        "requested_scale_factor": scale, "device_pixel_ratio": window.devicePixelRatioF(),
        "native_device_pixel_ratio": native_dpr,
        "keyboard_delivery": "QTest events to live widgets; not physical keyboard input",
        "checks": checks, "layout": layout, "snapshot": snapshot.name,
    }
    window.close()
    app.processEvents()
    return result


def tk_probe(output: Path, scale: str) -> dict:
    import tkinter as tk
    from tkinter import font
    sys.path.insert(0, str(Path(__file__).parent / "apps"))
    from tk_widgets import SpikeWindow

    root = tk.Tk()
    root.tk.call("tk", "scaling", (96 / 72) * float(scale))
    window = SpikeWindow(root, False)

    def pump(seconds):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            root.update()
            time.sleep(0.01)

    pump(0.2)
    window.start_button.focus_force()
    pump(0.1)
    checks = {"initial_focus": root.focus_get() == window.start_button}
    window.start_button.event_generate("<KeyPress-Tab>")
    window.start_button.event_generate("<KeyRelease-Tab>")
    pump(0.1)
    checks["tab_skips_disabled_cancel"] = root.focus_get() == window.start_button
    window.start_button.event_generate("<KeyPress-Return>")
    window.start_button.event_generate("<KeyRelease-Return>")
    pump(0.2)
    checks["return_starts"] = window.worker is not None
    if window.worker is None:
        window.start_button.event_generate("<KeyPress-space>")
        window.start_button.event_generate("<KeyRelease-space>")
        pump(0.2)
    checks["keyboard_starts"] = window.worker is not None and float(window.progress["value"]) > 0
    window.cancel_button.focus_set()
    window.cancel_button.event_generate("<KeyPress-space>")
    window.cancel_button.event_generate("<KeyRelease-space>")
    pump(0.3)
    checks["space_cancels"] = window.worker is None and "cancelada" in window.status["text"]
    window.start_button.focus_set()
    window.start_button.event_generate("<KeyPress-space>")
    window.start_button.event_generate("<KeyRelease-space>")
    pump(4.4)
    checks["space_restarts_and_completes"] = window.worker is None and float(window.progress["value"]) == 100
    layout = []
    for widget in (window.status, window.start_button, window.cancel_button):
        text = str(widget["text"])
        from tkinter import ttk
        style_font = ttk.Style(root).lookup(widget.winfo_class(), "font") or "TkDefaultFont"
        width = font.Font(root=root, font=style_font).measure(text)
        layout.append({
            "text": text, "width": widget.winfo_width(), "text_width": width,
            "text_fits": width <= widget.winfo_width(),
            "geometry_fits": widget.winfo_rooty() + widget.winfo_height() <= root.winfo_rooty() + root.winfo_height(),
        })
    result = {
        "candidate": "tk", "tcl": root.tk.call("info", "patchlevel"),
        "tk": root.tk.call("package", "require", "Tk"), "python": sys.version.split()[0],
        "requested_scale_factor": scale, "actual_tk_scaling": root.tk.call("tk", "scaling"),
        "keyboard_delivery": "Tk generated events to live widgets; cancel focus explicitly set",
        "checks": checks, "layout": layout,
    }
    window.close_window()
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("candidate", choices=("qt", "tk"))
    parser.add_argument("--scale", choices=("1", "1.25", "1.5", "2"), default="1")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--native-dpr", type=float, default=1,
                        help="measured native Qt device-pixel ratio before QT_SCALE_FACTOR")
    args = parser.parse_args()
    if not 0.5 <= args.native_dpr <= 4:
        parser.error("--native-dpr must be between 0.5 and 4")
    result = qt_probe(args.output, args.scale, args.native_dpr) if args.candidate == "qt" else tk_probe(args.output, args.scale)
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / f"{args.candidate}-scale-{args.scale}.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(result, ensure_ascii=False))
    # A failed observation is valid research evidence, not a passing test.
    return 0 if all(result["checks"].values()) and all(
        item["text_fits"] and item["geometry_fits"] for item in result["layout"]
    ) else 1


if __name__ == "__main__":
    raise SystemExit(main())
