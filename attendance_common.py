#!/usr/bin/env python3
"""Shared CSV parsing/normalization helpers for Harrow attendance uploads.

Attendance files use the student's Jamf inventory email address as the identity key.
The privileged controller resolves those emails to iPad serial numbers at processing time.
"""
from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

EMAIL_HEADER_ALIASES = {
    "email_address",
    "emailaddress",
    "email",
    "e_mail",
    "user_email",
    "student_email",
}
EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


class AttendanceCSVError(ValueError):
    pass


@dataclass(frozen=True)
class ParsedAttendance:
    emails: Sequence[str]
    rows_seen: int
    duplicate_count: int
    invalid_emails: Sequence[str]

    @property
    def absent_count(self) -> int:
        return len(self.emails)


def _normalized_header(name: str) -> str:
    return name.strip().lower().replace(" ", "_").replace("-", "_")


def _normalize_email(value: str) -> str:
    # Jamf/IdP email identifiers are matched case-insensitively for this workflow.
    return value.strip().lower()


def parse_csv_text(text: str) -> ParsedAttendance:
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise AttendanceCSVError("CSV has no header row")

    normalized = {_normalized_header(name): name for name in reader.fieldnames if name is not None}
    key = next((normalized[candidate] for candidate in EMAIL_HEADER_ALIASES if candidate in normalized), None)
    if not key:
        raise AttendanceCSVError(
            "CSV must contain an Email Address column. Supported headers: "
            "email_address, Email Address, Email, emailaddress, user_email, student_email"
        )

    ordered: list[str] = []
    seen: set[str] = set()
    duplicates = 0
    rows_seen = 0
    invalid: list[str] = []

    for row_no, row in enumerate(reader, start=2):
        rows_seen += 1
        raw = (row.get(key) or "").strip()
        if not raw:
            continue
        email = _normalize_email(raw)
        if not EMAIL_RE.match(email):
            invalid.append(f"row {row_no}: {raw}")
            continue
        if email in seen:
            duplicates += 1
            continue
        seen.add(email)
        ordered.append(email)

    if invalid:
        preview = ", ".join(invalid[:20])
        suffix = " ..." if len(invalid) > 20 else ""
        raise AttendanceCSVError(
            f"Found {len(invalid)} invalid email address(es): {preview}{suffix}"
        )

    return ParsedAttendance(
        emails=tuple(ordered),
        rows_seen=rows_seen,
        duplicate_count=duplicates,
        invalid_emails=tuple(),
    )


def parse_csv_bytes(data: bytes) -> ParsedAttendance:
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise AttendanceCSVError("CSV must be UTF-8/UTF-8-BOM encoded") from exc
    return parse_csv_text(text)


def parse_csv_path(path: Path) -> ParsedAttendance:
    return parse_csv_bytes(path.read_bytes())


def normalized_csv(emails: Iterable[str]) -> str:
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(["email_address"])
    for email in emails:
        normalized = _normalize_email(email)
        if normalized:
            writer.writerow([normalized])
    return output.getvalue()
