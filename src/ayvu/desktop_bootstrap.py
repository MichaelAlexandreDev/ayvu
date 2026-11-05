"""Packaging entry point for the future desktop application."""

import argparse
import sys


def start_desktop() -> int:
    """Placeholder until the desktop composition root is available."""
    print("A interface desktop ainda não está disponível (DESKTOP-010).", file=sys.stderr)
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="ayvu-desktop", description="Iniciar a interface desktop do Ayvu."
    )
    parser.parse_args()
    try:
        from PySide6 import QtWidgets  # noqa: F401
    except ModuleNotFoundError:
        print(
            'Dependência desktop indisponível. Instale com: pip install "ayvu[desktop]"',
            file=sys.stderr,
        )
        return 1
    except ImportError:
        # Qt is installed but will not load, usually for a missing system library, so reinstalling would not help.
        print(
            "O Qt está instalado, mas não pôde ser carregado. Verifique as bibliotecas gráficas do sistema.",
            file=sys.stderr,
        )
        return 1
    return start_desktop()
