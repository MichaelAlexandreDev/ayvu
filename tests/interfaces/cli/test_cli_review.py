from ayvu.interfaces.cli.app import app
from test_cli import _write_review_csv_file, runner


def test_apply_review_command_rejects_v1_before_opening_epub_or_reconstructing(tmp_path, monkeypatch):
    epub_path = tmp_path / "book.epub"
    epub_path.write_bytes(b"fake epub")
    review_path = _write_review_csv_file(tmp_path / "review.csv")

    def fail_apply(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("legacy v1 must be rejected before apply")

    monkeypatch.setattr("ayvu.composition.builders.translate_project.validate_output", fail_apply)

    result = runner.invoke(app, ["apply-review", str(epub_path), str(review_path)])

    assert result.exit_code == 1
    assert "CSV v1 serve apenas para inspeção" in result.output
    assert "legacy_reexport_required" in result.output
    assert "pacote v2" in result.output
    assert "Traceback" not in result.output


def test_apply_review_command_requires_a_v2_mapper_for_nonlegacy_input(tmp_path, monkeypatch):
    epub_path = tmp_path / "book.epub"
    epub_path.write_bytes(b"fake epub")
    review_path = tmp_path / "package.csv"
    review_path.write_text("placeholder", encoding="utf-8")
    result = runner.invoke(app, ["apply-review", str(epub_path), str(review_path)])

    assert result.exit_code == 1
    assert "invalid_preamble" in result.output
    assert "Traceback" not in result.output


def test_apply_review_command_rejects_legacy_before_output_conflict_check(tmp_path, monkeypatch):
    epub_path = tmp_path / "book.epub"
    epub_path.write_bytes(b"fake epub")
    review_path = _write_review_csv_file(tmp_path / "review.csv")
    output_path = tmp_path / "book-pt-reviewed.epub"
    output_path.write_bytes(b"already here")

    def fail_apply(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("apply should not run when output already exists")

    result = runner.invoke(app, ["apply-review", str(epub_path), str(review_path)])

    assert result.exit_code == 1
    assert "legacy_reexport_required" in result.output
    assert output_path.read_bytes() == b"already here"
    assert "Traceback" not in result.output


def test_apply_review_command_reports_unreadable_review_file(tmp_path, monkeypatch):
    epub_path = tmp_path / "book.epub"
    epub_path.write_bytes(b"fake epub")
    broken_csv = tmp_path / "broken.csv"
    broken_csv.write_text("segment_id,translated\nc0001-s0001,x\n", encoding="utf-8")

    result = runner.invoke(app, ["apply-review", str(epub_path), str(broken_csv)])

    assert result.exit_code == 1
    assert "Não foi possível ler o arquivo de revisão." in result.output
    assert "Traceback" not in result.output
