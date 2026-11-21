# Usage, cost and the cost cap

Ayvu counts what every translation request uses, prices it in US dollars and can stop a run before it
spends more than you allow. LibreTranslate and Ollama run on your computer and cost nothing; the cap and the
prices matter for paid model providers.

## What the report shows

After a translation or a resume, the terminal report and the Markdown report have a **Usage** section:

| Row | Meaning |
| --- | --- |
| Provider calls | Requests sent to the translator in this run |
| Characters sent | The characters of those requests |
| Tokens in / out / cached | What the model reported; `unknown` when it reported nothing, never a guessed zero |
| Cost (USD) | The cost of this run; `unknown` when the model has no known price |
| Cost per 1,000 words | The cost divided by the words of the text sent |
| Price table | The dated table that priced the run (`2026-11-20`), or `config.json` for your own price |
| Cost cap (USD) | With `--max-cost`: the cap, and what this run counted against it |
| Job total | On a resume: the calls and cost of the whole job, over the runs it continues |

LibreTranslate is not billed by tokens: it shows its characters, zero tokens and zero cost. Ollama shows the
tokens it reports, at zero cost. Each request is counted once, however many workers ran and however many
times the job was resumed; nothing is counted for text that came from the cache. Ayvu keeps only counts,
never text, in the book's project.

## Capping a run's cost

`--max-cost` goes before the command, like `--network-policy`, and applies to that one `translate` or
`resume`:

```bash
uv run ayvu --max-cost 2.50 translate book.epub --translator <provider>
uv run ayvu --max-cost 2.50 resume book.epub
```

Before each request Ayvu sets aside its worst case: the estimated input plus the most the model may answer
(its output allowance), at the model's price. A request whose worst case would pass the cap is not sent.
The job then ends as failed, publishes no EPUB and tells you how much it spent and what the cap was. What
was already translated stays in the cache, so `ayvu --max-cost <amount> resume book.epub` continues where
it stopped, with a new cap for that run.

Because the cap counts the worst case, a run may stop a little before reaching it. A model with no known
price cannot be capped: `--max-cost` is then refused before anything is sent. With a local provider the
price is zero and the cap never stops a run.

## Estimating before a run

`--dry-run` with Ollama or a remote provider estimates the run without sending anything: no request, no
consent, nothing leaves the computer.

```bash
uv run ayvu translate book.epub --translator ollama --dry-run
```

The report shows the estimated calls, tokens and cost, labelled **approximate**, and the **worst case**,
which is what `--max-cost` would set aside. The estimate assumes nothing is cached yet and counts a repeated
text once. Tokens are approximated from characters, so a real run usually costs somewhat less than the
estimate and never more than the worst case. A LibreTranslate dry run is unchanged and shows no estimate.

## Setting your own prices

The built-in table is dated and names its version in every report. To price a model yourself, add `prices`
to `config.json`, in US dollars per million tokens:

```json
{
  "prices": {
    "ollama": {"qwen2.5:7b": {"input": "0.10", "output": "0.40"}},
    "<provider>": {"*": {"input": "3", "output": "15", "cached_input": "0.30"}}
  }
}
```

`cached_input` is optional; `"*"` prices every model of a translator. A price you set wins over the table,
and the report says `config.json`. An invalid price stops the command with a message before any work.
