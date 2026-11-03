from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile, ZipInfo

import requests
from typer.testing import CliRunner

from ayvu.cli import app
from ayvu.resume import COMPLETED_STATUS, RUNNING_STATUS, ResumeStateStore


runner = CliRunner()


class FakeLibreTranslateSession:
    """A deterministic in-process LibreTranslate boundary for CLI workflows."""

    def __init__(
        self,
        languages: list[dict[str, object]],
        *,
        interrupt_on_text: str | None = None,
    ) -> None:
        self.languages = languages
        self.interrupt_on_text = interrupt_on_text
        self.gets: list[tuple[str, float]] = []
        self.posts: list[dict[str, str]] = []

    def get(self, url: str, *, timeout: float) -> requests.Response:
        self.gets.append((url, timeout))
        return _json_response(self.languages, url)

    def post(self, url: str, *, json: dict[str, str], timeout: float) -> requests.Response:
        payload = dict(json)
        self.posts.append(payload)
        if self.interrupt_on_text and self.interrupt_on_text in payload["q"]:
            raise KeyboardInterrupt

        source = payload["source"]
        target = payload["target"]
        if source == "fr" and target == "en":
            translated = f"EN:{payload['q']}"
        elif target == "pt":
            translated = f"PT:{payload['q']}"
        else:
            translated = f"{target.upper()}:{payload['q']}"
        return _json_response({"translatedText": translated}, url)


def _json_response(payload: object, url: str) -> requests.Response:
    response = requests.Response()
    response.status_code = 200
    response.url = url
    response._content = json.dumps(payload).encode("utf-8")
    return response


def _zip_info(name: str, compression: int = ZIP_DEFLATED) -> ZipInfo:
    info = ZipInfo(name, date_time=(2024, 1, 2, 3, 4, 6))
    info.compress_type = compression
    info.create_system = 3
    info.external_attr = 0o100644 << 16
    return info


def _write_epub(
    path: Path,
    *,
    language: str = "en",
    chapter_one: str | None = None,
    chapter_two: str | None = None,
) -> Path:
    chapter_one = chapter_one or """
      <!-- Reader-visible text is translated; this comment is not. -->
      <h1>Hello title</h1>
      <p>Hello <a href="chapter2.xhtml#two">reader</a>.</p>
      <p>Game Loop uses Observer.</p>
      <script>window.keep = "script keep";</script>
      <code>code keep</code><pre>pre keep</pre><kbd>kbd keep</kbd><samp>samp keep</samp>
      <svg><text>svg keep</text></svg><math><mi>math keep</mi></math>
      <p><img src="../images/pixel.png" alt="pixel"/></p>
    """
    chapter_two = chapter_two or '<h1 id="two">Chapter two</h1><p>Goodbye reader.</p>'

    opf = f"""<?xml version="1.0" encoding="utf-8"?>
    <package xmlns="http://www.idpf.org/2007/opf"
             xmlns:dc="http://purl.org/dc/elements/1.1/"
             unique-identifier="book-id" version="3.0">
      <metadata>
        <dc:identifier id="book-id">characterization-book</dc:identifier>
        <dc:title>Characterization Book</dc:title>
        <dc:language>{language}</dc:language>
      </metadata>
      <manifest>
        <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
        <item id="chapter-one" href="text/chapter1.xhtml" media-type="application/xhtml+xml"/>
        <item id="chapter-two" href="text/chapter2.xhtml" media-type="application/xhtml+xml"/>
        <item id="style" href="styles/book.css" media-type="text/css"/>
        <item id="pixel" href="images/pixel.png" media-type="image/png"/>
        <item id="keep" href="assets/keep.bin" media-type="application/octet-stream"/>
      </manifest>
      <spine><itemref idref="chapter-one"/><itemref idref="chapter-two"/></spine>
    </package>
    """
    nav = """<?xml version="1.0" encoding="utf-8"?>
    <html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">
      <head><title>Contents</title></head>
      <body><nav epub:type="toc"><ol>
        <li><a href="text/chapter1.xhtml">Chapter one</a></li>
        <li><a href="text/chapter2.xhtml">Chapter two</a></li>
      </ol></nav></body>
    </html>
    """
    container = """<?xml version="1.0" encoding="utf-8"?>
    <container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
      <rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles>
    </container>
    """

    members = (
        ("META-INF/container.xml", container.encode("utf-8")),
        ("OEBPS/content.opf", opf.encode("utf-8")),
        ("OEBPS/nav.xhtml", nav.encode("utf-8")),
        (
            "OEBPS/text/chapter1.xhtml",
            _xhtml_document("Chapter one", chapter_one).encode("utf-8"),
        ),
        (
            "OEBPS/text/chapter2.xhtml",
            _xhtml_document("Chapter two", chapter_two).encode("utf-8"),
        ),
        ("OEBPS/styles/book.css", b"body { color: #123456; }"),
        ("OEBPS/images/pixel.png", b"not-a-real-png-but-an-unchanged-member"),
        ("OEBPS/assets/keep.bin", b"unchanged-artifact\x00\x01"),
    )
    with ZipFile(path, "w") as archive:
        archive.writestr(_zip_info("mimetype", ZIP_STORED), b"application/epub+zip")
        for name, content in members:
            archive.writestr(_zip_info(name), content)
    return path


