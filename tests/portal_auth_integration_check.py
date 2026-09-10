#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "portal"))
from auth_store import initialize_users

with tempfile.TemporaryDirectory() as td:
    root = Path(td)
    auth = root / "auth"
    initialize_users(auth, ["admin1", "admin2", "admin3", "admin4", "admin5"], "harrow@dmin")
    shared = root / "shared"
    staging = root / "staging"
    queue = root / "queue"
    status = root / "status"
    for directory in (shared, staging, queue, status):
        directory.mkdir(parents=True, exist_ok=True)
    holidays = shared / "holidays.csv"
    holidays.write_text("date,end_date,description\n2026-12-21,2027-01-03,School Holiday\n", encoding="utf-8")
    config = {
        "timezone": "Asia/Bangkok",
        "paths": {
            "staging_dir": str(staging),
            "queue_dir": str(queue),
            "status_dir": str(status),
            "master_cache_file": str(shared / "master.txt"),
            "holiday_file": str(holidays),
            "manual_override_file": str(shared / "manual-overrides.json"),
        },
        "device_query": {"url": "http://127.0.0.1:65534", "timeout_seconds": 1, "minimum_query_length": 2},
    }
    config_path = root / "portal.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")

    os.environ["PORTAL_CONFIG"] = str(config_path)
    os.environ["HARROW_PORTAL_FORM_TOKEN"] = "test-form-token"
    os.environ["HARROW_AUTH_DIR"] = str(auth)
    os.environ["HARROW_AUTH_COOKIE_SECURE"] = "false"
    os.environ["HARROW_SESSION_TTL_SECONDS"] = "300"
    os.environ["HARROW_INTERNAL_API_TOKEN"] = "test-internal-token"

    spec = importlib.util.spec_from_file_location("attendance_portal_integration", ROOT / "portal" / "attendance_portal.py")
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)

    client = TestClient(module.app, follow_redirects=False)

    response = client.get("/healthz")
    assert response.status_code == 200

    response = client.get("/")
    assert response.status_code == 303 and response.headers["location"].startswith("/login")

    response = client.get("/login?next=https://example.com")
    assert response.status_code == 200 and 'name="next" value="/"' in response.text
    assert 'src="/static/harrow-logo.svg"' in response.text
    assert 'alt="Harrow School logo"' in response.text

    response = client.get("/static/harrow-logo.svg")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/svg+xml")

    response = client.post(
        "/login",
        data={
            "form_token": "test-form-token",
            "username": "admin1",
            "password": "harrow@dmin",
            "next": "/",
        },
    )
    assert response.status_code == 303 and response.headers["location"] == "/"
    session_cookie = response.cookies.get(module.SESSION_COOKIE)
    assert session_cookie
    client.cookies.set(module.SESSION_COOKIE, session_cookie)

    response = client.get("/")
    assert response.status_code == 200
    assert "admin1" in response.text and "Change Password" in response.text
    assert "date,end_date,description" in response.text
    assert response.text.count("data-file-drop") == 2
    assert '<script src="/static/upload.js" defer></script>' in response.text
    assert response.headers["cache-control"] == "no-store"

    response = client.post(
        "/change-password",
        data={
            "form_token": "test-form-token",
            "current_password": "harrow@dmin",
            "new_password": "Admin1-NewPass!",
            "confirm_password": "Admin1-NewPass!",
        },
    )
    assert response.status_code == 303
    new_cookie = response.cookies.get(module.SESSION_COOKIE)
    assert new_cookie and new_cookie != session_cookie

    old_client = TestClient(module.app, follow_redirects=False)
    old_client.cookies.set(module.SESSION_COOKIE, session_cookie)
    response = old_client.get("/")
    assert response.status_code == 303 and response.headers["location"].startswith("/login")

    fresh = TestClient(module.app, follow_redirects=False)
    response = fresh.post(
        "/login",
        data={
            "form_token": "test-form-token",
            "username": "admin1",
            "password": "Admin1-NewPass!",
            "next": "/",
        },
    )
    assert response.status_code == 303 and response.cookies.get(module.SESSION_COOKIE)

print("Portal auth integration: login/menu/change-password/session invalidation/ranges: PASS")
