from __future__ import annotations
from dataclasses import replace
from ayvu.interfaces.cli.app import app
from ayvu.review import decode_review_package, encode_review_package
from tests.support.workflow_characterization_support import FakeLibreTranslateSession, _direct_languages, _install_cli_environment, _isolated_from_the_user
from tests.support.workflow_characterization_support import _read_member, _translate_args
from tests.support.workflow_characterization_support import _write_epub, runner


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


def test_cli_review_export_is_v2_and_apply_reconstructs_only_after_validation(tmp_path, monkeypatch):
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
    package_bytes = review_path.read_bytes()
    assert package_bytes.startswith(b"AYVU-REVIEW-PACKAGE,2,")
    package = decode_review_package(package_bytes)
    edited = replace(package, rows=tuple(
        replace(row, reviewed_text=row.machine_translation.replace("PT:", "Reviewed:"), review_status="reviewed")
        for row in package.rows))
    review_path.write_bytes(encode_review_package(edited))

    output_path = tmp_path / "reviewable-pt-reviewed.epub"
    preview = runner.invoke(app, ["--mode", "developer", "apply-review", str(source_path), str(review_path),
                                  "--validate-only"])
    reviewed = runner.invoke(app, ["--mode", "developer", "apply-review", str(source_path), str(review_path),
                                   "--output", str(output_path)])

    assert preview.exit_code == 0, preview.output
    assert "Review package validation" in " ".join(preview.output.split())
    assert reviewed.exit_code == 0, reviewed.output
    assert "Review application report" in reviewed.output
    assert output_path.exists()
    assert 'Reviewed:Hello <a href="chapter2.xhtml#two">reader</a>.' in _read_member(output_path, "OEBPS/text/chapter1.xhtml")
    assert source_path.read_bytes() == source_bytes
