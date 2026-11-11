import hashlib
import re
from zipfile import ZIP_DEFLATED
from epub_fakes import epub_bytes
from ayvu.interfaces.cli.app import app
from ayvu.formats.markdown.writer import MarkdownDirectoryWriter
from ayvu.kernel import JobFailure, JobFailureFamily
from test_cli import runner


def test_inspect_command_reports_invalid_epub_without_traceback(tmp_path):
    epub_path = tmp_path / "bad.epub"
    epub_path.write_bytes(b"not a real epub")

    result = runner.invoke(app, ["inspect", str(epub_path)])

    assert result.exit_code == 1
    assert "Não foi possível ler o EPUB informado." in result.output
    assert "Confirme que o arquivo é um EPUB válido e legível" in result.output
    assert "Detalhe técnico: corrupt_archive" in result.output
    assert "Traceback" not in result.output


def test_extract_says_the_output_is_the_problem_when_the_book_is_fine(tmp_path):
    epub_path = tmp_path / "book.epub"
    epub_path.write_bytes(epub_bytes())
    output = tmp_path / "taken"
    output.write_text("mine")

    result = runner.invoke(app, ["extract", str(epub_path), "--output", str(output)])

    assert result.exit_code == 1
    assert "Não foi possível gravar na pasta de saída." in result.output
    assert "EPUB válido" not in result.output
    assert "Traceback" not in result.output
    assert output.read_text() == "mine"


def test_inspect_says_a_book_is_protected_without_refusing_it_or_showing_its_data(tmp_path):
    epub_path = tmp_path / "book.epub"
    encryption = (b'<encryption xmlns="urn:oasis:names:tc:opendocument:xmlns:container" '
                  b'xmlns:e="http://www.w3.org/2001/04/xmlenc#"><e:EncryptedData><e:EncryptionMethod '
                  b'Algorithm="http://www.w3.org/2001/04/xmlenc#aes256-cbc"/><e:CipherData><e:CipherReference '
                  b'URI="OEBPS/text/one.xhtml"/></e:CipherData></e:EncryptedData></encryption>')
    epub_path.write_bytes(epub_bytes(add=(("META-INF/encryption.xml", encryption, ZIP_DEFLATED),)))

    result = runner.invoke(app, ["--mode", "common", "inspect", str(epub_path)])

    assert result.exit_code == 0
    assert "EPUB válido" not in result.output
    assert "proteção" in result.output
    assert "EPUB information" not in result.output
    assert "The Title" not in result.output and "An Author" not in result.output


def test_extract_blames_the_chapter_name_when_it_cannot_become_a_file(tmp_path, monkeypatch):
    epub_path = tmp_path / "book.epub"
    epub_path.write_bytes(epub_bytes())
    monkeypatch.setattr(MarkdownDirectoryWriter, "_name", staticmethod(
        lambda _ordinal, _stem: (None, JobFailure(JobFailureFamily.POLICY, "unsafe_output_name"))))
    output = tmp_path / "out"

    result = runner.invoke(app, ["extract", str(epub_path), "--output", str(output)])

    assert result.exit_code == 1
    assert "nome que não pode virar arquivo" in result.output
    assert "EPUB válido" not in result.output
    assert not list(output.glob("*.md"))


def test_extract_command_reports_invalid_epub_without_traceback(tmp_path):
    epub_path = tmp_path / "bad.epub"
    epub_path.write_bytes(b"not a real epub")
    output_dir = tmp_path / "out"

    result = runner.invoke(app, ["extract", str(epub_path), "--output", str(output_dir)])

    assert result.exit_code == 1
    assert "Não foi possível ler o EPUB informado." in result.output
    assert "Confirme que o arquivo é um EPUB válido e legível" in result.output
    assert "Detalhe técnico: corrupt_archive" in result.output
    assert "Traceback" not in result.output


def test_inspect_command_preserves_the_six_current_fields(minimal_epub_path, tmp_path):
    result = runner.invoke(app, ["inspect", str(minimal_epub_path)])
    assert result.exit_code == 0
    for field in ("Path", "Title", "Authors", "Language", "Documents", "Items"):
        assert field in result.output
    assert str(minimal_epub_path) in result.output
    assert "Minimal Test Book" in result.output
    assert "Ayvu Tests" in result.output
    assert re.search(r"Language\s+│\s*en", result.output)
    assert re.search(r"Documents\s+│\s*3", result.output)
    assert re.search(r"Items\s+│\s*5", result.output)


def test_extract_command_preserves_filenames_text_order_and_original(minimal_epub_path, tmp_path):
    original = hashlib.sha256(minimal_epub_path.read_bytes()).hexdigest()
    output_dir = tmp_path / "extracted"
    result = runner.invoke(app, ["extract", str(minimal_epub_path), "--output", str(output_dir)])
    assert result.exit_code == 0
    assert f"Extracted 3 Markdown files to {output_dir}" in result.output
    assert [path.name for path in sorted(output_dir.glob("*.md"))] == [
        "001-chapter1.md", "002-chapter2.md", "003-nav.md",
    ]
    assert (output_dir / "001-chapter1.md").read_text(encoding="utf-8") == (
        "Chapter One\n\nHello reader. Visit\n\nchapter two\n\n.\n"
    )
    assert (output_dir / "002-chapter2.md").read_text(encoding="utf-8") == "Chapter Two\n\nGoodbye reader.\n"
    assert (output_dir / "003-nav.md").read_text(encoding="utf-8") == "Minimal Test Book\n\nChapter One\n\nChapter Two\n"
    assert hashlib.sha256(minimal_epub_path.read_bytes()).hexdigest() == original


def test_extract_command_keeps_the_nonempty_directory_conflict_message(tmp_path):
    source = tmp_path / "source.epub"
    source.write_bytes(b"not an EPUB")
    output = tmp_path / "out"
    output.mkdir()
    unrelated = output / "keep.txt"
    unrelated.write_text("keep", encoding="utf-8")
    result = runner.invoke(app, ["extract", str(source), "--output", str(output)])
    assert result.exit_code == 1
    assert f"Output directory is not empty: {output}" in result.output
    assert "Use --overwrite to write into it." in result.output
    assert unrelated.read_text(encoding="utf-8") == "keep"
