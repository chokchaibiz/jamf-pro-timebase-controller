"""Calendar, path, and manual-override state for the TimeBase controller."""
from __future__ import annotations

import csv
import hashlib
import json
import os
from datetime import date, datetime, time as dtime
from pathlib import Path
from typing import Dict, Optional, Set, Tuple

from .types import ConfigError, ControllerError


class ControllerStateMixin:
    def holiday_map(self) -> Dict[date, str]:
        path = Path(self.cfg["paths"]["holiday_file"])
        if not path.exists():
            raise ConfigError(f"Holiday file not found: {path}")
        result: Dict[date, str] = {}
        with path.open(newline="", encoding="utf-8-sig") as fh:
            reader = csv.DictReader(fh)
            if not reader.fieldnames or "date" not in [x.strip().lower() for x in reader.fieldnames]:
                raise ConfigError("holidays.csv must contain a 'date' column")
            for row in reader:
                raw_date = (row.get("date") or row.get("Date") or "").strip()
                if not raw_date:
                    continue
                try:
                    d = date.fromisoformat(raw_date)
                except ValueError as exc:
                    raise ConfigError(f"Invalid holiday date: {raw_date}") from exc
                result[d] = (row.get("description") or row.get("Description") or "").strip()
        return result

    def is_school_day(self, d: Optional[date] = None) -> Tuple[bool, str]:
        d = d or self.now().date()
        if d.weekday() >= 5:
            return False, "weekend"
        holidays = self.holiday_map()
        if d in holidays:
            return False, f"holiday: {holidays[d]}".rstrip()
        return True, "school day"

    def attendance_path(self, d: Optional[date] = None) -> Path:
        d = d or self.now().date()
        return Path(self.cfg["paths"]["attendance_dir"]) / f"absent-{d.isoformat()}.csv"

    def attendance_marker_path(self, d: Optional[date] = None) -> Path:
        d = d or self.now().date()
        return Path(self.cfg["paths"]["state_dir"]) / f"attendance-{d.isoformat()}.ok.json"

    def attendance_resolution_path(self, d: Optional[date] = None) -> Path:
        d = d or self.now().date()
        return Path(self.cfg["paths"]["state_dir"]) / f"attendance-{d.isoformat()}.resolution.json"

    def manual_override_path(self) -> Path:
        return Path(
            self.cfg["paths"].get(
                "manual_override_file",
                "/var/lib/harrow-timebase/shared/manual-overrides.json",
            )
        )

    def _load_manual_override_state(self) -> dict:
        path = self.manual_override_path()
        if not path.exists():
            return {"version": 1, "overrides": {}}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ConfigError(f"Invalid manual override state {path}: {exc}") from exc
        if not isinstance(data, dict):
            raise ConfigError(f"Invalid manual override state {path}: expected JSON object")
        overrides = data.get("overrides", {})
        if not isinstance(overrides, dict):
            raise ConfigError(f"Invalid manual override state {path}: overrides must be an object")
        data.setdefault("version", 1)
        data["overrides"] = overrides
        return data

    def _save_manual_override_state(self, data: dict) -> None:
        path = self.manual_override_path()
        if self.dry_run:
            self.logger.info("DRY-RUN would write manual override state %s", path)
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.chmod(tmp, 0o640)
        os.replace(tmp, path)

    def active_manual_overrides(self, master: Optional[Set[str]] = None) -> Dict[str, dict]:
        state = self._load_manual_override_state()
        current = self.now()
        result: Dict[str, dict] = {}
        for raw_serial, record in state.get("overrides", {}).items():
            serial = str(raw_serial).strip().upper()
            if not serial or not isinstance(record, dict):
                continue
            try:
                expires_at = datetime.fromisoformat(str(record.get("expires_at", "")))
            except ValueError:
                self.logger.warning("Ignoring manual override with invalid expires_at: %s", serial)
                continue
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=self.tz)
            if current >= expires_at.astimezone(self.tz):
                continue
            if master is not None and serial not in master:
                self.logger.warning("Ignoring manual override outside master group: %s", serial)
                continue
            result[serial] = record
        return result

    def set_manual_override(
        self, serial: str, *, email_address: str = "", username: str = "", device_name: str = "",
        submitted_by: str = "", reason: str = ""
    ) -> dict:
        serial = serial.strip().upper()
        school_day, school_reason = self.is_school_day()
        current = self.now()
        local_time = current.time().replace(tzinfo=None)
        if not school_day:
            raise ControllerError(f"Manual Out-Harrow override is not needed because today is {school_reason}")
        if not (dtime(7, 0) <= local_time < dtime(16, 0)):
            raise ControllerError("Manual Out-Harrow override is allowed only from 07:00 until before 16:00")
        master = self.master_members()
        if serial not in master:
            raise ControllerError(f"Device {serial} is not a member of {self.group_names['master']}")
        expires_at = datetime.combine(current.date(), dtime(16, 0), tzinfo=self.tz)
        state = self._load_manual_override_state()
        record = {
            "serial_number": serial,
            "email_address": email_address.strip().lower(),
            "username": username.strip(),
            "device_name": device_name.strip(),
            "submitted_by": submitted_by.strip(),
            "reason": reason.strip(),
            "created_at": current.isoformat(),
            "expires_at": expires_at.isoformat(),
        }
        state.setdefault("overrides", {})[serial] = record
        state["updated_at"] = current.isoformat()
        self._save_manual_override_state(state)
        self.logger.info(
            "Manual Out-Harrow override saved serial=%s email=%s username=%s expires=%s by=%s",
            serial, email_address, username, expires_at.isoformat(), submitted_by,
        )
        return record

    def clear_manual_override(self, serial: str) -> bool:
        serial = serial.strip().upper()
        state = self._load_manual_override_state()
        existed = serial in state.get("overrides", {})
        if existed:
            state["overrides"].pop(serial, None)
            state["updated_at"] = self.now().isoformat()
            self._save_manual_override_state(state)
            self.logger.info("Manual Out-Harrow override cleared serial=%s", serial)
        return existed

    def purge_expired_manual_overrides(self, *, clear_all: bool = False) -> int:
        state = self._load_manual_override_state()
        overrides = state.get("overrides", {})
        if not overrides:
            return 0
        current = self.now()
        kept = {}
        removed = 0
        for raw_serial, record in overrides.items():
            if clear_all:
                removed += 1
                continue
            try:
                expires_at = datetime.fromisoformat(str(record.get("expires_at", "")))
                if expires_at.tzinfo is None:
                    expires_at = expires_at.replace(tzinfo=self.tz)
            except (ValueError, AttributeError):
                removed += 1
                continue
            if current >= expires_at.astimezone(self.tz):
                removed += 1
            else:
                kept[raw_serial] = record
        if removed:
            state["overrides"] = kept
            state["updated_at"] = current.isoformat()
            self._save_manual_override_state(state)
            self.logger.info("Purged %d manual override(s)", removed)
        return removed

    @staticmethod
    def file_sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
