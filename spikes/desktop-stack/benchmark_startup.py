"""Measure fresh-process startup for one synthetic GUI spike app."""

from __future__ import annotations

import argparse
import json
import os
import platform
import queue
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path


SPIKE = Path(__file__).resolve().parent
APPS = {
    "qt": SPIKE / "apps" / "qt_widgets.py",
    "wx": SPIKE / "apps" / "wx_widgets.py",
    "tk": SPIKE / "apps" / "tk_widgets.py",
}
READY_MARKER = b"AYVU_SPIKE_READY"
MAX_STARTUP_OUTPUT = 4096
READY = "ready"
STDOUT_CLOSED = "stdout_closed"
OUTPUT_LIMIT = "output_limit"


def _child_environment(home: Path, tk_runtime: Path | None = None) -> dict[str, str]:
    common = ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "LC_MESSAGES")
    if os.name == "nt":
        platform_keys = (
            "SYSTEMROOT",
            "WINDIR",
            "COMSPEC",
            "PATHEXT",
            "TEMP",
            "TMP",
        )
    elif sys.platform == "darwin":
        platform_keys = ("DISPLAY", "TMPDIR")
    else:
        platform_keys = (
            "DISPLAY",
            "WAYLAND_DISPLAY",
            "XDG_RUNTIME_DIR",
            "XDG_SESSION_TYPE",
            "DBUS_SESSION_BUS_ADDRESS",
            "XAUTHORITY",
        )

    env = {
        key: os.environ[key]
        for key in (*common, *platform_keys)
        if key in os.environ
    }
    if os.name != "nt" and sys.platform != "darwin":
        if "XAUTHORITY" not in env and "DISPLAY" in env and os.environ.get("HOME"):
            env["XAUTHORITY"] = str(Path(os.environ["HOME"]) / ".Xauthority")
    isolated_tmp = home / "tmp"
    isolated_tmp.mkdir(mode=0o700)
    env.update(
        {
            "HOME": str(home),
            "TMPDIR": str(isolated_tmp),
            "TEMP": str(isolated_tmp),
            "TMP": str(isolated_tmp),
            "PYTHONNOUSERSITE": "1",
            "XDG_CONFIG_HOME": str(home / "config"),
            "XDG_CACHE_HOME": str(home / "cache"),
            "XDG_DATA_HOME": str(home / "data"),
        }
    )
    for name in ("config", "cache", "data"):
        (home / name).mkdir(mode=0o700)
    if os.name == "nt":
        env.update(
            {
                "USERPROFILE": str(home),
                "APPDATA": str(home / "roaming"),
                "LOCALAPPDATA": str(home / "local"),
            }
        )
        (home / "roaming").mkdir(mode=0o700)
        (home / "local").mkdir(mode=0o700)
    if tk_runtime is not None:
        # Explicit, trusted extracted runtime only; never inherit host loader
        # or Tcl injection variables into the reduced benchmark environment.
        library = tk_runtime / "usr" / "lib"
        env.update(
            LD_LIBRARY_PATH=str(library),
            TCL_LIBRARY=str(library / "tcl8.6"),
            TK_LIBRARY=str(library / "tk8.6"),
        )
    return env


def _stop_and_reap(process: subprocess.Popen[bytes]) -> bool:
    if process.poll() is not None:
        return True
    try:
        process.terminate()
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=1.0)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            return False
    return True


def _ready_reader(
    process: subprocess.Popen[bytes], ready: queue.Queue[str]
) -> None:
    assert process.stdout is not None
    consumed = 0
    marker_tail = b""
    while consumed < MAX_STARTUP_OUTPUT:
        chunk = process.stdout.readline(MAX_STARTUP_OUTPUT - consumed)
        if not chunk:
            ready.put(STDOUT_CLOSED)
            return
        if READY_MARKER in marker_tail + chunk:
            ready.put(READY)
            return
        marker_tail = (marker_tail + chunk)[-(len(READY_MARKER) - 1) :]
        consumed += len(chunk)
    ready.put(OUTPUT_LIMIT)


