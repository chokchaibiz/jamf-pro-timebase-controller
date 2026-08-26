#!/usr/bin/env python3
"""Shared validation and normalization helpers for Harrow holiday calendar uploads."""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Sequence

DATE_HEADER_ALIASES = {"date", "holiday_date", "holidaydate"}
DESCRIPTION_HEADER_ALIASES = {"description", "holiday", "holiday_name", "holidayname", "name", "reason"}
DATE_FORMATS = ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y")


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

    @property
    def holiday_count(self) -> int:
        return len(self.entries)


def _normalized_header(name: str) -> str:
    return name.strip().lower().replace(" ", "_").replace("-", "_")


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
    date_key = next((normalized[x] for x in DATE_HEADER_ALIASES if x in normalized), None)
    if not date_key:
        raise HolidayCSVError("Holiday CSV must contain a 'date' or 'Holiday Date' column")

    description_key = next((normalized[x] for x in DESCRIPTION_HEADER_ALIASES if x in normalized), None)
    entries = []
    seen_dates = set()
    rows_seen = 0

    for row_number, row in enumerate(reader, start=2):
        rows_seen += 1
        raw_date = (row.get(date_key) or "").strip()
        description = (row.get(description_key) or "").strip() if description_key else ""

        # Ignore completely empty rows but reject partially populated rows with no date.
        if not raw_date and not description and not any((v or "").strip() for v in row.values()):
            continue
        if not raw_date:
            raise HolidayCSVError(f"Row {row_number} has no holiday date")

        d = _parse_date(raw_date)
        if d in seen_dates:
            raise HolidayCSVError(f"Duplicate holiday date: {d.isoformat()}")
        seen_dates.add(d)
        entries.append(HolidayEntry(d, description))

    if not entries:
        raise HolidayCSVError("Holiday CSV contains no holiday dates")

    entries.sort(key=lambda x: x.holiday_date)
    return ParsedHolidays(entries=tuple(entries), rows_seen=rows_seen)


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
