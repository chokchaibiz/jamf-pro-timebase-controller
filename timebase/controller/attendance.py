"""Jamf inventory resolution and attendance state for the controller."""
from __future__ import annotations

import csv
import hashlib
import json
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple
from urllib.parse import quote

from .types import AttendanceError, ConfigError, ControllerError


class ControllerAttendanceMixin:
    @staticmethod
    def _inventory_dict(value) -> dict:
        return value if isinstance(value, dict) else {}

    def _classic_location_for_serial(self, serial: str) -> Tuple[str, str, str]:
        """Return (email, username, room) from Classic Mobile Device Location subset."""
        root = self.jamf.classic_xml(
            "GET",
            f"/JSSResource/mobiledevices/serialnumber/{quote(serial, safe='')}/subset/Location",
            expected=(200,),
        )
        location = root.find(".//location")
        if location is None:
            return "", "", ""
        email = (
            location.findtext("email_address")
            or location.findtext("email")
            or ""
        ).strip().lower()
        username = (location.findtext("username") or "").strip()
        room = (location.findtext("room") or "").strip()
        return email, username, room

    def _master_inventory_email_index_classic(
        self,
        master: Set[str],
        *,
        serials: Optional[Set[str]] = None,
    ) -> Tuple[Dict[str, Set[str]], Set[str], Dict[str, str]]:
        """Build/complete Email -> Serial mapping through Classic API Location subsets."""
        targets = set(serials if serials is not None else master)
        targets.intersection_update(master)
        if not targets:
            return {}, set(), {}

        workers = int(
            self.cfg.get("attendance", {}).get(
                "classic_fallback_concurrency",
                self.cfg.get("performance", {}).get("concurrency", 4),
            )
        )
        workers = min(max(workers, 1), 5)

        index: Dict[str, Set[str]] = {}
        seen: Set[str] = set()
        failures: Dict[str, str] = {}

        def fetch(serial: str) -> Tuple[str, str]:
            email, _username, _room = self._classic_location_for_serial(serial)
            return serial, email

        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="classic-email") as executor:
            futures = {executor.submit(fetch, serial): serial for serial in sorted(targets)}
            for future in as_completed(futures):
                serial = futures[future]
                try:
                    resolved_serial, email = future.result()
                except Exception as exc:
                    failures[serial] = str(exc)
                    continue
                seen.add(resolved_serial)
                if email:
                    index.setdefault(email, set()).add(resolved_serial)

        if failures:
            sample = "; ".join(
                f"{serial}: {message}" for serial, message in list(sorted(failures.items()))[:10]
            )
            self.logger.warning(
                "Classic Email fallback had %d failed device lookup(s). Sample: %s",
                len(failures),
                sample,
            )

        self.logger.info(
            "Classic Email fallback complete: requested=%d read=%d emails=%d failures=%d concurrency=%d",
            len(targets), len(seen), len(index), len(failures), workers,
        )
        return index, seen, failures

    def _master_inventory_email_index(self, master: Set[str]) -> Dict[str, Set[str]]:
        """Build an email -> serial(s) index for Harrow-All-iPads."""
        if not master:
            raise AttendanceError("Master iPad group is empty; cannot resolve attendance emails")

        attendance_cfg = self.cfg.get("attendance", {})
        page_size = int(attendance_cfg.get("inventory_page_size", 100))
        page_size = min(max(page_size, 25), 200)
        group_id = self.master_group().id
        index: Dict[str, Set[str]] = {}
        seen_master: Set[str] = set()
        serials_missing_email: Set[str] = set()
        page = 0
        max_pages = max(5, (len(master) // page_size) + 20)
        modern_error: Optional[str] = None

        try:
            while page < max_pages:
                params = [
                    ("page", page),
                    ("page-size", page_size),
                    ("sort", "serialNumber:asc"),
                    ("filter", f"groupId=={group_id}"),
                    ("section", "GENERAL"),
                    ("section", "USER_AND_LOCATION"),
                    ("exception-handling", "LENIENT"),
                ]
                response = self.jamf.request(
                    "GET", "/api/v2/mobile-devices/detail", params=params, expected=(200,)
                )
                payload = response.json()
                results = payload.get("results", []) if isinstance(payload, dict) else []
                if not isinstance(results, list):
                    raise AttendanceError("Jamf mobile inventory response did not contain a results list")

                for item in results:
                    if not isinstance(item, dict):
                        continue
                    general = self._inventory_dict(item.get("general"))
                    user_loc = self._inventory_dict(item.get("userAndLocation"))
                    if not user_loc:
                        user_loc = self._inventory_dict(item.get("location"))
                    serial = str(
                        general.get("serialNumber") or item.get("serialNumber") or ""
                    ).strip().upper()
                    if not serial or serial not in master:
                        continue
                    seen_master.add(serial)
                    email = str(
                        user_loc.get("emailAddress") or item.get("emailAddress") or ""
                    ).strip().lower()
                    if email:
                        index.setdefault(email, set()).add(serial)
                    else:
                        serials_missing_email.add(serial)

                total_count = payload.get("totalCount") if isinstance(payload, dict) else None
                if len(results) < page_size:
                    break
                if isinstance(total_count, int) and (page + 1) * page_size >= total_count:
                    break
                page += 1
            else:
                raise AttendanceError("Jamf inventory pagination exceeded the configured safety bound")
        except (ControllerError, AttendanceError, ValueError, KeyError) as exc:
            modern_error = str(exc)
            self.logger.warning(
                "Jamf Pro v2 Email inventory path failed; attempting Classic fallback: %s",
                modern_error,
            )

        minimum_coverage = float(self.cfg["safety"].get("email_inventory_min_coverage", 0.95))
        missing_serials = master - seen_master
        fallback_targets = set(missing_serials) | set(serials_missing_email)

        classic_enabled = bool(attendance_cfg.get("classic_email_fallback_enabled", True))
        fallback_used = False
        if classic_enabled and (modern_error is not None or fallback_targets):
            fallback_used = True
            targets = master if modern_error is not None else fallback_targets
            self.logger.warning(
                "Using Classic API Email fallback for %d/%d master iPads "
                "(modern_seen=%d, missing_serial=%d, missing_email=%d)",
                len(targets), len(master), len(seen_master), len(missing_serials),
                len(serials_missing_email),
            )
            classic_index, classic_seen, _classic_failures = (
                self._master_inventory_email_index_classic(master, serials=targets)
            )
            for email, serials_for_email in classic_index.items():
                index.setdefault(email, set()).update(serials_for_email)
            seen_master.update(classic_seen)

        coverage = len(seen_master) / len(master)
        if coverage < minimum_coverage:
            raise AttendanceError(
                f"Jamf inventory coverage too low for safe email resolution after fallback: "
                f"{len(seen_master)}/{len(master)} ({coverage:.1%}), "
                f"required >= {minimum_coverage:.1%}"
            )

        self.logger.info(
            "Built attendance email index: master=%d inventory_seen=%d emails=%d "
            "modern_pages=%d classic_fallback=%s",
            len(master), len(seen_master), len(index), page + 1,
            "yes" if fallback_used else "no",
        )
        return index

    def resolve_absent_emails(
        self, master: Set[str], emails: Iterable[str]
    ) -> Tuple[Set[str], List[str], Dict[str, List[str]]]:
        wanted = []
        seen = set()
        for value in emails:
            email = str(value).strip().lower()
            if email and email not in seen:
                seen.add(email)
                wanted.append(email)
        if not wanted:
            return set(), [], {}

        index = self._master_inventory_email_index(master)
        policy = str(self.cfg.get("attendance", {}).get("email_match_policy", "unique")).strip().lower()
        if policy not in {"unique", "all_matches"}:
            raise ConfigError("attendance.email_match_policy must be 'unique' or 'all_matches'")

        serials: Set[str] = set()
        unresolved: List[str] = []
        ambiguous: Dict[str, List[str]] = {}
        for email in wanted:
            matches = sorted(index.get(email, set()))
            if not matches:
                unresolved.append(email)
                continue
            if len(matches) > 1 and policy == "unique":
                ambiguous[email] = matches
                continue
            serials.update(matches)
        return serials, unresolved, ambiguous

    def load_attendance_resolution_cache(
        self, master: Set[str], path: Path, sha256: str, d: Optional[date] = None
    ) -> Optional[Set[str]]:
        cache_path = self.attendance_resolution_path(d)
        if not cache_path.exists():
            return None
        try:
            data = json.loads(cache_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if data.get("attendance_file") != str(path) or data.get("sha256") != sha256:
            return None
        raw_serials = data.get("resolved_serials", [])
        if not isinstance(raw_serials, list):
            return None
        serials = {str(x).strip().upper() for x in raw_serials if str(x).strip()}
        if not serials.issubset(master):
            self.logger.warning("Ignoring attendance resolution cache containing device(s) outside master group")
            return None
        self.logger.info(
            "Using cached Email -> iPad attendance resolution: %s resolved_devices=%d",
            cache_path, len(serials),
        )
        return serials

    def write_attendance_resolution_cache(
        self, path: Path, sha256: str, emails: Iterable[str], serials: Iterable[str], d: Optional[date] = None
    ) -> None:
        cache_path = self.attendance_resolution_path(d)
        if self.dry_run:
            self.logger.info("DRY-RUN would write attendance resolution cache %s", cache_path)
            return
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "date": (d or self.now().date()).isoformat(),
            "attendance_file": str(path),
            "sha256": sha256,
            "identity_field": "email_address",
            "emails": sorted({str(x).strip().lower() for x in emails if str(x).strip()}),
            "resolved_serials": sorted({str(x).strip().upper() for x in serials if str(x).strip()}),
            "resolved_at": self.now().isoformat(),
        }
        tmp = cache_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.chmod(tmp, 0o640)
        os.replace(tmp, cache_path)
        self.logger.info("Attendance Email resolution cache updated: %s", cache_path)

    def read_absent_serials(self, master: Set[str], d: Optional[date] = None) -> Tuple[Set[str], Path, str]:
        d = d or self.now().date()
        path = self.attendance_path(d)
        if not path.exists():
            policy = self.cfg["safety"].get("missing_attendance_policy", "error")
            if policy == "zero_absent":
                synthetic_sha256 = hashlib.sha256(
                    f"MISSING_ATTENDANCE_ZERO_ABSENT:{d.isoformat()}".encode("utf-8")
                ).hexdigest()
                self.logger.warning(
                    "Attendance CSV not found: %s; policy=zero_absent -> absent=0 and all %d master iPads are treated as present (Room=%s / In-Harrow)",
                    path, len(master), self.rooms["in_harrow"],
                )
                return set(), path, synthetic_sha256
            raise AttendanceError(
                f"Attendance confirmation file is missing: {path}. "
                "Set safety.missing_attendance_policy=zero_absent to treat a missing file as zero absences."
            )

        with path.open(newline="", encoding="utf-8-sig") as fh:
            reader = csv.DictReader(fh)
            if not reader.fieldnames:
                raise AttendanceError(f"Attendance CSV has no header: {path}")
            normalized = {
                name.strip().lower().replace(" ", "_").replace("-", "_"): name
                for name in reader.fieldnames if name is not None
            }
            key = None
            for candidate in (
                "email_address", "emailaddress", "email", "e_mail", "user_email", "student_email"
            ):
                if candidate in normalized:
                    key = normalized[candidate]
                    break
            if not key:
                raise AttendanceError(
                    f"Attendance CSV must contain an Email Address column; got {reader.fieldnames}"
                )
            emails: List[str] = []
            seen_emails: Set[str] = set()
            for row_no, row in enumerate(reader, start=2):
                email = (row.get(key) or "").strip().lower()
                if not email:
                    continue
                if "@" not in email:
                    raise AttendanceError(f"Invalid attendance email at row {row_no}: {email}")
                if email in seen_emails:
                    self.logger.warning("Duplicate attendance email ignored at row %d: %s", row_no, email)
                    continue
                seen_emails.add(email)
                emails.append(email)

        sha256 = self.file_sha256(path)
        cached_serials = self.load_attendance_resolution_cache(master, path, sha256, d)
        if cached_serials is not None:
            fraction = (len(cached_serials) / len(master)) if master else 1.0
            max_fraction = float(self.cfg["safety"]["max_absent_fraction"])
            if fraction > max_fraction:
                raise AttendanceError(
                    f"Cached absent device count {len(cached_serials)}/{len(master)} ({fraction:.1%}) exceeds safety limit {max_fraction:.1%}"
                )
            return cached_serials, path, sha256

        serials, unresolved, ambiguous = self.resolve_absent_emails(master, emails)
        if unresolved or ambiguous:
            parts = []
            if unresolved:
                parts.append(f"email(s) not found in {self.group_names['master']}: {unresolved[:20]}")
            if ambiguous:
                preview = {k: v for k, v in list(ambiguous.items())[:10]}
                parts.append(f"email(s) matched multiple master iPads: {preview}")
            raise AttendanceError("Attendance email resolution failed: " + "; ".join(parts))

        fraction = (len(serials) / len(master)) if master else 1.0
        max_fraction = float(self.cfg["safety"]["max_absent_fraction"])
        if fraction > max_fraction:
            raise AttendanceError(
                f"Absent device count {len(serials)}/{len(master)} ({fraction:.1%}) exceeds safety limit {max_fraction:.1%}"
            )
        self.logger.info(
            "Attendance email resolution complete: absent_emails=%d resolved_devices=%d",
            len(emails), len(serials),
        )
        self.write_attendance_resolution_cache(path, sha256, emails, serials, d)
        return serials, path, sha256

    def write_attendance_marker(self, path: Path, sha256: str, absent_count: int) -> None:
        marker = self.attendance_marker_path()
        if self.dry_run:
            self.logger.info("DRY-RUN would write attendance marker %s", marker)
            return
        marker.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "date": self.now().date().isoformat(),
            "attendance_file": str(path),
            "sha256": sha256,
            "absent_count": absent_count,
            "verified_at": self.now().isoformat(),
        }
        tmp = marker.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, marker)

    def attendance_marker_valid(self, path: Path, sha256: str) -> bool:
        marker = self.attendance_marker_path()
        if not marker.exists():
            return False
        try:
            data = json.loads(marker.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return False
        return data.get("sha256") == sha256 and data.get("attendance_file") == str(path)
