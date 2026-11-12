

from ayvu.formats.epub import adapter as epub_format
from ayvu.formats.epub.adapter import EPUB_REGISTRATION, epub_format_adapter
from ayvu.formats.contracts import ExtractionRequest, ReconstructionRequest
from ayvu.formats.service import FormatRegistry, FormatService, format_safe_points
from tests.support.epub_fakes import epub_bytes
from tests.support.format_contract_env import PROJECT, FormatEnv, TriggerToken
from ayvu.kernel import JobFailureFamily

from tests.formats.epub.epub_format_support import crash_at, done, extract
from tests.formats.epub.epub_format_support import members_of, rebuild, service, source_of
from tests.support.paths import PROJECT_ROOT


def test_a_crash_after_the_rename_is_not_success_and_reconciles_to_ready(service, tmp_path):
    _, source = source_of(tmp_path)
    documents = service.extract("epub", source, ExtractionRequest(PROJECT)).documents
    env = FormatEnv(tmp_path / "env")
    restore = crash_at(env, "commit")
    result, publication = rebuild(service, env, source, documents)
    restore()
    assert result.artifact is None and result.failure.code == "crashed"
    assert not env.is_ready(publication)
    # While the job is alive nothing is decided, because it may still finish the work itself.
    assert len(env.artifacts.reconcile().active) == 1 and not env.is_ready(publication)
    env.jobs.release_lease(env.job.job_id, env.job.lease.owner_token)
    report = env.artifacts.reconcile()
    assert (len(report.completed), report.abandoned) == (1, ())
    assert env.is_ready(publication)
    assert members_of(env.read(publication))["OEBPS/style.css"] == b"p { color: red }"


def test_a_crash_before_the_rename_never_becomes_ready(service, tmp_path):
    _, source = source_of(tmp_path)
    documents = service.extract("epub", source, ExtractionRequest(PROJECT)).documents
    env = FormatEnv(tmp_path / "env")
    restore = crash_at(env, "rename")
    result, publication = rebuild(service, env, source, documents)
    restore()
    assert result.artifact is None and result.failure.code == "crashed" and not env.is_ready(publication)
    env.jobs.release_lease(env.job.job_id, env.job.lease.owner_token)
    report = env.artifacts.reconcile()
    assert (report.completed, len(report.abandoned)) == ((), 1) and not env.is_ready(publication)


def test_a_failing_validation_leaves_no_ready_artifact_after_reconciliation(service, tmp_path, monkeypatch):
    _, source = source_of(tmp_path)
    documents = service.extract("epub", source, ExtractionRequest(PROJECT)).documents
    monkeypatch.setattr(epub_format, "_findings", lambda *_args: (_ for _ in ()).throw(RuntimeError("Call me Ishmael")))
    env = FormatEnv(tmp_path / "env")
    result, publication = rebuild(service, env, source, documents)
    assert result.failure.code == "adapter_failed" and "Ishmael" not in repr(result)
    env.jobs.release_lease(env.job.job_id, env.job.lease.owner_token)
    env.artifacts.reconcile()
    assert not env.is_ready(publication)


def test_the_source_is_unchanged_after_success_and_after_every_kind_of_failure(service, tmp_path):
    path, source = source_of(tmp_path)
    fingerprint = source.fingerprint()
    documents = extract(service, source).documents
    env = FormatEnv(tmp_path / "env")
    rebuild(service, env, source, documents)
    rebuild(service, env, source, documents, ())
    for point in format_safe_points(epub_format_adapter().descriptor):
        token = TriggerToken(format_safe_points(epub_format_adapter().descriptor), point)
        service.reconstruct("epub", source, ReconstructionRequest(documents, done(documents)), env.publication(), cancellation=token)
    assert source.fingerprint() == fingerprint and path.read_bytes() == epub_bytes()


# The spool

def test_a_large_book_spills_and_comes_back_whole(service, tmp_path, monkeypatch):
    monkeypatch.setattr(epub_format, "SPOOL_MEMORY_BYTES", 16)
    _, source = source_of(tmp_path)
    documents = service.extract("epub", source, ExtractionRequest(PROJECT)).documents
    env = FormatEnv(tmp_path / "env")
    result, publication = rebuild(service, env, source, documents)
    assert result.failure is None and members_of(env.read(publication))["OEBPS/style.css"] == b"p { color: red }"


