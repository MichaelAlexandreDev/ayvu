"""The run's meter: the cost cap shared by workers, and usage recorded once per request (#380)."""
from __future__ import annotations

import threading
import time

import pytest

from ayvu.providers import DeliveryState, ProviderUsage, UsageTotals
from ayvu.providers.prices import ModelPrice, PriceSource, ResolvedPrice
from ayvu.translation import RunMeter, count_words

# $1 per million input tokens and $2 per million output tokens: one input token is one micro-dollar.
PRICE = ResolvedPrice(ModelPrice("1", "2"), PriceSource.CONFIG, "config")


class Writer:
    def __init__(self) -> None:
        self.records, self.asked = [], []

    def record_many(self, records):
        self.records.extend(records)
        return len(records)

    def totals(self, job_ids):
        self.asked.append(job_ids)
        return UsageTotals(7, 70, 14, 700, 140, 0, 980)


def used(input_tokens, output_tokens, cached=0):
    return ProviderUsage(input_tokens=input_tokens, output_tokens=output_tokens, cached_input_tokens=cached)


def test_a_reservation_fits_up_to_the_cap_exactly_and_not_one_micro_dollar_more():
    meter = RunMeter("ollama", "m", PRICE, max_cost_micro_usd=300)
    first = meter.reserve(100, 50)  # 100 + 2 * 50 = 200
    assert first is not None
    meter.settle(first, None)  # nothing known: the whole 200 counts
    assert meter.reserve(1, 50) is None  # 200 + 101 > 300
    assert meter.reserve(0, 50) is not None  # 200 + 100 == 300 still fits


def test_a_worst_case_that_fits_what_is_spent_waits_for_the_calls_in_flight():
    meter = RunMeter("ollama", "m", PRICE, max_cost_micro_usd=300)
    first = meter.reserve(100, 50)  # 200 in flight
    got = []
    waiting = threading.Thread(target=lambda: got.append(meter.reserve(100, 50)))
    waiting.start()
    waiting.join(0.05)
    assert waiting.is_alive() and got == []  # 200 + 200 > 300, but only 0 is really spent
    meter.settle(first, used(50, 0))  # the first call cost 50
    waiting.join(5)
    assert got and got[0] is not None  # 50 + 200 <= 300
    meter.settle(got[0], None)  # nothing known of it: its whole 200 counts
    assert meter.reserve(100, 50) is None  # 250 + 200 > 300 even with nothing in flight: refused at once


def test_a_worst_case_that_exactly_fills_the_cap_waits_rather_than_being_refused():
    meter = RunMeter("ollama", "m", PRICE, max_cost_micro_usd=300)
    meter.settle(meter.reserve(100, 0), used(100, 0))  # 100 really spent
    in_flight = meter.reserve(0, 25)  # 50 in flight
    got = []
    waiting = threading.Thread(target=lambda: got.append(meter.reserve(100, 50)))  # 200: 100 + 200 == 300
    waiting.start()
    waiting.join(0.05)
    assert waiting.is_alive() and got == []
    meter.settle(in_flight, used(0, 0))
    waiting.join(5)
    assert got and got[0] is not None


def test_a_settled_call_frees_what_it_did_not_use():
    meter = RunMeter("ollama", "m", PRICE, max_cost_micro_usd=300)
    reservation = meter.reserve(100, 50)
    meter.settle(reservation, used(100, 10))  # it cost 120, not 200
    assert meter.reserve(0, 90) is not None  # 120 + 180 == 300
    assert meter.summary().spent_micro_usd == 120


def test_an_unknown_count_keeps_its_reserved_worst_case():
    meter = RunMeter("ollama", "m", PRICE, max_cost_micro_usd=10**6)
    meter.settle(meter.reserve(100, 50), used(None, 10))  # input unknown: the estimated 100 counts
    meter.settle(meter.reserve(100, 50), None)  # nothing known: the whole 200 counts
    assert meter.summary().spent_micro_usd == 120 + 200


