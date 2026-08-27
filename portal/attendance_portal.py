#!/usr/bin/env python3
"""Harrow Attendance Upload Portal.

The portal never receives Jamf credentials. It validates/normalizes uploads and queues
jobs for the privileged attendance importer. Nginx handles Basic Authentication.
"""
from __future__ import annotations

import errno
import json
import os
import secrets
import shutil
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional, Set

import requests
from zoneinfo import ZoneInfo

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from attendance_common import AttendanceCSVError, normalized_csv, parse_csv_bytes
from holiday_common import HolidayCSVError, normalized_holiday_csv, parse_holiday_csv_bytes, parse_holiday_csv_path

PORTAL_CONFIG = Path(os.environ.get("PORTAL_CONFIG", "/etc/harrow-timebase/portal.json"))
FORM_TOKEN = os.environ.get("HARROW_PORTAL_FORM_TOKEN", "")


def load_json(path: Path) -> dict:
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


CFG = load_json(PORTAL_CONFIG)
TZ = ZoneInfo(CFG.get("timezone", "Asia/Bangkok"))
STAGING_DIR = Path(CFG["paths"]["staging_dir"])
QUEUE_DIR = Path(CFG["paths"]["queue_dir"])
STATUS_DIR = Path(CFG["paths"]["status_dir"])
MASTER_CACHE = Path(CFG["paths"]["master_cache_file"])
HOLIDAY_FILE = Path(CFG["paths"]["holiday_file"])
MANUAL_OVERRIDE_FILE = Path(CFG["paths"].get("manual_override_file", "/var/lib/harrow-timebase/shared/manual-overrides.json"))
DEVICE_QUERY_CFG = CFG.get("device_query", {})
DEVICE_QUERY_URL = str(DEVICE_QUERY_CFG.get("url", "http://127.0.0.1:8091")).rstrip("/")
DEVICE_QUERY_TIMEOUT = int(DEVICE_QUERY_CFG.get("timeout_seconds", 45))
MIN_QUERY_LENGTH = int(DEVICE_QUERY_CFG.get("minimum_query_length", 2))
INTERNAL_API_TOKEN = os.environ.get("HARROW_INTERNAL_API_TOKEN", "").strip()
MAX_UPLOAD_BYTES = int(CFG.get("max_upload_bytes", 2 * 1024 * 1024))
PAST_DAYS = int(CFG.get("date_window", {}).get("past_days", 7))
FUTURE_DAYS = int(CFG.get("date_window", {}).get("future_days", 30))

