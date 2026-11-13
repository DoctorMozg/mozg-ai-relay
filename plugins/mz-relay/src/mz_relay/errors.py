"""The errors mz-relay raises; each carries the wire code it maps to."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import ClassVar


class RelayError(Exception):
    """A failure with a stable wire code; str(err) is the message alone."""

    # Empty means any code; subclasses list the codes they may carry.
    CODES: ClassVar[frozenset[str]] = frozenset()

    def __init__(self, code: str, message: str, data: Mapping[str, object] | None = None) -> None:
        if not code:
            raise ValueError("error code must be a non-empty string")
        if self.CODES and code not in self.CODES:
            allowed = ", ".join(sorted(self.CODES))
            name = type(self).__name__
            raise ValueError(f"{name} cannot carry code {code!r} (allowed: {allowed})")
        super().__init__(message)
        self.code = code
        self.message = message
        self.data: dict[str, object] = dict(data) if data else {}

    def to_wire(self) -> dict[str, object]:
        wire: dict[str, object] = {"code": self.code, "message": self.message}
        if self.data:
            wire["data"] = dict(self.data)
        return wire


class _SingleCodeError(RelayError):
    """An error with exactly one code, built as Sub(message)."""

    CODE: ClassVar[str]

    def __init__(self, message: str, data: Mapping[str, object] | None = None) -> None:
        super().__init__(self.CODE, message, data)


class RosterError(_SingleCodeError):
    CODE = "roster_invalid"

    def __init__(self, message: str, *, path: Path | None = None, key: str | None = None) -> None:
        super().__init__(message)
        self.path = path
        self.key = key


class IdentityError(RelayError):
    CODES = frozenset(
        {"identity_unresolved", "identity_ambiguous", "unknown_agent", "harness_mismatch"}
    )


class EnvelopeError(_SingleCodeError):
    CODE = "invalid_args"


class PolicyViolation(RelayError):
    CODES = frozenset(
        {
            "unknown_recipient",
            "not_allowed",
            "echo_to_origin",
            "hop_exceeded",
            "unknown_parent",
            "body_too_large",
            "turn_cap",
            "inbox_full",
            "context_closed",
        }
    )


class AuthError(_SingleCodeError):
    CODE = "bad_token"


class NotFound(RelayError):
    CODES = frozenset({"not_found", "not_held"})


class BindingError(_SingleCodeError):
    CODE = "binding_invalid"


class ProtocolError(RelayError):
    CODES = frozenset({"invalid_request", "line_too_long", "unknown_op"})


class ProtocolMismatch(ProtocolError):
    CODES = frozenset({"protocol_mismatch"})

    def __init__(self, message: str, data: Mapping[str, object] | None = None) -> None:
        super().__init__("protocol_mismatch", message, data)


class DaemonUnavailable(_SingleCodeError):
    CODE = "daemon_unavailable"


class PathError(_SingleCodeError):
    CODE = "path_invalid"


class StoreError(_SingleCodeError):
    CODE = "store_invalid"
