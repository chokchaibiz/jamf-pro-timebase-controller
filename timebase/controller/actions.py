"""Preflight, reconciliation, and scheduled controller actions."""
from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import time as dtime
from typing import Dict, Optional, Set, Tuple

from .types import (
    AttendanceError,
    BatchResult,
    ControllerError,
    GroupInfo,
    PreflightError,
    VerificationError,
)


class ControllerActionsMixin:
    def preflight(self) -> dict:
        self.logger.info("Starting preflight")
        catalog = self.groups(refresh=True)
        master = self.require_group(self.group_names["master"])
        in_group = self.require_group(self.group_names["in_harrow"])
        out_group = self.require_group(self.group_names["out_harrow"])
        if master.is_smart:
            raise PreflightError(f"{master.name} must be a Static Mobile Device Group")
        if not in_group.is_smart:
            raise PreflightError(f"{in_group.name} must be a Smart Mobile Device Group")
        if not out_group.is_smart:
            raise PreflightError(f"{out_group.name} must be a Smart Mobile Device Group")

        master_members = self.jamf.group_members(master.id)
        minimum = int(self.cfg["safety"]["min_master_devices"])
        maximum = int(self.cfg["safety"]["max_master_devices"])
        if not minimum <= len(master_members) <= maximum:
            raise PreflightError(
                f"Master group count {len(master_members)} is outside safety range {minimum}-{maximum}"
            )
        self.write_master_cache(master_members)

        self._assert_room_criterion(in_group, self.rooms["in_harrow"])
        self._assert_room_criterion(out_group, self.rooms["out_harrow"])

        profiles = self.profiles(refresh=True)
        assure_id = self.require_profile(self.profile_names["assure"])
        wifi_id = self.require_profile(self.profile_names["wifi"])
        assure_xml = self.jamf.get_profile_xml(assure_id)
        assure_targets = self.jamf.profile_target_groups(assure_xml)
        assure_exclusions = self.jamf.profile_exclusion_groups(assure_xml)
        if in_group.name not in assure_targets:
            raise PreflightError(f"ASSURE must target Smart Group '{in_group.name}'")
        if out_group.name not in assure_exclusions:
            raise PreflightError(f"ASSURE must exclude Smart Group '{out_group.name}'")

        wifi_xml = self.jamf.get_profile_xml(wifi_id)
        scope = wifi_xml.find("scope")
        if scope is None:
            raise PreflightError("WiFi-Harrow has no scope")
        all_mobile = (scope.findtext("all_mobile_devices") or "false").strip().lower()
        if all_mobile == "true":
            raise PreflightError("WiFi-Harrow must not have all_mobile_devices=true")

        holidays = self.holiday_map()
        self.logger.info(
            "Preflight OK: master=%d groups=%d profiles=%d holidays=%d",
            len(master_members), len(catalog), len(profiles), len(holidays),
        )
        return {
            "master_count": len(master_members),
            "master_group_id": master.id,
            "in_group_id": in_group.id,
            "out_group_id": out_group.id,
            "assure_profile_id": assure_id,
            "wifi_profile_id": wifi_id,
            "holiday_count": len(holidays),
        }

    def _assert_room_criterion(self, group: GroupInfo, expected_room: str) -> None:
        root = self.jamf.get_group_xml(group.id)
        criteria = self.jamf.group_criteria(root)
        matches = [
            (name, search, value)
            for name, search, value in criteria
            if name.strip().lower() == "room" and value.strip() == str(expected_room)
        ]
        if not matches:
            raise PreflightError(
                f"Smart Group '{group.name}' does not contain Room={expected_room} criterion; criteria={criteria}"
            )

    def _batch_set_room(self, serials: Set[str], room: str) -> BatchResult:
        serials = {s.upper() for s in serials if s}
        if not serials:
            return BatchResult(0, set(), {})
        concurrency = int(self.cfg["performance"]["concurrency"])
        batch_rounds = int(self.cfg["performance"]["batch_retry_rounds"])
        pending = set(serials)
        succeeded: Set[str] = set()
        failed: Dict[str, str] = {}

        for round_no in range(batch_rounds + 1):
            if not pending:
                break
            if round_no:
                delay = min(5 * round_no, 15)
                self.logger.warning(
                    "Batch retry round %d for %d device(s) after %ds",
                    round_no, len(pending), delay,
                )
                time.sleep(delay)
            current = sorted(pending)
            pending = set()
            failed = {}
            with ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="room") as executor:
                futures = {
                    executor.submit(self.jamf.update_room, serial, room): serial
                    for serial in current
                }
                for future in as_completed(futures):
                    serial = futures[future]
                    try:
                        future.result()
                        succeeded.add(serial)
                    except Exception as exc:
                        failed[serial] = str(exc)
                        pending.add(serial)
                        self.logger.error("Room update failed %s -> %s: %s", serial, room, exc)
        return BatchResult(len(serials), succeeded, failed)

    def _validate_group_partition(
        self,
        master: Set[str],
        expected_in: Set[str],
        expected_out: Set[str],
        *,
        timeout: Optional[int] = None,
    ) -> None:
        timeout = timeout if timeout is not None else int(self.cfg["performance"]["verify_timeout_seconds"])
        interval = int(self.cfg["performance"]["verify_interval_seconds"])
        deadline = time.monotonic() + timeout
        last = None

        while True:
            actual_in_all, actual_out_all = self.current_in_out()
            actual_in = actual_in_all & master
            actual_out = actual_out_all & master
            extras_in = actual_in_all - master
            extras_out = actual_out_all - master
            missing_in = expected_in - actual_in
            unexpected_in = actual_in - expected_in
            missing_out = expected_out - actual_out
            unexpected_out = actual_out - expected_out
            overlap = actual_in & actual_out
            last = (missing_in, unexpected_in, missing_out, unexpected_out, overlap, extras_in, extras_out)

            if not any((missing_in, unexpected_in, missing_out, unexpected_out, overlap)):
                if self.cfg["safety"].get("strict_extra_group_members", False) and (extras_in or extras_out):
                    raise VerificationError(
                        f"Smart Groups contain non-master devices: In extra={len(extras_in)}, Out extra={len(extras_out)}"
                    )
                if extras_in or extras_out:
                    self.logger.warning(
                        "Smart Groups include non-master device(s): In=%d Out=%d",
                        len(extras_in), len(extras_out),
                    )
                self.logger.info(
                    "Verification OK: master=%d In=%d Out=%d",
                    len(master), len(actual_in), len(actual_out),
                )
                return

            if self.dry_run:
                self.logger.info(
                    "DRY-RUN verification skipped after observing current state: missing_in=%d unexpected_in=%d missing_out=%d unexpected_out=%d overlap=%d",
                    len(missing_in), len(unexpected_in), len(missing_out), len(unexpected_out), len(overlap),
                )
                return

            if time.monotonic() >= deadline:
                break
            self.logger.info(
                "Waiting for Smart Group recalculation: missing_in=%d unexpected_in=%d missing_out=%d unexpected_out=%d overlap=%d",
                len(missing_in), len(unexpected_in), len(missing_out), len(unexpected_out), len(overlap),
            )
            time.sleep(interval)

        missing_in, unexpected_in, missing_out, unexpected_out, overlap, _, _ = last
        raise VerificationError(
            "Smart Group verification timed out: "
            f"missing_in={len(missing_in)} sample={sorted(missing_in)[:20]}, "
            f"unexpected_in={len(unexpected_in)} sample={sorted(unexpected_in)[:20]}, "
            f"missing_out={len(missing_out)} sample={sorted(missing_out)[:20]}, "
            f"unexpected_out={len(unexpected_out)} sample={sorted(unexpected_out)[:20]}, "
            f"overlap={len(overlap)} sample={sorted(overlap)[:20]}"
        )

    def set_all_in(self) -> None:
        master = self.master_members()
        manual_out = set(self.active_manual_overrides(master))
        desired_in = master - manual_out
        desired_out = manual_out
        current_in, current_out = self.current_in_out()
        need_in = desired_in - current_in
        need_out = desired_out - current_out
        self.logger.info(
            "Set school-start state: master=%d manual_out=%d need_room100=%d need_room200=%d",
            len(master), len(manual_out), len(need_in), len(need_out),
        )
        r1 = self._batch_set_room(need_in, self.rooms["in_harrow"])
        r2 = self._batch_set_room(need_out, self.rooms["out_harrow"])
        failed = {**r1.failed, **r2.failed}
        if failed:
            raise ControllerError(
                f"Failed school-start Room updates on {len(failed)} device(s): {list(failed.items())[:10]}"
            )
        self._validate_group_partition(master, desired_in, desired_out)

    def set_all_out(self) -> None:
        master = self.master_members()
        _, current_out = self.current_in_out()
        need_update = master - current_out
        self.logger.info("Set ALL Out-Harrow: master=%d need_room200=%d", len(master), len(need_update))
        result = self._batch_set_room(need_update, self.rooms["out_harrow"])
        if result.failed:
            raise ControllerError(
                f"Failed Room=200 on {len(result.failed)} device(s): {list(result.failed.items())[:10]}"
            )
        self._validate_group_partition(master, set(), master)

    def apply_attendance(self) -> Tuple[int, int]:
        master = self.master_members()
        attendance_absent, path, sha256 = self.read_absent_serials(master)
        manual_out = set(self.active_manual_overrides(master))
        effective_out = attendance_absent | manual_out
        present = master - effective_out
        actual_in, actual_out = self.current_in_out()
        need_out = effective_out - actual_out
        need_in = present - actual_in
        self.logger.info(
            "Apply attendance: total=%d present=%d attendance_absent=%d manual_out=%d effective_out=%d need_room100=%d need_room200=%d",
            len(master), len(present), len(attendance_absent), len(manual_out),
            len(effective_out), len(need_in), len(need_out),
        )
        r1 = self._batch_set_room(need_in, self.rooms["in_harrow"])
        r2 = self._batch_set_room(need_out, self.rooms["out_harrow"])
        failed = {**r1.failed, **r2.failed}
        if failed:
            raise ControllerError(
                f"Attendance Room update failed on {len(failed)} device(s): {list(failed.items())[:10]}"
            )
        self._validate_group_partition(master, present, effective_out)
        self.write_attendance_marker(path, sha256, len(attendance_absent))
        return len(present), len(effective_out)

    def set_wifi_scope(self, enabled: bool, enforce_attendance_guard: bool = True) -> None:
        in_group = self.in_group()
        wifi_id = self.require_profile(self.profile_names["wifi"])
        if enabled and enforce_attendance_guard and self.cfg["safety"].get("require_attendance_for_wifi", True):
            master = self.master_members()
            _absent, path, sha256 = self.read_absent_serials(master)
            if not self.attendance_marker_valid(path, sha256):
                raise AttendanceError(
                    "WiFi-Harrow ON blocked: today's attendance has not been successfully verified "
                    "or the attendance CSV changed after verification"
                )

        self.jamf.set_profile_target_group(wifi_id, in_group, enabled)
        if self.dry_run:
            return
        root = self.jamf.get_profile_xml(wifi_id)
        targets = self.jamf.profile_target_groups(root)
        actual = in_group.name in targets
        if actual != enabled:
            raise VerificationError(
                f"WiFi-Harrow scope verification failed: expected target {in_group.name} enabled={enabled}, actual={actual}"
            )
        self.logger.info("WiFi-Harrow target %s verified: %s", in_group.name, "ON" if enabled else "OFF")

    def action_0700(self) -> None:
        school_day, reason = self.is_school_day()
        if not school_day:
            self.logger.info("07:00 skipped: %s", reason)
            return
        self.preflight()
        self.set_wifi_scope(False, enforce_attendance_guard=False)
        self.set_all_in()

    def action_0800(self) -> None:
        school_day, reason = self.is_school_day()
        if not school_day:
            self.logger.info("08:00 skipped: %s", reason)
            return
        self.preflight()
        present, absent = self.apply_attendance()
        self.logger.info("08:00 attendance complete: present=%d absent=%d", present, absent)

    def action_0810(self) -> None:
        school_day, reason = self.is_school_day()
        if not school_day:
            self.logger.info("08:10 skipped: %s", reason)
            return
        self.preflight()
        present, absent = self.apply_attendance()
        self.set_wifi_scope(True, enforce_attendance_guard=True)
        self.logger.info("08:10 WiFi ON complete: present=%d absent=%d", present, absent)

    def action_1600(self) -> None:
        school_day, reason = self.is_school_day()
        if not school_day:
            self.logger.info("16:00 skipped: %s", reason)
            return
        self.preflight()
        self.set_wifi_scope(False, enforce_attendance_guard=False)
        self.set_all_out()
        self.purge_expired_manual_overrides(clear_all=True)

    def verify_current(self) -> dict:
        self.preflight()
        master = self.master_members()
        actual_in_all, actual_out_all = self.current_in_out()
        actual_in = actual_in_all & master
        actual_out = actual_out_all & master
        wifi_id = self.require_profile(self.profile_names["wifi"])
        wifi_targets = self.jamf.profile_target_groups(self.jamf.get_profile_xml(wifi_id))
        status = {
            "timestamp": self.now().isoformat(),
            "school_day": self.is_school_day()[0],
            "master": len(master),
            "in_harrow": len(actual_in),
            "out_harrow": len(actual_out),
            "unclassified": len(master - actual_in - actual_out),
            "overlap": len(actual_in & actual_out),
            "wifi_target_in_harrow": self.in_group().name in wifi_targets,
            "manual_out_overrides": len(self.active_manual_overrides(master)),
        }
        self.logger.info("Current state: %s", json.dumps(status, sort_keys=True))
        return status

    def reconcile(self, *, preflight: bool = True) -> None:
        if preflight:
            self.preflight()
        now = self.now()
        school_day, reason = self.is_school_day(now.date())
        self.logger.info("Reconcile desired state at %s (%s)", now.isoformat(), reason)

        if not school_day:
            self.set_wifi_scope(False, enforce_attendance_guard=False)
            self.set_all_out()
            self.purge_expired_manual_overrides()
            return

        t = now.time().replace(tzinfo=None)
        if t < dtime(7, 0):
            self.set_wifi_scope(False, enforce_attendance_guard=False)
            self.set_all_out()
        elif t < dtime(8, 0):
            self.set_wifi_scope(False, enforce_attendance_guard=False)
            self.set_all_in()
        elif t < dtime(8, 10):
            self.set_wifi_scope(False, enforce_attendance_guard=False)
            self.apply_attendance()
        elif t < dtime(16, 0):
            self.apply_attendance()
            self.set_wifi_scope(True, enforce_attendance_guard=True)
        else:
            self.set_wifi_scope(False, enforce_attendance_guard=False)
            self.set_all_out()
            self.purge_expired_manual_overrides(clear_all=True)
