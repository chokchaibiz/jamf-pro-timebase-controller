#!/usr/bin/env python3
"""Behavior checks for backward-compatible holiday range uploads."""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from holiday_common import (
    MAX_RANGE_DAYS,
    HolidayCSVError,
    normalized_holiday_csv,
    parse_holiday_csv_bytes,
    parse_holiday_csv_text,
)
from timebase.controller.state import ControllerStateMixin


def expect_error(csv_text: str, expected: str) -> None:
    try:
        parse_holiday_csv_text(csv_text)
    except HolidayCSVError as exc:
        assert expected in str(exc), (expected, str(exc))
    else:
        raise AssertionError(f"Expected HolidayCSVError containing {expected!r}")


# Existing files remain valid and keep their original single-day behavior.
legacy = parse_holiday_csv_text(
    "date,description\n"
    "2026-10-13,Annual Holiday\n"
    "23/10/2026,Annual Holiday\n"
)
assert legacy.holiday_count == 2
assert legacy.range_count == 0

# end_date is inclusive; an empty end_date remains a single-day holiday.
ranged = parse_holiday_csv_bytes(
    (
        "\ufeffdate,end_date,description\r\n"
        "19/10/2026,23-10-2026,Midterm Break\r\n"
        "2026-12-10,,Constitution Day\r\n"
    ).encode("utf-8")
)
assert ranged.range_count == 1
assert ranged.holiday_count == 6
assert [entry.holiday_date for entry in ranged.entries[:5]] == [
    date(2026, 10, 19),
    date(2026, 10, 20),
    date(2026, 10, 21),
    date(2026, 10, 22),
    date(2026, 10, 23),
]

# start_date is accepted as a more explicit alias.
explicit = parse_holiday_csv_text(
    "start_date,end_date,description\n2027-01-04,2027-01-05,Staff Training\n"
)
assert explicit.holiday_count == 2

# A range-format calendar also works when read directly by the controller, which
# covers first installation before a Portal upload has normalized the file.
with TemporaryDirectory() as raw_tmp:
    holiday_path = Path(raw_tmp) / "holidays.csv"
    holiday_path.write_text(
        "date,end_date,description\n2027-02-15,2027-02-19,Midterm Break\n",
        encoding="utf-8",
    )

    class TestControllerState(ControllerStateMixin):
        cfg = {"paths": {"holiday_file": str(holiday_path)}}

    holiday_map = TestControllerState().holiday_map()
    assert len(holiday_map) == 5
    assert holiday_map[date(2027, 2, 15)] == "Midterm Break"
    assert holiday_map[date(2027, 2, 19)] == "Midterm Break"

# Canonical storage deliberately remains one date per row for controller compatibility.
normalized = normalized_holiday_csv(ranged.entries)
assert normalized.startswith("date,description\n")
assert "end_date" not in normalized
assert "2026-10-19,Midterm Break\n" in normalized
assert "2026-10-23,Midterm Break\n" in normalized

expect_error(
    "date,end_date,description\n2026-10-23,2026-10-19,Reverse Range\n",
    "is before start date",
)
expect_error(
    "date,end_date,description\n"
    "2026-10-19,2026-10-23,Midterm Break\n"
    "2026-10-23,,Public Holiday\n",
    "overlaps row 2",
)
expect_error(
    f"date,end_date,description\n2026-01-01,2027-01-06,Too Long\n",
    f"maximum allowed is {MAX_RANGE_DAYS}",
)

print("Holiday range checks: PASS")
