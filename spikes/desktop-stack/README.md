# Desktop stack spike

Disposable, synthetic UI probes for issue #124. These programs do not import
Ayvu production code or intentionally open EPUBs, user files, caches,
glossaries, or network endpoints. A native toolkit can still read system fonts,
plugins, configuration, and graphical-session services during startup. Do not
treat the probes as a Desktop product or a core API example.

## Environment and dependency isolation

The spike has its own project manifest. It does not alter the root
`pyproject.toml` or `uv.lock`. Use Python 3.11+ and a graphical session on the
OS being evaluated. Extras select each candidate's dependencies, but all
commands below use the same `UV_PROJECT_ENVIRONMENT`; each `uv sync` replaces
the installed package set for that environment. Run the matching `uv sync`
before each candidate, or use a separate environment path per candidate to
keep them installed side by side:

```bash
SPIKE_WORK_ROOT="$(mktemp -d)"
export UV_CACHE_DIR="$SPIKE_WORK_ROOT/uv-cache"
export UV_PROJECT_ENVIRONMENT="$SPIKE_WORK_ROOT/venv"
uv lock --project spikes/desktop-stack
uv sync --project spikes/desktop-stack --extra qt --extra packager --locked --no-build
uv sync --project spikes/desktop-stack --extra wx --extra packager --locked --no-build
uv sync --project spikes/desktop-stack --extra packager --locked --no-build
```

The third command is for Tkinter; its Tcl/Tk runtime is supplied by the Python
installation or OS and must be recorded separately. The commands create the
spike-local environment and lock only. They do not add production dependencies.
Run these commands in one shell session so the isolated cache and environment
paths remain set. Record the exact Python, toolkit, OS, architecture, libc,
display server, and scale factor. The transitive resolution is fixed by the
generated spike `uv.lock`. Keep it unchanged for all runs in one evaluation and
record its digest with each result so the exact dependency resolution can be
identified.

`--no-build` refuses a source distribution without a compatible wheel instead
of compiling it on the host. That keeps toolkit build backends out of this
spike; a source-only candidate must be evaluated separately with an explicit
build environment.

The work root and package build root are disposable generated directories and
are not cleaned automatically. Retain any needed evidence, then remove those
private temporary directories using the host's normal cleanup method. The
benchmark narrows the child environment and uses temporary home/cache/config
paths, but it is not an OS sandbox. On Linux it passes through graphical and
session endpoints such as `DISPLAY`, `WAYLAND_DISPLAY`, `XDG_RUNTIME_DIR`,
`XAUTHORITY`, and `DBUS_SESSION_BUS_ADDRESS` when present; those can let the
probe interact with the logged-in desktop session. It also inherits `PATH` to
load its interpreter and libraries. Reduced environment variables and
temporary directories do not restrict filesystem or network access. The `uv`
lock/sync/build commands and direct UI launches run with the current account's
permissions; use only trusted, pinned wheels. Evaluate untrusted dependencies
under a disposable OS account or virtual machine with suitable filesystem and
network restrictions.

## Keyboard and progress probe

Run one candidate directly in its matching environment:

```bash
uv run --project spikes/desktop-stack --extra qt --locked --no-build python spikes/desktop-stack/apps/qt_widgets.py
uv run --project spikes/desktop-stack --extra wx --locked --no-build python spikes/desktop-stack/apps/wx_widgets.py
uv run --project spikes/desktop-stack --locked --no-build python spikes/desktop-stack/apps/tk_widgets.py
```

Each window uses a synthetic four-second task. Try the start and cancel buttons,
tab/shift-tab, Enter/Space, focus visibility, progress announcements, and
restarting after cancellation. The worker does no I/O. A toolkit API or widget
name is not evidence that an assistive technology announces the right role,
label, value, and state; complete the
[manual checklist](MANUAL-CHECKLIST.md) with the actual screen reader.

## Qt / Accerciser compatibility on the evaluated Linux host

On Linux with Qt 6.11.2, the AT-SPI adaptor advertises
`GetApplicationBusAddress` but does not implement it. The opt-in
`--atspi-compat` flag supplies an empty address, keeping clients on the existing
accessibility bus. Other messages and introspection are delegated to Qt's
original adaptor. This workaround depends on Qt's internal connection name
and was evaluated only with the pinned Qt 6.11.2. It is spike code, not a
production fix or a reason to disable accessibility warnings.

The inspector launcher also corrects Accerciser's API Browser: it uses the
accessible object for the Accessible view and queries Application properties
only when that interface is present. Select the application root to inspect
`id`, `toolkitName`, `toolkitVersion`, and `atspiVersion`; those properties do
not belong to individual buttons. The correction applies only to this
inspector process and does not overwrite installed plugins.

