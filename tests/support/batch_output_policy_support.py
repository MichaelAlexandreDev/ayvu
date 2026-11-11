from pathlib import Path
import pytest
from ayvu.translation.batch_output import BatchOutputError, BatchOutputPolicy, OutputIntent
from ayvu.kernel import OutputPlan

def intent(root: Path, name: str, *, dry=False, overwrite=False):
    source = root / f"{name}.epub"
    source.write_bytes(name.encode())
    return OutputIntent(source, OutputPlan(root / f"{name}-pt.epub", dry_run=dry), root / "reports",
                        f"{name}-pt-report", f"{name}-pt-missing", overwrite)
