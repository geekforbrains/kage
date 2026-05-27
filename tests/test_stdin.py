"""Tests for non-blocking optional-stdin reading (the stdin-hang fix)."""
import os
import time

from kage.cli import _read_available


def test_reads_available_then_eof():
    r, w = os.pipe()
    os.write(w, b"hello context")
    os.close(w)  # EOF
    try:
        assert _read_available(r) == "hello context"
    finally:
        os.close(r)


def test_does_not_block_on_open_pipe_with_no_data():
    """The hang case: pipe open, no data, no EOF — must return promptly."""
    r, w = os.pipe()
    try:
        start = time.monotonic()
        out = _read_available(r, timeout=0.1)
        elapsed = time.monotonic() - start
        assert out == ""
        assert elapsed < 1.0  # returned without blocking
    finally:
        os.close(r)
        os.close(w)


def test_reads_partial_data_without_eof_then_stops():
    """Data present but writer keeps the pipe open: drain what's there, stop."""
    r, w = os.pipe()
    try:
        os.write(w, b"partial")  # note: no close, no EOF
        out = _read_available(r, timeout=0.15)
        assert out == "partial"
    finally:
        os.close(r)
        os.close(w)


def test_bad_fd_returns_empty():
    assert _read_available(-1) == ""
