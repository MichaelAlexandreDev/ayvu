# HTTP network policy

Use `--network-policy none|loopback` to choose the HTTP policy of a run. Without the option, the visible
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

Both policies ignore proxies and credentials from the environment and `.netrc`, refuse credentials in the
URL, and refuse any redirect, even a local one. HTTPS, proxies, explicit credentials and non-loopback
destinations depend on future contracts.

The EPUB reader the library opens is **another process, outside the offline guarantee**, and you get an
explicit notice before it opens. `none` does not contain that reader, and a local server may also have its
own network access and retention. See the [HTTP policy contract](../architecture/cross-cutting/network-policy.md).
