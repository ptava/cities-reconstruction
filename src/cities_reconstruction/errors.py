"""Application-level errors shared by configuration, planning, and the CLI."""

from __future__ import annotations

from enum import StrEnum


class ErrorCategory(StrEnum):
    """Stable machine-readable categories for expected application failures."""

    USAGE = "usage"
    CONFIGURATION = "configuration"
    PLANNING = "planning"


class ApplicationError(Exception):
    """Base class for expected failures that the CLI can render safely."""

    category = ErrorCategory.CONFIGURATION
    exit_code = 2
    human_label: str | None = None

    def __init__(
        self,
        message: str,
        *,
        usage: str | None = None,
        program: str | None = None,
    ) -> None:
        super().__init__(message)
        self.usage = usage
        self.program = program

    def to_dict(self) -> dict[str, str | int]:
        """Return the stable JSON representation of this failure."""

        return {
            "category": self.category.value,
            "message": str(self),
            "exit_code": self.exit_code,
        }

    def format_human(self) -> str:
        """Return the established human-readable diagnostic."""

        message = str(self)
        if self.human_label is not None:
            message = f"{self.human_label}: {message}"
        if self.program is not None:
            message = f"{self.program}: error: {message}"
        if self.usage is not None:
            message = f"{self.usage.rstrip()}\n{message}"
        return message


class UsageError(ApplicationError):
    """Raised for invalid command-line syntax or option scope."""

    category = ErrorCategory.USAGE


class ConfigError(ApplicationError, ValueError):
    """Raised when configuration or runtime inputs are missing or invalid."""

    category = ErrorCategory.CONFIGURATION
    human_label = "Configuration error"


class PlanningError(ConfigError):
    """Raised when a requested pipeline plan cannot be resolved safely."""

    category = ErrorCategory.PLANNING
