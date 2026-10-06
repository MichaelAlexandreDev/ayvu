"""Create a private temporary root for disposable package builds."""

from __future__ import annotations

import tempfile
from pathlib import Path


def main() -> int:
    root = Path(
        tempfile.mkdtemp(
            prefix="ayvu-desktop-stack-",
            dir=tempfile.gettempdir(),
        )
    )
    print(root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
