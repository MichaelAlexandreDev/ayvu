"""Local filesystem and process adapters for the library application service."""
from __future__ import annotations

import os
import shlex
import shutil
import stat
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable
from uuid import uuid4

from .library_service import Candidate, Warning, MAX_WARNINGS


@dataclass(frozen=True)
class _Token:
    path: Path = field(repr=False)
    root: Path = field(repr=False)
    identity: tuple[int, ...] = field(repr=False)
    root_identity: tuple[int, ...] = field(repr=False)


def _identity(info: os.stat_result) -> tuple[int, ...]:
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


class LocalLibraryFilesystem:
    def __init__(self, root: Path, original_folder: str = "Original", translated_folder: str = "Traduzidos"):
        self._root = Path(root).expanduser()
        self._folders = ((original_folder, "original"), (translated_folder, "translation"))
        self.root_ref = uuid4().hex

    def discover(self, cancelled: Callable[[], bool], limit: int) -> tuple[list[Candidate], list[Warning], bool]:
        candidates: list[Candidate] = []
        warnings: list[Warning] = []
        def warn(ref: str, reason: str) -> None:
            if len(warnings) < MAX_WARNINGS:
                warnings.append(Warning(ref, reason))
        if cancelled():
            return candidates, warnings, True
        try:
            root = self._root.resolve(strict=True)
            root_info = root.stat()
        except FileNotFoundError:
            return candidates, warnings, False
        except (OSError, RuntimeError):
            warn(self.root_ref, "root_unavailable")
            return candidates, warnings, False
        for folder, kind in self._folders:
            if cancelled():
                return candidates, warnings, True
            # Folder settings must designate exactly one directory under the root.
            if not folder or folder in (".", "..") or Path(folder).name != folder:
                warn(self.root_ref, "invalid_folder")
                continue
            try:
                directory = root / folder
                resolved_directory = directory.resolve(strict=True)
                if not resolved_directory.is_relative_to(root):
                    warn(self.root_ref, "directory_escape")
                    continue
                with os.scandir(directory) as scan:
                    # Bound retained names even for huge folders; final selection is deterministic.
                    names: list[str] = []
                    overflow = False
                    for item in scan:
                        if cancelled():
                            return candidates, warnings, True
                        if not item.name.endswith(".epub"):
                            continue
                        names.append(item.name)
                        if len(names) > limit:
                            names.sort(key=lambda n: (n.casefold(), n))
                            names.pop()
                            overflow = True
                    if overflow:
                        warn(self.root_ref, "entry_limit")
            except FileNotFoundError:
                continue
            except (OSError, RuntimeError):
                warn(self.root_ref, "directory_unavailable")
                continue
            for name in sorted(names, key=lambda n: (n.casefold(), n)):
                if cancelled():
                    return candidates, warnings, True
                if len(candidates) >= limit:
                    warn(self.root_ref, "entry_limit")
                    return candidates, warnings, False
                path = directory / name
                try:
                    resolved = path.resolve(strict=True)
                    info = resolved.stat()
                    if not resolved.is_relative_to(root) or not stat.S_ISREG(info.st_mode):
                        warn(uuid4().hex, "invalid_entry")
                        continue
                    token = _Token(path, root, _identity(info), (root_info.st_dev, root_info.st_ino))
                    candidates.append(Candidate(name[:-5], kind, token))
                except (OSError, RuntimeError):
                    warn(uuid4().hex, "entry_unavailable")
        return candidates, warnings, False

    def authorize(self, token: object) -> str | None:
        if not isinstance(token, _Token):
            return None
        try:
            root = self._root.resolve(strict=True)
            root_info = root.stat()
            if root != token.root or (root_info.st_dev, root_info.st_ino) != token.root_identity:
                return None
            if token.path.suffix != ".epub":
                return None
            path = token.path.resolve(strict=True)
            if not path.is_relative_to(root):
                return None
            info = path.stat()
            if not stat.S_ISREG(info.st_mode) or _identity(info) != token.identity:
                return None
            return str(path)
        except (OSError, RuntimeError, ValueError):
            return None


class ProcessLauncher:
    """Only structured executable/argv are accepted; no shell interpreter is used."""
    def __init__(self, runner=None):
        self._runner = runner or subprocess.Popen

    def launch(self, executable: str, arguments: tuple[str, ...]) -> None:
        if not isinstance(executable, str) or not isinstance(arguments, tuple) or not all(isinstance(arg, str) for arg in arguments):
            raise ValueError("invalid process arguments")
        self._runner([executable, *arguments])


class ReaderLauncher:
    def __init__(self, reader_app: str | None = None, runner=None, which=None, platform: str | None = None):
        self._reader_app = reader_app
        self._process = ProcessLauncher(runner)
        self._which = which or shutil.which
        self._platform = platform or sys.platform

    def launch(self, path: str, cancelled: Callable[[], bool], revalidate: Callable[[], bool]) -> str:
        if self._reader_app:
            if len(self._reader_app) > 4096:
                return "configuration_invalid"
            try:
                command = shlex.split(self._reader_app)
            except ValueError:
                return "configuration_invalid"
            if len(command) > 64 or any(len(arg) > 1024 or "\x00" in arg for arg in command):
                return "configuration_invalid"
        else:
            command = []
        if not command:
            options = (("open",),) if self._platform == "darwin" else (() if self._platform.startswith("win") else (("xdg-open",), ("gio", "open"), ("open",)))
            command = next((list(option) for option in options if self._which(option[0])), [])
        if not command:
            return "unavailable"
        executable_name = command[0].replace("\\", "/").rsplit("/", 1)[-1].lower()
        if executable_name in {"sh", "bash", "dash", "zsh", "fish", "ksh", "cmd", "cmd.exe", "powershell", "powershell.exe", "pwsh", "pwsh.exe"}:
            return "configuration_invalid"
        if cancelled():
            return "cancelled"
        if not revalidate():
            return "unauthorized"
        if cancelled():
            return "cancelled"
        try:
            self._process.launch(command[0], (*command[1:], path))
        except (OSError, ValueError):
            return "launch_failed"
        return "opened"