def measure(
    candidate: str, runs: int, timeout: float, tk_runtime: Path | None = None
) -> dict[str, object]:
    samples: list[float] = []
    app = APPS[candidate]
    for run_number in range(runs):
        ready: queue.Queue[str] = queue.Queue(maxsize=1)
        with tempfile.TemporaryDirectory(prefix="ayvu-desktop-spike-home-") as home:
            child_env = _child_environment(Path(home), tk_runtime)
            started = time.perf_counter_ns()
            process = subprocess.Popen(
                [sys.executable, str(app), "--benchmark"],
                cwd=SPIKE,
                env=child_env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
            reader = threading.Thread(
                target=_ready_reader, args=(process, ready), daemon=True
            )
            reader_started = False
            try:
                reader.start()
                reader_started = True
                try:
                    reported_ready = ready.get(timeout=timeout)
                except queue.Empty:
                    raise RuntimeError(
                        f"{candidate} run {run_number + 1} did not report GUI readiness "
                        f"within {timeout:g}s"
                    ) from None
                if reported_ready == STDOUT_CLOSED:
                    return_code = process.poll()
                    exit_detail = (
                        f" (exit status {return_code})"
                        if return_code is not None
                        else " (stdout closed before process exit)"
                    )
                    raise RuntimeError(
                        f"{candidate} run {run_number + 1} exited before GUI readiness"
                        f"{exit_detail}"
                    )
                if reported_ready == OUTPUT_LIMIT:
                    raise RuntimeError(
                        f"{candidate} run {run_number + 1} reached the "
                        f"{MAX_STARTUP_OUTPUT}-byte stdout limit before GUI readiness"
                    )
                if reported_ready != READY:
                    raise RuntimeError(
                        f"{candidate} run {run_number + 1} returned an unknown "
                        "readiness status"
                    )
                elapsed = (time.perf_counter_ns() - started) / 1_000_000_000
                return_code = process.wait(timeout=1.0)
                if return_code != 0:
                    raise RuntimeError(
                        f"{candidate} run {run_number + 1} exited with status {return_code}"
                    )
                samples.append(elapsed)
            except subprocess.TimeoutExpired:
                raise RuntimeError(
                    f"{candidate} reported readiness but did not exit within 1s"
                ) from None
            finally:
                reaped = _stop_and_reap(process)
                if process.stdout is not None and (reaped or not reader_started):
                    process.stdout.close()
                if reader_started:
                    reader.join(timeout=1.0)
                if not reaped:
                    raise RuntimeError("benchmark child could not be reaped after kill")

    ordered = sorted(samples)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        median = ordered[middle]
    else:
        median = (ordered[middle - 1] + ordered[middle]) / 2
    return {
        "candidate": candidate,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "samples_seconds": samples,
        "first_gui_ready_seconds": samples[0],
        "gui_ready_median_seconds": median,
        "runs": runs,
        "measurement": (
            "new process per sample; readiness is the first app callback after "
            "showing the window; OS page-cache state is uncontrolled"
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("candidate", choices=sorted(APPS))
    parser.add_argument("--runs", type=int, default=7)
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument(
        "--tk-runtime-root", type=Path,
        help="explicit trusted Arch Tcl/Tk 8.6 package extraction root (Linux Tk only)",
    )
    args = parser.parse_args()
    if not 1 <= args.runs <= 30:
        parser.error("--runs must be between 1 and 30")
    if not 1 <= args.timeout <= 30:
        parser.error("--timeout must be between 1 and 30 seconds")
    runtime = args.tk_runtime_root
    if runtime is not None:
        if sys.platform != "linux" or args.candidate != "tk":
            parser.error("--tk-runtime-root is only supported for Tk on Linux")
        try:
            runtime = runtime.resolve(strict=True)
        except OSError:
            parser.error("--tk-runtime-root does not resolve to an existing directory")
        required = (
            "usr/lib/libtcl8.6.so", "usr/lib/libtk8.6.so",
            "usr/lib/tcl8.6/init.tcl", "usr/lib/tk8.6/tk.tcl",
        )
        if not all((runtime / item).is_file() for item in required):
            parser.error("--tk-runtime-root must contain the extracted Tcl/Tk 8.6 runtime")
    try:
        print(json.dumps(measure(args.candidate, args.runs, args.timeout, runtime), indent=2))
    except (OSError, RuntimeError) as exc:
        parser.exit(1, f"startup measurement failed: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