APP_DIR = Path(__file__).resolve().parent
app = FastAPI(title="Harrow Attendance Upload Portal", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=str(APP_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(APP_DIR / "templates"))


def now() -> datetime:
    return datetime.now(TZ)


def current_user(request: Request) -> str:
    return request.headers.get("x-remote-user") or "authenticated-user"


def validate_form_token(token: str) -> None:
    if not FORM_TOKEN or not secrets.compare_digest(token, FORM_TOKEN):
        raise HTTPException(status_code=403, detail="Invalid form token")


def parse_selected_date(value: str) -> date:
    try:
        selected = date.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid attendance date") from exc
    today = now().date()
    if selected < today - timedelta(days=PAST_DAYS) or selected > today + timedelta(days=FUTURE_DAYS):
        raise HTTPException(
            status_code=400,
            detail=f"Date must be between {today - timedelta(days=PAST_DAYS)} and {today + timedelta(days=FUTURE_DAYS)}",
        )
    return selected


def safe_replace(src: Path, dst: Path, *, mode: int = 0o640) -> None:
    """Replace dst with src, including when src/dst are on different filesystems.

    os.replace() is atomic on one filesystem but raises EXDEV across filesystems.
    For EXDEV, copy to a hidden temp file inside the destination directory, fsync it,
    then os.replace() that local temp into place. The watched queue therefore never
    sees a half-written final .job.json file.
    """
    src = Path(src)
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.replace(src, dst)
        return
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise

    tmp_dst = dst.with_name(f".{dst.name}.{secrets.token_hex(4)}.xdev.tmp")
    try:
        with src.open("rb") as inf, tmp_dst.open("wb") as outf:
            shutil.copyfileobj(inf, outf, length=1024 * 1024)
            outf.flush()
            os.fsync(outf.fileno())
        os.chmod(tmp_dst, mode)
        os.replace(tmp_dst, dst)
        src.unlink(missing_ok=True)
    finally:
        try:
            tmp_dst.unlink()
        except FileNotFoundError:
            pass


def load_master_cache() -> Optional[Set[str]]:
    if not MASTER_CACHE.exists():
        return None
    serials = {line.strip().upper() for line in MASTER_CACHE.read_text(encoding="utf-8").splitlines() if line.strip()}
    return serials or None


def atomic_write(path: Path, data: bytes, mode: int = 0o640) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    with tmp.open("wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.chmod(tmp, mode)
    safe_replace(tmp, path, mode=mode)


def queue_job(*, action: str, user: str, selected: Optional[date] = None, original_filename: str = "", serial_count: int = 0, email_count: int = 0,
              duplicate_count: int = 0, staged_filename: str = "", holiday_count: int = 0,
              holiday_range_count: int = 0,
              serial_number: str = "", email_address: str = "", username: str = "",
              device_name: str = "", reason: str = "") -> str:
    job_id = f"{now().strftime('%Y%m%dT%H%M%S')}-{secrets.token_hex(6)}"
    if action == "upload_holidays":
        job_type = "holiday"
    elif action in {"manual_out", "manual_clear"}:
        job_type = "device_override"
    else:
        job_type = "attendance"
    job = {
        "job_id": job_id,
        "job_type": job_type,
        "action": action,
        "submitted_at": now().isoformat(),
        "submitted_by": user,
        "original_filename": original_filename,
        "staged_filename": staged_filename,
        "serial_count": serial_count,
        "email_count": email_count,
        "duplicate_count": duplicate_count,
        "holiday_count": holiday_count,
        "holiday_range_count": holiday_range_count,
    }
    if serial_number:
        job["serial_number"] = serial_number.strip().upper()
    if email_address:
        job["email_address"] = email_address.strip().lower()
    if username:
        job["username"] = username.strip()
    if device_name:
        job["device_name"] = device_name.strip()
    if reason:
        job["reason"] = reason.strip()
    if selected is not None:
        job["attendance_date"] = selected.isoformat()
    payload = (json.dumps(job, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    # Write outside the watched queue, then atomically rename the complete job into it.
    # This prevents systemd.path from waking the importer on a half-written temp file.
    tmp_job = STAGING_DIR / f".{job_id}.job.tmp"
    atomic_write(tmp_job, payload)
    QUEUE_DIR.mkdir(parents=True, exist_ok=True)
    safe_replace(tmp_job, QUEUE_DIR / f"{job_id}.job.json", mode=0o640)
    return job_id




def holiday_summary() -> dict:
    if not HOLIDAY_FILE.exists():
        return {"ready": False, "count": 0, "upcoming": [], "error": "Holiday calendar not found"}
    try:
        parsed = parse_holiday_csv_path(HOLIDAY_FILE)
    except (OSError, HolidayCSVError) as exc:
        return {"ready": False, "count": 0, "upcoming": [], "error": str(exc)}
    today = now().date()
    collapsed = []
    for entry in (x for x in parsed.entries if x.holiday_date >= today):
        if (
            collapsed
            and collapsed[-1]["description"] == entry.description
            and collapsed[-1]["end"] + timedelta(days=1) == entry.holiday_date
        ):
            collapsed[-1]["end"] = entry.holiday_date
        else:
            collapsed.append({
                "start": entry.holiday_date,
                "end": entry.holiday_date,
                "description": entry.description,
            })
    upcoming = [
        {
            "date": (
                item["start"].isoformat()
                if item["start"] == item["end"]
                else f"{item['start'].isoformat()} – {item['end'].isoformat()}"
            ),
            "description": item["description"],
        }
        for item in collapsed[:12]
    ]
    return {
        "ready": True,
        "count": parsed.holiday_count,
        "upcoming": upcoming,
        "updated_at": datetime.fromtimestamp(HOLIDAY_FILE.stat().st_mtime, TZ).isoformat(),
    }

def query_device_broker(path: str, *, params: Optional[dict] = None) -> dict:
    if not INTERNAL_API_TOKEN:
        raise RuntimeError("Internal device-query token is not configured")
    url = f"{DEVICE_QUERY_URL}{path}"
    try:
        response = requests.get(
            url,
            params=params,
            headers={"X-Internal-Token": INTERNAL_API_TOKEN},
            timeout=DEVICE_QUERY_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise RuntimeError(f"Device Query service is unavailable: {exc}") from exc
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    if response.status_code != 200:
        detail = payload.get("detail") if isinstance(payload, dict) else None
        raise RuntimeError(str(detail or f"Device Query service returned HTTP {response.status_code}"))
    if not isinstance(payload, dict):
        raise RuntimeError("Device Query service returned an invalid response")
    return payload


def broker_health() -> dict:
    try:
        response = requests.get(f"{DEVICE_QUERY_URL}/healthz", timeout=min(5, DEVICE_QUERY_TIMEOUT))
        if response.status_code == 200:
            return {"ready": True}
        return {"ready": False, "error": f"HTTP {response.status_code}"}
    except requests.RequestException as exc:
        return {"ready": False, "error": str(exc)}

def load_active_overrides() -> list[dict]:
    if not MANUAL_OVERRIDE_FILE.exists():
        return []
    try:
        data = json.loads(MANUAL_OVERRIDE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    current = now()
    rows = []
    overrides = data.get("overrides", {}) if isinstance(data, dict) else {}
    if not isinstance(overrides, dict):
        return []
    for serial, record in overrides.items():
        if not isinstance(record, dict):
            continue
        try:
            expires = datetime.fromisoformat(str(record.get("expires_at", "")))
        except ValueError:
            continue
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=TZ)
        if expires.astimezone(TZ) <= current:
            continue
        row = dict(record)
        row["serial_number"] = str(serial).upper()
        rows.append(row)
    rows.sort(key=lambda x: (x.get("email_address", "").lower(), x.get("serial_number", "")))
    return rows


def read_recent_status(limit: int = 30) -> list[dict]:
    rows = []
    if not STATUS_DIR.exists():
        return rows
    paths = sorted(STATUS_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    for path in paths[:limit]:
        try:
            rows.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
    return rows


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok", "time": now().isoformat()}


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    today = now().date()
    recent = read_recent_status(20)
    today_status = next((x for x in recent if x.get("job_type", "attendance") == "attendance" and x.get("attendance_date") == today.isoformat()), None)
    holidays = holiday_summary()
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "today": today.isoformat(),
            "user": current_user(request),
            "form_token": FORM_TOKEN,
            "master_cache_ready": MASTER_CACHE.exists(),
            "today_status": today_status,
            "holidays": holidays,
        },
    )


@app.post("/upload", response_class=HTMLResponse)
async def upload_attendance(
    request: Request,
    attendance_date: str = Form(...),
    form_token: str = Form(...),
    file: UploadFile = File(...),
):
    validate_form_token(form_token)
    selected = parse_selected_date(attendance_date)
    filename = Path(file.filename or "attendance.csv").name
    if not filename.lower().endswith(".csv"):
        return templates.TemplateResponse(
            request=request, name="error.html", status_code=400,
            context={"message": "กรุณาเลือกไฟล์ .csv เท่านั้น"},
        )

    data = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        return templates.TemplateResponse(
            request=request, name="error.html", status_code=413,
            context={"message": f"ไฟล์ใหญ่เกินกำหนด ({MAX_UPLOAD_BYTES // 1024} KB)"},
        )

    try:
        parsed = parse_csv_bytes(data)
    except AttendanceCSVError as exc:
        return templates.TemplateResponse(
            request=request, name="error.html", status_code=400,
            context={"message": str(exc)},
        )

    # The unprivileged portal validates CSV shape/email syntax only. The privileged importer
    # resolves every Email Address against live Jamf inventory before activating the file.
    job_stub = f"upload-{now().strftime('%Y%m%dT%H%M%S')}-{secrets.token_hex(6)}.csv"
    normalized = normalized_csv(parsed.emails).encode("utf-8")
    atomic_write(STAGING_DIR / job_stub, normalized)
    job_id = queue_job(
        action="upload",
        selected=selected,
        user=current_user(request),
        original_filename=filename,
        email_count=parsed.absent_count,
        duplicate_count=parsed.duplicate_count,
        staged_filename=job_stub,
    )
    return RedirectResponse(url=f"/status/{job_id}", status_code=303)


@app.get("/holidays/current")
def download_current_holidays():
    if not HOLIDAY_FILE.exists():
        raise HTTPException(status_code=404, detail="Holiday calendar not found")
    return FileResponse(path=str(HOLIDAY_FILE), filename="holidays.csv", media_type="text/csv")


@app.post("/upload-holidays")
async def upload_holidays(
    request: Request,
    form_token: str = Form(...),
    file: UploadFile = File(...),
):
    validate_form_token(form_token)
    filename = Path(file.filename or "holidays.csv").name
    if not filename.lower().endswith(".csv"):
        return templates.TemplateResponse(
            request=request, name="error.html", status_code=400,
            context={"message": "กรุณาเลือกไฟล์ holidays เป็น .csv เท่านั้น"},
        )

    data = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        return templates.TemplateResponse(
            request=request, name="error.html", status_code=413,
            context={"message": f"ไฟล์ใหญ่เกินกำหนด ({MAX_UPLOAD_BYTES // 1024} KB)"},
        )

    try:
        parsed = parse_holiday_csv_bytes(data)
    except HolidayCSVError as exc:
        return templates.TemplateResponse(
            request=request, name="error.html", status_code=400,
            context={"message": str(exc)},
        )

    staged_name = f"holidays-{now().strftime('%Y%m%dT%H%M%S')}-{secrets.token_hex(6)}.csv"
    atomic_write(STAGING_DIR / staged_name, normalized_holiday_csv(parsed.entries).encode("utf-8"))
    job_id = queue_job(
        action="upload_holidays",
        user=current_user(request),
        original_filename=filename,
        staged_filename=staged_name,
        holiday_count=parsed.holiday_count,
        holiday_range_count=parsed.range_count,
    )
    return RedirectResponse(url=f"/status/{job_id}", status_code=303)


@app.post("/zero")
def zero_absent(
    request: Request,
    attendance_date: str = Form(...),
    form_token: str = Form(...),
):
    validate_form_token(form_token)
    selected = parse_selected_date(attendance_date)
    job_id = queue_job(
        action="zero_absent",
        selected=selected,
        user=current_user(request),
        original_filename="No Absent Students Today",
    )
    return RedirectResponse(url=f"/status/{job_id}", status_code=303)


@app.get("/device-override", response_class=HTMLResponse)
def device_override_page(request: Request, q: str = ""):
    query = q.strip()
    results: list[dict] = []
    search_state = broker_health()
    search_mode = ""
    if query:
        if len(query) < MIN_QUERY_LENGTH:
            search_state = {"ready": False, "error": f"กรุณากรอกอย่างน้อย {MIN_QUERY_LENGTH} ตัวอักษร"}
        else:
            if "@" not in query or any(ch.isspace() for ch in query):
                search_state = {"ready": False, "error": "กรุณากรอก Email Address ให้ครบ เช่น student@harrowbangkok.th"}
            else:
                try:
                    payload = query_device_broker("/search", params={"email": query.lower()})
                    results = payload.get("devices", []) if isinstance(payload.get("devices"), list) else []
                    search_mode = str(payload.get("mode", ""))
                    search_state = {"ready": True}
                except RuntimeError as exc:
                    search_state = {"ready": False, "error": str(exc)}
    return templates.TemplateResponse(
        request=request, name="device_override.html",
        context={
            "user": current_user(request),
            "form_token": FORM_TOKEN,
            "query": query,
            "results": results,
            "search_state": search_state,
            "search_mode": search_mode,
            "active_overrides": load_active_overrides(),
            "minimum_query_length": MIN_QUERY_LENGTH,
        },
    )


@app.post("/device-override/submit")
def submit_device_override(
    request: Request,
    form_token: str = Form(...),
    serial_number: str = Form(...),
    search_email: str = Form(...),
    reason: str = Form(""),
):
    validate_form_token(form_token)
    serial = serial_number.strip().upper()
    if not serial:
        raise HTTPException(status_code=400, detail="Serial is required")
    requested_email = search_email.strip().lower()
    if not requested_email or "@" not in requested_email or any(ch.isspace() for ch in requested_email):
        raise HTTPException(status_code=400, detail="A valid Email Address is required")
    # Never trust hidden form metadata. Re-read the selected device live from the privileged broker,
    # then verify that its current Jamf Email Address still matches the exact email searched by the admin.
    try:
        device = query_device_broker(f"/device/{serial}")
    except RuntimeError as exc:
        return templates.TemplateResponse(
            request=request, name="error.html", status_code=400,
            context={"message": f"ไม่สามารถยืนยัน Device {serial} กับ Jamf Pro ได้: {exc}"},
        )
    live_email = str(device.get("email", "")).strip().lower()
    if live_email != requested_email:
        return templates.TemplateResponse(
            request=request, name="error.html", status_code=409,
            context={
                "message": (
                    f"Email ของ Device {serial} ใน Jamf เปลี่ยนไปหรือไม่ตรงกับที่ค้นหา "
                    f"(ค้นหา: {requested_email}, ปัจจุบัน: {live_email or '-'}). กรุณาค้นหาใหม่ก่อน Submit"
                )
            },
        )
    job_id = queue_job(
        action="manual_out",
        user=current_user(request),
        serial_number=str(device.get("serial_number", serial)).strip().upper(),
        email_address=live_email,
        username=str(device.get("username", "")),
        device_name=str(device.get("device_name", "")),
        reason=reason[:250],
    )
    return RedirectResponse(url=f"/status/{job_id}", status_code=303)


@app.post("/device-override/clear")
def clear_device_override(
    request: Request,
    form_token: str = Form(...),
    serial_number: str = Form(...),
):
    validate_form_token(form_token)
    serial = serial_number.strip().upper()
    if not serial:
        raise HTTPException(status_code=400, detail="Serial is required")
    job_id = queue_job(
        action="manual_clear",
        user=current_user(request),
        serial_number=serial,
    )
    return RedirectResponse(url=f"/status/{job_id}", status_code=303)


@app.get("/status/{job_id}", response_class=HTMLResponse)
def job_status(request: Request, job_id: str):
    if not all(c.isalnum() or c in "-_" for c in job_id):
        raise HTTPException(status_code=400, detail="Invalid job id")
    status_path = STATUS_DIR / f"{job_id}.json"
    queue_path = QUEUE_DIR / f"{job_id}.job.json"
    data = None
    if status_path.exists():
        try:
            data = json.loads(status_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = None
    if data is None and queue_path.exists():
        try:
            data = json.loads(queue_path.read_text(encoding="utf-8"))
            data["status"] = "QUEUED"
            data["message"] = "กำลังรอ Attendance Importer ประมวลผล"
        except (OSError, json.JSONDecodeError):
            data = None
    if data is None:
        data = {"job_id": job_id, "status": "QUEUED", "message": "กำลังรอผลการประมวลผล"}
    terminal = data.get("status") in {"SUCCESS", "SUCCESS_WAITING_SCHEDULE", "FAILED", "IMPORTED_RECONCILE_FAILED"}
    return templates.TemplateResponse(
        request=request, name="status.html",
        context={"job": data, "terminal": terminal},
    )


@app.get("/history", response_class=HTMLResponse)
def history(request: Request):
    return templates.TemplateResponse(
        request=request, name="history.html",
        context={"rows": read_recent_status(100)},
    )
