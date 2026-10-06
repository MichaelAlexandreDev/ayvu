"""Synthetic wxPython UI for the disposable desktop-stack spike."""

from __future__ import annotations

import argparse
import threading

import wx


class SpikeFrame(wx.Frame):
    def __init__(self) -> None:
        super().__init__(None, title="Ayvu — prova de interface wxPython", size=(420, 180))
        self._cancel = threading.Event()
        self._closing = threading.Event()
        self._worker: threading.Thread | None = None

        panel = wx.Panel(self)
        layout = wx.BoxSizer(wx.VERTICAL)
        heading = wx.StaticText(panel, label="Tarefa sintética de progresso")
        self.status = wx.StaticText(panel, label="Nenhum trabalho está em andamento.")
        self.gauge = wx.Gauge(panel, range=100)
        self.gauge.SetName("Progresso da tarefa de demonstração")
        self.start_button = wx.Button(panel, label="Iniciar demonstração")
        self.cancel_button = wx.Button(panel, label="Cancelar")
        self.cancel_button.SetName("Cancelar demonstração")
        self.cancel_button.Disable()

        for control, flag, border in (
            (heading, wx.EXPAND | wx.ALL, 8),
            (self.status, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8),
            (self.gauge, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8),
        ):
            layout.Add(control, 0, flag, border)

        actions = wx.BoxSizer(wx.HORIZONTAL)
        actions.Add(self.start_button, 0, wx.RIGHT, 8)
        actions.Add(self.cancel_button, 0)
        layout.Add(actions, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
        panel.SetSizer(layout)

        self.start_button.Bind(wx.EVT_BUTTON, self.start_work)
        self.cancel_button.Bind(wx.EVT_BUTTON, self.cancel_work)
        self.Bind(wx.EVT_CLOSE, self.close_window)

    def start_work(self, _event: wx.Event) -> None:
        if self._worker is not None:
            return
        self._cancel.clear()
        self.gauge.SetValue(0)
        self.status.SetLabel("Demonstração em andamento.")
        self.start_button.Disable()
        self.cancel_button.Enable()

        def work() -> None:
            for step in range(1, 41):
                if self._cancel.wait(0.1):
                    if not self._closing.is_set():
                        wx.CallAfter(self.finish_work, "Demonstração cancelada.")
                    return
                if not self._closing.is_set():
                    wx.CallAfter(self.update_progress, step * 100 // 40)
            if not self._closing.is_set():
                wx.CallAfter(self.finish_work, "Demonstração concluída.")

        self._worker = threading.Thread(target=work, name="synthetic-progress")
        self._worker.start()

    def cancel_work(self, _event: wx.Event) -> None:
        if self._worker is not None:
            self._cancel.set()
            self.cancel_button.Disable()

    def update_progress(self, value: int) -> None:
        if not self._closing.is_set() and not self.IsBeingDeleted():
            self.gauge.SetValue(value)

    def finish_work(self, message: str) -> None:
        if self._closing.is_set() or self.IsBeingDeleted():
            return
        self.status.SetLabel(message)
        self._worker = None
        self.start_button.Enable()
        self.cancel_button.Disable()

    def close_window(self, event: wx.CloseEvent) -> None:
        self._closing.set()
        self._cancel.set()
        if self._worker is not None:
            self._worker.join(timeout=1.5)
        event.Skip()


class SpikeApp(wx.App):
    def __init__(self, benchmark: bool) -> None:
        self._benchmark = benchmark
        super().__init__(clearSigInt=True)

    def OnInit(self) -> bool:  # noqa: N802 - wx API name
        frame = SpikeFrame()
        self.SetTopWindow(frame)
        frame.Show()
        if self._benchmark:
            wx.CallAfter(self.report_ready)
        return True

    def report_ready(self) -> None:
        print("AYVU_SPIKE_READY", flush=True)
        self.ExitMainLoop()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--benchmark",
        action="store_true",
        help="show the synthetic window, process initial events, then exit",
    )
    args = parser.parse_args()
    app = SpikeApp(args.benchmark)
    app.MainLoop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
