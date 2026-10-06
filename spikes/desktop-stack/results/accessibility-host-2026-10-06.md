# Accessibility session observations — 2026-10-06

This records the host, a silent accessibility-tree inspection, and an
offscreen Qt accessibility-action probe. It is not a keyboard, visual-layout,
or screen-reader pass. The startup and package measurements from the previous
day remain in [`host-2026-10-05.md`](host-2026-10-05.md).

## Host and session

| Item | Observed value |
| --- | --- |
| OS / architecture | Arch Linux rolling / x86_64 |
| Kernel / Python | Linux 6.18.54 LTS / Python 3.14.7 |
| Session | GNOME on Wayland |
| Current logical monitor mode | 1920x1080 at about 60 Hz |
| GNOME logical monitor scale | 1.25 (125%) |
| GNOME text scaling | 1.0 (100%) |
| AT-SPI bus / registry | Available in the user session |
| Orca / Accerciser | Orca 50.2-1 / Accerciser 3.48.0-2 |
| AT-SPI runtime | at-spi2-core 2.60.7-1 / PyAT-SPI available |

The scale was read from GNOME Mutter's current logical monitor state. The
separate XWayland `xrandr` mode is not used to infer the Wayland scale. Orca
was started briefly and produced synthesized speech in English; it was stopped
after the unexpected audio. No spoken announcement was evaluated.

## Qt accessible tree and actions

With Orca stopped, the synthetic Qt window was run in the graphical session
with `QT_LINUX_ACCESSIBILITY_ALWAYS_ON=1`, which Qt documents as an alternative
to enabling the AT-SPI D-Bus status properties
([Qt QAccessible documentation](https://doc.qt.io/QT-6/qaccessible.html)). A
PyAT-SPI query found this tree:

```text
frame: Ayvu — prova de interface Qt
  label: Tarefa sintética de progresso
  label: Estado da tarefa
  progress bar: Progresso da tarefa de demonstração
  button: Iniciar demonstração
  button: Cancelar demonstração
```

An offscreen Qt `QAccessible` probe reported the progress value as `0`, exposed
the start button's `Press` and `SetFocus` actions, and changed the task text
from `Nenhum trabalho está em andamento.` to `Demonstração em andamento.` and
then `Demonstração cancelada.` through the accessible press actions. The
progress value advanced during the run. This checks published names, roles,
values, and Qt actions; it does not check AT-SPI action dispatch in the live
window, spoken announcements, actual keyboard navigation, or visible focus.

## Manual checklist status

- Keyboard navigation, visible focus, Enter/Space activation, cancellation,
  completion, and restart: **Not run**.
- Screen-reader speech, announcement quality, and announcement timing:
  **Not run**. The tree was inspected silently; the English speech from Orca
  startup was not treated as a pass.
- Layout at 100%, 125%, 150%, and 200%: **Not run**. The active 125% scale was
  recorded, but the probe was not visually inspected at that scale.
- Packaged artifact interaction: **Not run in this session**.

A person with access to the desktop must run the
[manual checklist](../MANUAL-CHECKLIST.md) with the screen reader and keyboard
before this candidate can receive an accessibility result.

## Qt / Accerciser compatibility correction

Later on the same host, the live inspector reproduced two separate causes:

- Qt 6.11.2 advertises `GetApplicationBusAddress` in introspection but its
  Application handler does not implement it. See the
  [pinned Qt source](https://github.com/qt/qtbase/blob/v6.11.2/src/gui/accessible/linux/atspiadaptor.cpp).
- Accerciser's API Browser enumerates PyAT-SPI properties that belong to the
  Application interface while inspecting a button that exposes Accessible,
  Action, Collection and Component. Qt correctly has no Application interface
  on that button. The original browser also expects `queryAccessible()`,
  which the installed PyAT-SPI does not provide.

The optional spike workaround answers the bus-address method with an empty
string, leaving the existing accessibility-bus connection in use. This is the
fallback handled by
[libatspi](https://github.com/GNOME/at-spi2-core/blob/main/atspi/atspi-misc.c)
when no private P2P address is available. It delegates every other request to
the original Qt virtual object and retains normal logging.

The process-local API Browser correction avoids Application-only properties
on child controls, while retaining them on the application root. No installed
Qt library or Accerciser plugin was replaced. This Qt workaround depends on
internal registration details and is restricted to the evaluated Linux
Qt 6.11.2; it is not proposed for Ayvu production code.

Validation command (the temporary interpreter existed on this host):

```bash
/usr/bin/python -I spikes/desktop-stack/inspect_qt.py \
  --qt-python /tmp/ayvu-desktop-stack-20261006/qt-env/bin/python --verify
```

The command exited with status 0. It verified Accessible API properties on the
button, Application properties on the root, starting the task, reading live
progress, cancelling, restarting and reaching 100% completion via AT-SPI.
Both original Qt warning messages were absent. A separate `dbind` warning
about an unavailable old session socket (`No such file or directory`) remained
at inspector startup; it did not prevent this verification and was not fixed
by changing system services or hiding logs.

These are automated protocol/action checks. Keyboard, visible focus, speech,
scaling and packaged-artifact checks above remain unverified.
