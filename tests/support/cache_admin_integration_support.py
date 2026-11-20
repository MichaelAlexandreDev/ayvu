import errno
import hashlib
import json
import os
import sqlite3
import threading
import pytest
from typer.testing import CliRunner
from ayvu.knowledge.cache import admin as cache_admin, admin_store as cache_admin_store
from tests.support.legacy_cache_file import CacheKey, LegacyCacheFile
from ayvu.knowledge.cache.admin import (CacheRef, FileRef, CacheFilter, InspectRequest, PreviewCleanRequest,
    ExecuteCleanRequest, ExportRequest, ImportRequest)
from ayvu.composition.builders.cache_admin import cache_admin_services
from ayvu.interfaces.cli.app import app
from ayvu.kernel import LanguagePair

def key(text):
    return CacheKey(text=text, language_pair=LanguagePair(source="en", target="pt"))


def seed(path, *texts):
    with LegacyCacheFile(path) as cache:
        for text in texts:
            cache.set(key(text), "translated " + text)


def inspect(services, path):
    return services.inspect.execute(InspectRequest(CacheRef(str(path))))


class CancelAt:
    def __init__(self, point):
        self.point = point
        self.seen = []
    def checkpoint(self, point):
        self.seen.append(point)
        return object() if point == self.point else None


def _export(services, cache, destination, overwrite=False):
    return services.export.execute(ExportRequest(CacheRef(str(cache)), FileRef(str(destination)), CacheFilter(), overwrite))


def _empty_sqlite_file(path):
    path.write_bytes(b"")


def _sqlite_with_another_table(path):
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE other (a)")


def _no_links(code):
    def link(*_args, **_kwargs):
        raise OSError(code, "no hard links here")
    return link
