# Installation and setup

## Usage notice

Ayvu is meant for personal use, study and accessibility with EPUB files you provide. It does not remove
DRM, does not download books, and must not be used to distribute copyrighted content.

By default, `.epub` files, the local cache and private glossaries stay out of Git.

## Requirements

- Python 3.11 or newer. Whether the desktop's native dependencies are available depends on the Python
  version, operating system and architecture.
- `uv` or `pip`.
- A LibreTranslate-compatible server for real translations.
- For the desktop, a graphical session and the native dependencies Qt requires.

## Installing with uv

Inside the project directory, install just the CLI:

```bash
cd ayvu
uv sync
uv run ayvu --help
```

To install the optional desktop support and run its entry point:

```bash
uv sync --extra desktop
uv run --extra desktop ayvu-desktop
```

The extra installs `PySide6-Essentials==6.11.2`, without Addons or WebEngine. The desktop interface is
not available yet: its entry point says so and exits with code 1, while `ayvu-desktop --help` returns 0.
Without the extra, the command explains how to install the dependency and exits with code 1. Every
translation flow in this guide is available through the CLI, whose default installation does not
require PySide6.

To develop and run the CLI tests:

```bash
uv sync --extra dev
uv run --extra dev pytest
```

To develop the desktop, select both extras:

```bash
uv sync --extra dev --extra desktop
uv run --extra dev --extra desktop pytest
```

When you switch between these profiles, include the extras you want in the `uv run` command, because uv
syncs the environment to the selection you give it.

## Installing with pip

```bash
cd ayvu
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .              # CLI
# Or: python -m pip install -e ".[desktop]"
# For development: python -m pip install -e ".[dev,desktop]"
```

With the environment active, use `ayvu --help`, or `ayvu-desktop` with the desktop extra.

## Translation server

Start LibreTranslate locally with Docker:

```bash
docker run -it -p 5000:5000 libretranslate/libretranslate
```

The endpoint the CLI uses:

```text
http://localhost:5000/translate
```

Test the connection:

```bash
uv run ayvu test-translator --url http://localhost:5000
```
