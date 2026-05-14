"""Shared pytest helpers for kage backend tests."""
from __future__ import annotations

from pathlib import Path


def load_fixture(backend: str, name: str) -> str:
    """Read a pane fixture for a given backend by short name (no extension)."""
    path = Path(__file__).parent / backend / "fixtures" / f"{name}.txt"
    return path.read_text(encoding="utf-8")
