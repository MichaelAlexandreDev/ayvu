# Performance and request control

## Performance profiles

`translate` accepts `--performance-profile` to apply an execution preset:

```bash
uv run ayvu translate book.epub --target pt --performance-profile low
uv run ayvu translate book.epub --target pt --performance-profile high
```

| Profile | Recommended for | Settings |
| --- | --- | --- |
| `low` | Weak machines or an unstable server | `workers=1`, `requests_per_second=1`, `chunk_limit=1500`, more patient retries |
| `standard` | Safe default | `workers=1`, no rate limit, `chunk_limit=3000`, default retries |
| `high` | A stable machine and server | `workers=4`, no rate limit, `chunk_limit=4000`, default retries |
| `custom` | Manual tuning through flags | Starts from `standard` and applies the overrides you pass |

`standard` is the default when `--performance-profile` is not given. Stronger settings, such as `high`,
are always opt-in. Explicit flags still win over the profile: for example,
`--performance-profile high --workers 2` uses the rest of `high` but limits the workers to 2.

`high` is currently incompatible with `--translation-memory`, which needs `--workers 1` in this version.

## Request control

By default, Ayvu does not limit the request rate to the local translator. For weaker or unstable servers,
use `--requests-per-second` to limit how many HTTP calls may start per second:

```bash
uv run ayvu translate book.epub \
  --target pt \
  --url http://localhost:5000 \
  --requests-per-second 2
```

`--retries` controls retries. Connection failures, timeouts, HTTP `429` and HTTP `5xx` can be retried. The
delay between attempts uses exponential backoff: it starts at `--retry-backoff` seconds (`0.5` by
default) and is capped by `--retry-backoff-max` (`8.0` by default).

```bash
uv run ayvu translate book.epub \
  --target pt \
  --retries 4 \
  --retry-backoff 1 \
  --retry-backoff-max 10
```

`test-translator` sends the text `Hello world` to the server, and only that text; no other command sends
content to test the translator. In developer mode, errors from `test-translator`, `languages` and the
initial check show a code, such as `provider_timeout`, instead of the server's message, which could
contain the URL or part of the response.

The same options exist in `test-translator` and `languages`, so you can test the server with the controls
the translation will use.

## Parallel workers

To process several of the EPUB's internal documents in parallel, use `--workers`. The default,
`--workers 1`, keeps the conservative sequential run. With more than one worker:

- each worker has its own translator and its own SQLite connection;
- results are applied in the chapters' original order, so the EPUB, the report and the review CSV keep
  the book's order;
- a `--requests-per-second` limit is shared by all the workers.

The translation plan shows `Workers`, `Rate limit` and `Retry policy` before starting. In a parallel run,
the chapter progress bar also says that progress is reported in EPUB order.

```bash
uv run ayvu translate book.epub \
  --target pt \
  --workers 2 \
  --requests-per-second 2
```

With a model label, the rich runtime also runs online translation and `--cache-only` with several
workers; see [Model label, rich runtime and v2 cache](rich-runtime.md).
