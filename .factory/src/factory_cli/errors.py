"""Errors the CLI turns into exit codes."""

from __future__ import annotations


class FactoryError(Exception):
    """A failure the user can act on. Exit code 1."""

    exit_code = 1


class UsageError(FactoryError):
    """Bad arguments or input. Exit code 2."""

    exit_code = 2
