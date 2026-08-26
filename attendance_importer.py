#!/usr/bin/env python3
"""Process jobs queued by the unprivileged Harrow web portal."""
from __future__ import annotations

import argparse
import json
import logging
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from harrow_timebase import load_config, setup_logging
from timebase.importer.handlers import JobContext, handler_for
from timebase.importer.storage import append_audit, atomic_json


def load_job(job_path: Path, logger: logging.Logger) -> dict:
    """Load one queued job, removing malformed queue entries."""
    try:
        return json.loads(job_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.error("Invalid queued job %s: %s", job_path, exc)
        try:
            job_path.unlink()
        except OSError:
            pass
        return {}


def process_job(job_path: Path, cfg: dict, portal_cfg: dict, logger: logging.Logger) -> dict:
    """Orchestrate one job while action handlers own business-specific behavior."""
    started = datetime.now(ZoneInfo(cfg["timezone"]))
    job = load_job(job_path, logger)
    if not job:
        return {}

    job_id = str(job.get("job_id", job_path.stem.replace(".job", "")))
    status_path = Path(portal_cfg["paths"]["status_dir"]) / f"{job_id}.json"
    audit_file = Path(portal_cfg["paths"]["audit_file"])
    base = {
        **job,
        "job_id": job_id,
        "processing_started_at": started.isoformat(),
        "status": "PROCESSING",
    }
    atomic_json(status_path, base)

    context = JobContext(
        job=job,
        base=base,
        cfg=cfg,
        portal_cfg=portal_cfg,
        logger=logger,
        started=started,
        staging_dir=Path(portal_cfg["paths"]["staging_dir"]),
        archive_dir=Path(portal_cfg["paths"]["archive_dir"]),
    )
    try:
        action = str(job["action"])
        result = handler_for(action)(context)
        atomic_json(status_path, result)
        append_audit(audit_file, result)
        return result
    except Exception as exc:  # queue boundary: record failure and continue draining
        logger.exception("Importer job %s failed", job_id)
        failed = {
            **base,
            "status": "FAILED",
            "message": str(exc),
            "completed_at": datetime.now(ZoneInfo(cfg["timezone"])).isoformat(),
        }
        atomic_json(status_path, failed)
        append_audit(audit_file, failed)
        return failed
    finally:
        if context.staged_path is not None:
            try:
                context.staged_path.unlink()
            except OSError:
                pass
        try:
            job_path.unlink()
        except OSError:
            pass


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="/etc/harrow-timebase/config.json")
    parser.add_argument("--portal-config", default="/etc/harrow-timebase/portal.json")
    args = parser.parse_args()
    cfg = load_config(Path(args.config))
    portal_cfg = json.loads(Path(args.portal_config).read_text(encoding="utf-8"))
    logger = setup_logging(cfg)
    queue_dir = Path(portal_cfg["paths"]["queue_dir"])
    queue_dir.mkdir(parents=True, exist_ok=True)
    jobs = sorted(queue_dir.glob("*.job.json"))
    if not jobs:
        logger.info("Attendance importer: no queued jobs")
        return 0
    logger.info("Attendance importer: processing %d queued job(s)", len(jobs))
    for job_path in jobs:
        process_job(job_path, cfg, portal_cfg, logger)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
