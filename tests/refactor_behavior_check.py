#!/usr/bin/env python3
"""Focused checks for the controller/importer responsibility split."""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from attendance_importer import process_job
from harrow_timebase import TimeBaseController
from timebase.controller.actions import ControllerActionsMixin
from timebase.controller.attendance import ControllerAttendanceMixin
from timebase.controller.state import ControllerStateMixin
from timebase.importer.handlers import ACTION_HANDLERS


assert issubclass(TimeBaseController, ControllerStateMixin)
assert issubclass(TimeBaseController, ControllerAttendanceMixin)
assert issubclass(TimeBaseController, ControllerActionsMixin)
assert set(ACTION_HANDLERS) == {
    "manual_out", "manual_clear", "upload_holidays", "upload", "zero_absent"
}


def portal_config(root: Path) -> dict:
    return {
        "paths": {
            "status_dir": str(root / "status"),
            "staging_dir": str(root / "staging"),
            "archive_dir": str(root / "archive"),
            "audit_file": str(root / "audit" / "jobs.jsonl"),
        }
    }


with TemporaryDirectory() as raw_tmp:
    tmp = Path(raw_tmp)
    cfg = {"timezone": "Asia/Bangkok"}
    portal_cfg = portal_config(tmp)
    logger = logging.getLogger("refactor-behavior")
    logger.disabled = True

    success_job = tmp / "success.job.json"
    success_job.write_text(
        json.dumps({"job_id": "success", "action": "upload", "attendance_date": "2026-08-26"}),
        encoding="utf-8",
    )

    def successful_handler(context):
        return context.result(status="SUCCESS", message="handled")

    with patch("attendance_importer.handler_for", return_value=successful_handler):
        result = process_job(success_job, cfg, portal_cfg, logger)
    assert result["status"] == "SUCCESS"
    assert not success_job.exists()
    assert json.loads((tmp / "status" / "success.json").read_text())["message"] == "handled"

    failed_job = tmp / "failed.job.json"
    failed_job.write_text(
        json.dumps({"job_id": "failed", "action": "upload", "attendance_date": "2026-08-26"}),
        encoding="utf-8",
    )

    def failing_handler(_context):
        raise RuntimeError("handler failed")

    with patch("attendance_importer.handler_for", return_value=failing_handler):
        result = process_job(failed_job, cfg, portal_cfg, logger)
    assert result["status"] == "FAILED"
    assert result["message"] == "handler failed"
    assert not failed_job.exists()

    audit_rows = (tmp / "audit" / "jobs.jsonl").read_text(encoding="utf-8").splitlines()
    assert [json.loads(row)["status"] for row in audit_rows] == ["SUCCESS", "FAILED"]

print("Refactor behavior checks: PASS")