def test_output_chunks_are_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(epub_format, "_CHUNK_BYTES", 10)
    registry = FormatRegistry()
    registry.register(EPUB_REGISTRATION, epub_format_adapter())
    service = FormatService(registry)
    _, source = source_of(tmp_path)
    documents = service.extract("epub", source, ExtractionRequest(PROJECT)).documents
    env = FormatEnv(tmp_path / "env")
    sizes = []
    inner = env.publisher.publish

    def spying(request, chunks, **options):
        def counted():
            for chunk in chunks:
                sizes.append(len(chunk))
                yield chunk
        return inner(request, counted(), **options)

    env.publisher.publish = spying
    result, _ = rebuild(service, env, source, documents)
    assert result.failure is None and max(sizes) <= 10 and len(sizes) > 1


# Privacy and boundaries

def test_nothing_from_the_book_appears_in_a_result_or_a_representation(service, tmp_path):
    _, source = source_of(tmp_path)
    documents = extract(service, source).documents
    inventory = service.inspect("epub", source)
    for value in (inventory, inventory.inventory, documents[1], documents[1].units[0], epub_format_adapter(), epub_format_adapter().inspector):
        assert not any(secret in repr(value) for secret in ("Hello", "The Title", "An Author", "text/one"))


def test_the_adapter_imports_no_provider_cache_glossary_or_interface():
    import ast
    import subprocess
    import sys

    forbidden = ("ayvu.v1.cache", "ayvu.knowledge.glossary", "ayvu.providers.libretranslate.client", "ayvu.knowledge.memory", "ayvu.translation", "ayvu.providers", "ayvu.interfaces", "ayvu.formats.epub.io", "ayvu.v1.html_translate", "ayvu.formats.epub.validation", "ebooklib", "typer", "rich", "requests", "sqlite3")
    source = (PROJECT_ROOT / "src" / "ayvu" / "formats" / "epub" / "adapter.py").read_text()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom):
            name = f"ayvu.{node.module}" if node.level else node.module
            assert not name.startswith(forbidden), name
    code = f"import sys, ayvu.formats.epub.adapter; print([m for m in sys.modules if m.startswith({forbidden!r})])"
    done_ = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert done_.stdout.strip() == "[]"


def test_no_html_is_traversed_here():
    source = (PROJECT_ROOT / "src" / "ayvu" / "formats" / "epub" / "adapter.py").read_text()
    assert ".children" not in source and "NavigableString" not in source and "get_text" not in source


def test_the_current_flow_writes_no_durable_artifact(tmp_path):
    for source in ((PROJECT_ROOT / "src" / "ayvu" / name).read_text() for name in ("formats/epub/io.py", "v1/epub.py")):
        assert "epub_format" not in source and "ArtifactService" not in source and "artifact" not in source.lower()


def test_cancelling_between_documents_stops_before_any_document_is_rebuilt(service, tmp_path, monkeypatch):
    _, source = source_of(tmp_path)
    documents = service.extract("epub", source, ExtractionRequest(PROJECT)).documents
    rebuilt = []
    monkeypatch.setattr(epub_format, "reconstruct_html", lambda *_args, **_options: rebuilt.append(1) or b"<html/>")
    token = TriggerToken(format_safe_points(epub_format_adapter().descriptor), epub_format.BETWEEN_DOCUMENTS)
    env = FormatEnv(tmp_path / "env")
    result = service.reconstruct("epub", source, ReconstructionRequest(documents, done(documents)), env.publication(), cancellation=token)
    assert result.failure.family is JobFailureFamily.CANCELLED and rebuilt == []


def test_the_descriptor_states_the_source_limit():
    assert epub_format_adapter().descriptor.limits.max_source_bytes == 8 * 1024 ** 3


def test_a_small_book_is_built_in_memory(service, tmp_path, monkeypatch):
    sizes = []
    real = epub_format.tempfile.SpooledTemporaryFile

    def spying(*args, max_size, **options):
        sizes.append(max_size)
        return real(*args, max_size=max_size, **options)

    monkeypatch.setattr(epub_format.tempfile, "SpooledTemporaryFile", spying)
    _, source = source_of(tmp_path)
    documents = service.extract("epub", source, ExtractionRequest(PROJECT)).documents
    result, _ = rebuild(service, FormatEnv(tmp_path / "env"), source, documents)
    assert result.failure is None and sizes == [64 * 1024 ** 2]
