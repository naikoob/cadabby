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


# Builtins Cadabby raises directly, in resolution order. FileNotFoundError,
# FileExistsError and PermissionError are all OSError subclasses, so OSError
# must come last or it swallows them.
_BUILTIN_CODES: tuple[tuple[type[BaseException], str, bool], ...] = (
    (FileNotFoundError, NOT_FOUND, False),
    (FileExistsError, ALREADY_EXISTS, False),
    (PermissionError, PERMISSION_DENIED, False),
    (OSError, IO_ERROR, False),
    (ValueError, INVALID_ARGUMENT, False),
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
    }
)


def classify(exc: BaseException) -> ErrorInfo:
    """Assign a stable code to any exception reaching the boundary.

    Deliberately total: anything unrecognized becomes INTERNAL rather than
    propagating. An MCP server that dies on a surprise is worse than one that
    reports the surprise badly, and the caller still learns the request
    failed and that retrying it unchanged will not help.
    """
    if isinstance(exc, CadabbyError):
        return ErrorInfo(exc.code, str(exc), exc.retryable)

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
    IO_ERROR: EXIT_UNEXPECTED,
    INTERNAL: EXIT_UNEXPECTED,
}


def exit_code_for(code: str) -> int:
    """Map an error code to a process exit status (§5.4)."""
    return _EXIT_BY_CODE.get(code, EXIT_UNEXPECTED)
