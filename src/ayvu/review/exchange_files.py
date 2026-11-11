from __future__ import annotations

from pathlib import Path

from .exchange_codec import decode_review_package, encode_review_package
from .exchange_model import MAX_REVIEW_RAW_BYTES, ReviewExchangeError, ReviewPackage


def write_review_package(path: str | Path, package: ReviewPackage, *, overwrite: bool = False) -> Path:
    """Write only when explicitly requested; errors never echo the destination path."""
    destination = Path(path)
    data = encode_review_package(package)
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("wb" if overwrite else "xb") as output:
            output.write(data)
    except FileExistsError:
        raise ReviewExchangeError("review_file_exists") from None
    except OSError:
        raise ReviewExchangeError("review_file_unwritable") from None
    return destination


def read_review_package(path: str | Path) -> ReviewPackage:
    """Read at most the raw-byte budget before decoding or parsing the file."""
    try:
        with Path(path).open("rb") as stream:
            raw = stream.read(MAX_REVIEW_RAW_BYTES + 1)
    except FileNotFoundError:
        raise ReviewExchangeError("review_file_not_found") from None
    except OSError:
        raise ReviewExchangeError("review_file_unreadable") from None
    return decode_review_package(raw)