With the Qt environment already synced, run the system Python that has
Accerciser, GI and PyAT-SPI installed, passing the Qt interpreter separately:

```bash
/usr/bin/python -I spikes/desktop-stack/inspect_qt.py \
  --qt-python "$UV_PROJECT_ENVIRONMENT/bin/python"
```

The launcher opens both windows and closes its Qt child when the inspector
exits. Add `--verify` to exercise API properties, start, progress, cancellation,
restart and completion through live AT-SPI actions, then close both windows.
This checks protocol behavior, not keyboard navigation or screen-reader speech.
The launcher uses XWayland (`xcb` for Qt and `x11` for GTK) on the evaluated
GNOME Wayland session. It does not start Orca or change desktop settings.

Run the Qt window alone with the workaround using its environment:

```bash
uv run --project spikes/desktop-stack --extra qt --locked --no-build python \
  spikes/desktop-stack/apps/qt_widgets.py --atspi-compat
```

Keep startup and bundle measurements without this optional flag, so the
original benchmark protocol remains comparable.

## Fresh-process startup measurement

Use the same run count for all available candidates:

```bash
uv run --project spikes/desktop-stack --extra qt --locked --no-build python spikes/desktop-stack/benchmark_startup.py qt --runs 7
uv run --project spikes/desktop-stack --extra wx --locked --no-build python spikes/desktop-stack/benchmark_startup.py wx --runs 7
uv run --project spikes/desktop-stack --locked --no-build python spikes/desktop-stack/benchmark_startup.py tk --runs 7
```

The benchmark starts a new process per sample and records the app's readiness
marker from its first callback after `show()`. The first sample is reported
separately; the median covers all samples. This measures fresh-process startup
to the toolkit readiness callback, not confirmed presentation of pixels by the
window compositor. OS page-cache state is not reset, so it is not a controlled
cold-cache measurement. Run on an idle machine, use the same display
session/scale, and retain the JSON output without book or user data. The child
gets a reduced environment, a temporary home/cache/config directory, and only
the session variables needed to open its GUI; proxy and Python injection
variables are omitted.

## Onedir package measurement

Build with the same pinned PyInstaller version on a POSIX target. Example for
Qt; substitute `wx` or `tk` and the matching app file for the other candidates:

```bash
SPIKE_BUILD_ROOT="$(python3 spikes/desktop-stack/prepare_bundle_root.py)"
mkdir -p "$SPIKE_BUILD_ROOT/qt/dist" "$SPIKE_BUILD_ROOT/qt/work" "$SPIKE_BUILD_ROOT/qt/spec"
uv run --project spikes/desktop-stack --extra qt --extra packager --locked --no-build pyinstaller \
  --noconfirm --clean --onedir --name ayvu-qt-spike \
  --distpath "$SPIKE_BUILD_ROOT/qt/dist" \
  --workpath "$SPIKE_BUILD_ROOT/qt/work" \
  --specpath "$SPIKE_BUILD_ROOT/qt/spec" \
  spikes/desktop-stack/apps/qt_widgets.py
python3 spikes/desktop-stack/measure_bundle.py "$SPIKE_BUILD_ROOT" qt
```

The output counts each unique regular file's logical size plus the text of
relative in-bundle symlinks without following them. This includes shared
libraries copied into the artifact by PyInstaller; only runtime libraries
provided by the target OS outside the artifact are excluded. Absolute links and
relative targets whose normalized path escapes the artifact are rejected;
symlinks are counted by their target text and are not followed. The helper
reports the tree it finds but does not validate that it contains an executable
or represents a complete build, so launch and inspect the artifact separately.
Inaccessible files fail the measurement.
The private temporary root prevents other users from replacing build paths.
This helper supports POSIX hosts. It does not measure a signed installer,
notarization, download compression, or libraries supplied by the OS. Windows
bundle measurement needs a native equivalent; this command does not imply
cross-compilation or a release packaging recipe. The `SPIKE_BUILD_ROOT` path is
created with private permissions and can be removed after retaining results.

## Reproduce the Linux Tk comparison without an OS install

The host Python can import Tk once its matching native runtime is available.
For this evaluation, official signed Arch Tcl/Tk 8.6.16 packages were extracted
into `$SPIKE_BUILD_ROOT/native-runtime`. Package URLs, SHA-256 digests and
signature provenance are recorded in
[the comparative result](results/host-2026-10-06.md). Download only those
trusted inputs, verify the detached signatures using the Arch keyring, and
extract into a new private directory. This is an explicit native-runtime
experiment, not a fallback that downloads packages during application startup.

