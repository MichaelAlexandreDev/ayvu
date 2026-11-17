"""Observable model-label admission evidence shared by CLI regression tests."""
import hashlib

REFUSAL = "To translate, declare your translator's model label."
NEXT_STEP = ('Open Settings (option 4) and choose "5. Change translator model label", '
             'or add translator_models.libretranslate to config.json.')


def snapshot(root):
    """Include directories and every file byte, so transient stores cannot hide in a known folder."""
    return {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
            for path in root.rglob("*")}


def assert_refused(result):
    output = " ".join(result.output.split())
    assert result.exit_code == 1, result.output
    assert isinstance(result.exception, SystemExit), repr(result.exception)
    assert REFUSAL in output
    assert NEXT_STEP in output
