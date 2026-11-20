# Per-run HTTP policy (#159)

The closed vocabulary is `none`, `loopback` and, since #379, `remote` (see [Remote egress](#remote-egress-379)). `network.policy.resolve_network_policy`
resolves the selection in composition, without I/O, and produces an immutable `NetworkDecision`.
An absent selection records `defaulted=True` and uses `loopback`; unknown values
fail closed. No selection is persisted in preferences, projects or checkpoints.

`composition.cli` (#158) forwards input to preflight and
`composition.builders.providers`. Preflight creates a single `NetworkAuthorization`,
shared by the worker factory; each worker keeps its own HTTP session.
`CliProviderSession` derives the bound decisions required by the #128 use cases
from that same snapshot. Discovery and diagnostics continue using their existing DTOs,
operations and contracts. Final authorization is also in the translator,
including when consumed directly, without CLI. No contract imports Typer,
Rich or Desktop; there is no new Desktop entry point.

## Accepted and refused destinations

Only `http`. Accepted hosts are `localhost` and `localhost.` (case-insensitive),
canonical decimal IPv4 in `127.0.0.0/8`, and IPv6 `::1`, bracketed
in the URL (its full form is also accepted, normalized to `::1`).
Ports are decimal from 1 to 65535; omission means 80. Path prefixes and the
legacy URL ending in `/translate` remain accepted.

The following are refused before resolution: other names, localhost subdomains,
abbreviated/octal/hexadecimal/integer IPv4, IPv4-mapped IPv6, IPv6 zones,
userinfo, query, fragment, controls, spaces, backslashes, ambiguous authorities
and schemes other than HTTP. LAN, VPN, RFC1918/ULA, link-local,
metadata, multicast, unspecified, broadcast and public addresses receive no authority
from being reachable or apparently private. HTTPS and remote destinations exist only under
`remote`, below. There is no silent migration of existing URLs.

`NetworkAuthorization` resolves a localhost name once per run using an
injectable resolver. All results must have valid family/type/protocol,
port and loopback address, without zone/scope/flow. An empty list, mixed
or invalid response is refused. A literal address bypasses DNS. Resolution failure
is a connection failure, distinct from policy refusal, and does not cause another resolution.

`network.http.PinnedLoopbackAdapter` uses the existing Requests/urllib3 implementation,
with `HTTPConnection._new_conn` connecting directly to validated sockaddrs.
If the first address does not accept a connection, only the remaining addresses from the same
observation may be tried. Neither retries nor workers submit the name to a
second resolution. The canonical name remains in HTTP Host, without changing the socket
destination. The transport accepts only the two authorized endpoints.

## Guarantees and diagnostics

`none` refuses before resolution, socket/session creation, Requests dispatch,
redirect processing or translation payload construction/serialization. Cache-only and
dry-run keep their HTTP-free flows; they are not new isolation mechanisms.

All real sessions use `trust_env=False`; environment proxies, `.netrc` and
ambient credentials do not participate in sending. Explicit proxy and credentials
still have no contract and are not supported inputs. GET and POST use
`allow_redirects=False`; any 3xx is `provider_redirect_refused`, without a second
request, even to the same host. A session reaches only its provider's endpoints: GET `/languages` and
POST `/translate` for LibreTranslate, with existing timeout, retry and rate limit controls, and GET
`/api/tags` and POST `/api/chat` for [Ollama](../modules/providers-ollama.md).

Refusals expose operation, policy, class, reason and default/explicit origin,
without URL, book text, response, credential or `.netrc` material. The CLI
also presents the required action in common mode; bounded details stay in
developer mode. Connection and protocol errors retain their own categories.

## Remote egress (#379)

`remote` is explicit and never a default. A loopback URL under it is decided exactly as under
`loopback`, and `NetworkAuthorization` refuses anything but a loopback decision. An `https` URL is
allowed only to a host in the `ALLOWED_HOSTS` that one remote adapter declares (`host` for port 443, or
`host:port`); `resolve_network_policy(value, url, allowed_hosts)` reads that list only under `remote`. An
address literal passes only when it is itself declared; userinfo, query, fragment, escapes and plain
HTTP off the machine are refused. The decision carries the normalized list, so every later hop is
decided again against it.

`network.remote_http.RemoteHttpClient` is the only transport for such a decision. Its session sets
`trust_env=False` and `verify=True`, mounts a refusing adapter on `http://`, and its `https://` adapter
decides the prepared URL again right before the wire, refusing any proxy, client certificate or TLS
override (`proxy_refused`). Requests never follows a redirect itself: only 307 and 308 to a declared
host are followed, at most three times; any other 3xx is refused, an undeclared target is
`redirect_refused`, and credential headers go only to the host the run was authorized for. DNS is
resolved by the platform; the TLS certificate of the declared name authenticates the peer.

Composition (`composition.builders.remote`) decides the policy, reads the credential from the
adapter's `CREDENTIAL_ENV` as an opaque `providers.RemoteCredential`, and checks the project's consent
(`composition.builders.remote_consent`, stored by `projects.consent`) before the book is read. The
threat assessment is the [remote egress entry](../decisions/product-threat-model.md#remote-egress-379).

## External reader

The former `open_library_epub()` in `library.py` has already been replaced on main by
`LibraryService.open_entry` and `ReaderLauncher.launch`. This boundary remains an
independent process launch. The CLI warns before launch that the reader is
outside the offline guarantee, including with `none`. The result of opening
a book is not an HTTP isolation receipt, and the policy does not contain subprocesses,
the LibreTranslate server or its own recipients.

## Automated evidence

`tests/network/test_network_policy.py` instruments DNS, sockets, Session/adapter send, payload
construction and body preparation; it simulates the external socket while keeping real Requests,
urllib3, HTTP framing and parsing. It covers classification, zone, mixed responses,
rebinding, proxies, `.netrc` and redirects. `tests/network/test_network_policy_flows.py` traverses
composition, #128, preflight, workers, CLI, menus, preview, translation, batch, resume
and external reader. No test uses a real server or personal data. Integrations
requiring a trusted filesystem must run outside sandboxes that
reject temporary ancestors with `unsafe_path`.