The extraction contains `usr/lib/libtcl8.6.so`, `usr/lib/libtk8.6.so`,
`usr/lib/tcl8.6/init.tcl` and `usr/lib/tk8.6/tk.tcl`. Measure using the same
Python and run count as Qt, sequentially after builds have finished:

```bash
uv run --project spikes/desktop-stack --extra qt --extra packager --locked --no-build python \
  spikes/desktop-stack/benchmark_startup.py qt --runs 7
uv run --project spikes/desktop-stack --extra qt --extra packager --locked --no-build python \
  spikes/desktop-stack/benchmark_startup.py tk --runs 7 \
  --tk-runtime-root "$SPIKE_BUILD_ROOT/native-runtime"
```

For the Tk PyInstaller build, set the loader and Tcl paths explicitly for that
command. The onedir copies the runtime; the smoke launch must remove these
paths and run outside the checkout to prove the artifact can start alone.

```bash
LD_LIBRARY_PATH="$SPIKE_BUILD_ROOT/native-runtime/usr/lib" \
TCL_LIBRARY="$SPIKE_BUILD_ROOT/native-runtime/usr/lib/tcl8.6" \
TK_LIBRARY="$SPIKE_BUILD_ROOT/native-runtime/usr/lib/tk8.6" \
uv run --project spikes/desktop-stack --extra qt --extra packager --locked --no-build pyinstaller \
  --noconfirm --clean --onedir --name ayvu-tk-spike \
  --distpath "$SPIKE_BUILD_ROOT/tk/dist" \
  --workpath "$SPIKE_BUILD_ROOT/tk/work" \
  --specpath "$SPIKE_BUILD_ROOT/tk/spec" \
  spikes/desktop-stack/apps/tk_widgets.py
python3 spikes/desktop-stack/measure_bundle.py "$SPIKE_BUILD_ROOT" tk
```

## Automated keyboard and layout observations

Run `probe_widgets.py` in the candidate's environment. It saves JSON reports
and Qt-only synthetic window captures to the supplied output directory. It
returns 1 when an observation fails; retain that result as negative research
evidence instead of treating it as a pass. The probe never opens Ayvu files.

```bash
uv run --project spikes/desktop-stack --extra qt --locked --no-build python \
  spikes/desktop-stack/probe_widgets.py qt --scale 1 --native-dpr 2 \
  --output "$SPIKE_BUILD_ROOT/widget-probes"
LD_LIBRARY_PATH="$SPIKE_BUILD_ROOT/native-runtime/usr/lib" \
TCL_LIBRARY="$SPIKE_BUILD_ROOT/native-runtime/usr/lib/tcl8.6" \
TK_LIBRARY="$SPIKE_BUILD_ROOT/native-runtime/usr/lib/tk8.6" \
uv run --project spikes/desktop-stack --extra qt --locked --no-build python \
  spikes/desktop-stack/probe_widgets.py tk --scale 1 \
  --output "$SPIKE_BUILD_ROOT/widget-probes"
```

Repeat sequentially for `--scale 1.25`, `1.5` and `2`. Set `--native-dpr` to
the Qt ratio measured on your host before applying `QT_SCALE_FACTOR`; 2 was
observed on this host and must not be assumed elsewhere. The raw result
records the effective Qt ratio. The probe delivers toolkit-generated key
events; it is not a physical keyboard or spoken screen-reader test. See the
[manual checklist](MANUAL-CHECKLIST.md) for that required assessment.

Run the research-tool and link checks alongside the production suite from the
repository root using its development environment:

```bash
uv run pytest tests spikes/desktop-stack/test_spike_tools.py
```

## Current evidence

The initial host inventory is in
[`results/host-2026-10-02.md`](results/host-2026-10-02.md), and the locked Qt
startup and bundle measurements plus current candidate gaps are in
[`results/host-2026-10-05.md`](results/host-2026-10-05.md). No missing runtime is
silently replaced with a mock result. An absent dependency means that candidate
was not measured.

The later [same-host comparison](results/host-2026-10-06.md) supplies Qt and Tk
startup, standalone-bundle, AT-SPI and native-widget evidence. Qt snapshots
and raw JSON are linked there. The
[packaging review](results/packaging-review-2026-10-06.md) records notices and
distribution gaps. Manual speech, physical keyboard, monitor-switch checks,
and scope approval remain explicit closure gaps. Startup here means a new
application process with fresh local state; controlled OS cold-cache timing
was not measured.
