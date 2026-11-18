# Translating with Ollama

[Ollama](https://ollama.com) serves open-weight language models on your own machine. Ayvu can translate a
book with a model you installed there, through the rich runtime: durable jobs, provenance and the v2 cache.
Ollama listens on loopback (`http://localhost:11434`), so it works under the default network policy and
the book never leaves your computer.

## Setup

1. Install Ollama and start it (`ollama serve`, or the desktop app).
2. Download a model. Ayvu never downloads one for you:

   ```bash
   ollama pull qwen2.5:7b
   ollama list
   ```

3. Name the model in the configuration file (`$XDG_CONFIG_HOME/ayvu/config.json`, or
   `~/.config/ayvu/config.json`), keeping its other fields:

   ```json
   {
     "translator_models": {
       "ollama": "qwen2.5:7b"
     }
   }
   ```

   The value is the name exactly as `ollama list` shows it: letters, digits, `.`, `_`, `-`, `:` and `/`,
   up to 128 characters. A LibreTranslate label can sit next to it under `libretranslate`.

4. Check that Ayvu reaches the model. This sends only the text `Hello world`:

   ```bash
   uv run ayvu test-translator --translator ollama
   uv run ayvu test-translator --translator ollama --model llama3.2:3b   # another installed model
   ```

## Translating

```bash
uv run ayvu translate book.epub --translator ollama --model qwen2.5:7b --target pt
```

- **Address:** with `--translator ollama`, the default `--url` means `http://localhost:11434`. Give
  `--url http://127.0.0.1:11500` for another port. Only loopback HTTP is accepted, as for LibreTranslate,
  and `--network-policy none` refuses the run before any socket is opened.
- **Before any book work,** Ayvu lists the installed models (`GET /api/tags`) and refuses the run when the
  configured model is missing, with the `ollama pull` command to run. Nothing is created or sent.
- **Timeouts:** a local model can be slow. Raise `--timeout` (seconds per request) for long chapters on a
  CPU. A timed-out request is not retried, because the server may still be working on it; `--retries`
  covers connection failures and server errors only.
- **Workers:** Ollama runs with one worker. `--workers 2` or more is refused before anything starts, since
  Ollama's own `OLLAMA_NUM_PARALLEL` limits how many requests the server answers at once.

`--model` overrides the selected profile, then `translator_models.ollama`. A profile may contain
`"translator": "ollama", "model": "qwen2.5:7b"`; use it with `--profile <name>`. Without an explicit
Ollama selection or profile, LibreTranslate remains the default.

In Settings (option 4), choose **6. Choose translator and model**, then `ollama`. Ayvu lists installed
models through `GET /api/tags`, under the invocation's network policy. Choose a number or enter a valid
model name. Discovery failure permits manual entry; Enter cancels without changing configuration.
Translation checks that the model is installed even when it was entered manually.

A durable resume reads its original provider and model from job provenance, regardless of current
configuration. If the model's weights changed, resume refuses; restore the original weights or start a
new translation. Plans and terminal/Markdown reports name the provider and model, with no endpoint or
credentials.

## Languages

A model has no fixed language list, so Ayvu declares one for Ollama: Arabic, Chinese, Dutch, English,
French, German, Hindi, Italian, Japanese, Korean, Polish, Portuguese, Russian, Spanish, Swedish, Turkish
and Ukrainian. `ayvu languages --translator ollama` shows it once the server answers. A pair outside the
list is refused before the run starts. How well a pair translates depends on the model you choose.

## Model identity and the cache

The producer of every translation is recorded as `ollama` with the model `<name>@<digest>`, where the
digest is the sha256 Ollama reports for the installed model. When `ollama pull` brings new weights under
the same name, the digest changes, so the next run translates again instead of reusing results of the old
weights; going back to the old weights reuses their results. The prompt, its revision, the batch policy and
the decoding settings are part of the cache identity too.

## Privacy and cost

The book's text goes only to the Ollama server at the address you give, on loopback. Ayvu sends no
credential and Ollama needs none. There is no per-token cost; Ollama reports token counts, which Ayvu
does not record yet.

## Not available yet

- `--dry-run`, `--preview` and `--cache-only` with `--translator ollama` are refused with an explanation.
- Remote providers (Anthropic, OpenAI) need an explicit egress policy and are separate issues.
