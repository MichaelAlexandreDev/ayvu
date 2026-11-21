"""The usage and cost of one invocation: counted once across its workers, and capped (#380).

One ``RunMeter`` is shared by every worker of a ``translate`` or ``resume``. It is the LLM core's cost
meter: before each model call the core reserves the call's worst case (estimated input plus the full
output allowance) at the model's price, and a call whose reservation does not fit under ``--max-cost``
is never sent. The reservation is decided under a lock, so workers running together cannot pass the cap
between them: a worker whose worst case fits only once the calls in flight are settled waits for them.
After the call the reservation is settled with what the model reported, or kept whole when nothing is
known. It is also the translation's usage recorder: each provider result becomes one record, written
through a port by request id when the worker's document ends. The cap applies to this invocation; a report also shows the job's total,
over the jobs its resume continues.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Protocol

from ..content import PROTECTED_TOKEN_PATTERN, TAG_TOKEN_PATTERN
from ..kernel import JobFailure, JobFailureFamily
from ..providers import DeliveryState, ProviderUsage, ResolvedPrice, UsageRecord, UsageTotals, cost_micro_usd
from .document_contracts import TranslationExecutionError


def count_words(text: str) -> int:
    """Words of the text a person wrote: Ayvu's placeholder tokens and bare punctuation are not words."""
    plain = PROTECTED_TOKEN_PATTERN.sub(" ", TAG_TOKEN_PATTERN.sub(" ", text))
    return sum(1 for word in plain.split() if any(character.isalnum() for character in word))


class UsageWriter(Protocol):
    def record_many(self, records: tuple[UsageRecord, ...]) -> int: ...
    def totals(self, job_ids: tuple[str, ...]) -> UsageTotals: ...


@dataclass(frozen=True)
class UsageSummary:
    """What a report shows. ``job`` is None when the store could not be read or the job did not start."""

    provider: str
    model: str | None
    run: UsageTotals
    job: UsageTotals | None
    resumed: bool
    price: ResolvedPrice | None
    max_cost_micro_usd: int | None
    spent_micro_usd: int | None


@dataclass(frozen=True)
class _Reservation:
    amount: int
    input_tokens: int
    output_tokens: int


class RunMeter:
    """Thread-safe. ``price`` None is an unknown price: costs are unknown, and a cap cannot be enforced."""

    def __init__(self, provider: str, model: str | None, price: ResolvedPrice | None, *,
                 max_cost_micro_usd: int | None = None) -> None:
        if max_cost_micro_usd is not None and (type(max_cost_micro_usd) is not int or max_cost_micro_usd < 0):
            raise ValueError("invalid_max_cost")
        if max_cost_micro_usd is not None and price is None:
            raise ValueError("unknown_price")
        self.provider, self.model, self.price = provider, model, price
        self.max_cost_micro_usd = max_cost_micro_usd
        self._lock = threading.Condition()
        self._spent = 0
        self._reserved = 0
        self._totals = UsageTotals()
        self._writer: UsageWriter | None = None
        self._pending: list[UsageRecord] = []
        self._jobs: tuple[str, ...] = ()

    def __repr__(self) -> str:
        return f"RunMeter(provider={self.provider!r})"

    def _cost(self, input_tokens: int | None, output_tokens: int | None, cached: int | None) -> int | None:
        return cost_micro_usd(None if self.price is None else self.price.price, input_tokens, output_tokens, cached)

    def reserve(self, input_tokens: int, max_output_tokens: int) -> _Reservation | None:
        """The call's worst case, held until it is settled; None when it would pass the cap.

        While other calls are in flight, a worst case that fits what is really spent waits for them to settle,
        since their own worst cases may not be used; it is refused only when it cannot fit even then.
        """
        amount = self._cost(input_tokens, max_output_tokens, 0) or 0
        cap = self.max_cost_micro_usd
        with self._lock:
            while cap is not None and self._spent + self._reserved + amount > cap:
                if self._spent + amount > cap:
                    return None
                self._lock.wait()
            self._reserved += amount
        return _Reservation(amount, input_tokens, max_output_tokens)

    def settle(self, reservation: _Reservation, spent: ProviderUsage | None) -> None:
        """Replace the reservation by the call's cost. An unknown count keeps its reserved worst case."""
        if spent is None:
            actual = reservation.amount
        else:
            actual = self._cost(reservation.input_tokens if spent.input_tokens is None else spent.input_tokens,
                                reservation.output_tokens if spent.output_tokens is None else spent.output_tokens,
                                spent.cached_input_tokens or 0) or 0
        with self._lock:
            self._reserved -= reservation.amount
            self._spent += actual
            self._lock.notify_all()

    def bind(self, job_id: str, continued: tuple[str, ...], writer: UsageWriter | None) -> None:
        """The job this invocation runs, the jobs it continues (nearest first) and where records go."""
        with self._lock:
            self._jobs, self._writer = (job_id, *continued), writer

    def record(self, *, job_id: str, document_id: str, unit_id: str, request_id: str, text: str,
               usage: ProviderUsage, delivery: DeliveryState) -> None:
        """One provider result. A request never sent used nothing and is not a call."""
        if delivery is DeliveryState.NOT_SENT:
            return
        price = self.price
        record = UsageRecord(
            request_id, job_id, document_id, unit_id, self.provider, self.model,
            len(text) if usage.characters_submitted is None else usage.characters_submitted, count_words(text),
            usage.input_tokens, usage.output_tokens, usage.cached_input_tokens,
            self._cost(usage.input_tokens, usage.output_tokens, usage.cached_input_tokens),
            None if price is None else price.version, None if price is None else price.source.value)
        with self._lock:
            self._totals = self._totals.plus(UsageTotals.of(record))
            self._pending.append(record)

    def flush(self, *, quiet: bool = False) -> None:
        """Write the pending records in one transaction. Workers flush when a document ends, however it ends,
        so the project database sees one usage write per document rather than one per request."""
        with self._lock:
            writer, pending = self._writer, tuple(self._pending)
            if writer is None or not pending:
                return
            self._pending.clear()
        try:
            writer.record_many(pending)
        except Exception:
            if not quiet:
                # The requests were made; without their records the job's usage would be wrong, so it stops.
                raise TranslationExecutionError(JobFailure(JobFailureFamily.RESOURCE, "usage_record_failed")) from None

    def summary(self) -> UsageSummary:
        self.flush(quiet=True)
        with self._lock:
            run, jobs, writer, spent = self._totals, self._jobs, self._writer, self._spent
        job = None
        if writer is not None and jobs:
            try:
                job = writer.totals(jobs)
            except Exception:
                job = None  # A report never fails for a total it cannot read; it shows it as unknown.
        return UsageSummary(self.provider, self.model, run, job, len(jobs) > 1, self.price, self.max_cost_micro_usd,
                            None if self.price is None else spent)
