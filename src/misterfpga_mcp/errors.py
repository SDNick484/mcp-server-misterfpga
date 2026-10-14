"""Errors the model or the user can act on.

``MisterError`` is a ``ToolError``, so its message reaches the model as the
tool's result (is_error) instead of an "internal error". Every message should
say what happened *and* what to do about it, in a sentence a person could read.
"""

from __future__ import annotations

from mcp.server.mcpserver.exceptions import ToolError


class MisterError(ToolError):
    """Something about the request or the MiSTer that the caller can act on."""


class Unavailable(MisterError):
    """A backend this call needs (Zaparoo, SSH) isn't reachable or isn't set up for that host."""


class ZaparooError(MisterError):
    """Zaparoo Core answered with an error. ``category`` is its error.data.category, when it sent one."""

    def __init__(self, message: str, category: str | None = None, reason: str | None = None) -> None:
        super().__init__(message)
        self.category = category
        self.reason = reason