def test_four_workers_together_never_pass_the_cap():
    cap, call = 10_000, (300, 100)  # each call reserves 500 and really costs 350
    meter = RunMeter("ollama", "m", PRICE, max_cost_micro_usd=cap)
    sent, refused, lock = [], [], threading.Lock()
    start = threading.Barrier(4)

    def worker():
        start.wait()
        while True:
            reservation = meter.reserve(*call)
            if reservation is None:
                with lock:
                    refused.append(1)
                return
            time.sleep(0.001)  # the call is in flight while the other workers reserve
            meter.settle(reservation, used(300, 25))
            with lock:
                sent.append(1)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    spent = meter.summary().spent_micro_usd
    assert spent == 350 * len(sent) and spent <= cap
    assert len(refused) == 4
    assert cap - spent < 500  # it stopped only when the next worst case no longer fitted


def test_without_a_cap_every_call_fits_and_an_unknown_price_costs_unknown():
    meter = RunMeter("fake_remote", "m", None)
    reservation = meter.reserve(10**9, 10**9)
    assert reservation is not None
    meter.settle(reservation, used(1, 1))
    meter.record(job_id="j", document_id="d", unit_id="u", request_id="r", text="Hello reader.",
                 usage=used(5, 6), delivery=DeliveryState.COMPLETED)
    summary = meter.summary()
    assert summary.run.cost_micro_usd is None and summary.spent_micro_usd is None and summary.price is None


def test_a_cap_needs_a_known_price():
    with pytest.raises(ValueError, match="unknown_price"):
        RunMeter("fake_remote", "m", None, max_cost_micro_usd=1)
    with pytest.raises(ValueError, match="invalid_max_cost"):
        RunMeter("ollama", "m", PRICE, max_cost_micro_usd=-1)


def test_each_result_is_one_record_with_counts_only_and_a_request_never_sent_is_none():
    writer = Writer()
    meter = RunMeter("ollama", "qwen2.5:7b", PRICE)
    meter.bind("job-2", ("job-1",), writer)
    meter.record(job_id="job-2", document_id="doc", unit_id="unit", request_id="r1",
                 text="Hello __AYVU_TAG_0__dear__AYVU_TAG_1__ reader.", usage=used(10, 20), delivery=DeliveryState.COMPLETED)
    meter.record(job_id="job-2", document_id="doc", unit_id="unit", request_id="r2", text="Never sent.",
                 usage=used(0, 0), delivery=DeliveryState.NOT_SENT)
    assert writer.records == []  # written when the worker's document ends, in one transaction
    meter.flush()
    [record] = writer.records
    assert (record.request_id, record.job_id, record.provider, record.model) == ("r1", "job-2", "ollama", "qwen2.5:7b")
    assert (record.words, record.input_tokens, record.output_tokens, record.cost_micro_usd) == (3, 10, 20, 50)
    assert (record.price_version, record.price_source) == ("config", "config")
    assert not any(isinstance(value, str) and "reader" in value for value in vars(record).values())
    summary = meter.summary()
    assert summary.run == UsageTotals(1, record.characters, 3, 10, 20, 0, 50)
    assert summary.resumed and summary.job == UsageTotals(7, 70, 14, 700, 140, 0, 980)
    assert writer.asked == [("job-2", "job-1")]


def test_a_job_total_that_cannot_be_read_is_unknown_and_the_report_still_works():
    class Broken(Writer):
        def totals(self, job_ids):
            raise OSError("disk")

    meter = RunMeter("ollama", "m", PRICE)
    meter.bind("job", (), Broken())
    summary = meter.summary()
    assert summary.job is None and not summary.resumed


def test_words_are_what_a_person_wrote_not_placeholders():
    assert count_words("Hello __AYVU_TAG_0__dear__AYVU_TAG_1__ reader, __AYVU_PROTECTED_0__ again.") == 4
    assert count_words("   ") == 0


def test_records_that_cannot_be_written_stop_the_job_unless_the_run_is_already_failing():
    from ayvu.translation import TranslationExecutionError

    class Full(Writer):
        def record_many(self, records):
            raise OSError("disk full")

    meter = RunMeter("ollama", "m", PRICE)
    meter.bind("job", (), Full())
    meter.record(job_id="job", document_id="d", unit_id="u", request_id="r", text="Hi.", usage=used(1, 1),
                 delivery=DeliveryState.COMPLETED)
    with pytest.raises(TranslationExecutionError) as stopped:
        meter.flush()
    assert stopped.value.failure.code == "usage_record_failed"
    meter.record(job_id="job", document_id="d", unit_id="u", request_id="r2", text="Hi.", usage=used(1, 1),
                 delivery=DeliveryState.COMPLETED)
    meter.flush(quiet=True)
    assert meter.summary().run.calls == 2