def _xhtml_document(title: str, body: str) -> str:
    return f"""<?xml version="1.0" encoding="utf-8"?>
    <html xmlns="http://www.w3.org/1999/xhtml">
      <head><title>{title}</title></head>
      <body>{body}</body>
    </html>
    """


def _install_cli_environment(monkeypatch, tmp_path: Path, session: FakeLibreTranslateSession) -> Path:
    processing_dir = tmp_path / "processing"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr("ayvu.cli._processing_dir", lambda _config: processing_dir)
    monkeypatch.setattr("ayvu.translator.requests.Session", lambda: session)
    return processing_dir


def _translate_args(
    epub_path: Path,
    cache_path: Path,
    *,
    output_path: Path | None = None,
    output_dir: Path | None = None,
    source: str = "en",
    target: str = "pt",
    glossary_path: Path | None = None,
    review_path: Path | None = None,
) -> list[str]:
    args = [
        "--mode",
        "developer",
        "translate",
        str(epub_path),
        "--source",
        source,
        "--target",
        target,
        "--cache",
        str(cache_path),
        "--url",
        "http://translator.test",
        "--retries",
        "0",
    ]
    if output_path is not None:
        args.extend(["--output", str(output_path)])
    if output_dir is not None:
        args.extend(["--output-dir", str(output_dir)])
    if glossary_path is not None:
        args.extend(["--glossary", str(glossary_path)])
    if review_path is not None:
        args.extend(["--review-output", str(review_path)])
    return args


def _read_member(epub_path: Path, member_name: str) -> str:
    with ZipFile(epub_path) as archive:
        return archive.read(member_name).decode("utf-8")


def _zip_metadata(info: ZipInfo) -> tuple[object, ...]:
    return (
        info.filename,
        info.date_time,
        info.compress_type,
        info.flag_bits,
        info.external_attr,
        info.create_system,
        info.extra,
        info.comment,
    )


def _direct_languages() -> list[dict[str, object]]:
    return [
        {"code": "en", "name": "English", "targets": ["pt"]},
        {"code": "pt", "name": "Portuguese", "targets": []},
    ]


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


