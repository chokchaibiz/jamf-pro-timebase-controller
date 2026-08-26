"""Action-specific handlers for privileged importer jobs."""
from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from datetime import date, datetime, time as dtime
from pathlib import Path
from typing import Callable, Optional
from zoneinfo import ZoneInfo

from attendance_common import AttendanceCSVError, parse_csv_path
from holiday_common import parse_holiday_csv_path
from harrow_timebase import TimeBaseController
from .storage import (
    archive_attendance,
    archive_holidays,
    write_attendance,
    write_holidays,
)


@dataclass
class JobContext:
    job: dict
    base: dict
    cfg: dict
    portal_cfg: dict
    logger: logging.Logger
    started: datetime
    staging_dir: Path
    archive_dir: Path
    staged_path: Optional[Path] = None

    @property
    def job_id(self) -> str:
        return str(self.base["job_id"])

    def now(self) -> datetime:
        return datetime.now(ZoneInfo(self.cfg["timezone"]))

    def result(self, **values) -> dict:
        return {**self.base, **values, "completed_at": self.now().isoformat()}


def run_controller(args: list[str], *, timeout: int = 2700) -> subprocess.CompletedProcess[str]:
    command = [
        "/opt/harrow-timebase/venv/bin/python",
        "/opt/harrow-timebase/harrow_timebase.py",
        "--config", "/etc/harrow-timebase/config.json",
        *args,
    ]
    return subprocess.run(command, text=True, capture_output=True, timeout=timeout)


def handle_manual_override(ctx: JobContext) -> dict:
    action = str(ctx.job["action"])
    serial = str(ctx.job.get("serial_number", "")).strip().upper()
    if not serial:
        raise ValueError(f"{action} job has no serial_number")

    args = ["--serial", serial]
    if action == "manual_out":
        args.extend([
            f"--email-address={str(ctx.job.get('email_address', ''))}",
            f"--username={str(ctx.job.get('username', ''))}",
            f"--device-name={str(ctx.job.get('device_name', ''))}",
            f"--submitted-by={str(ctx.job.get('submitted_by', ''))}",
            f"--reason={str(ctx.job.get('reason', ''))}",
            "manual-out",
        ])
    else:
        args.append("manual-clear")

    ctx.logger.info("Processing %s for serial=%s", action, serial)
    proc = run_controller(args)
    if proc.returncode != 0:
        raise RuntimeError(
            f"{action} failed for {serial}: {proc.stderr[-1800:] or proc.stdout[-1800:]}"
        )
    message = (
        "Device is now held in Out-Harrow until 16:00 or until the override is cleared."
        if action == "manual_out"
        else "Manual Out-Harrow override cleared; device state was reconciled from attendance."
    )
    ctx.logger.info("Manual override job %s complete action=%s serial=%s", ctx.job_id, action, serial)
    return ctx.result(
        status="SUCCESS",
        message=message,
        serial_number=serial,
        email_address=str(ctx.job.get("email_address", "")),
        username=str(ctx.job.get("username", "")),
        device_name=str(ctx.job.get("device_name", "")),
        controller_output=proc.stdout[-3000:],
    )


def handle_holiday_upload(ctx: JobContext) -> dict:
    staged_name = Path(str(ctx.job.get("staged_filename", ""))).name
    if not staged_name:
        raise ValueError("Queued holiday upload has no staged filename")
    ctx.staged_path = ctx.staging_dir / staged_name
    if not ctx.staged_path.exists():
        raise FileNotFoundError(f"Staged holiday CSV not found: {ctx.staged_path}")

    parsed_holidays = parse_holiday_csv_path(ctx.staged_path)
    holiday_file = Path(ctx.cfg["paths"]["holiday_file"])
    if holiday_file.is_symlink():
        holiday_file = holiday_file.resolve()
    archived = archive_holidays(holiday_file, ctx.archive_dir, ctx.started)
    write_holidays(holiday_file, parsed_holidays.entries)
    validated = parse_holiday_csv_path(holiday_file)

    status = "SUCCESS"
    message = "Holiday calendar uploaded and activated successfully."
    reconcile_exit = None
    if bool(ctx.portal_cfg.get("reconcile_on_holiday_upload", True)):
        ctx.logger.info("Holiday import %s triggers immediate reconcile", ctx.job_id)
        try:
            proc = run_controller(["reconcile"])
            reconcile_exit = proc.returncode
            if proc.returncode == 0:
                message = "Holiday calendar uploaded, activated, and reconciled successfully."
            else:
                status = "IMPORTED_RECONCILE_FAILED"
                message = "Holiday calendar was activated, but immediate Jamf reconcile failed. The periodic reconciler can retry."
                ctx.logger.error(
                    "Holiday reconcile failed for %s rc=%d stdout=%s stderr=%s",
                    ctx.job_id, proc.returncode, proc.stdout[-2000:], proc.stderr[-2000:],
                )
        except Exception as exc:
            status = "IMPORTED_RECONCILE_FAILED"
            message = "Holiday calendar was activated, but immediate Jamf reconcile could not run. The periodic reconciler can retry."
            ctx.logger.exception("Holiday reconcile execution failed for %s: %s", ctx.job_id, exc)

    ctx.logger.info("Holiday job %s complete holidays=%d", ctx.job_id, validated.holiday_count)
    return ctx.result(
        status=status,
        message=message,
        holiday_count=validated.holiday_count,
        final_file=str(holiday_file),
        archived_previous_file=archived,
        reconcile_exit_code=reconcile_exit,
    )


