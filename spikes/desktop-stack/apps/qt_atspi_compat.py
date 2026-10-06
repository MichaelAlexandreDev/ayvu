"""Opt-in workaround for the Qt 6.11.2 Linux AT-SPI bus-address method.

This depends on Qt's internal ``a11y`` connection name and must stay in the
disposable spike. All other messages go to Qt's original virtual object.
"""

from __future__ import annotations

import sys

from PySide6.QtCore import QObject, QTimer, qVersion
from PySide6.QtDBus import QDBusConnection, QDBusVirtualObject

ACCESSIBLE_PATH = "/org/a11y/atspi/accessible"
ROOT_PATH = ACCESSIBLE_PATH + "/root"
APPLICATION_INTERFACE = "org.a11y.atspi.Application"


class BusAddressAdaptor(QDBusVirtualObject):
    def __init__(self, original: QDBusVirtualObject) -> None:
        super().__init__(original.parent())
        self.original = original

    def introspect(self, path: str) -> str:
        return self.original.introspect(path)

    def handleMessage(self, message, connection) -> bool:  # noqa: N802
        if (
            message.path() == ROOT_PATH
            and message.interface() == APPLICATION_INTERFACE
            and message.member() == "GetApplicationBusAddress"
            and not message.arguments()
        ):
            # Qt has no private P2P bus. An empty address keeps the client on
            # the existing accessibility bus; it is not the session-bus URL.
            return connection.send(message.createReply([""]))
        return self.original.handleMessage(message, connection)


class BusAddressCompatibility(QObject):
    def __init__(self, parent: QObject) -> None:
        super().__init__(parent)
        if sys.platform != "linux" or qVersion() != "6.11.2":
            raise RuntimeError("AT-SPI workaround is only evaluated on Linux Qt 6.11.2")
        self.adaptor: BusAddressAdaptor | None = None
        self.timer = QTimer(self)
        self.timer.setInterval(1)
        self.timer.timeout.connect(self.try_install)
        self.timer.start()
        QTimer.singleShot(5000, self.stop_waiting)

    def try_install(self) -> None:
        connection = QDBusConnection("a11y")
        if not connection.isConnected():
            return
        original = connection.objectRegisteredAt(ACCESSIBLE_PATH)
        if original is None:
            return
        self.timer.stop()
        if not isinstance(original, QDBusVirtualObject):
            print("AT-SPI workaround skipped: unexpected Qt adaptor", file=sys.stderr)
            return
        adaptor = BusAddressAdaptor(original)
        connection.unregisterObject(ACCESSIBLE_PATH)
        if not connection.registerVirtualObject(
            ACCESSIBLE_PATH, adaptor, QDBusConnection.SubPath
        ):
            restored = connection.registerVirtualObject(
                ACCESSIBLE_PATH, original, QDBusConnection.SubPath
            )
            if not restored:
                print("AT-SPI adaptor restoration failed", file=sys.stderr)
            print("AT-SPI workaround registration failed", file=sys.stderr)
            return
        self.adaptor = adaptor
        print("AYVU_QT_ATSPI_COMPAT_READY", flush=True)

    def stop_waiting(self) -> None:
        if self.timer.isActive():
            self.timer.stop()
            print("AT-SPI workaround unavailable: Qt bus did not initialize", file=sys.stderr)
