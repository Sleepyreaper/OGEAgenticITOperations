"""Explicit error type for the operations evidence collectors.

Collectors must raise `OperationsCollectionError` (never return a
success-shaped empty list/dict) when they cannot produce a trustworthy
result -- auth failure, a non-2xx API response, or a payload they can't
safely normalize. The orchestrator (app/operations/service.py) catches
this *specific* type per source and turns it into an explicit 'error'
envelope; it never uses a bare `except:`/`except Exception: pass`, and a
failure in one source never erases another source's successful results.
"""

from typing import Optional


class OperationsCollectionError(RuntimeError):
    """A named signal source failed to produce a trustworthy result."""

    def __init__(self, source: str, message: str, *, detail: Optional[str] = None):
        self.source = source
        self.detail = detail
        full_message = f"[{source}] {message}"
        if detail:
            full_message = f"{full_message} ({detail})"
        super().__init__(full_message)
