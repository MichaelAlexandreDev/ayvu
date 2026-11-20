from __future__ import annotations
import json
import logging
import re
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from zipfile import ZipFile
import pytest
from typer.testing import CliRunner
from ayvu.interfaces.cli import app as cli, settings as cli_settings, options as cli_options
import ayvu.composition.builders.translate_project as composition
from ayvu.preferences.model import AyvuConfig
from ayvu.jobs.model import JobState
from ayvu.jobs.events import JobEventSequence, encode_event
from ayvu.preferences.model import PreferencesError
from ayvu.preferences.service import PreferencesSnapshot
from ayvu.projects.schema import open_provenance_repository
from ayvu.projects.store import ProjectJsonRepository, build_project_request
from ayvu.review import decode_review_package
from ayvu.translation.project_contracts import ContinuationTicket, TRANSLATE_BEFORE_START, TranslateObserver
from ayvu.translation.engine import ENGINE_SAFE_POINTS
from tests.support.cli_translate_project_support import Session, _args, _book, _jobs
from tests.support.translate_project_support import CancelAt
from ayvu.kernel import LanguagePair, OutputPlan, TranslationOptions, JobWarning

runner = CliRunner()


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli_settings, "_processing_dir", lambda _: tmp_path / "processing")


class Transport(Session):
    """Records every method, URL and payload; no socket is used."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.requests = []

    def get(self, url, **kwargs):
        self.requests.append(("GET", url, None))
        return super().get(url, **kwargs)

    def post(self, url, **kwargs):
        self.requests.append(("POST", url, dict(kwargs["json"])))
        return super().post(url, **kwargs)


def config(tmp_path, label="local-model-2026", **values):
    path = tmp_path / "config" / "ayvu" / "config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {"books_dir": str(tmp_path / "books"), **values}
    if label is not None:
        data["translator_models"] = {"libretranslate": label}
    path.write_text(json.dumps(data))
    return path


def invoke(tmp_path, monkeypatch, source, *arguments, session=None, output=None):
    session = session or Transport()
    monkeypatch.setattr("ayvu.providers.libretranslate.client.requests.Session", lambda: session)
    result = runner.invoke(cli.app, _args(tmp_path, source, *arguments, output=output))
    return result, session


def text(result):
    return re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", result.output)


def records(tmp_path, job):
    paths = ProjectJsonRepository(tmp_path / "processing").resolve_database_paths(job.request.project_id)
    return [node.record.request for node in open_provenance_repository(paths.database, paths.backups,
        job.request.project_id, clock=lambda: datetime.now(UTC)).list_by_job(job.job_id, limit=1024).nodes]


@pytest.fixture(autouse=True)
def labeled_runtime(tmp_path, _isolated_from_the_user):
    """Declare the runtime while retaining each CLI suite's isolated defaults."""
    path = tmp_path / "config" / "ayvu" / "config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.loads(path.read_text()) if path.exists() else {}
    data["translator_models"] = {"libretranslate": "local-model-2026"}
    path.write_text(json.dumps(data))


def declare_model(path, label="local-model-2026", **values):
    """Add an explicit model to an existing isolated configuration."""
    data = json.loads(path.read_text())
    data.update(values, translator_models={"libretranslate": label})
    path.write_text(json.dumps(data))
