# Library discovery and external opening

A abertura que antes se chamava `open_library_epub()` em `library.py` está hoje
em `LibraryService.open_entry`/`ReaderLauncher.launch`. O leitor é um processo
independente, explicitamente fora da [garantia HTTP offline](network-policy.md).
A CLI exibe esse limite antes de lançar o processo, inclusive com
`--network-policy none`; o resultado de abertura não representa contenção de
rede do leitor. A política não é passada como promessa de isolamento ao filho.

The guided Library flow composes [`LibraryService`](../../src/ayvu/library_service.py)
with the local adapters in [`library_adapters.py`](../../src/ayvu/library_adapters.py).
The service uses separate discovery, authorization and launcher protocols and does not import a UI, EPUB
parser or process API. A consumer supplies adapters and must explicitly authorize
external opening; discovery alone does not launch a reader.

## Discovery and references

The local adapter accepts a configured root and two folder names. It examines only
immediate `.epub` entries in those folders, normally `Original` and `Traduzidos`;
it does not read EPUB contents. It resolves the root and directories, rejects a
directory outside the root, checks regular files and collects identity metadata.
A missing root or folder produces an empty contribution. Other directory and entry
errors produce bounded reason-code warnings, without exception text or paths.

The service groups translations by original name or filename suffix, supplies
language labels, and orders books and translations deterministically. Names with
control characters, invalid Unicode surrogates or more than 255 characters are
rejected with a warning. A scan retains at most 10,000 candidates and 100 warnings;
cancellation is checked during enumeration and grouping. The local enumeration
selects names in sorted order when a folder exceeds the entry limit.

Books and individual files receive opaque, scan-local references. `describe`
looks up a book in the latest scan and `open` looks up a file; every scan clears
previous references. Display names are derived from filenames and are therefore
still user data. Adapter tokens and the successful open path are omitted from
default representations and results. The CLI explicitly requests and displays the
selected path after a successful open.

## Authorization and launch

At open, the local filesystem adapter resolves the configured root and candidate
again, checks canonical containment and regular-file status, and compares recorded
root and file identity metadata. Missing, changed or escaping selections fail
before the launcher is called. This check is immediately before launch in the
service flow, but it cannot guarantee what a reader later opens: another process
can replace a path after validation. The reader and its environment are outside
Ayvu's control.

The reader adapter parses a configured command into bounded arguments, or chooses
an available system opener. It passes an executable and argument tuple to the
process adapter, which constructs an argv list and invokes no shell. The selected
path is appended as one argument. Outcomes distinguish cancellation, invalid
configuration, unavailable opener, launch failure and successful process creation;
success does not mean that the reader rendered the book. Cancellation is checked
before process creation and does not terminate a reader already started. The
current system selection supports macOS `open` and Unix `xdg-open`, `gio open`,
and `open` as fallbacks. Configured shell interpreters are rejected;
without a configured reader, Windows returns unavailable.
