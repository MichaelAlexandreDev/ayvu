# HTTP network policy

Use `--network-policy none|loopback|remote` to choose the HTTP policy of a run. Without the option, the visible
default is `loopback`: **HTTP** only, to `localhost`, `localhost.`, a decimal IPv4 address in
`127.0.0.0/8` or IPv6 `[::1]` (also in its full form), on port 1–65535, or 80 when omitted. Other names,
LAN, VPN, private or public addresses and HTTPS are refused; an old URL like that must be replaced
explicitly with a loopback HTTP endpoint. There is no automatic migration.

```bash
uv run ayvu languages --network-policy loopback --url http://127.0.0.1:5000
uv run ayvu translate book.epub --network-policy none --cache-only
uv run ayvu --network-policy none --preview book.epub
uv run ayvu resume --network-policy loopback
uv run ayvu --network-policy none
```

`none` blocks Ayvu's HTTP **before DNS, sockets, session creation and payload construction**. Online
translation, listing languages, testing the translator and the preflight are therefore refused, while
`--dry-run` and `--cache-only` stay available. The root option applies to previews and menus, including
the guided language choice and the offered resume; a subcommand's own option wins for that request. The
policy is never saved.

`none` and `loopback` ignore proxies and credentials from the environment and `.netrc`, refuse credentials
in the URL, and refuse any redirect, even a local one.

## Remote providers (`remote`)

`remote` is never the default. It keeps everything `loopback` allows, so LibreTranslate and Ollama work
exactly as before, and adds **HTTPS to the hosts one remote provider declares in its code**, and nothing
else: no other host, no plain HTTP off this machine, no credentials, query or fragment in the URL, no
address literal the provider did not declare. Environment proxies, `.netrc` and CA overrides are always
ignored. A redirect is followed only when it keeps the method (307 or 308) and its target passes the same
check; the API key is sent only to the host the run started with. No remote provider ships yet: Anthropic
and OpenAI come in later issues.

What leaves the computer: **the book's visible text and your glossary terms**, sent to that provider.
File names, folders and settings are not sent. Before the first remote translation of a project, Ayvu
says this in plain words, with the provider's own retention claim, and asks once; "no" stops with exit
code 1 and nothing sent. The answer is recorded in the project (provider, UTC time and the version of
that text). In developer mode there is no question: pass `--consent-remote`, or the run is refused. The
plan and the report show where the text goes and the retention claim.

```bash
export PROVIDER_API_KEY=...   # read from the provider's standard variable at run time, never saved
uv run ayvu --consent-remote translate book.epub --translator <provider> --network-policy remote
uv run ayvu consent revoke book.epub          # or a project id; the next remote run asks again
```

A remote provider translates only inside a project: previews, dry runs, cache-only runs and legacy
resumes are refused, and it runs with one worker.

The EPUB reader the library opens is **another process, outside the offline guarantee**, and you get an
explicit notice before it opens. `none` does not contain that reader, and a local server may also have its
own network access and retention. See the [HTTP policy contract](../architecture/cross-cutting/network-policy.md).
