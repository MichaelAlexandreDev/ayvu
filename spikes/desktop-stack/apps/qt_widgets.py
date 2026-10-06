"""Synthetic Qt Widgets UI for the disposable desktop-stack spike."""

from __future__ import annotations

import argparse
import sys

from PySide6.QtCore import QThread, QTimer, Signal
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)


class SyntheticWorker(QThread):
    progress = Signal(int)
    message = Signal(str)

    def run(self) -> None:
        for step in range(1, 41):
            if self.isInterruptionRequested():
                self.message.emit("Demonstração cancelada.")
                return
            self.msleep(100)
            self.progress.emit(step * 100 // 40)
        self.message.emit("Demonstração concluída.")


class SpikeWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Ayvu — prova de interface Qt")
        self._worker: SyntheticWorker | None = None

        heading = QLabel("Tarefa sintética de progresso")
        heading.setAccessibleName("Tarefa sintética de progresso")

        self.status = QLabel("Nenhum trabalho está em andamento.")
        self.status.setAccessibleName("Estado da tarefa")
        self.status.setAccessibleDescription(
            "Anuncia o estado da demonstração local, sem abrir arquivos."
        )

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setAccessibleName("Progresso da tarefa de demonstração")

        self.start_button = QPushButton("Iniciar demonstração")
        self.start_button.setAccessibleName("Iniciar demonstração")
        self.cancel_button = QPushButton("Cancelar")
        self.cancel_button.setAccessibleName("Cancelar demonstração")
        self.cancel_button.setEnabled(False)

        actions = QHBoxLayout()
        actions.addWidget(self.start_button)
        actions.addWidget(self.cancel_button)

        content = QWidget()
        layout = QVBoxLayout(content)
        layout.addWidget(heading)
        layout.addWidget(self.status)
        layout.addWidget(self.progress)
        layout.addLayout(actions)
        self.setCentralWidget(content)

        self.setTabOrder(self.start_button, self.cancel_button)
        self.start_button.clicked.connect(self.start_work)
        self.cancel_button.clicked.connect(self.cancel_work)

    def start_work(self) -> None:
        if self._worker is not None:
            return
        self.progress.setValue(0)
        self.status.setText("Demonstração em andamento.")
        self.start_button.setEnabled(False)
        self.cancel_button.setEnabled(True)
        self.cancel_button.setFocus()

        worker = SyntheticWorker(self)
        worker.progress.connect(self.progress.setValue)
        worker.message.connect(self.status.setText)
        worker.finished.connect(self.finish_work)
        self._worker = worker
        worker.start()

    def cancel_work(self) -> None:
        if self._worker is not None:
            self._worker.requestInterruption()
            self.cancel_button.setEnabled(False)

    def finish_work(self) -> None:
        self._worker = None
        self.start_button.setEnabled(True)
        self.cancel_button.setEnabled(False)

        self.start_button.setFocus()

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt API name
        if self._worker is not None:
            self._worker.requestInterruption()
            self._worker.wait(1500)
        event.accept()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--atspi-compat",
        action="store_true",
        help="enable the experimental Linux Qt 6.11.2 AT-SPI bus-address workaround",
    )
    parser.add_argument(
        "--benchmark",
        action="store_true",
        help="show the synthetic window, process initial events, then exit",
    )
    args = parser.parse_args()

    app = QApplication(sys.argv[:1])
    if args.atspi_compat:
        from qt_atspi_compat import BusAddressCompatibility

        app._atspi_compat = BusAddressCompatibility(app)
    window = SpikeWindow()
    window.resize(420, 180)
    window.show()
    if args.benchmark:
        def report_ready() -> None:
            print("AYVU_SPIKE_READY", flush=True)
            app.quit()

        QTimer.singleShot(0, report_ready)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
