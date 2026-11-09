"""Command-line entry point."""

from __future__ import annotations

import argparse

from . import __version__


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="subwatch",
        description="Find the subscriptions and free trials draining your money.",
    )
    ap.add_argument("--version", action="version", version=f"subwatch {__version__}")
    ap.parse_args(argv)
    ap.print_help()
    return 0
