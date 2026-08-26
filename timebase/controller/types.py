"""Shared controller exceptions and value objects.

This module deliberately has no application dependencies so controller mixins,
the CLI facade, and tests can import the same types without circular imports.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Set


EXIT_OK = 0
EXIT_CONFIG = 2
EXIT_PREFLIGHT = 3
EXIT_API = 4
EXIT_VERIFY = 5
EXIT_ATTENDANCE = 6
EXIT_LOCK = 75


class ControllerError(RuntimeError):
    exit_code = EXIT_API


class ConfigError(ControllerError):
    exit_code = EXIT_CONFIG


class PreflightError(ControllerError):
    exit_code = EXIT_PREFLIGHT


class VerificationError(ControllerError):
    exit_code = EXIT_VERIFY


class AttendanceError(ControllerError):
    exit_code = EXIT_ATTENDANCE


@dataclass(frozen=True)
class GroupInfo:
    id: int
    name: str
    is_smart: bool


@dataclass
class BatchResult:
    requested: int
    succeeded: Set[str]
    failed: Dict[str, str]
