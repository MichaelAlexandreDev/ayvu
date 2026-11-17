from __future__ import annotations
import hashlib
import json
from dataclasses import replace
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile, ZipInfo
import pytest
import requests
from typer.testing import CliRunner
from ayvu.interfaces.cli.app import app
from ayvu.recovery.checkpoint import COMPLETED_STATUS, RUNNING_STATUS, ResumeStateStore
from ayvu.review import decode_review_package, encode_review_package

runner = CliRunner()


@pytest.fixture(autouse=True)
def _isolated_from_the_user(tmp_path, monkeypatch):
    """The CLI's defaults are relative to the working directory and the home folder, which hold real books and caches."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.chdir(tmp_path)


class FakeLibreTranslateSession:
    """A deterministic in-process LibreTranslate boundary for CLI workflows."""

    def mount(self, prefix, adapter):
        assert prefix == "http://"

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

    def close(self) -> None:
        pass

    def get(self, url: str, *, timeout: float, allow_redirects: bool = False) -> requests.Response:
        assert allow_redirects is False
        self.gets.append((url, timeout))
        return _json_response(self.languages, url)

    def post(self, url: str, *, json: dict[str, str], timeout: float, allow_redirects: bool = False) -> requests.Response:
        assert allow_redirects is False
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
    monkeypatch.setattr("ayvu.interfaces.cli.settings._processing_dir", lambda _config: processing_dir)
    monkeypatch.setattr("ayvu.providers.libretranslate.client.requests.Session", lambda: session)
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
        "http://localhost:5000",
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


# The translation command as a user meets it, through the CLI and a fake HTTP server only.
# #145 moves what is behind it, so nothing here may patch anything inside Ayvu's translation.

class FailingSession(FakeLibreTranslateSession):
    def __init__(self, languages, failing_text: str) -> None:
        super().__init__(languages)
        self.failing_text = failing_text

    def post(self, url: str, *, json: dict[str, str], timeout: float, allow_redirects: bool = False) -> requests.Response:
        assert allow_redirects is False
        if self.failing_text in json["q"]:
            self.posts.append(dict(json))
            raise requests.exceptions.ConnectionError("translator went away")
        return super().post(url, json=json, timeout=timeout, allow_redirects=allow_redirects)


def _run_translate(tmp_path, monkeypatch, *extra, session=None, source_path=None, input=None):
    source_path = source_path or _write_epub(tmp_path / "book.epub")
    session = session or FakeLibreTranslateSession(_direct_languages())
    _install_cli_environment(monkeypatch, tmp_path, session)
    args = _translate_args(source_path, tmp_path / "cache.sqlite", output_path=tmp_path / "out.epub")
    result = runner.invoke(app, [*args, *extra], input=input)
    return result, session, tmp_path / "out.epub", source_path


def _report_value(output: str, label: str) -> str:
    for line in output.splitlines():
        if label in line:
            return line.split(label, 1)[1].strip(" │|")
    raise AssertionError(f"{label} not in report:\n{output}")


def _members(path: Path) -> dict[str, bytes]:
    with ZipFile(path) as archive:
        return {info.filename: archive.read(info) for info in archive.infolist()}
