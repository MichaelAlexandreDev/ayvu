# Packaging and license feasibility — 2026-10-06

This records artifact evidence for issue #124. No binary was published.

## Artifact observations

Both onedir artifacts launched outside the source checkout after external
Python, Tcl/Tk, loader and Qt plugin paths were removed. The Tk artifact
contains its Tcl/Tk runtime. Qt contains PySide6, shiboken6, Python, Qt and
copied platform libraries; the Qt onedir is 190.75 MiB versus Tk's 45.19 MiB.
Neither includes the Ayvu core, a translator or translation models.

The Qt artifact contains these Qt library families: Core, DBus, Gui, Widgets,
Network, OpenGL, Svg, WaylandClient, WlShellIntegration, XcbQpa,
EglFSDeviceIntegration and EglFsKmsSupport. It also includes ICU libraries,
OpenSSL libraries and native platform dependencies. A filename scan found no
LICENSE, COPYING or NOTICE files in either bundle. Package builds therefore
prove packaging feasibility, not distribution readiness.

## License constraints and reproducible evidence

| Component | Evidence | Distribution work needed |
| --- | --- | --- |
| PySide6-Essentials / shiboken6 6.11.2 | Installed wheel metadata identifies LGPL/GPL alternatives; no commercial license was used | Preserve exact-version notices and corresponding source access; verify the selected license for each included module |
| Qt shared libraries | Included libraries are visible in `_internal/PySide6/Qt/lib` | Confirm the module inventory, applicable LGPL conditions and user replacement of shared libraries |
| Tcl/Tk 8.6.16 | Signed Arch packages contain `usr/share/licenses/{tcl,tk}/license.terms` | Retain the copyright and license notices in any distributed artifact |
| Python, OpenSSL, ICU and copied OS libraries | Files are copied by the packager | Map each file to its exact source/version/license; include its required notices |
| PyInstaller 6.22.3 | Bootloader exception is documented by the project | Retain notices and evaluate bundled dependencies individually |

Primary sources consulted on 2026-10-06:
[Qt for Python licenses](https://doc.qt.io/qtforpython-6/licenses.html),
[Qt LGPL obligations](https://www.qt.io/development/open-source-lgpl-obligations),
[Tcl/Tk license](https://www.tcl-lang.org/software/tcltk/license.html),
[PyInstaller license and exception](https://pyinstaller.org/en/stable/license.html).

The spike requires no paid toolkit license. A future release must fulfill the
selected open-source license conditions. The Python files' project license
does not replace the licenses of packaged dependencies.

## Recommendation and release gate

Retain onedir packaging for this evaluation so shared libraries remain
separate. Before distributing Ayvu, generate a file-level dependency/license
inventory, ship license texts and notices, retain exact corresponding source
references, test shared-library replacement where required, and validate on a
clean target system. Signing, installers and older Linux libc compatibility
were not assessed. The present bundles are local research artifacts and must
not be advertised as release-ready executables.
