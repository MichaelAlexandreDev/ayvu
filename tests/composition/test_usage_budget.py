"""Usage and the cost cap through the rich runtime, with four workers and real project stores (#380).

The same door the CLI uses (``open_translate_project`` with a ``RichTranslationRuntime``) runs over a fake
model that reports its tokens; the binding carries the run's meter, as ``composition.builders.usage`` builds it.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from ayvu.composition.builders import translate_project as composition
from ayvu.composition.builders import usage as usage_builder
from ayvu.composition.builders.llm import LlmBinding
from ayvu.composition.builders.rich_workers import project_store_options
from ayvu.jobs.model import JobState
from ayvu.kernel import LanguagePair, OutputPlan, TranslationOptions
from ayvu.projects.schema import open_provider_usage
from ayvu.projects.store import ProjectJsonRepository, build_project_request
from ayvu.providers.prices import ModelPrice, PriceSource, ResolvedPrice
from ayvu.translation import PreflightError, RunMeter
from ayvu.translation.project_contracts import TranslateProjectCommand
from tests.support.llm_fakes import LLM_PRODUCER, CountingModel, fake_snapshot
from tests.support.rich_preview_support import preview_book

PAIR = LanguagePair(source="en", target="pt")
# One micro-dollar per input token, two per output token.
PRICE = ResolvedPrice(ModelPrice("1", "2"), PriceSource.CONFIG, "config")


def translate(tmp_path, source, meter, log, *, workers=4):
    projects = ProjectJsonRepository(tmp_path / "processing")
    project = projects.resolve(build_project_request(source, source="en", target="pt")).project
    paths = projects.resolve_database_paths(project.project_id)
    options = TranslationOptions(language_pair=PAIR, workers=workers)
    binding = LlmBinding(lambda: CountingModel(log), LLM_PRODUCER, fake_snapshot, clock=composition._utc_now)
    runtime = composition.RichTranslationRuntime(
        source, options, lambda: composition.LegacyResources(None, None, lambda: None), tmp_path / "cache.sqlite",
        "unused", paths.database, paths.backups, projects.resolve_artifact_root(project.project_id), project.project_id,
        binding=binding.with_usage(meter))
    destination = tmp_path / "out.epub"
    stores = project_store_options(SimpleNamespace(model_label="fake-model", options=options), runtime)
    use_case = composition.open_translate_project(tmp_path / "processing", project.project_id, source, destination,
                                                  overwrite=False, runtime=runtime, **stores)
    command = TranslateProjectCommand(project.project_id, project.source_sha256, options,
                                      OutputPlan.for_translation(source, destination, PAIR), execution_revision=2,
                                      producer=runtime.producer, additional_output_roles=("raw_results",))
    result = use_case.execute(command)
    usage = open_provider_usage(paths.database, paths.backups, project.project_id)
    return result, destination, usage


def test_four_workers_record_every_model_call_once(tmp_path):
    source = preview_book(tmp_path / "book.epub")
    meter, log = RunMeter("fake_llm", "fake-model", PRICE), []
    result, output, usage = translate(tmp_path, source, meter, log)
    assert result.state is JobState.VALIDATED and output.exists()
    rows = usage.records(result.job_id)
    assert len(rows) == len(log) > 4 and len({row.request_id for row in rows}) == len(rows)
    assert sum(row.input_tokens for row in rows) == sum(item[0] for item in log)
    assert sum(row.output_tokens for row in rows) == sum(item[1] for item in log)
    assert all(row.cost_micro_usd == row.input_tokens + 2 * row.output_tokens for row in rows)
    summary = meter.summary()
    assert summary.run.calls == len(rows) and summary.job == usage.totals((result.job_id,))
    assert summary.run.cost_micro_usd == summary.spent_micro_usd == sum(row.cost_micro_usd for row in rows)


@pytest.mark.parametrize("workers", [1, 4])
def test_the_cap_stops_before_the_request_that_would_pass_it(tmp_path, workers):
    source = preview_book(tmp_path / "book.epub")
    probe_log = []
    translate(tmp_path / "probe", source, RunMeter("fake_llm", "fake-model", PRICE), probe_log, workers=workers)
    full_cost = sum(item[0] + 2 * item[1] for item in probe_log)
    # Room for one worst case (about 8,600 micro-dollars) and a few real calls, far from the whole book.
    cap = 8_600 + full_cost // 4
    meter, log = RunMeter("fake_llm", "fake-model", PRICE, max_cost_micro_usd=cap), []
    result, output, usage = translate(tmp_path / "capped", source, meter, log, workers=workers)
    assert result.state is JobState.FAILED and result.failure.code == "cost_budget_reached"
    assert not output.exists()  # no partial EPUB
    spent = sum(item[0] + 2 * item[1] for item in log)
    assert 0 < len(log) < len(probe_log) and spent <= cap
    rows = usage.records(result.job_id)
    assert len(rows) == len(log) and sum(row.cost_micro_usd for row in rows) == spent == meter.summary().spent_micro_usd


def test_a_cap_without_a_known_price_is_refused_before_anything_is_prepared():
    book = SimpleNamespace(translator_name="fake_remote", translator_models={"fake_remote": "fake-model-1"},
                           prices={}, max_cost=1_000_000)
    with pytest.raises(PreflightError) as refused:
        usage_builder.run_meter(book)
    assert refused.value.detail == "price_unknown" and "--max-cost" in refused.value.summary


def test_a_configured_price_makes_the_meter_and_local_models_cost_nothing_by_default():
    configured = SimpleNamespace(translator_name="ollama", translator_models={"ollama": "qwen2.5:7b"}, max_cost=5,
                                 prices={"ollama": {"qwen2.5:7b": {"input": "1", "output": "2"}}})
    meter = usage_builder.run_meter(configured)
    assert meter.price.source is PriceSource.CONFIG and meter.max_cost_micro_usd == 5
    default = usage_builder.run_meter(SimpleNamespace(translator_name="ollama", translator_models={"ollama": "m"},
                                                      prices={}, max_cost=None))
    assert default.price.source is PriceSource.TABLE and default.price.price == ModelPrice("0", "0")
