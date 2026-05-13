"""Backend registry."""
from __future__ import annotations

from .base import Backend, Menu, State
from .claude import ClaudeBackend

_REGISTRY: dict[str, type[Backend]] = {
    "claude": ClaudeBackend,
}


def get_backend(name: str, **kwargs) -> Backend:
    if name not in _REGISTRY:
        raise ValueError(f"unknown backend: {name!r}. available: {sorted(_REGISTRY)}")
    return _REGISTRY[name](**kwargs)


def list_backends() -> list[str]:
    return sorted(_REGISTRY)


__all__ = ["Backend", "Menu", "State", "get_backend", "list_backends"]
