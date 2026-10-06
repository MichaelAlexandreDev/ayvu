"""Synthetic Tkinter/ttk UI for the disposable desktop-stack spike."""

from __future__ import annotations

import argparse
import queue
import threading
import tkinter as tk
from tkinter import ttk


class SpikeWindow:
    def __init__(self, root: tk.Tk, benchmark: bool) -> None:
        self.root = root
        self.events: queue.Queue[tuple[str, int | str]] = queue.Queue()
        self.cancel = threading.Event()
        self.worker: threading.Thread | None = None

        root.title("Ayvu — prova de interface Tk")
        root.minsize(360, 160)
        content = ttk.Frame(root, padding=12)
        content.grid(sticky="nsew")
        root.columnconfigure(0, weight=1)
        root.rowconfigure(0, weight=1)
        content.columnconfigure(0, weight=1)

        heading = ttk.Label(content, text="Tarefa sintética de progresso")
        self.status = ttk.Label(content, text="Nenhum trabalho está em andamento.")
        self.progress = ttk.Progressbar(content, maximum=100, mode="determinate")
        self.start_button = ttk.Button(
            content, text="Iniciar demonstração", command=self.start_work
        )
        self.cancel_button = ttk.Button(
            content, text="Cancelar", command=self.cancel_work, state="disabled"
        )

        for row, widget in enumerate((heading, self.status, self.progress)):
            widget.grid(row=row, column=0, columnspan=2, sticky="ew", pady=(0, 8))
        self.start_button.grid(row=3, column=0, sticky="w", padx=(0, 8))
        self.cancel_button.grid(row=3, column=1, sticky="w")
        root.geometry("420x180")
        root.protocol("WM_DELETE_WINDOW", self.close_window)
        self._poll_events()

        if benchmark:
            root.after_idle(self.report_ready)

    def report_ready(self) -> None:
        print("AYVU_SPIKE_READY", flush=True)
        self.root.destroy()

    def start_work(self) -> None:
        if self.worker is not None:
            return
        self.cancel.clear()
        self.progress.configure(value=0)
        self.status.configure(text="Demonstração em andamento.")
        self.start_button.configure(state="disabled")
        self.cancel_button.configure(state="normal")

        def work() -> None:
            for step in range(1, 41):
                if self.cancel.wait(0.1):
                    self.events.put(("message", "Demonstração cancelada."))
                    self.events.put(("done", ""))
                    return
                self.events.put(("progress", step * 100 // 40))
            self.events.put(("message", "Demonstração concluída."))
            self.events.put(("done", ""))

        self.worker = threading.Thread(target=work, name="synthetic-progress")
        self.worker.start()

    def cancel_work(self) -> None:
        if self.worker is not None:
            self.cancel.set()
            self.cancel_button.configure(state="disabled")

    def close_window(self) -> None:
        self.cancel.set()
        if self.worker is not None:
            self.worker.join(timeout=1.5)
        self.root.destroy()

    def _poll_events(self) -> None:
        try:
            while True:
                kind, value = self.events.get_nowait()
                if kind == "progress":
                    self.progress.configure(value=value)
                elif kind == "message":
                    self.status.configure(text=value)
                elif kind == "done":
                    self.worker = None
                    self.start_button.configure(state="normal")
                    self.cancel_button.configure(state="disabled")
        except queue.Empty:
            pass
        if self.root.winfo_exists():
            self.root.after(50, self._poll_events)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--benchmark",
        action="store_true",
        help="show the synthetic window, process initial events, then exit",
    )
    args = parser.parse_args()
    root = tk.Tk()
    SpikeWindow(root, args.benchmark)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
