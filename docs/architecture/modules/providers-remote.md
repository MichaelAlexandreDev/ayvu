# Remote providers and consented egress

The contract a remote model provider follows, from
[#379](https://github.com/MichaelAlexandreDev/ayvu/issues/379). It sits on the [LLM core](providers.md#llm-core)
like [Ollama](providers-ollama.md), under the `remote` policy of the
[HTTP policy contract](../cross-cutting/network-policy.md#remote-egress-379). Its threat assessment is the
[remote egress entry](../decisions/product-threat-model.md#remote-egress-379); the user-facing page is the
[network policy guide](../../guide/network-policy.md). No remote provider ships yet: Anthropic (AI-07) and
OpenAI (AI-08) will be the first, and only declare their hosts, retention claim and credential variable.

## Modules

| Module | Layer | Responsibility |
| --- | --- | --- |
| `ayvu.network.policy` | Domain | `NetworkPolicy.REMOTE`: HTTPS to declared hosts only, loopback still allowed, never a default |
| `ayvu.network.remote_http` | Provider | `RemoteHttpClient`: `trust_env=False`, verified TLS, every hop decided again, only 307/308 to a declared host, credential headers only to the authorized host, a shared rate limiter, retries of connection failures and 5xx within the configured delays (never a timeout or a 429), typed `RemoteTransportError` |
| `ayvu.providers.remote` | Domain | `RemoteModelAdapter` (what an adapter declares), the opaque `RemoteCredential`, `credential_from_environment` and `check_remote_adapter` |
| `ayvu.projects.consent` | Infrastructure | Project schema version 5: `remote_egress_consents` and `SqliteRemoteConsentRepository` (current, grant, revoke) |
| `ayvu.composition.builders.remote` | Composition | `REMOTE_ADAPTERS` (empty in this release), `egress_notice` and the preflight that keeps book text on the machine until every check passes |
| `ayvu.composition.builders.remote_consent` | Composition | The project's `ConsentGate`, its scope during model preparation, and `revoke_remote_consent` |
| `ayvu.interfaces.cli.commands.consent` | Interface | `ayvu consent revoke <epub or project id> [--translator NAME]` |

`composition.builders.rich_provider` treats a registered remote adapter as a model translator
(`IdentitySource.REMOTE_MODEL`) and prepares it inside the book's consent scope; `composition.cli` exposes
`remote_egress_notice` and `revoke_remote_consent` as named factories. The CLI's root `--consent-remote`
answers for developer mode; the guided and common modes ask through `run_support._remote_consent`.

## What an adapter declares

- `ADAPTER_ID`: the translator name and the producer's provider.
- `BASE_URL` and `ALLOWED_HOSTS`: `https://` only; `host` (port 443) or `host:port` entries. A host that
  is not declared is unreachable, even through a redirect.
- `RETENTION_CLAIM`: the provider's published terms in one line, shown in the consent question, the plan
  and the report. It is a claim Ayvu repeats, not a guarantee it enforces.
- `CREDENTIAL_ENV`: the provider's standard environment variable. No provider SDK is used, because an SDK
  brings its own HTTP client; all HTTP goes through `ayvu.network`.
- `DECLARED_CODES`, `producer(model)`, `snapshot(model, now, timeout_seconds=...)` and
  `model(client, credential, model)`, which returns the `ModelPort` the core calls.

## Preflight order

Every refusal happens before the book is read and before any request is sent:

1. previews, cache-only runs and more than one worker are refused (also at selection); a dry run is an
   estimate made on this machine that sends nothing and never reaches this preflight ([usage](providers-usage.md));
2. the network decision must be `remote` and land on a declared host;
3. the credential is read now, from `CREDENTIAL_ENV`, and kept as an opaque `RemoteCredential`;
4. the project's consent to the current text version (`remote-egress-v1`) is checked or asked once;
   without a project there is no gate and the run is refused;
5. the book and the cache are checked; only then is the `LlmBinding` built, one client per worker.

## Privacy and persistence

- The credential never reaches configuration, checkpoints, the cache, provenance, reports, logs, the
  consent record or output; `RemoteCredential` does not print or serialize.
- The consent row holds the project id, the provider, the UTC time and the text version, nothing else.
  A new text version is asked again; `consent revoke` deletes the rows.
- Producer identity and cache identity follow the [LLM core](providers.md#llm-core): the provider and
  its versioned model id are part of the producer, so a remote result never reuses a local one.
- Resuming a remote job is not supported yet; it is refused like any unknown original model.

## Enforced contracts

`tests/network/test_network_policy_remote.py`, `tests/network/test_remote_http.py`,
`tests/providers/test_remote.py`, `tests/projects/test_consent.py`, `tests/projects/test_project_schema.py`,
`tests/composition/test_remote_preflight.py` and `tests/interfaces/cli/test_cli_remote_consent.py`, with
the fake adapter and instrumented wire of `tests/support/remote_fakes.py`.
