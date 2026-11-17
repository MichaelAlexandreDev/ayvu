from __future__ import annotations
from tests.support.cli_rich_runtime_support import labeled_runtime
import hashlib
import json
from zipfile import ZIP_STORED, ZipFile
from ayvu.interfaces.cli.app import app
from ayvu.recovery.checkpoint import COMPLETED_STATUS, RUNNING_STATUS, ResumeStateStore
from tests.support.workflow_characterization_support import FakeLibreTranslateSession, _direct_languages, _install_cli_environment, _isolated_from_the_user
from tests.support.workflow_characterization_support import _read_member, _translate_args
from tests.support.workflow_characterization_support import _write_epub, _zip_metadata, runner


def test_translate_cli_preserves_source_archive_and_visible_text(tmp_path, monkeypatch):
    source_path = _write_epub(tmp_path / "book.epub")
    source_bytes = source_path.read_bytes()
    source_hash = hashlib.sha256(source_bytes).hexdigest()
    session = FakeLibreTranslateSession(_direct_languages())
    processing_dir = _install_cli_environment(monkeypatch, tmp_path, session)

    output_dir = tmp_path / "translated"
    result = runner.invoke(
        app,
        _translate_args(source_path, tmp_path / "cache.sqlite", output_dir=output_dir),
    )

    output_path = output_dir / "book-pt.epub"
    assert result.exit_code == 0, result.output
    assert output_path.exists()
    assert "Validação OK" in result.output
    assert source_path.read_bytes() == source_bytes
    assert hashlib.sha256(source_path.read_bytes()).hexdigest() == source_hash
    manifests = list(processing_dir.glob('projects/*/project.json'))
    assert len(manifests) == 1
    project = json.loads(manifests[0].read_text())
    assert project['source_sha256'] == source_hash
    assert project['source_path'] == str(source_path)
    assert project['target'] == 'pt'
    assert project['project_id'] == manifests[0].parent.name

    chapter = _read_member(output_path, "OEBPS/text/chapter1.xhtml")
    assert "PT:Hello title" in chapter
    assert "PT:Hello <a href=\"chapter2.xhtml#two\">reader</a>." in chapter
    assert "PT:Game Loop uses Observer." in chapter
    for hidden_text in ("script keep", "code keep", "pre keep", "kbd keep", "samp keep", "svg keep", "math keep"):
        assert hidden_text in chapter

    submitted_text = "\n".join(payload["q"] for payload in session.posts)
    assert "Hello " in submitted_text
    assert "reader" in submitted_text
    for hidden_text in ("script keep", "code keep", "pre keep", "kbd keep", "samp keep", "svg keep", "math keep"):
        assert hidden_text not in submitted_text

    unchanged_members = (
        "OEBPS/styles/book.css",
        "OEBPS/images/pixel.png",
        "OEBPS/assets/keep.bin",
    )
    with ZipFile(source_path) as source, ZipFile(output_path) as output:
        assert source.namelist() == output.namelist()
        assert output.infolist()[0].filename == "mimetype"
        assert output.getinfo("mimetype").compress_type == ZIP_STORED
        assert output.read("mimetype") == b"application/epub+zip"
        for member in unchanged_members:
            assert output.read(member) == source.read(member)
            assert _zip_metadata(output.getinfo(member)) == _zip_metadata(source.getinfo(member))


def test_translate_cli_interrupts_then_resume_reuses_checkpoint_and_cache(tmp_path, monkeypatch):
    source_path = _write_epub(
        tmp_path / "interrupted.epub",
        chapter_one="<p>First chapter text.</p>",
        chapter_two='<p id="two">Interrupt when translating this sentence.</p>',
    )
    cache_path = tmp_path / "cache.sqlite"
    output_path = tmp_path / "interrupted-pt.epub"
    interrupted_session = FakeLibreTranslateSession(
        _direct_languages(),
        interrupt_on_text="Interrupt when translating",
    )
    processing_dir = _install_cli_environment(monkeypatch, tmp_path, interrupted_session)

    interrupted = runner.invoke(
        app,
        _translate_args(source_path, cache_path, output_path=output_path),
    )

    assert interrupted.exit_code == 1
    assert "Translation interrupted by user." in interrupted.output
    assert not output_path.exists()
    scan = ResumeStateStore(processing_dir).scan()
    assert len(scan.running) == 1
    state = scan.running[0]
    assert state.status == RUNNING_STATUS
    assert state.completed_chapters
    assert state.current_chapter is not None

    resumed_session = FakeLibreTranslateSession(_direct_languages())
    monkeypatch.setattr("ayvu.providers.libretranslate.client.requests.Session", lambda: resumed_session)
    resumed = runner.invoke(
        app,
        ["--mode", "developer", "resume", str(source_path), "--target", "pt"],
    )

    assert resumed.exit_code == 0, resumed.output
    assert output_path.exists()
    resumed_text = "\n".join(payload["q"] for payload in resumed_session.posts)
    assert "First chapter text." not in resumed_text
    assert "Interrupt when translating this sentence." in resumed_text
    completed = ResumeStateStore(processing_dir).scan()
    assert completed.running == ()
    state_path = processing_dir / "interrupted-pt.ayvu-state.json"
    assert ResumeStateStore(processing_dir).load(state_path).status == COMPLETED_STATUS
    assert "PT:First chapter text." in _read_member(output_path, "OEBPS/text/chapter1.xhtml")
    assert "PT:Interrupt when translating this sentence." in _read_member(
        output_path,
        "OEBPS/text/chapter2.xhtml",
    )
