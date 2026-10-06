"""Measure a PyInstaller onedir artifact inside a private temporary root."""

from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
from pathlib import Path


NAMES = {
    "qt": "ayvu-qt-spike",
    "wx": "ayvu-wx-spike",
    "tk": "ayvu-tk-spike",
}


def validate_root(raw_root: str) -> Path:
    if not hasattr(os, "getuid"):
        raise SystemExit("bundle measurement currently requires a POSIX host")
    supplied = Path(raw_root)
    if not supplied.is_absolute() or supplied.is_symlink():
        raise SystemExit("build root must be an absolute, non-symlink temp directory")
    temp_root = Path(tempfile.gettempdir()).resolve(strict=True)
    if supplied.parent.resolve(strict=True) != temp_root:
        raise SystemExit("build root must be a direct child of the system temp directory")
    if not supplied.name.startswith("ayvu-desktop-stack-"):
        raise SystemExit("build root must use prepare_bundle_root.py")

    root = temp_root / supplied.name
    info = root.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise SystemExit("build root must be a directory owned by the current user")
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise SystemExit("build root must be private to the current user")
    return root


def _safe_link_bytes(path: Path, artifact: Path) -> int:
    target = os.readlink(path)
    target_path = Path(target)
    if target_path.is_absolute():
        raise SystemExit("refusing absolute symlinks in the measured artifact")
    normalized = Path(os.path.normpath(path.parent / target_path))
    try:
        normalized.relative_to(artifact)
    except ValueError:
        raise SystemExit("refusing symlinks that escape the measured artifact") from None
    return len(os.fsencode(target))


def main() -> int:
    if len(sys.argv) != 3 or sys.argv[2] not in NAMES:
        raise SystemExit("usage: measure_bundle.py PRIVATE_TEMP_ROOT {qt|wx|tk}")
    root = validate_root(sys.argv[1])
    candidate = sys.argv[2]
    relative = Path(candidate) / "dist" / NAMES[candidate]
    artifact = root / relative
    current = root
    for part in relative.parts:
        current = current / part
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            raise SystemExit("expected a PyInstaller onedir output directory") from None
        if stat.S_ISLNK(mode):
            raise SystemExit("refusing symlinks in the artifact's directory path")
    if not artifact.is_dir():
        raise SystemExit("expected a PyInstaller onedir output directory")

    total_file_bytes = 0
    total_link_bytes = 0
    files = 0
    symlinks = 0
    seen_inodes: set[tuple[int, int]] = set()

    def fail_walk(error: OSError) -> None:
        raise error

    for directory, subdirs, filenames in os.walk(
        artifact, followlinks=False, onerror=fail_walk
    ):
        base = Path(directory)
        for name in (*subdirs, *filenames):
            path = base / name
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode):
                total_link_bytes += _safe_link_bytes(path, artifact)
                symlinks += 1
            elif stat.S_ISREG(info.st_mode):
                identity = (info.st_dev, info.st_ino)
                if identity not in seen_inodes:
                    total_file_bytes += info.st_size
                    seen_inodes.add(identity)
                files += 1
            elif not stat.S_ISDIR(info.st_mode):
                raise SystemExit("refusing non-regular files in the measured artifact")

    total = total_file_bytes + total_link_bytes
    print(
        json.dumps(
            {
                "candidate": candidate,
                "artifact": relative.as_posix(),
                "format": "PyInstaller onedir logical payload bytes",
                "regular_file_count": files,
                "unique_regular_file_bytes": total_file_bytes,
                "symlink_count": symlinks,
                "relative_symlink_target_bytes": total_link_bytes,
                "total_bundle_payload_bytes": total,
                "mebibytes": round(total / (1024 * 1024), 2),
                "excludes": (
                    "installer, signing, notarization, download compression, and "
                    "runtime libraries provided by the target OS outside the artifact"
                ),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
