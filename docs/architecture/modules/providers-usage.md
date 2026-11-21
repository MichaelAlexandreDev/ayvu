# Usage, cost and the cost cap

Issue #380 (AI-06). What a provider request uses is counted once, priced in US dollars and capped per
invocation. The owner accepted the design on 2026-11-21 (Q1 to Q5 below).

## Modules

| Module | Owns |
| --- | --- |
| `providers/ports.py` | `ProviderUsage`: characters, `input_tokens`, `output_tokens`, `cached_input_tokens`; `None` is unknown, never zero |
| `providers/prices.py` | The dated price table (`PRICE_TABLE_VERSION`), `price_overrides`, `resolve_price`, `cost_micro_usd`, `micro_usd`, and the values `UsageRecord`, `UsageTotals`, `UsageEstimate` |
| `providers/llm/estimate.py` | The `TokenCounter` and `CostMeter` ports, `estimate_call` and `LlmUsageEstimator` |
| `providers/llm/core.py` | Fills each result's tokens from `ModelResponse` and reserves from the meter before every call |
| `preferences/prices.py` | The `prices` preference: validated through `providers.prices`, kept immutable, written back as strings |
| `projects/usage.py` | The `provider_usage` table (project schema version 6) and `SqliteProviderUsageRepository` |
| `translation/usage.py` | `RunMeter`, the invocation's cost cap and usage recorder, `UsageSummary` and `count_words` |
| `translation/document_bindings.py` | `_ContractProvider` hands each provider result to the recorder port (`UsageRecorder`) |
| `composition/builders/usage.py` | The price of the selected model, the meter, the binding that carries it and a dry run's estimate |
| `interfaces/cli/usage_report.py` | The "Usage" rows and section of the terminal and Markdown reports, and the line a stop at the cap prints |

## Prices (Q5)

- Money is US dollars. A price is dollars per million tokens, so one token at price `p` costs `p`
  micro-dollars; every cost is an integer number of micro-dollars, rounded up.
- `PRICE_TABLE_VERSION = "2026-11-20"`. The table prices LibreTranslate and Ollama, every model, at zero.
  Remote providers add their dated prices in their own issues (AI-07, AI-08).
- `prices.<translator>.<model>` in `config.json` overrides the table, with `input`, `output` and an optional
  `cached_input`; `"*"` prices every model of a translator. A report cites `config.json` for such a price.
- A model the table does not know has no price: its cost is unknown. With `--max-cost` it is refused before
  anything is prepared or sent (`price_unknown`).
- `input_tokens` excludes `cached_input_tokens`; each is billed at its own price. An adapter normalizes its
  vendor's counts to this meaning. LibreTranslate is not billed by tokens and reports zero tokens.

## Counting (Q1)

- The LLM core splits a call's reported tokens evenly over the units of that call, so the shares add up to
  what the model reported; a unit retried alone adds its own call.
- `_ContractProvider.translate` records every result through the `UsageRecorder` port, success or failure;
  a request never sent is not a call. A record holds counts only: characters, words (placeholders and bare
  punctuation are not words), tokens, cost, price version and source.
- One row per request in `provider_usage`, keyed by `request_id` (`ON CONFLICT DO NOTHING`), so a request is
  counted once whichever worker records it. The meter keeps the records in memory until the worker's document
  ends, however it ends (`rich_workers.flushing`), and writes them in one transaction, so the project database
  sees one usage write per document; the run's summary writes whatever is left. A resume that replays raw results sends nothing and records
  nothing. Provenance records are not changed; a row joins them through its job and document.
- Rows that cannot be written stop the job (`usage_record_failed`, family `resource`): the requests were
  made, and the job's usage would otherwise be wrong. When the document is already failing, that first
  failure is reported instead. A process killed in the middle of a document loses that document's rows.

## The cap (Q2, Q3)

- `--max-cost <USD>` is a root option, like `--network-policy`, so it applies to `translate` and `resume`.
  It is per invocation and never saved; the amount is rounded down to a micro-dollar.
- Before each model call the core reserves the worst case: the approximate input tokens of the rendered
  request plus its `max_output_tokens`, at the model's price. The reservation is decided under the meter's
  lock, shared by every worker. A worst case that fits what is really spent, but not with the calls in flight,
  waits for them to settle; one that cannot fit even then is refused.
- A refused call is not sent: `DeliveryState.NOT_SENT` and `JobFailure(POLICY, "cost_budget_reached")`. The
  job ends `FAILED`, publishes no EPUB and can be resumed (`recovery` treats every `FAILED` job as an
  interrupted one). The CLI prints what this run counted against the cap and the cap itself.
- After the call the reservation is settled with the reported counts. An unknown count keeps its reserved
  worst case; a call whose outcome is unknown (a timeout, a malformed answer, an HTTP error) keeps all of it.
- Residual risk: the input estimate is characters divided by `chars_per_token` (3). A model whose tokenizer
  counts more input tokens than that spends more than was reserved, by at most that difference on the calls
  in flight; the next reservation sees the real spend.
- With a local provider every price is zero, so the cap never stops a run.

## Dry-run estimate (Q4)

- A dry run with Ollama or a remote provider is an estimate made on this machine: zero requests, no consent,
  no network decision. Selection lets it through; previews and cache-only runs stay refused for both, and the
  preflights still refuse a dry run as a defence.
- It reads no cache, because the plan needs the model's identity: it estimates as if no text were cached,
  counting each distinct text once, as a run's own cache would.
- Each request is the chunk the stages would send, rendered with the run's prompt and glossary hints. Input
  is the rendered characters divided by `chars_per_token`; output is the answer's characters times
  `OUTPUT_TOKEN_FACTOR` (6/5), a declared factor; both are labelled approximate. The worst case is the full
  `max_output_tokens` of every request, which is what the cap reserves.
- `TokenCounter` is the port for a provider's own count. It may send text, so it is wired only where egress
  is consented; no implementation ships here (AI-07). With a counter the input is not labelled approximate.
- On a fake model with a subword tokenizer the estimate is within the stated ±25% of the real run
  (`tests/providers/llm/test_estimate.py`).
- A LibreTranslate dry run is unchanged: it costs nothing and shows no estimate.

## Reports

The terminal report and the Markdown report (section `## Usage`) show provider calls, characters sent,
tokens in, out and cached, the cost, the cost per 1,000 words, the price table and, with a cap, the cap and
what was counted against it. A resume also shows the job's total over the jobs it continues. A dry run
shows `## Usage estimate`: calls, approximate tokens and cost, the worst case and the price table.
