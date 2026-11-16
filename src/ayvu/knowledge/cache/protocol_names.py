"""Names of the files the v2 cache protocol keeps next to a cache database.

Every name is the cache's own name plus a fixed suffix, so the files stay immediate
children of the authorized directory. Pure path arithmetic: nothing here touches disk.
"""
from __future__ import annotations

_SUFFIXES = ("-wal", "-shm", "-journal", ".lock", ".migration", ".migration-next", ".v1-backup.sqlite",
             ".v1-backup.sqlite.partial")


def _marker(path):
    return path.with_name(path.name + ".migration")


def _next_marker(path):
    return path.with_name(path.name + ".migration-next")


def _backup(path):
    return path.with_name(path.name + ".v1-backup.sqlite")


def _partial_backup(path):
    return path.with_name(path.name + ".v1-backup.sqlite.partial")


def _partial_backup_files(path):
    partial = _partial_backup(path)
    return (partial, *(partial.with_name(partial.name + sidecar) for sidecar in ("-wal", "-shm", "-journal")))
