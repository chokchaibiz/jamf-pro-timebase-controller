"""Filesystem operations shared by attendance importer job handlers."""
from __future__ import annotations

import json
import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import Iterable, Sequence

from attendance_common import normalized_csv
from holiday_common import HolidayEntry, normalized_holiday_csv


def atomic_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o640)
    os.replace(tmp, path)


def append_audit(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def archive_attendance(final_path: Path, archive_root: Path, stamp: datetime) -> str:
    if not final_path.exists():
        return ""
    iso = final_path.stem.removeprefix("absent-")
    yyyy, mm, _ = iso.split("-")
    dest_dir = archive_root / yyyy / mm
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{final_path.stem}_{stamp.strftime('%H%M%S')}.csv"
    counter = 1
    while dest.exists():
        dest = dest_dir / f"{final_path.stem}_{stamp.strftime('%H%M%S')}_{counter}.csv"
        counter += 1
    shutil.copy2(final_path, dest)
    return str(dest)


def write_attendance(final_path: Path, emails: Iterable[str]) -> None:
    final_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = final_path.with_name(f".{final_path.name}.{os.getpid()}.tmp")
    tmp.write_text(normalized_csv(emails), encoding="utf-8")
    os.chmod(tmp, 0o640)
    os.replace(tmp, final_path)


def archive_holidays(final_path: Path, archive_root: Path, stamp: datetime) -> str:
    if not final_path.exists():
        return ""
    dest_dir = archive_root / "holidays" / stamp.strftime("%Y")
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"holidays_{stamp.strftime('%Y%m%d_%H%M%S')}.csv"
    counter = 1
    while dest.exists():
        dest = dest_dir / f"holidays_{stamp.strftime('%Y%m%d_%H%M%S')}_{counter}.csv"
        counter += 1
    shutil.copy2(final_path, dest)
    return str(dest)


def write_holidays(final_path: Path, entries: Sequence[HolidayEntry]) -> None:
    final_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = final_path.with_name(f".{final_path.name}.{os.getpid()}.tmp")
    tmp.write_text(normalized_holiday_csv(entries), encoding="utf-8")
    os.chmod(tmp, 0o640)
    os.replace(tmp, final_path)
