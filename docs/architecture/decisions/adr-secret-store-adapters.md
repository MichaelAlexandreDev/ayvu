# ADR: Native credential storage selection (DESKTOP-129)

Status: Proposed
Decision-Date: `2026-11-06`
Owners: Ayvu repository owner and maintainer (human acceptance)
Issue: [#160](https://github.com/DevStarrk1137/ayvu/issues/160).

Local evidence: 2026-11-06 (UTC).

Mandatory dependency: [threat model #129](product-threat-model.md),
`Accepted` on 2026-11-05, present in the `main` baseline
`79bcc6ec45d603bcab1d74da58021f1edf0657ca`.
The [desktop ADR](adr-desktop-stack.md) is also `Accepted`; it does not provide
an operating system or distribution support matrix.

## Proposed decision and validity limit

Platforms with proven support in this review: **none**.
The effective production allowlist remains empty. The recommendation is to qualify only
the Linux candidate below before proposing its admission. The sandbox refused the
D-Bus socket before any CRUD. Later, the reviewer ran the proof outside the
sandbox, in the authorized session, and observed real CRUD with confirmed cleanup in
the environment locked by `uv.lock` (see below). That is insufficient for support:
locked, denied/cancelled, service identity and packaging remain unproven,
and there is no license to promote a package installation to a storage guarantee.
This record and the research commands are a partial delivery of #160.

| Environment | Minimum version / architecture / distribution | Sole candidate backend | Support in this review |
| --- | --- | --- | --- |
| Linux GNOME/Wayland desktop, GNOME Keyring Secret Service | Only the Arch Linux rolling snapshot observed on 2026-11-06, x86_64, CPython 3.14.9, GNOME Shell 51.1, GNOME Keyring 51.1; no extrapolation to another version/distro | `keyring.backends.SecretService.Keyring`, distribution `keyring==25.7.0`, SecretStorage 3.5.0 | Pending: native failures (locked, denied/cancelled, missing collection), service identity and packaging. Real CRUD, unavailability and locked resolution observed by the reviewer |
| Linux KWallet or another Secret Service provider | No approved version/architecture | None | Unsupported |
| Linux headless, container, session without D-Bus, minimal system | No approved version/architecture | None | Unsupported |
| Windows | No approved version/architecture/package format | None | Unsupported; no native host |
| macOS | No approved version/architecture/signed identity | None | Unsupported; no native host |

There is no accepted generic minimum Linux version. The snapshot is a research
restriction, not a commitment to Arch rolling or a new CLI requirement.
The packaged target still needs to be identified by format, architecture,
artifact hash, resolution and execution identity before joining the matrix.

## Reproducible evidence and its provenance

The [isolated spike](../../../spikes/secret-store/README.md) contains a manifest,
executable proof and repeat commands. The
[sanitized output](../../../spikes/secret-store/results/sandbox-2026-11-06.json)
records timestamps, exit codes, installed inventory, licenses and metadata
hashes. The [observed native inventory](../../../spikes/secret-store/results/native-packages-2026-11-06.txt)
is package manager metadata, not a complete redistribution audit.
The [installed transitive closure](../../../spikes/secret-store/results/native-closure-2026-11-06.json)
records 190 packages, with declared versions/licenses, graphs and virtual provider
resolution; all 190 queries returned zero. It is not the file inventory
of a distributed executable.

| Proof executed | Observed result | What it establishes |
| --- | --- | --- |
| `native` | `access_denied`, no CRUD event, zero files in isolated HOME/config | Sandbox cannot reach the service; does not prove refusal by GNOME Keyring |
| `missing-bus` | `access_denied`, no CRUD event | Sandbox also prevents access to the missing socket; does not prove the socket error on a normal host |
| `missing-service` | `worker_failed`, exit 127 | Private D-Bus did not start in sandbox; does not prove missing service |
| Null, fail-only, chain, unexpected, class with spoofed name | `backend_rejected` | Identity policy rejects real Null/fail/chain objects and unexpected synthetic objects before operation |
| Backend and property override | `override_rejected` | Even the allowed candidate's name grants no authority through environment |
| `keyrings.alt` plaintext/encrypted | `dependency_missing` | Not installed; execution with these real classes remains pending |
| Offline optional resolution | Failed: `cffi==2.1.1` unavailable in the new isolated cache | No complete resolution/lock or reproducible installation proved |
| Installed host | `jaraco.context` has a development version different from the pin | System installation is not equivalent to the research manifest |
| Packaging | PyInstaller absent; offline resolution unavailable | No backend or dependency validated in a package |

The table above is execution inside the agent's sandbox. The reviewer repeated the
proof outside it, on 2026-11-06, in the owner-authorized GNOME session, with
`uv lock`/`uv sync --locked --no-build` in a temporary environment (network only for
that environment; root `pyproject.toml` and `uv.lock` intact). The
[reviewer's result](../../../spikes/secret-store/results/reviewer-native-2026-11-06.json)
records the hash of the [research `uv.lock`](../../../spikes/secret-store/uv.lock).

| Proof outside sandbox | Observed result | What it establishes |
| --- | --- | --- |
| `native` | `crud_pass`, create, read, rotate, delete events, `cleanup_verified`; zero isolated files | The candidate backend, with guards, creates, reads, rotates and deletes a canary entry in the unlocked default collection and confirms cleanup |
| `missing-bus` | `unavailable`, exit 1 | Missing socket becomes unavailability, without CRUD |
| `missing-service` | `missing_service`, exit 1 | Private bus without `org.freedesktop.secrets` is refused without service activation |
| Null, fail-only, chain, unexpected, spoofed name | `backend_rejected` | Same as sandbox |
| Overrides in the evaluated mapping and process entry | `override_rejected` | Same as sandbox; override at entry refuses before launching a worker |
| `keyrings.alt` plaintext/encrypted (5.0.2, separate environment of the `rejected` group) | `backend_rejected` | The real prohibited classes are rejected by identity |
| Resolution and installation | `uv.lock` with 13 packages; the 10 installed pins match the manifest (`matches_pin=true`) | Reproducible installation of the research set; does not approve redistribution |

With system Python, `missing-bus` returned `probe_failed` because the working
folder had a long path: the fake socket exceeded the 108-byte `AF_UNIX`
limit. Use a short `--work-root`. Locked, denied/cancelled and missing
collection remain **unobserved**: authorization prohibits provoking them in
the personal keyring.

No personal entry was read, enumerated, exported or changed.
The proof generates service `ayvu-test-<uuid>` and account `canary-<uuid>`, with two
random nonsecret values, compares them only in memory and does not print them. It only
searches attributes of that exact key after attempting to create it. Cleanup in
`finally` deletes all exact matches and checks absence without reading
values. Cleanup failure prevents success and requires investigation; forced
process interruption or service loss prevents promising completed cleanup.
These risks require supervision during proof outside the sandbox.

The installed implementation was inspected: `get_default_collection()` may
create a collection, and `get_preferred_collection()`/`unlock()` may unlock.
Therefore the research uses the exact keyring class with temporary guards:
preexisting default collection, no collection creation/lock/unlock/deletion,
no prompt and no service activation. The CRUD call still uses the real backend.
A successful proof must record `guarded_upstream=true`; it validates neither
the upstream path without these guards nor an Ayvu adapter.

## Official sources and refresh

No network in this session. The current inspection concerns installed package
metadata and code, not a claim about the latest public version. The links below
are primary sources for the connected review; availability, current content
and latest version **were not checked online**. Refreshing the
sources is a pending gate, not satisfied by third-party summaries.

| Official source | Required use in review |
| --- | --- |
| [keyring: documentation](https://keyring.readthedocs.io/en/latest/) | Backends, headless, disabling, configuration, security and packaging |
| [keyring: PyPI metadata](https://pypi.org/pypi/keyring/25.7.0/json), [upstream license](https://github.com/jaraco/keyring/blob/v25.7.0/LICENSE) | Confirm version, dependencies, wheel hashes and MIT license |
| [SecretService upstream](https://github.com/jaraco/keyring/blob/v25.7.0/keyring/backends/SecretService.py) | Exact identity and creation/unlocking effects |
| [SecretStorage](https://secretstorage.readthedocs.io/en/latest/), [code/license](https://github.com/mitya57/secretstorage) | Protocol, sessions, errors and dependencies |
| [Secret Service specification](https://specifications.freedesktop.org/secret-service-spec/latest/) | Locked, prompts, attributes, session and D-Bus identity |
| [GNOME Keyring](https://gitlab.gnome.org/GNOME/gnome-keyring), [GNOME libsecret](https://gnome.pages.gitlab.gnome.org/libsecret/) | Native service and its limits; libsecret is not a selected Python dependency |
| [Jeepney](https://gitlab.com/takluyver/jeepney), [cryptography licensing](https://cryptography.io/en/latest/faq/#what-license-is-cryptography-under) | Transport and cryptography dependencies |
| [cffi](https://github.com/python-cffi/cffi), [pycparser](https://github.com/eliben/pycparser) | Licenses, libraries and build |
| [jaraco.classes](https://github.com/jaraco/jaraco.classes), [jaraco.context](https://github.com/jaraco/jaraco.context), [jaraco.functools](https://github.com/jaraco/jaraco.functools), [more-itertools](https://github.com/more-itertools/more-itertools) | Licenses and auxiliary dependencies |
| [OpenSSL license](https://openssl-library.org/source/license/), [libffi](https://github.com/libffi/libffi), [D-Bus](https://dbus.freedesktop.org/doc/dbus-specification.html), [GLib](https://gitlab.gnome.org/GNOME/glib) | Native components and distribution conditions |
| [Windows Credential Manager](https://learn.microsoft.com/en-us/windows/win32/secauthn/credentials-management), [Apple Keychain](https://developer.apple.com/documentation/security/keychain-services) | Future evaluations, without granting support |

## Dependencies and distribution

The manifest is research-only, for CPython 3.14 x86_64 Linux. It does not change
root `pyproject.toml`/`uv.lock` or introduce a production extra. The pins below
are the candidate set. The reviewer generated a research `uv.lock` with wheel
hashes and performed a clean, locked installation with it; the license and
distributed artifact obligation audit remains mandatory before considering
the supply chain validated. Python 3.11 would add
`importlib_metadata`/`zipp` and `backports.tarfile`; it is not a target of this proposal.

| Package / candidate pin | Relationship | Observed license and obligation to check for redistribution |
| --- | --- | --- |
| keyring 25.7.0 | Direct | MIT confirmed in installed LICENSE: preserve notice and permission |
| SecretStorage 3.5.0 | keyring Linux | BSD-3-Clause: preserve notices, conditions and disclaimer, including binary documentation |
| jeepney 0.9.0 | keyring + SecretStorage | MIT: preserve notice and permission |
| cryptography 50.0.2 | SecretStorage | Apache-2.0 OR BSD-3-Clause; choose and comply with the applicable route, include third-party and bundled library licenses/notices |
| cffi 2.1.1 | cryptography CPython | MIT-0 metadata; installed text included in evidence; audit libffi separately |
| pycparser 3.12 | cffi | BSD-3-Clause: preserve notices and conditions |
| jaraco.classes 3.4.0 | keyring | MIT confirmed in installed LICENSE |
| jaraco.context 6.1.2 | keyring | MIT in installed **6.1.3.dev0+g098f39c91.d20260403**; pin license/artifact not yet checked |
| jaraco.functools 4.1.0 | keyring | MIT confirmed in installed LICENSE |
| more-itertools 11.2.1 | jaraco.classes/functools | MIT in installed package |

Installed texts and hashes are evidence of local origin. They do not prove upstream
wheel hashes. The inventory includes cryptography's LICENSE, LICENSE.APACHE and LICENSE.BSD,
but does not complete the notices for all native components actually
bundled in a wheel/package: the notice chain remains incomplete.
No legal or redistribution approval follows from isolated SPDX fields.

Initial native components observed: GNOME Keyring `1:51.1-1`, GLib
`2.90.1-1`, D-Bus `1.16.2-2`, dbus-broker `37-3`, OpenSSL `3.6.5-1`, libffi
`3.8.0-1`, glibc `2.44+r50+g1848099f063e-1`, Python `3.14.9-1`.
The transitive inventory completes the metadata of installed mandatory dependencies
of `gnome-keyring`, `python-keyring`, `python-cryptography`, `python`
and `dbus-broker`, including LGPL, GPL and permissive license families. It includes neither
optional dependencies nor proof that all are loaded or will be
redistributed. **It does not complete all native license texts/obligations
or an Ayvu package inventory**.

Proposed strategy: system-provided credential service and bus;
Ayvu does not install, package, configure or unlock them. SecretStorage/Jeepney
use D-Bus without requiring dbus-python or libsecret on this path. Cryptography
wheels may incorporate OpenSSL and other Rust/C components; the distro package
and the wheel are not interchangeable evidence. The future package needs an inventory
of actually included files/DSOs, versions, sources and hashes,
license/NOTICE texts and source/relink obligations according to the applicable
license. Using a system service does not eliminate obligations for binaries that
Ayvu redistributes. Without this completion, distribution remains blocked.

## Closed allowlist and future selection

These are proposed rules, not production code:

1. First validate packaged target, OS/snapshot, architecture, dependency set
   and artifact integrity against approved evidence. Currently
   no target passes this gate. Replaced Python code or a compromised OS
   are in T10's trusted base; name comparison is not isolation.
2. Refuse runtime overrides `PYTHON_KEYRING_BACKEND`, any
   `PYTHON_KEYRING*`, `KEYRING_*` (including `KEYRING_PROPERTY_*`), plugin/configuration
   paths and loader injections. Do not use `get_keyring()`, discovery,
   priority or a configuration file as authority. Environment/config never
   receives the credential value.
3. After human approval and evidence, directly import the registered class
   from the verified artifact and require `type(active) is approved_class`. Check
   distribution/version and fixed properties (collection/schema/application), not
   just `__module__`/`__name__`. Instantiate without overrides. The isolated class
   test does not detect monkeypatching or a compromised dependency.
4. Check the current owner of `org.freedesktop.secrets`, approved GNOME Keyring
   implementation and accessible preexisting collection, without autoactivation/unlocking.
   The Secret Service protocol and reported availability do not identify the
   provider; the spike **does not attest** the owner. This check still needs to be
   defined and proved in the package before admitting the target.
5. Require a real operation with the application's own canary, without enumerating
   others' entries, and clean up. Revalidate identity/state on new connections/operations;
   an initial check does not resolve a lock/service replacement race.
6. Any failure ends the operation without fallback. Never choose
   `keyring.backends.null.Keyring`, fail-only backend, `keyrings.alt` (including
   `PlaintextKeyring` and encrypted-file), Ayvu-managed plaintext/encrypted files,
   environment/config, unexpected class, unapproved subclass or
   chain. This design rejects every chain; only a new ADR with identification and
   validation of **each** reachable backend could admit one.

T03 requires future binding of the credential to the authorized destination; T04 prevents
values, paths and upstream messages in diagnostics; T10 requires provenance;
T11 prevents promising secure erasure of OS backups. Secret Service must not
be advertised as isolation between applications of the same user. Labels
and attributes must not contain credentials, titles, URLs or personal paths.

## Desktop, failures and diagnostic limits

The candidate assumes an interactive GNOME Wayland session, accessible session bus,
already active GNOME Keyring service and preexisting unlocked default collection.
A service is not accepted merely because it is activatable. Absence of `DISPLAY` proves
neither headless operation nor working prompts; native endpoint and state govern.
No session, no collection, inaccessible bus, minimal installation, container or
headless: no support. Do not recommend a privileged container.

| Distinct future result | Action communicable to the user, without upstream error |
| --- | --- |
| Unsupported target / missing dependency | Inform that persisted credentials are unavailable in this installation; use a subsequently qualified target |
| Unexpected backend / override | Refuse; advise removing keyring override/configuration and restarting with the official package |
| Unavailable bus/service/collection | Inform that credential service is unavailable; check the desktop session with OS tools; Ayvu does not provision a service or collection |
| Locked collection/item | Inform of lock; user decides whether to unlock with OS tools and retry; Ayvu does not unlock |
| Denied/cancelled | Inform of denied/cancelled access; respect refusal, without automatic retry or backend switch |
| Unexpected error / timeout / unconfirmed cleanup | Stop and report sanitized code; never declare success or confirmed erasure |

`access_denied` observed in sandbox is a socket access refusal; locked and
denied/cancelled from the real service remain **unobserved**. Fakes exercise
guards and cleanup; they do not satisfy these proofs. Authorization for this machine
prohibits provoking lock/unlock, creating/deleting collections or changing configuration.
To qualify such states, use an authorized disposable session/VM where the
state is already prepared by its operator, without touching the personal keyring.
Until then support remains absent; do not omit these cases from the matrix.

For Windows/macOS, neither development Python nor upstream support proves
Ayvu package isolation. Future research needs OS name/version/architecture,
executable format and signed identity, Credential Manager/Keychain access
rules, updates and real prompts, plus a specific inventory.
No Linux result is inherited by these systems.

## Mandatory CI gate and human acceptance

Each packaged target intended for support must execute, without skips:

- clean resolution of the optional set, lock/hashes, transitive Python licenses
  and actual native components, with notices/source/relink where applicable;
- backend and service identity in the distributed executable, including
  modules/metadata missing or changed by the packager;
- create, read, rotate, delete and absence after cleanup in the real native store,
  under an exclusive namespace, without reading/listing personal entries;
- locked, denied/cancelled, missing-bus, missing-service and missing collection in an
  authorized disposable environment; no auto-unlock/auto-create/fallback;
- Null, fail-only, real `keyrings.alt` plaintext/encrypted classes, environment,
  properties, unexpected class/spoofed name and unapproved chain;
  confirm specific refusal, zero write calls and zero file/config;
- behavior on package/identity update, reconnection, cleanup after
  error and redacted failures. Archive UTC timestamps, OS, package/hash, versions,
  codes and sanitized results.

A runner without a secure session stays outside the supported matrix. Skips, mocks, Null,
memory and evidence from another target do not satisfy this gate. Unit tests
in this PR protect research tools; they do not turn the gate green.

The maintainer must explicitly record human acceptance, dated and with the
matrix/evidence and accepted risks, before changing `Proposed`. Output attached
by the reviewer needs source/license review and all cases above;
a successful CRUD alone is insufficient. A later issue will implement the port,
`SecretRef`, adapter and composition only after this acceptance. This issue does not
authorize those types, UI, authentication, OAuth, migration, cloud vaults or a
production dependency.

## Alternatives and remaining risks

- Install keyring and accept its automatic selection: rejected, because environment,
  configuration, priority and chains may change the backend.
- KWallet: deferred; without specific session/proof/inventory. Linux or protocol
  compatibility does not authorize replacing GNOME Keyring.
- Windows and macOS: deferred for lack of real hosts/packages, without inference
  from upstream documentation.
- Encrypted file, plaintext, environment, Null or fail as fallback:
  rejected; they would change threat and retention without authorization.
- Continue without credential persistence: current behavior preserved and
  operational recommendation until evidence and acceptance are complete.

Open risks: service integrity/identity under the same user, state races,
risk of the observed development package, cleanup limits on session loss,
current public version not consulted, incomplete native transitive licenses
and unmeasured packaging effects. These risks are not
implicitly accepted by the existence of this ADR.
