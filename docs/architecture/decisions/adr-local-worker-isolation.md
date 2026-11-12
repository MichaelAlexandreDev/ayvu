# ADR: Lifecycle and IPC of trusted local workers — DESKTOP-136

Status: Accepted
Decision-Date: `2026-11-06`
Owners: Project maintainer (acceptance)
Issue: [#167](https://github.com/DevStarrk1137/ayvu/issues/167).

Accepted on 2026-11-10 by the maintainer; see the [acceptance record](#acceptance-record).
Acceptance does not enable a production runner.

## Gate and preserved behavior

The gate was checked on local `main`, HEAD `3e667fb`, without network access. The five
artifacts are versioned in that commit:

| Dependency | Artifact and required fact |
| --- | --- |
| #124 | [Desktop stack](adr-desktop-stack.md#acceptance-record): `Accepted`, maintainer approval on 2026-11-05; PySide6/Qt Widgets, Linux x86_64 as the first evaluation/product target; Windows/macOS deferred until native evidence |
| #126 | [Events and cancellation](../modules/jobs.md): schema 1, single producer, outcome, failures and safe points |
| #129 | [Threat model](../product-threat-model.md): `Accepted`, approval on 2026-11-05; a subprocess is not a sandbox |
| #137 | [Artifacts](job-artifacts.md): `Accepted`, staging/ready/abandoned, lease and reconciliation |
| #159 | [HTTP policy](../network-policy.md): `none`/`loopback`, denial before I/O and no ambient authority |

#124 accepts the toolkit while retaining distribution/accessibility gates. No
Desktop support has been delivered. The conservative interpretation for this matrix
is to measure Linux x86_64, without promoting Windows/macOS to accepted targets. Older
passages in #124 still say acceptance is pending; its explicit record takes precedence.
This research does not change platforms or resolve release gates.

Parallelism in `formats/epub/io.py` with `ThreadPoolExecutor`, CLI callbacks and v1
resume remains intact. `main` already has job and artifact contracts later than the
issue's original context; their existence is neither a process supervisor nor
proof of containment. CLI `--help` was run with temporary home/config/cwd:
exit code 0, 3973 bytes of stdout and no stderr.

## Reversible recommendation and initial trust

Propose `subprocess.Popen` with a fixed package entry point and an inherited anonymous
`AF_UNIX socketpair` for IPC, one worker per run and its own process group.
Both stdout/stderr channels stay separate from the protocol. The choice prioritizes
this separation and the absence of a named endpoint; the similar timings do not prove
performance superiority. Do not reuse processes between jobs in v1.

**Only reviewed, allowlisted in-tree commands are trusted.** The caller
selects an identifier and a closed schema; it supplies no executable, module,
code, unrestricted argv or shell. Plugins, dependencies of arbitrary origin, executable
models and external commands require another decision. A compromised parser
retains the user's authority. This recommendation provides process separation,
without promising a security sandbox.

The minimum launch requirements are mandatory for both alternatives: an executable resolved
from the trusted installation, a fixed entry point, `shell=False`, an environment replaced by
a minimal allowlist, an exclusive temporary `0700` cwd, `close_fds=True`, only required IPC and
streams inherited, time limits and group cleanup. In the spike, Python
uses `-I -S`, and the environment contains only `LANG=C` and `LC_ALL=C`; neither PATH, HOME,
PYTHONPATH, proxies nor parent variables are copied. A real package will need to
validate its installed bootstrap; `-S` is not a recipe for importing Ayvu.
Any mechanism that fails to meet this minimum is refused for production.

There is no credential field or `SecretRef` resolution. Credentials in
argv, environment, temporary files, messages or diagnostics are not authorized.
A real, accepted and implemented secret reference adapter is a gate for
any future capability that needs it; the proposed #160 decision does not
satisfy it. Canaries in this research are random and synthetic, with no credentials.

## Comparison under the same criteria

| Criterion | Popen + stdin/stdout with framed JSON | Popen + socketpair with framed JSON (proposed) |
| --- | --- | --- |
| Fresh startup, 7 samples | Median 24.910 ms | Median 27.204 ms |
| Packaging | stdlib; reexec of the fixed entry point needs a bundle smoke test | stdlib; same bundle gate, plus explicit fd passing |
| Portability | Pipes exist on all three OSes, but this POSIX loop with select was run only on Linux | Inherited AF_UNIX measured only on Linux; no Windows/macOS promise |
| Framing | uint32 big-endian prefix + canonical JSON | Same framing and codec |
| Backpressure | Native pipe blocks producer; application queue with credit | Native socket blocks producer; same queue with credit |
| Accidental stdout | Corrupts the protocol; must fail closed | Drained/discarded outside IPC; independent stderr limit |
| Cancellation | Message through stdin; no magic interruption of a blocked call | Message through socket; same limitation |
| Crash/EOF | EOF or deadline; exit code necessary, insufficient for success | Same; stdout does not hide IPC EOF |
| Cleanup | POSIX group and bounded wait | Same mechanism; close both ends created by the parent |
| Parent died | Cooperative EOF reader kills its own group | Same; parent writer must not leak to descendants |
| Peer/endpoint | Private OS-created descriptors, no listener | Private fd capability, pair created atomically, no listener/address on disk |
| Synthetic throughput, 128 frames | 15410.1 frames/s; 8.306 ms | 16773.9 frames/s; 7.631 ms |

Pipes remain a viable, reversible alternative if descriptor packaging
fails. There is no silent transport change: switching requires repeating the matrix.
`multiprocessing` with spawn is also compatible with Python, but its usual object
channels and queues serialize objects; they are refused in this design.
A variant limited to `send_bytes` with its own codec would require the same
framing controls and another packaging bootstrap, with no demonstrated benefit.
Do not use `Connection.recv`, `Queue`, `pickle`, `eval`, object hooks, decoder
class imports or payload exec. Threads are neither an isolation candidate
nor a replacement for the existing path. A local TCP endpoint would add an unnecessary
listener, authentication and port contention; it was not selected.

## Envelope and deterministic rejection

The external protocol is independent of the canonical #126 event schema.
The form below describes the envelope, without user IDs, content or paths:

```text
uint32_be comprimento | JSON ASCII canônico
{v:1, run:UUID, job:UUID, request:UUID, seq:inteiro, kind:tipo, body:objeto fechado}
```

`run` identifies a single attempt; `job` is the durable opaque reference, and
`request` correlates command/response and the operation. All are canonical UUIDs,
without path or authorization meaning. `seq` is per direction, starts at 1 and
increases by exactly one, below 2^31. It is not the `JobEvent` sequence.
A run/job/request change is `invalid_peer`; a transport gap/replay/regression
is `invalid_sequence`. Rejections do not advance the sequence. Durable replays still
obey `JobEventSequence`, including identical duplicates, without redefining it.

| Type | Proposed contract |
| --- | --- |
| `hello` | First in each direction. Parent offers `versions:[1]`, `event_schema:1`; child confirms `version:1`, `event_schema:1`. No intersection, unknown version, change during execution or repeated hello: refuse, without downgrade or command |
| `command` | One per attempt. Allowlisted type/version, opaque references to the job and authorized artifacts and optional UTC deadline from the operation contract. No path, text, URL, object codec or new policy |
| `event` | Progress/warning/ack data according to #126; no free strings. Supervisor validates DTOs and remains the durable stream's single producer, assigning IDs, timestamps, correlation and canonical sequence |
| `cancel` | `CancellationRequest`: closed reason code and UTC requested_at; idempotent, forwarded to `CancellationSource`. Ack only at the declared safe point; request does not mean outcome |
| `result` | One response per request: candidate `JobOutcome` according to #126 and up to 16 UUID references to already reserved artifacts; no payload or location. Supervisor validates lease, identity, integrity, state and publication before deciding any success |

There is no job runner in the spike: `event.body.event` carries canonical #126
fixtures and `result` uses `artifacts:[]`. The test decodes the fixtures with the real
decoder and ordering logic, including cancelled request/ack/outcome. The future implementation
will have the supervisor as single producer; do not copy the spike's synthetic producer.
The numeric `pids` fields and authority flags are **probe-only** extensions,
not production fields or persistable diagnostics.

Reading checks the prefix before reserving a body. Standard JSON, without hooks,
passes through a lexical depth limit before parsing and canonical re-encoding
with byte-for-byte equality. This rejects duplicates, alternative whitespace, noncanonical
UTF-8, extra/missing fields, NaN/Infinity and executable forms. UUIDs,
exact types (bool is not an integer), schemas and direction are validated before using
a payload. The production validator will reuse the existing contracts for the
body; it will not replace `decode_event` or its ordering rules.

| Deterministic budget for the research/initial proposal | Limit |
| --- | --- |
| Frame body / header | 32768 bytes / 4 bytes |
| Internal canonical event | 16384 bytes, according to #126 |
| JSON depth | 8 levels before parsing |
| Application queue | 8 frames (262144 raw bytes), plus one frame being read and one being written; Python objects have additional overhead |
| Simultaneous jobs/commands per channel | 1; cancellation control needs a reader independent of the work |
| Frame I/O | 1 s total per frame, without renewal for each fragment |
| Startup | up to 1 s to send hello + 3 s for the response; terminate if incomplete |
| Operation deadline | UTC received by the operation, converted once to a monotonic clock; clamp to the local budget; never extended by progress/retry |
| Cancel grace / TERM / KILL | 250 ms / 250 ms / 1000 ms; at most 1500 ms after a cancellation request before declaring cleanup pending |
| Session closure | up to 1250 ms for termination + two 1000 ms drain waits (3250 ms); limit under normal scheduling, no real-time guarantee |
| Stdout and stderr | 16384 bytes each as a cumulative quota; discard bytes, counter saturated at 16385; exceeding it refuses success |
| Buffer per drain | 4096 bytes; keeps discarding until termination, without accumulating output |
| Test case / full measurement | 8 s per test (SIGALRM); 120 s in the collector and external test command |

A full queue prevents further reading until credit is released; no accepted event is
silently lost. A stalled producer and an expired deadline lead to failure and
cleanup, not an unbounded queue. The cancellation reader must continue even
with a full event queue; the spike measures real transport blocking but does not
implement the supervisor's multiplexer. Native buffers have finite capacity
managed by the OS; they are not the application's heap budget. The sandbox refused
`getsockopt` and `setsockopt`, so **no numeric native buffer ceiling was
measured or enforced here**. A gate for the future adapter is to configure/check this
ceiling on the packaged host, or reject the configuration if the product requires it.

EOF/truncation/crash is `worker_crashed`; an invalid frame is `invalid_frame`, a prefix
above the ceiling is `oversized_frame`, version is `incompatible_version`, schema/type is
`invalid_message`, deadline is `timeout`. The first failure invalidates the session: neither
a late response nor exit 0 recovers it. Terminate the group, discard results in
transit and close descriptors. Errors are allowlisted codes, without raw body,
stdout/stderr, path, canary, environment value or original exception message.

## Peer binding, filesystem and network policy

The OS creates both ends of the `socketpair` atomically. The parent passes only
the child end using `pass_fds`, closes its copy and keeps the other; the child marks the
fd non-inheritable on exec. Possession of the descriptor itself is the per-run authority;
there is no secret token in argv, environment or handshake. Additional IDs bind the
conversation but do not authenticate someone who has already stolen the descriptor. Do not use the
`SO_PEERCRED` PID of a pre-created pair as proof of the worker's identity.

A separate process without an inherited fd attempted injection and received an OS refusal. The
worker also sent an incorrect run through the real channel: the receiver refused it with
`invalid_peer`. A synthetic file with an old socket name remained intact.
Without bind/listen/pathname there is no stale endpoint to reuse, replace
or unlink; cleanup closes descriptors and never removes third-party files.
A process of the same user capable of inspecting/subverting the parent remains outside the
guarantee. Fork inherits descriptors until exec; the trusted worker must close
unneeded ones when creating descendants. This is an operational risk, not isolation.

Cwd does not constrain the filesystem: the worker read and wrote in a sibling directory with
synthetic canaries. Access to the user's other permissions was not examined.
The #159 `NetworkDecision` policy remains `none`/`loopback`: it must be resolved
in the parent and revalidated in the worker's authorized adapter, without implicit network or
ambient credentials. Do not serialize the Python object; only the accepted contract's
data representation, bound to the run and operation. Do not introduce
competing network classes or infer authorization from reachability.

The controlled listener test was **skipped**: the sandbox refuses AF_INET
socket creation. Internet, DNS, LAN and the user's real listener were not tested. The host's
refusal does not prove denial caused by the selected mechanism. Since no OS
network policy is part of this baseline, filesystem and network containment are
unavailable. `none` is enforceable only in trusted code through #159; if the requirement is
OS denial even after worker compromise, reject this proposal and open a
separate isolation review before executing the operation. Do not bypass #159.

## Control matrix by platform

E = enforced by the explicitly identified mechanism; B = best-effort under trusted
code and normal scheduling; U = unavailable in the baseline. NM = not measured,
with no guarantee attributed. There is no GPU control in this research.

| Control | Linux x86_64, measured | Windows, deferred by #124 | macOS, deferred by #124 |
| --- | --- | --- | --- |
| Minimal environment / unneeded fds | E at Popen launch; absence of canary and fd verified | NM; do not port pass_fds/select as if they were Windows handles | NM |
| Explicit temporary cwd | E as initial directory; does not contain the filesystem | NM | NM |
| Filesystem | U; read/write outside cwd in synthetic roots confirmed | U/NM | U/NM |
| Network | U; loopback probe skipped by host; #159 is an application control | U/NM | U/NM |
| Child creation | U; synthetic subprocess executed | U/NM | U/NM |
| Child/grandchild cleanup | B: killpg + wait; cases without escape passed | NM; evaluate native mechanism in its own research | NM; groups not yet measured |
| Wall time / cancellation | B: deadlines, EOF, TERM/KILL; not a real-time deadline | NM | NM |
| Memory | U for RSS/tree; E experimentally for per-process virtual space through RLIMIT_AS, 96 MiB, 128 MiB allocation refused | U/NM | U/NM |
| Process count | U: group is not a quota; per-UID limit is not a per-job limit | U/NM | U/NM |
| CPU | U for tree rate/quota; E experimentally for per-process CPU time with RLIMIT_CPU, SIGXCPU observed (soft 1 s, hard 2 s) | U/NM | U/NM |
| Recovery after parent crash | B: thread observes EOF and kills group; stopped/compromised worker may not cooperate | NM | NM |

RLIMITs were applied in the synthetic bootstrap itself. These limits are not installed
in production, and there is no proof of RSS, CPU percentage or total descendants.
Do not use preexec_fn in the multithreaded UI process. A future trusted entry point
could lower hard limits before loading work, after specific validation.
Cgroups/delegation, namespaces, seccomp, native Windows/macOS mechanisms and
packaging were neither probed nor accepted. Guaranteeing a tree quota,
cleanup after simultaneous parent and worker death, or containing descendants that use
setsid will require another primitive and new evidence. Do not change the platform
matrix to hide this gap; recommend a scope review if required.

## Cancellation, parent death and reconciliation

The cooperative request is idempotent; only the safe point confirms the ack. Escalation
kills the group even after the leader exits. Death by signal does not mean
acknowledged `cancelled` or `validated`. The future supervisor applies the
[job state machine](job-lifecycle.md) transitions through CAS. Without an ack, failure/timeout may become
resource/internal `JobFailure` according to the cause; after an ack, never emit a contradictory
outcome. If facts are missing for a conclusion, keep `requires_reconciliation`
without fabricating an outcome. Lease expiry is neither success nor an outcome.

After EOF from abrupt parent death, the synthetic worker with a separate reader kills
its group, including child/grandchild. The Linux harness uses subreaper and pidfds to
observe death and reap orphans, without confusing a zombie with a running process.
The subreaper belongs only to the test; it is not a production guarantee. SIGSTOP, group
escape, a blocked kernel and reader death are not contained; EOF alone is
best-effort. A worker crash with living descendants requires the parent's deadline and killpg.
If exit cannot be proved within budget, record pending cleanup and block
reuse/publication; do not delete content based only on PID.

A worker response never publishes an artifact. Only the #137 service may reserve,
verify size/digest, publish without overwrite and CAS to ready under a valid
lease. A `.part` file remains staging/incomplete after a crash and a new worker.
Real `ArtifactService` reconciliation, exercised with synthetic ports, kept the
active lease intact and abandoned a missing digest or final file with the
canonical reasons. Real SQLite and payload store remain under #137 tests;
their integration did not pass in this sandbox due to `unsafe_path`. Do not attribute
proof of that integration to synthetic ports. [Resume](../modules/recovery.md) is unchanged.

## Evidence, operation and follow-up

Sources, commands, limitations and the test matrix are in the
[removable spike](../../../spikes/local-worker/README.md), with
[structured metrics](../../../spikes/local-worker/results/linux-x86_64.json).
These are local observations on Python 3.12.12/Linux x86_64, without rebooting the host or
clearing page cache, not performance targets or distribution results.
No source text, EPUB, translation, absolute path, canary value or secret
enters the evidence. The scripts do not import Ayvu; only tests consume existing
contracts to check compatibility. There is no installed dependency/entry point.

Follow-up proposals, each reviewable, without creating remote issues in this session:

1. **M — IPC codec and adapter**: command/result schemas, validation of existing
   DTOs, single producer in supervisor, credits with responsive control and
   fuzz/limit tests, without job execution or secrets.
2. **L — trusted Linux supervisor**: fixed packaged entry point, environment/handles,
   deadlines, cancellation, bounded output, cleanup and safe diagnostics. Gate:
   bundle smoke test and explicit definition of the native buffer ceiling.
3. **M — lease/artifact/recovery integration**: CAS, ownership, digest,
   late result, reconciliation and SQLite tests outside this sandbox; preserve CLI.
4. **M — separate OS containment**: decide whether a trusted subprocess suffices for each
   operation; otherwise measure tree quotas and network/filesystem denial,
   without claiming a sandbox before proof. Plugins remain prohibited.
5. **M per platform — native research and packaging**: only if #124 incorporates
   Windows/macOS; repeat the entire matrix, including handles, tree and parent death.
6. **S — human review of this ADR**: accept/reject/restrict operations and risks,
   record authorship/date. Done on 2026-11-10; see the record below.

If rejected, remove `spikes/local-worker`, its tests/record/links and this ADR;
no data migration, CLI rollback or dependency would have been needed.

## Acceptance record

Accepted on 2026-11-10 by the project maintainer, through explicit approval in the
development session, without restrictions on the recommendation above:

- Desktop runs heavy work in a separate local worker, launched through
  `subprocess.Popen` with a fixed package entry point and IPC through an inherited
  anonymous `AF_UNIX socketpair`;
- one worker per run, in its own process group, without reuse between
  jobs in v1, with stdout/stderr separate from the protocol;
- only reviewed, allowlisted in-tree commands are trusted; plugins,
  unrestricted executables, code and argv remain prohibited;
- the minimum launch requirements described above are mandatory, and no credential passes
  to the worker.

Acceptance neither implements nor enables a runner. Follow-ups 1 through 5 remain
separate issues, each with its own review. The following remain open, without waiver:

- a trusted subprocess is not a sandbox; OS containment (follow-up 4)
  has not yet been decided;
- Windows and macOS still lack native evidence;
- the native buffer ceiling and bundle smoke test are gates for the Linux supervisor.

`spikes/local-worker` remains disposable research, outside production.
