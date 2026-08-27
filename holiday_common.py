#!/usr/bin/env python3
"""Shared validation and normalization helpers for Harrow holiday calendar uploads."""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional, Sequence

DATE_HEADER_ALIASES = (
    "date",
    "start_date",
    "holiday_date",
    "holiday_start_date",
    "holidaydate",
)
END_DATE_HEADER_ALIASES = (
    "end_date",
    "holiday_end_date",
    "holiday_end",
    "enddate",
)
DESCRIPTION_HEADER_ALIASES = (
    "description",
    "holiday",
    "holiday_name",
    "holidayname",
    "name",
    "reason",
)
DATE_FORMATS = ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y")
MAX_RANGE_DAYS = 370
MAX_EXPANDED_HOLIDAY_DATES = 2000


class HolidayCSVError(ValueError):
    pass


@dataclass(frozen=True)
class HolidayEntry:
    holiday_date: date
    description: str


@dataclass(frozen=True)
class ParsedHolidays:
    entries: Sequence[HolidayEntry]
    rows_seen: int
    range_count: int

    @property
    def holiday_count(self) -> int:
        return len(self.entries)


def _normalized_header(name: str) -> str:
    return name.strip().lower().replace(" ", "_").replace("-", "_")


def _first_header(normalized: dict[str, str], aliases: Sequence[str]) -> Optional[str]:
    return next((normalized[name] for name in aliases if name in normalized), None)


def _has_row_content(row: dict) -> bool:
    for value in row.values():
        if isinstance(value, list):
            if any(str(item or "").strip() for item in value):
                return True
        elif str(value or "").strip():
            return True
    return False


def _parse_date(value: str) -> date:
    raw = value.strip()
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            pass
    raise HolidayCSVError(
        f"Invalid holiday date '{raw}'. Supported formats: YYYY-MM-DD, DD/MM/YYYY, DD-MM-YYYY"
    )


def parse_holiday_csv_text(text: str) -> ParsedHolidays:
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise HolidayCSVError("Holiday CSV has no header row")

    normalized = {_normalized_header(name): name for name in reader.fieldnames if name is not None}
    date_key = _first_header(normalized, DATE_HEADER_ALIASES)
    if not date_key:
        raise HolidayCSVError(
            "Holiday CSV must contain a 'date', 'start_date', or 'Holiday Date' column"
        )

    end_date_key = _first_header(normalized, END_DATE_HEADER_ALIASES)
    description_key = _first_header(normalized, DESCRIPTION_HEADER_ALIASES)
    entries_by_date: dict[date, HolidayEntry] = {}
    source_row_by_date: dict[date, int] = {}
    rows_seen = 0
    range_count = 0

    for row_number, row in enumerate(reader, start=2):
        rows_seen += 1
        raw_date = (row.get(date_key) or "").strip()
        raw_end_date = (row.get(end_date_key) or "").strip() if end_date_key else ""
        description = (row.get(description_key) or "").strip() if description_key else ""

        # Ignore completely empty rows but reject partially populated rows with no date.
        if not raw_date and not raw_end_date and not description and not _has_row_content(row):
            continue
        if not raw_date:
            raise HolidayCSVError(f"Row {row_number} has no holiday start date")

        try:
            start_date = _parse_date(raw_date)
        except HolidayCSVError as exc:
            raise HolidayCSVError(f"Row {row_number}: {exc}") from exc
        try:
            end_date = _parse_date(raw_end_date) if raw_end_date else start_date
        except HolidayCSVError as exc:
            raise HolidayCSVError(f"Row {row_number} end_date: {exc}") from exc

        if end_date < start_date:
            raise HolidayCSVError(
                f"Row {row_number} end_date {end_date.isoformat()} is before "
                f"start date {start_date.isoformat()}"
            )
        span_days = (end_date - start_date).days + 1
        if span_days > MAX_RANGE_DAYS:
            raise HolidayCSVError(
                f"Row {row_number} holiday range contains {span_days} days; "
                f"maximum allowed is {MAX_RANGE_DAYS}"
            )
        if end_date > start_date:
            range_count += 1

        for offset in range(span_days):
            holiday_date = start_date + timedelta(days=offset)
            if holiday_date in entries_by_date:
                previous_row = source_row_by_date[holiday_date]
                raise HolidayCSVError(
                    f"Holiday date {holiday_date.isoformat()} from row {row_number} "
                    f"overlaps row {previous_row}"
                )
            if len(entries_by_date) >= MAX_EXPANDED_HOLIDAY_DATES:
                raise HolidayCSVError(
                    f"Expanded holiday calendar exceeds {MAX_EXPANDED_HOLIDAY_DATES} dates"
                )
            entries_by_date[holiday_date] = HolidayEntry(holiday_date, description)
            source_row_by_date[holiday_date] = row_number

    if not entries_by_date:
        raise HolidayCSVError("Holiday CSV contains no holiday dates")

    entries = tuple(entries_by_date[d] for d in sorted(entries_by_date))
    return ParsedHolidays(entries=entries, rows_seen=rows_seen, range_count=range_count)


def parse_holiday_csv_bytes(data: bytes) -> ParsedHolidays:
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise HolidayCSVError("Holiday CSV must be UTF-8/UTF-8-BOM encoded") from exc
    return parse_holiday_csv_text(text)


def parse_holiday_csv_path(path: Path) -> ParsedHolidays:
    return parse_holiday_csv_bytes(path.read_bytes())


def normalized_holiday_csv(entries: Sequence[HolidayEntry]) -> str:
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(["date", "description"])
    for entry in sorted(entries, key=lambda x: x.holiday_date):
        writer.writerow([entry.holiday_date.isoformat(), entry.description])
    return output.getvalue()