def handle_attendance(ctx: JobContext) -> dict:
    action = str(ctx.job["action"])
    attendance_date = date.fromisoformat(str(ctx.job["attendance_date"]))
    controller = TimeBaseController(ctx.cfg, ctx.logger, dry_run=False)
    controller.preflight()
    master = controller.master_members()

    if action == "zero_absent":
        emails: list[str] = []
        resolved_serials: set[str] = set()
        duplicates = 0
    else:
        staged_name = Path(str(ctx.job.get("staged_filename", ""))).name
        if not staged_name:
            raise ValueError("Queued upload has no staged filename")
        ctx.staged_path = ctx.staging_dir / staged_name
        if not ctx.staged_path.exists():
            raise FileNotFoundError(f"Staged CSV not found: {ctx.staged_path}")
        parsed = parse_csv_path(ctx.staged_path)
        emails = list(parsed.emails)
        duplicates = parsed.duplicate_count

        resolved_serials, unresolved, ambiguous = controller.resolve_absent_emails(master, emails)
        if unresolved or ambiguous:
            details = []
            if unresolved:
                details.append(
                    f"{len(unresolved)} email(s) not found in {ctx.cfg['groups']['master']}: {unresolved[:30]}"
                )
            if ambiguous:
                preview = {k: v for k, v in list(ambiguous.items())[:20]}
                details.append(
                    f"{len(ambiguous)} email(s) matched multiple master iPads: {preview}"
                )
            raise AttendanceCSVError("; ".join(details))

        fraction = (len(resolved_serials) / len(master)) if master else 1.0
        max_fraction = float(ctx.cfg["safety"]["max_absent_fraction"])
        if fraction > max_fraction:
            raise AttendanceCSVError(
                f"Absent device count {len(resolved_serials)}/{len(master)} ({fraction:.1%}) exceeds safety limit {max_fraction:.1%}"
            )

    final_path = Path(ctx.cfg["paths"]["attendance_dir"]) / f"absent-{attendance_date.isoformat()}.csv"
    archived = archive_attendance(final_path, ctx.archive_dir, ctx.started)
    write_attendance(final_path, emails)

    if action == "upload":
        controller.write_attendance_resolution_cache(
            final_path,
            controller.file_sha256(final_path),
            emails,
            resolved_serials,
            attendance_date,
        )
    else:
        resolution_cache = controller.attendance_resolution_path(attendance_date)
        if resolution_cache.exists():
            resolution_cache.unlink()

    marker = Path(ctx.cfg["paths"]["state_dir"]) / f"attendance-{attendance_date.isoformat()}.ok.json"
    if marker.exists():
        marker.unlink()

    status = "SUCCESS_WAITING_SCHEDULE"
    message = "Attendance imported successfully; scheduler will apply it at the normal time."
    reconcile_exit = None
    today = ctx.started.date()
    school_day, reason = controller.is_school_day(attendance_date)
    if attendance_date == today and school_day and dtime(8, 0) <= ctx.started.time().replace(tzinfo=None) < dtime(16, 0):
        ctx.logger.info("Attendance import %s triggers immediate reconcile", ctx.job_id)
        proc = run_controller(["reconcile"])
        reconcile_exit = proc.returncode
        if proc.returncode == 0:
            status = "SUCCESS"
            message = "Attendance imported and applied to Jamf successfully."
        else:
            status = "IMPORTED_RECONCILE_FAILED"
            message = "Attendance file was imported, but immediate Jamf reconcile failed. The periodic reconciler can retry."
            ctx.logger.error(
                "Reconcile failed for %s rc=%d stdout=%s stderr=%s",
                ctx.job_id, proc.returncode, proc.stdout[-2000:], proc.stderr[-2000:],
            )
    elif attendance_date == today and not school_day:
        message = f"Attendance imported; no Jamf apply because today is {reason}."
    elif attendance_date < today:
        message = "Attendance imported for a past date; no Jamf changes were applied."

    ctx.logger.info(
        "Attendance job %s complete status=%s absent_emails=%d resolved_devices=%d",
        ctx.job_id, status, len(emails), len(resolved_serials),
    )
    return ctx.result(
        status=status,
        message=message,
        absent_count=len(emails),
        absent_device_count=len(resolved_serials),
        present_count=len(master) - len(resolved_serials),
        master_count=len(master),
        duplicate_count=duplicates,
        final_file=str(final_path),
        archived_previous_file=archived,
        reconcile_exit_code=reconcile_exit,
    )


JobHandler = Callable[[JobContext], dict]

ACTION_HANDLERS: dict[str, JobHandler] = {
    "manual_out": handle_manual_override,
    "manual_clear": handle_manual_override,
    "upload_holidays": handle_holiday_upload,
    "upload": handle_attendance,
    "zero_absent": handle_attendance,
}


def handler_for(action: str) -> JobHandler:
    try:
        return ACTION_HANDLERS[action]
    except KeyError as exc:
        raise ValueError(f"Unsupported job action: {action}") from exc
