# ADR: Desktop technology evaluation

Status: `Proposed`

Date: 2026-10-02

Evidence updated: 2026-10-06

Related issue: [#124 — DESKTOP-003](https://github.com/DevStarrk1137/ayvu/issues/124)

## Question

Which Python desktop toolkit should Ayvu use for a future accessible interface
that calls the same application use cases as the CLI, while keeping the current
Python 3.11+ core independent of presentation frameworks?

This record evaluates Qt Widgets through PySide6, wxPython Phoenix, and
Tkinter/ttk. It proposes Linux x86_64 as the first evaluation/product target,
with Windows and macOS deferred until native evidence is available. This scope
still needs the maintainer's recorded approval. The actual runtime evidence is
limited to one Arch Linux host; no Desktop platform is currently delivered or
claimed as supported.

## Constraints and decision drivers

- Keep the desktop toolkit in an optional interface adapter and composition
  root. Domain and application code must not import widgets or toolkit types.
- Reuse the shared application use cases; do not duplicate EPUB, cache, HTTP, or
  filesystem rules in the UI.
- Preserve keyboard operation, screen-reader semantics, high-DPI behavior, and
  cooperative background progress/cancellation.
- Keep Python 3.11+ as the production baseline. A toolkit's support for Python
  alone does not prove that its native runtime or wheels are available on every
  target OS and architecture.
- Do not add a production entry point, dependency, or user-facing Desktop
  capability in this issue.

The weights below are provisional decision priorities. Ratings are evidence
scores from 1 (weak or not established) to 5 (strongly documented), not a
complete product evaluation. The total is a prioritization aid; it is
calculated as `sum(rating × weight) / 100`, and is not a substitute for the
remaining manual checks. Published wheel sizes are download sizes; Qt and
Tk now have comparable measured synthetic onedir artifacts.

| Criterion | Weight |
| --- | ---: |
| Accessibility mechanisms and keyboard support | 30% |
| Supported OS/Python combinations | 20% |
| Packaging and dependency footprint | 20% |
| High-DPI behavior | 10% |
| Background work, progress, and cancellation primitives | 10% |
| Ability to remain an optional adapter over the shared core | 10% |

## Evidence and comparison

Evidence links refer to official toolkit, Python, or package sources. They were
consulted on 2026-10-02. Package wheel sizes are download sizes, not measured
Ayvu bundle sizes.

| Candidate | A11y 30% | OS/Python 20% | Package 20% | DPI 10% | Work 10% | Adapter 10% | Weighted score |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| PySide6 / Qt Widgets | 4 | 4 | 2 | 5 | 4 | 5 | 3.80 / 5 |
| wxPython Phoenix | 2 | 3 | 2 | 3 | 4 | 5 | 2.80 / 5 |
| Tkinter / ttk (8.6.16 on this Linux host) | 1 | 4 | 4 | 2 | 3 | 5 | 2.90 / 5 |

The packaging score covers minimal runtime acquisition, wheel availability,
packaging constraints, and licensing risk. Qt remains at 2 after one Linux
onedir measurement of 190.75 MiB: the PySide6-Essentials wheel is about 80.1
MB, but the bundle also carries the Qt runtime and copied platform libraries.
This probe does not include PySide Addons and is not a release installer. Tk's
package score increased after its 45.19 MiB onedir artifact launched outside
the checkout with its bundled Tcl/Tk runtime. Its accessibility score decreased
because the Tk 8.6.16 window was absent from live AT-SPI polling; that result
does not establish Tk 9.1 behavior. wx remains unmeasured: the locked PyPI
resolution has no compatible Linux wheel for this host. Official Ubuntu wheels
exist, but were not treated as validated Arch support. Ratings remain
provisional while manual checks and other OS evidence are missing.

Ratings and limitations:

- **PySide6 / Qt Widgets.** Qt documents accessibility integrations for
  Windows MSAA, macOS Accessibility, and Unix/X11 AT-SPI; standard widgets
  provide accessibility interfaces. Qt documents automatic high-DPI behavior
  for Widgets and worker-thread communication through signals/slots. Its
  current Python support floor is lower than Ayvu's. Qt's support matrix is not
  a guarantee that every PySide wheel exists for every OS/Python/architecture
  combination. The Linux x86_64 PySide6-Essentials 6.11.2 wheel is about
  80.1 MB; the separate Addons wheel is about 175.1 MB. Those figures are a
  footprint warning, not an application-size result. LGPLv3/GPLv3 or commercial
  licensing requires distribution-specific review. Sources: [PySide6 release
  notes](https://doc.qt.io/qtforpython-6/release_notes/pyside6_release_notes.html),
  [Qt 6.11 supported platforms](https://doc.qt.io/qtforpython-6/overviews/qtdoc-supported-platforms.html),
  [QAccessible](https://doc.qt.io/qtforpython-6/PySide6/QtGui/QAccessible.html),
  [Qt high DPI](https://doc.qt.io/qt-6.11/highdpi.html),
  [threads and QObjects](https://doc.qt.io/qt-6.11/threads-qobject.html),
  [PySide package details](https://doc.qt.io/qtforpython-6/package_details.html),
  [Essentials 6.11.2 wheel](https://pypi.org/project/PySide6-Essentials/6.11.2/),
  [Addons 6.11.2 wheel](https://pypi.org/project/PySide6-Addons/6.11.2/),
  [Qt open-source obligations](https://www.qt.io/development/open-source-lgpl-obligations).
- **wxPython Phoenix.** wxPython 4.3.1 declares Python 3.10+ and Windows,
  macOS, and Linux support. Its Linux wheels are distro-specific; the current
  release publishes Ubuntu-specific Linux wheels, so a generic Linux label does
  not guarantee an installable wheel. The official `wx.Accessible` API
  documents MSAA on Windows; this is a gap in cross-platform evidence, not
  proof that native controls are inaccessible elsewhere. The high-DPI guide
  calls out unfinished Python-specific Windows guidance. A current Ubuntu
  x86_64 wheel is roughly 169–170 MB; final application size is unmeasured. The
  wxWindows Library Licence has a binary-distribution exception, but bundled
  dependency notices still need review. Sources: [wxPython 4.3.1 on PyPI](https://pypi.org/project/wxPython/4.3.1/),
  [Phoenix release assets](https://github.com/wxWidgets/Phoenix/releases/expanded_assets/wxPython-4.3.1),
  [wx.Accessible](https://wxpython.org/Phoenix/docs/html/wx.Accessible.html),
  [high-DPI overview](https://wxpython.org/Phoenix/docs/html/high_dpi_overview.html),
  [thread-safe wx functions](https://docs.wxpython.org/wx.functions.html),
  [wxPython licence](https://wxpython.org/pages/license/).
- **Tkinter / ttk.** Tkinter is Python's standard interface to Tcl/Tk, but the
  module and native runtime may be absent from a Python installation. The
  Python 3.11 documentation describes bundled Tcl/Tk 8.6 for official builds.
  Tcl/Tk 9.1 adds screen-reader APIs for Windows, macOS, and X11, but that does
  not establish that a Python 3.11+ distribution or future Ayvu bundle includes
  Tk 9.1. The official Tcl/Tk 9.1 notes list limitations, including canvas and
  image accessibility. Tk's threading model requires short event handlers or
  cooperative worker communication. High-DPI behavior depends on the runtime
  and window system. Sources: [Python 3.11 Tkinter](https://docs.python.org/3.11/library/tkinter.html),
  [Tk 9.1 release](https://www.tcl-lang.org/software/tcltk/9.1.html),
  [Tk 9.1 accessibility](https://www.tcl-lang.org/man/tcl9.1/TkCmd/accessible.html),
  [TIP 733: screen-reader support](https://core.tcl-lang.org/tips/doc/trunk/tip/733.md),
  [Python Tkinter threading model](https://docs.python.org/3.14/library/tkinter.html#threading-model),
  [Python GUI packaging FAQ](https://docs.python.org/3.14/faq/gui.html),
  [Tk licensing terms](https://github.com/tcltk/tk/blob/main/license.terms).

All three candidates can be kept outside the core if the future composition
root constructs the chosen adapter and passes it presentation-neutral
application services. That architectural fit is an inference from the accepted
[modular-monolith boundaries](modular-monolith-boundaries.md), not a toolkit
feature.

## Disposable spike and measured evidence

The spike sources and setup/measurement commands live in
[`spikes/desktop-stack/`](../../spikes/desktop-stack/README.md). They use only a
synthetic progress task and do not intentionally open Ayvu content, user files,
caches, glossaries, or network endpoints. Native toolkit startup may still read
system fonts, plugin/configuration files, and graphical-session services. The
spike-local lock was generated on 2026-10-05; its SHA-256 and the measured
environment are recorded in
[`host-2026-10-05.md`](../../spikes/desktop-stack/results/host-2026-10-05.md).

On that Arch Linux host, the Qt source app had a 0.123102459 s median from new
process start to the toolkit readiness callback across seven runs. Its
PyInstaller onedir artifact measured 200,017,048 logical payload bytes (190.75
MiB). The callback is not proof that pixels reached the compositor, and the OS
page cache was not reset. The packaged executable emitted its readiness marker
once. wxPython had no compatible Linux wheel in the locked PyPI resolution;
Tkinter could not import because `libtk8.6.so` was absent. Those candidates were
not measured in that initial run.

The later [comparative run](../../spikes/desktop-stack/results/host-2026-10-06.md)
used the same Python 3.14.7 host, pinned packager and fresh-process runner for
Qt and Tk. Signed Tcl/Tk 8.6.16 packages were extracted into a private temporary
directory without installing OS packages. The lock remained unchanged.

| Same-host observation | Qt Widgets 6.11.2 | Tkinter/ttk 8.6.16 |
| --- | ---: | ---: |
| Fresh-process median, seven runs | 131.02 ms | 79.09 ms |
| Onedir logical payload | 190.75 MiB | 45.19 MiB |
| Standalone startup outside checkout | Pass | Pass |
| Live AT-SPI application/actions | Observed; action sequence passed | Application not observed in ten polls |
| Automated Space task lifecycle | Pass | Pass with explicit Cancel focus |
| Automated Enter start | Pass | Did not start |
| Automated scale/layout probes | Passed at DPR 1, 1.25, 1.5, 2 | Button rectangles exceeded window height |

Qt's focus transitions needed a correction in the disposable window. After
that correction, keyboard-event, progress, cancellation, restart and completion
probes passed. The AT-SPI probe passed with an optional, version-scoped
workaround for Qt's unimplemented bus-address method and a process-local
Accerciser property-enumeration correction. Those dependencies on workaround
code are maintenance risks, not production recommendations.

Tk is materially smaller and faster in this experiment. Its missing AT-SPI
application and this prototype's layout/Enter findings outweigh that benefit
for the current accessibility requirement. This is an inference limited to
Tk 8.6.16 on the observed host; revising the layout or evaluating Tk 9.1 could
change the result. Toolkit-generated key events and widget captures do not
replace human keyboard, speech, focus-usability or monitor-switch testing.

For this protocol, cold application startup means a fresh process with a new
home/config/cache directory and no retained application state. The runner does
not reset OS page caches. These values must not be represented as controlled
OS cold-cache or reboot-level startup measurements.

The available host was checked on 2026-10-02:

| Item | Observed value |
| --- | --- |
| OS / architecture | Arch Linux rolling / x86_64 |
| Python / libc | Python 3.14.7 / glibc 2.44 |
| Tkinter runtime | Import fails: `libtk8.6.so` is not installed |
| PySide6 / wxPython / PyInstaller | Not installed |
| Orca / Accerciser | Not installed |

At that time no GUI launch, startup timing, application bundle, keyboard test,
or assistive-technology test was performed. The later Qt measurements are
recorded separately in the 2026-10-05 result. That run still does not provide a
controlled cold-cache benchmark: each sample is a new process, but OS
page-cache state is uncontrolled. Readiness is marked by the first app
callback after the window is shown; this does not confirm that the compositor
has presented pixels. The PyInstaller probe counts bundled relative symlinks
without following them, since POSIX onedir bundles may use symlinks; absolute
and escaping links fail closed. See
[PyInstaller's symlink guidance](https://pyinstaller.org/en/latest/common-issues-and-pitfalls.html).

The manual checklist requires keyboard, screen-reader, and scaling results on
every OS later claimed as supported. Linux-only observations cannot establish
Windows or macOS accessibility. The original host snapshot and current
measurements are recorded in
[`results/host-2026-10-02.md`](../../spikes/desktop-stack/results/host-2026-10-02.md)
and
[`results/host-2026-10-05.md`](../../spikes/desktop-stack/results/host-2026-10-05.md).

## Proposed recommendation

**Keep PySide6 with Qt Widgets as a conditional first candidate for a future
desktop vertical**, using only the modules needed by that vertical and keeping
the dependency in an optional interface extra. It has the strongest documented
cross-platform accessibility, high-DPI, and worker/progress primitives among
the candidates reviewed. The measured 190.75 MiB onedir payload is a material
footprint risk; the product has no recorded size limit against which to accept
it. Do not add Qt to the production project in this issue.

This recommendation is reversible and is not yet ready for acceptance. Before a
human accepts it, the team must decide whether the measured footprint fits the
product constraints, complete keyboard and screen-reader checks on the declared
Linux target, and resolve Qt licensing and notices for the exact included-file
inventory. The same-host comparison now supplies that second alternative. Both
bundle scans found no `LICENSE`, `COPYING`, or `NOTICE` files; the
[packaging review](../../spikes/desktop-stack/results/packaging-review-2026-10-06.md)
records the observed library families and a release gate for dependency
notices, source references and license conditions. This is feasibility
evidence, not distribution approval. If Windows or macOS are claimed, repeat
the native checks on each system. If footprint or distribution obligations
fail the product constraints, reconsider the alternatives using the same
criteria.

## Consequences

- The ADR and Desktop capability remain `Proposed`; the live issue stays open
  until its evidence and acceptance criteria have direct results.
- The generated spike `uv.lock` and its digest accompany the current Qt
  measurements; later changes to that lock invalidate direct comparability.
- No production dependency, entry point, screen, lockfile, or supported-OS
  promise is introduced here; the manifest and lock remain spike-local.
- The next implementation issue must invoke shared application use cases and
  keep UI state, widget classes, and toolkit events at the interface boundary.
- Packaging, accessibility, licensing, and platform claims must be rechecked
  against the exact pinned versions and built artifact before release.

## Acceptance record

No human acceptance has been recorded. This ADR must remain `Proposed` until a
maintainer explicitly accepts or rejects the recommendation after the missing
spike evidence is reviewed.
