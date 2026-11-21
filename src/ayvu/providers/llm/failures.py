"""Map what can go wrong with a model call to the existing ``JobFailure`` families. No new family is added."""
from __future__ import annotations

from ..ports import DeliveryState
from ...kernel import JobFailure, JobFailureFamily
from .model import ModelCallError, TransportReason

MAX_RETRY_AFTER_MS = 2**31 - 1

# A refusal or safety stop will not change on a retry of the same prompt.
REFUSAL = JobFailure(JobFailureFamily.PROVIDER_PERMANENT, "provider_refusal")
# Structure was still wrong when the unit was asked for alone.
OUTPUT_INVALID = JobFailure(JobFailureFamily.PROVIDER_PERMANENT, "provider_output_invalid")
OUTPUT_TRUNCATED = JobFailure(JobFailureFamily.PROVIDER_PERMANENT, "provider_output_truncated")
EMPTY_RESULT = JobFailure(JobFailureFamily.PROVIDER_PERMANENT, "empty_result")
PLACEHOLDER_MISMATCH = JobFailure(JobFailureFamily.PROVIDER_PERMANENT, "placeholder_mismatch")
# The run's cost cap left no room for the next call's worst case, so the call was not sent. A job that stops
# with it is FAILED and can be resumed, with a new cap.
BUDGET_REACHED = JobFailure(JobFailureFamily.POLICY, "cost_budget_reached")


def retry_after_ms(error: ModelCallError) -> int | None:
    if error.retry_after_seconds is None:
        return None
    return min(MAX_RETRY_AFTER_MS, round(error.retry_after_seconds * 1000))


def _http_failure(error: ModelCallError) -> JobFailure:
    status = error.status_code
    if status in (401, 403):
        return JobFailure(JobFailureFamily.PROVIDER_PERMANENT, "provider_unauthorized")
    if status == 429:
        wait = retry_after_ms(error)
        diagnostics = () if wait is None else (("retry_after_ms", wait),)
        return JobFailure(JobFailureFamily.PROVIDER_TRANSIENT, "provider_rate_limited", True, diagnostics)
    if status is not None and status >= 500:
        return JobFailure(JobFailureFamily.PROVIDER_TRANSIENT, "provider_server_error", True)
    return JobFailure(JobFailureFamily.PROVIDER_PERMANENT, "provider_rejected")


def transport_failure(error: ModelCallError) -> tuple[JobFailure, DeliveryState]:
    """Classified from typed attributes only, as LibreTranslate's adapter does."""
    if error.reason is TransportReason.POLICY:
        return JobFailure(JobFailureFamily.POLICY, "provider_policy_refused"), DeliveryState.NOT_SENT
    if error.reason is TransportReason.HTTP:
        return _http_failure(error), DeliveryState.COMPLETED
    if error.reason is TransportReason.TIMEOUT:
        return JobFailure(JobFailureFamily.PROVIDER_TRANSIENT, "provider_timeout", True), DeliveryState.UNKNOWN
    if error.reason is TransportReason.CONNECTION:
        return JobFailure(JobFailureFamily.PROVIDER_TRANSIENT, "provider_unavailable", True), DeliveryState.UNKNOWN
    return JobFailure(JobFailureFamily.PROVIDER_TRANSIENT, "provider_request_failed", True), DeliveryState.UNKNOWN
