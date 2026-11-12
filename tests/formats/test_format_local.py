import os

import pytest

from ayvu.formats.contracts import AliasVerdict, FormatError
from ayvu.formats.local import LocalFormatSource, LocalOutputGuard
from tests.support.format_contract_env import CountingSource

from tests.formats.format_adapter_support import CARDS
from tests.formats.format_adapter_support import cards
from tests.formats.format_adapter_support import service_for


def test_a_local_source_reads_and_fingerprints(cards):
    source = LocalFormatSource(cards)
    assert source.size() == len(CARDS) and source.head(6) == b"CARDS1" and source.extension == ".cards"
    assert source.fingerprint().startswith("sha256:") and len(source.fingerprint()) == 71
    with source.open() as stream:
        assert stream.read() == CARDS


def test_a_local_source_cannot_be_written_through(cards):
    with LocalFormatSource(cards).open() as stream:
        with pytest.raises(OSError):
            stream.write(b"x")
    assert cards.read_bytes() == CARDS


@pytest.mark.parametrize("name,extension", [
    ("book.EPUB", ".epub"), ("book", None), ("book.", None), (".hidden", None), ("a.we ird", None), ("a.açb", None), ("a." + "x" * 17, None),
])
def test_only_a_plain_extension_is_evidence(tmp_path, name, extension):
    assert LocalFormatSource(tmp_path / name).extension == extension


def test_a_fifo_is_refused_without_blocking(tmp_path):
    fifo = tmp_path / "pipe.cards"
    os.mkfifo(fifo)
    source = LocalFormatSource(fifo)
    with pytest.raises(FormatError, match="^source_not_regular_file$"):
        source.open()
    assert service_for().inspect("fake_cards", source).failure.code == "source_unreadable"


def test_a_directory_is_refused(tmp_path):
    with pytest.raises(FormatError):
        LocalFormatSource(tmp_path).fingerprint()


def test_a_missing_source_reports_a_code(tmp_path):
    with pytest.raises(FormatError, match="^source_unreadable$"):
        LocalFormatSource(tmp_path / "gone").open()


def test_the_guard_accepts_a_new_or_different_file(tmp_path, cards):
    other = tmp_path / "other"
    other.write_bytes(CARDS)  # Equal content is not the same file.
    source = LocalFormatSource(cards)
    assert LocalOutputGuard(other).verdict(source) is AliasVerdict.DISTINCT
    assert LocalOutputGuard(tmp_path / "new.cards").verdict(source) is AliasVerdict.DISTINCT


def test_the_guard_recognizes_a_chain_of_links(tmp_path, cards):
    first, second = tmp_path / "first", tmp_path / "second"
    first.symlink_to(cards)
    second.symlink_to(first)
    assert LocalOutputGuard(second).verdict(LocalFormatSource(cards)) is AliasVerdict.SAME
    assert LocalOutputGuard(cards).verdict(LocalFormatSource(second)) is AliasVerdict.SAME


def test_the_guard_recognizes_a_linked_directory(tmp_path, cards):
    linked = tmp_path / "linked"
    linked.symlink_to(tmp_path, target_is_directory=True)
    assert LocalOutputGuard(linked / cards.name).verdict(LocalFormatSource(cards)) is AliasVerdict.SAME


@pytest.mark.parametrize("kind", ["dangling", "loop", "under_a_file", "unknown_source", "missing_source"])
def test_the_guard_says_unknown_when_it_cannot_tell(tmp_path, cards, kind):
    source = LocalFormatSource(cards)
    destination = tmp_path / "destination"
    if kind == "dangling":
        destination.symlink_to(tmp_path / "nowhere")
    elif kind == "loop":
        destination.symlink_to(destination)
    elif kind == "under_a_file":
        destination = cards / "child"
    elif kind == "unknown_source":
        source = CountingSource(source)
    else:
        source = LocalFormatSource(tmp_path / "gone")
    assert LocalOutputGuard(destination).verdict(source) is AliasVerdict.UNKNOWN


# Values
