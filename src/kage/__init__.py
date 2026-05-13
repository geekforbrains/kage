"""kage - scriptable bridge for interactive AI CLIs."""
from .session import Session, SendResult, MenuPending, State
from .backends import get_backend, list_backends

__version__ = "0.1.0"
__all__ = [
    "Session",
    "SendResult",
    "MenuPending",
    "State",
    "get_backend",
    "list_backends",
]