def test_translate_cli_reuses_exact_cache_and_applies_glossary_after_cache(tmp_path, monkeypatch):
    source_path = _write_epub(tmp_path / "book.epub")
    cache_path = tmp_path / "cache.sqlite"
    session = FakeLibreTranslateSession(_direct_languages())
    processing_dir = _install_cli_environment(monkeypatch, tmp_path, session)

    first_output = tmp_path / "first.epub"
    first = runner.invoke(app, _translate_args(source_path, cache_path, output_path=first_output))
    assert first.exit_code == 0, first.output
    post_count_after_first_run = len(session.posts)

    glossary_path = tmp_path / "glossary.json"
    glossary_path.write_text(json.dumps({"Game Loop": "loop de jogo"}), encoding="utf-8")
    second_output = tmp_path / "second.epub"
    second = runner.invoke(
        app,
        _translate_args(
            source_path,
            cache_path,
            output_path=second_output,
            glossary_path=glossary_path,
        ),
    )

    assert second.exit_code == 0, second.output
    assert len(session.posts) == post_count_after_first_run
    assert "Texts from cache" in second.output
    assert "PT:Game Loop uses Observer." in _read_member(first_output, "OEBPS/text/chapter1.xhtml")
    assert "PT:loop de jogo uses Observer." in _read_member(second_output, "OEBPS/text/chapter1.xhtml")
    # New glossary intent gets a distinct project, preserving the first one.
    manifests = list(processing_dir.glob('projects/*/project.json'))
    assert len(manifests) == 2
    assert len({json.loads(path.read_text())['project_id'] for path in manifests}) == 2
    third_output = tmp_path / 'third.epub'
    third = runner.invoke(app, _translate_args(source_path, cache_path, output_path=third_output,
                                               glossary_path=glossary_path))
    assert third.exit_code == 0, third.output
    assert len(list(processing_dir.glob('projects/*/project.json'))) == 2
    assert len(session.posts) == post_count_after_first_run


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
    monkeypatch.setattr("ayvu.translator.requests.Session", lambda: resumed_session)
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


def test_translate_cli_resolves_intermediate_route_end_to_end(tmp_path, monkeypatch):
    source_path = _write_epub(tmp_path / "route.epub", language="fr")
    session = FakeLibreTranslateSession(
        [
            {"code": "fr", "name": "French", "targets": ["en"]},
            {"code": "en", "name": "English", "targets": ["pt"]},
            {"code": "pt", "name": "Portuguese", "targets": []},
        ]
    )
    _install_cli_environment(monkeypatch, tmp_path, session)
    output_path = tmp_path / "route-pt.epub"

    result = runner.invoke(
        app,
        _translate_args(
            source_path,
            tmp_path / "cache.sqlite",
            output_path=output_path,
            source="fr",
        ),
    )

    assert result.exit_code == 0, result.output
    assert "Route: fr -> en -> pt" in result.output
    assert "PT:EN:Hello title" in _read_member(output_path, "OEBPS/text/chapter1.xhtml")
    pairs = {(payload["source"], payload["target"]) for payload in session.posts}
    assert ("fr", "en") in pairs
    assert ("en", "pt") in pairs


def test_review_export_and_apply_review_cli_round_trip(tmp_path, monkeypatch):
    source_path = _write_epub(tmp_path / "reviewable.epub")
    source_bytes = source_path.read_bytes()
    session = FakeLibreTranslateSession(_direct_languages())
    _install_cli_environment(monkeypatch, tmp_path, session)
    translated_path = tmp_path / "reviewable-pt.epub"
    review_path = tmp_path / "review.csv"

    translated = runner.invoke(
        app,
        _translate_args(
            source_path,
            tmp_path / "cache.sqlite",
            output_path=translated_path,
            review_path=review_path,
        ),
    )
    assert translated.exit_code == 0, translated.output
    assert review_path.exists()

    with review_path.open(encoding="utf-8", newline="") as input_file:
        reader = csv.DictReader(input_file)
        rows = list(reader)
        fieldnames = reader.fieldnames
    assert fieldnames is not None
    edited = False
    for row in rows:
        if "Hello reader" in row["original"]:
            row["translated"] = "Reviewed reader."
            edited = True
    assert edited
    with review_path.open("w", encoding="utf-8", newline="") as output_file:
        writer = csv.DictWriter(output_file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    reviewed = runner.invoke(app, ["--mode", "developer", "apply-review", str(source_path), str(review_path)])

    reviewed_path = tmp_path / "reviewable-pt-reviewed.epub"
    assert reviewed.exit_code == 0, reviewed.output
    assert reviewed_path.exists()
    assert "Reviewed EPUB saved to:" in reviewed.output
    assert "Reviewed reader." in _read_member(reviewed_path, "OEBPS/text/chapter1.xhtml")
    assert source_path.read_bytes() == source_bytes
