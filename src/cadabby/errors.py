"""Stable error taxonomy for the CLI and MCP surfaces (§5.4).

Before this module the taxonomy was one member wide and lived inside a
message string: `VAULT_CONFLICT: ...` was formatted into the text of two
different exceptions in two different files, with no shared constant, while
the other four exception types carried no marker at all. A caller could not
tell whether matching on the prefix was supported, and the MCP boundary
flattened every failure to prose regardless, so an agent had no way to
distinguish "wait and retry" from "fix your input" from "stop and ask a
human".

The codes here are part of the public surface. `retryable` is the field that
prose cannot carry and the one an agent actually branches on: it means an
identical request could plausibly succeed later, without the caller changing
anything.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

# --- Codes -----------------------------------------------------------------
# Deliberate collision: FRONTMATTER_UNPARSEABLE is also a lint code (§6.3).
# It is the same condition observed from two surfaces, and giving it two
# names would be the defect, not the sharing.

VAULT_CONFLICT = "VAULT_CONFLICT"
LOCK_TIMEOUT = "LOCK_TIMEOUT"
CONFIG_INVALID = "CONFIG_INVALID"
FRONTMATTER_UNPARSEABLE = "FRONTMATTER_UNPARSEABLE"
FRONTMATTER_UNSERIALIZABLE = "FRONTMATTER_UNSERIALIZABLE"
NOT_FOUND = "NOT_FOUND"
ALREADY_EXISTS = "ALREADY_EXISTS"
INVALID_ARGUMENT = "INVALID_ARGUMENT"
PERMISSION_DENIED = "PERMISSION_DENIED"
IO_ERROR = "IO_ERROR"
UNKNOWN_TOOL = "UNKNOWN_TOOL"
INTERNAL = "INTERNAL"

# Distinct from INVALID_ARGUMENT on purpose. An agent that reads
# "invalid argument" may reasonably try another spelling of the actor;
# this code says the refusal is categorical and the only fix is to stop
# claiming human review (§3.4).
HUMAN_ATTESTATION_REFUSED = "HUMAN_ATTESTATION_REFUSED"

# Also distinct from INVALID_ARGUMENT, for the same reason: retrying with
# another `method` label is exactly the move this refusal must not invite. A
# machine attestation would contradict the note (an Unverified Supplement
# callout, or source-crosscheck with no sources); the fix is to leave it
# unverified or to ground the claim (§3.4).
VERIFICATION_REFUSED = "VERIFICATION_REFUSED"


@dataclass(frozen=True)
class ErrorInfo:
    """What a failure looks like once it has crossed the boundary."""

    code: str
    message: str
    retryable: bool


class CadabbyError(Exception):
    """Base for every failure Cadabby raises on purpose.

    Subclasses set `code` and `retryable` as class attributes so the values
    travel with the exception rather than being reconstructed by whoever
    catches it.
    """

    code: str = INTERNAL
    retryable: bool = False


class VaultConflictError(CadabbyError):
    """An atomic write found the file had changed since the operation began.

    Retryable: the caller re-reads, recomputes its expected hash, and tries
    again. Nothing is wrong with the request, it merely lost a race.
    """

    code = VAULT_CONFLICT
    retryable = True


class LockTimeoutError(CadabbyError):
    """An advisory lock could not be acquired within the timeout.

    Retryable for the same reason: another process holds the vault and will
    almost certainly release it.
    """

    code = LOCK_TIMEOUT
    retryable = True


class VaultConfigError(CadabbyError, ValueError):
    """`.cadabby.json` is malformed or structurally wrong.

    ValueError stays in the bases because `installer.py` and `vault.py`
    already catch it that way; dropping it would silently change which
    handler runs.
    """

    code = CONFIG_INVALID


class HumanAttestationRefusedError(CadabbyError):
    """A `human:*` attestation was requested through a non-interactive surface (§3.4).

    One exception type for every surface, so the MCP tool and the use case
    report the same categorical code rather than one of them PERMISSION_DENIED.
    """

    code = HUMAN_ATTESTATION_REFUSED


class VerificationRefusedError(CadabbyError):
    """A machine attestation would contradict the note it signs (§3.4).

    Raised by the Note aggregate, so disk and in-memory storage refuse alike:
    the body carries an Unverified Supplement callout, or `source-crosscheck`
    was claimed for a note with no `sources:`. Humans are never refused.
    """

    code = VERIFICATION_REFUSED


class VaultEscapeError(CadabbyError, PermissionError):
    """A write would land outside the vault, through a symlink or otherwise (§8).

    A vault is a folder anyone can hand you: a clone, a template, a synced
    share. `log.md -> ~/.bashrc` made every ledger append a write into the
    user's shell startup file. PermissionError stays in the bases so existing
    `except PermissionError` handlers keep catching it.
    """

    code = PERMISSION_DENIED


class InvalidArgumentError(CadabbyError, ValueError):
    """Caller-supplied input failed validation (§5.4).

    ValueError stays in the bases so existing `except ValueError` handlers and
    tests continue to catch it, while unexpected bare `ValueError`s from the
    standard library classify as INTERNAL rather than caller mistakes.
    """

    code = INVALID_ARGUMENT


class NotFoundError(CadabbyError, FileNotFoundError):
    """A requested note, raw source, template, or vault was not found (§5.4)."""

    code = NOT_FOUND


class AlreadyExistsError(CadabbyError, FileExistsError):
    """A destination note or folder already exists (§5.4)."""

    code = ALREADY_EXISTS


class AmbiguousNoteError(InvalidArgumentError):
    """A note name matches more than one note, and a write must not guess (§5.1).

    An InvalidArgumentError, so it classifies as INVALID_ARGUMENT like any other
    input the caller has to correct; `candidates` lists what the name could mean.
    """

    def __init__(self, name: str, candidates: list[str]) -> None:
        self.candidates = candidates
        super().__init__(
            f"'{name}' matches {len(candidates)} notes ({', '.join(candidates)}); name one by its full path"
        )


# Builtins the OS or standard library raises directly, in resolution order.
# FileNotFoundError, FileExistsError and PermissionError are all OSError
# subclasses, so OSError must come last or it swallows them. Bare ValueError is
# deliberately omitted: every intentional caller-input validation error raises
# InvalidArgumentError, so an unexpected ValueError classifies as INTERNAL.
_BUILTIN_CODES: tuple[tuple[type[BaseException], str, bool], ...] = (
    # A decode error nobody anticipated is the engine's surprise, not bad input.
    (UnicodeError, INTERNAL, False),
    (FileNotFoundError, NOT_FOUND, False),
    (FileExistsError, ALREADY_EXISTS, False),
    (PermissionError, PERMISSION_DENIED, False),
    (OSError, IO_ERROR, False),
)

ALL_CODES: frozenset[str] = frozenset(
    {
        VAULT_CONFLICT,
        LOCK_TIMEOUT,
        CONFIG_INVALID,
        FRONTMATTER_UNPARSEABLE,
        FRONTMATTER_UNSERIALIZABLE,
        NOT_FOUND,
        ALREADY_EXISTS,
        INVALID_ARGUMENT,
        PERMISSION_DENIED,
        IO_ERROR,
        UNKNOWN_TOOL,
        INTERNAL,
        HUMAN_ATTESTATION_REFUSED,
        VERIFICATION_REFUSED,
    }
)


def _is_sqlite_contention(exc: sqlite3.OperationalError) -> bool:
    message = str(exc).lower()
    return "locked" in message or "busy" in message


def classify(exc: BaseException) -> ErrorInfo:
    """Assign a stable code to any exception reaching the boundary.

    Deliberately total: anything unrecognized becomes INTERNAL rather than
    propagating. An MCP server that dies on a surprise is worse than one that
    reports the surprise badly, and the caller still learns the request
    failed and that retrying it unchanged will not help.
    """
    if isinstance(exc, CadabbyError):
        return ErrorInfo(exc.code, str(exc), exc.retryable)
    if isinstance(exc, sqlite3.OperationalError) and _is_sqlite_contention(exc):
        # Another process holds the cache (the MCP server and a CLI on one vault
        # is the designed setup): transient, so say so, rather than INTERNAL.
        return ErrorInfo(LOCK_TIMEOUT, f"The vault cache is busy in another process: {exc}", True)

    for exc_type, code, retryable in _BUILTIN_CODES:
        if isinstance(exc, exc_type):
            return ErrorInfo(code, str(exc), retryable)

    return ErrorInfo(INTERNAL, str(exc), False)


# --- Process exit codes ----------------------------------------------------
# Six outcomes, because each one calls for a different reaction from a
# script. 2 is argparse's own usage exit and is not reused. 1 keeps its
# existing meaning -- the command ran and the vault has findings -- because
# pre-commit hooks already depend on it.

EXIT_OK = 0
EXIT_FINDINGS = 1
EXIT_USAGE = 2
EXIT_ENVIRONMENT = 3  # fix the input, the path, or the config
EXIT_TRANSIENT = 4  # nothing is wrong; try again
EXIT_UNEXPECTED = 5  # a bug or a failing disk

_EXIT_BY_CODE: dict[str, int] = {
    VAULT_CONFLICT: EXIT_TRANSIENT,
    LOCK_TIMEOUT: EXIT_TRANSIENT,
    CONFIG_INVALID: EXIT_ENVIRONMENT,
    FRONTMATTER_UNPARSEABLE: EXIT_ENVIRONMENT,
    FRONTMATTER_UNSERIALIZABLE: EXIT_ENVIRONMENT,
    NOT_FOUND: EXIT_ENVIRONMENT,
    ALREADY_EXISTS: EXIT_ENVIRONMENT,
    INVALID_ARGUMENT: EXIT_ENVIRONMENT,
    PERMISSION_DENIED: EXIT_ENVIRONMENT,
    UNKNOWN_TOOL: EXIT_USAGE,
    HUMAN_ATTESTATION_REFUSED: EXIT_ENVIRONMENT,
    VERIFICATION_REFUSED: EXIT_ENVIRONMENT,
    IO_ERROR: EXIT_UNEXPECTED,
    INTERNAL: EXIT_UNEXPECTED,
}


def exit_code_for(code: str) -> int:
    """Map an error code to a process exit status (§5.4)."""
    return _EXIT_BY_CODE.get(code, EXIT_UNEXPECTED)
