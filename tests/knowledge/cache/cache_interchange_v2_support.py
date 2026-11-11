import hashlib
import io
import json
from types import SimpleNamespace
import pytest
from ayvu.knowledge.cache.admin import CacheAdminError
from ayvu.knowledge.cache.interchange import iter_records, validate_v2_entry, write_envelope
from ayvu.knowledge.cache.repository import SQLiteExactCache
from ayvu.knowledge.cache.schema import V2_COLUMNS, participating_connection
from tests.support.cache_repository_v2_support import entry

def limits(**values):
    return SimpleNamespace(**values)


def legacy(text="abc"):
    return dict(source_lang="en", target_lang="pt", original_text_hash=hashlib.sha256(text.encode()).hexdigest(), original_text=text, translated_text="olá", created_at="2020")


@pytest.fixture
def v2row(tmp_path):
    path = tmp_path / "row.sqlite"
    SQLiteExactCache(path).put_if_absent(entry())
    with participating_connection(path) as conn:
        row = conn.execute('SELECT ' + ','.join(V2_COLUMNS) + ' FROM cache_entries_v2').fetchone()
    desc = dict(kind="v2", scope_kind=row[0], scope_id=row[1])
    item = {name: value.decode() if isinstance(value, bytes) else value for name, value in zip(V2_COLUMNS[2:], row[2:])}
    return desc, item


class InstrumentedInput(io.BytesIO):
    def __init__(self, data):
        super().__init__(data)
        self.requests = []
    def read(self, size=-1):
        self.requests.append(size)
        assert 0 < size <= 65536
        return super().read(min(size, 13))


class InstrumentedOutput(io.BytesIO):
    def __init__(self):
        super().__init__()
        self.writes = []
    def write(self, data):
        self.writes.append(len(data))
        return super().write(data)
