"""Domain model, state machines and error types."""
from __future__ import annotations

import enum


class VersionState(str, enum.Enum):
    """Lifecycle of one document version.

    ``adopted`` -> ``effective`` -> ``superseded`` / ``repealed``
    (plus the ``adopted`` -> ``repealed`` shortcut used when a version is
    repealed before it ever enters force).
    """

    ADOPTED = "adopted"
    EFFECTIVE = "effective"
    SUPERSEDED = "superseded"
    REPEALED = "repealed"


# Allowed lifecycle transitions; everything else raises IllegalTransition.
VERSION_TRANSITIONS: dict[str, frozenset[str]] = {
    VersionState.ADOPTED.value: frozenset(
        {VersionState.EFFECTIVE.value, VersionState.REPEALED.value}
    ),
    VersionState.EFFECTIVE.value: frozenset(
        {VersionState.SUPERSEDED.value, VersionState.REPEALED.value}
    ),
    VersionState.SUPERSEDED.value: frozenset({VersionState.REPEALED.value}),
    VersionState.REPEALED.value: frozenset(),
}

LEGAL_SNAPSHOT_TRANSITIONS = {
    "open": frozenset({"published"}),
    "published": frozenset(),
}


def check_transition(table: dict[str, frozenset[str]], frm: str, to: str) -> None:
    allowed = table.get(frm, frozenset())
    if to not in allowed:
        raise IllegalTransition(frm, to, sorted(allowed))


class CiteWeaveError(Exception):
    """Base class for expected, user-facing errors."""

    code = "error"

    def __init__(self, message: str = "", **extra: object) -> None:
        super().__init__(message or self.code)
        self.extra = extra


class NotFound(CiteWeaveError):
    code = "not_found"


class Conflict(CiteWeaveError):
    """Stale revision / duplicate business key (HTTP 409)."""

    code = "conflict"

    def __init__(self, message: str = "", *, base: object = None,
                 current: object = None, **extra: object) -> None:
        super().__init__(message, base=base, current=current, **extra)
        self.base = base
        self.current = current


class IllegalTransition(CiteWeaveError):
    code = "illegal_transition"

    def __init__(self, frm: str, to: str, allowed: list[str]) -> None:
        super().__init__(f"illegal transition {frm!r} -> {to!r}",
                         frm=frm, to=to, allowed=allowed)
        self.frm = frm
        self.to = to
        self.allowed = allowed


class ValidationError(CiteWeaveError):
    code = "validation_error"


class StagedError(CiteWeaveError):
    """Raised inside an import on purpose to verify crash recovery."""

    code = "staged_crash"
