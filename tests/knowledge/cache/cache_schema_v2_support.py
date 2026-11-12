import errno
from contextlib import closing
import json
import os
import sqlite3
import threading
from pathlib import Path
import pytest
from ayvu.knowledge.cache import schema as schema
from ayvu.knowledge.cache import LEGACY_SCHEMA as SCHEMA
from ayvu.v1.cache import text_hash

class Cancel(BaseException):
    pass


def make_v1(path):
    with closing(sqlite3.connect(path)) as db, db:
        db.execute(SCHEMA)
        db.execute("INSERT INTO translations (source_lang,target_lang,original_text_hash,original_text,translated_text) VALUES (?,?,?,?,?)", ("en", "pt", text_hash("hello"), "hello", "olá"))


def contents(path):
    with closing(sqlite3.connect(path)) as db:
        return db.execute("SELECT * FROM translations").fetchall()


def state(path):
    with schema._connection_closed(schema._connect(path)) as db:
        return schema._state(db)


def assert_v2(path, rows):
    assert state(path) == schema.SchemaState.V2
    assert contents(path) == rows
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("SELECT count(*) FROM cache_entries_v2").fetchone() == (0,)
        assert db.execute("SELECT * FROM cache_schema").fetchall() == [(1, 2, "legacy-v1")]


def _classified_then_stopped(p):
    def stop(phase):
        if phase == "classified":
            raise KeyboardInterrupt
    with pytest.raises(BaseException):
        schema.prepare_cache(p, checkpoint=stop)
    assert not schema._backup(p).exists()
