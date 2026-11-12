import errno
import json
import os

import pytest

from ayvu.composition.builders.cache_admin import cache_admin_services

from tests.support.cache_admin_integration_support import _export, _no_links
from tests.support.cache_admin_integration_support import seed


@pytest.mark.parametrize("code", [errno.EPERM, errno.ENOTSUP])
def test_an_export_reaches_a_filesystem_without_hard_links(tmp_path, monkeypatch, code):
    services = cache_admin_services()
    path = tmp_path / "cache.sqlite"
    seed(path, "one")
    monkeypatch.setattr(os, "link", _no_links(code))
    out = tmp_path / "out.json"

    assert _export(services, path, out).value == 1

    assert len(json.loads(out.read_text())["entries"]) == 1
    assert [item.name for item in tmp_path.iterdir() if item.name.startswith(".ayvu-")] == []


def test_without_hard_links_an_existing_export_is_still_not_replaced(tmp_path, monkeypatch):
    services = cache_admin_services()
    path = tmp_path / "cache.sqlite"
    seed(path, "one")
    monkeypatch.setattr(os, "link", _no_links(errno.EPERM))
    out = tmp_path / "out.json"
    out.write_text("mine")

    assert _export(services, path, out).failure.code == "already_exists"

    assert out.read_text() == "mine"
    assert [item.name for item in tmp_path.iterdir() if item.name.startswith(".ayvu-")] == []


def test_a_link_refused_for_another_reason_is_still_a_failure(tmp_path, monkeypatch):
    services = cache_admin_services()
    path = tmp_path / "cache.sqlite"
    seed(path, "one")
    monkeypatch.setattr(os, "link", _no_links(errno.EACCES))
    out = tmp_path / "out.json"

    assert _export(services, path, out).failure.code == "permission_denied"
    assert not out.exists()
