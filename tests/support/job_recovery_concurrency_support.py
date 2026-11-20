from __future__ import annotations
import os
import signal
import subprocess
import sys
import textwrap
import threading
from datetime import timedelta
import pytest
from ayvu.jobs.model import JobContinuationError, JobRequest, JobState
from ayvu.recovery.service import CompatibilityStatus, RequiredDecision, ResumeRequest, ResumeStatus
from ayvu.translation.resume_reuse import RecoveryReason
from ayvu.recovery.store import _options
from ayvu.projects.store import ProjectJsonRepository
from tests.support.paths import PROJECT_ROOT
from ayvu.translation.project_contracts import OUTPUT_ROLE, TRANSLATE_COMMAND_TYPE, TRANSLATE_COMMAND_VERSION, TranslateObserver
from ayvu.composition.builders.translate_project import LegacyResources, open_job_recovery, open_translate_project
from tests.support.cli_translate_project_support import _args, _book
from tests.support.job_recovery_integration_support import DECIDED, Runner, Scene
from ayvu.kernel import OutputPlan

CHILD = textwrap.dedent('''
    import json, os, pathlib, sys, time
    import requests
    from typer.testing import CliRunner
    import ayvu.providers.libretranslate.client
    from ayvu.interfaces.cli import app as cli, settings as cli_settings
    class Session:
        def mount(self, prefix, adapter):
            assert prefix == "http://"
        def _response(self, payload):
            response = requests.Response()
            response.status_code = 200
            response.url = "http://translator.test"
            response._content = json.dumps(payload).encode()
            return response
        def get(self, url, *, timeout, allow_redirects=False):
            assert allow_redirects is False
            return self._response([{"code": "en", "name": "E", "targets": ["pt"]}, {"code": "pt", "name": "P", "targets": []}])
        def post(self, url, *, json, timeout, allow_redirects=False):
            assert allow_redirects is False
            if "Goodbye" in json["q"]:
                os.write(1, b"blocked\\n")
                while True:
                    time.sleep(60)
            return self._response({"translatedText": "PT:" + json["q"]})
        def close(self):
            pass
    ayvu.providers.libretranslate.client.requests.Session = Session
    processing = sys.argv[1]
    cli_settings._processing_dir = lambda _config: pathlib.Path(processing)
    if os.environ.get("AYVU_TEST_V1_JOB") == "1":
        from tests.support.job_recovery_integration_support import Scene
        scene = Scene(pathlib.Path(processing).parent, url="http://localhost:5000")
        scene.save_state()
        def block(text):
            if "Goodbye" in text:
                os.write(1, b"blocked\\n")
                while True:
                    time.sleep(60)
        scene.translator.on_call = block
        scene.translate()
    else:
        CliRunner().invoke(cli.app, sys.argv[2:])
''')


@pytest.fixture(autouse=True)
def _isolated_from_the_user(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.chdir(tmp_path)


@pytest.fixture
def scene(tmp_path):
    return Scene(tmp_path)


class Blocked:
    """Holds the run inside a provider call until the test lets it go."""

    def __init__(self, scene, on="Goodbye"):
        self.entered, self.release = threading.Event(), threading.Event()
        scene.translator.on_call = self._call
        self.on = on

    def _call(self, text):
        if self.on in text:
            self.entered.set()
            assert self.release.wait(30)


def _kill_a_real_run(tmp_path):
    source = _book(tmp_path / "book.epub")
    arguments = _args(tmp_path, source)
    # The child runs from tmp_path, so the repository root must be on its path to import tests.support.
    python_path = os.pathsep.join(filter(None, (str(PROJECT_ROOT), os.environ.get("PYTHONPATH"))))
    environment = {**os.environ, "AYVU_TEST_V1_JOB": "1", "PYTHONPATH": python_path, "HOME": str(tmp_path / "home"),
                   "XDG_CONFIG_HOME": str(tmp_path / "config")}
    child = subprocess.Popen([sys.executable, "-c", CHILD, str(tmp_path / "processing"), *arguments], stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, env=environment, cwd=tmp_path)
    assert child.stdout.readline() == b"blocked\n"
    return child, source
