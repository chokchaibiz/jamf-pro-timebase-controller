#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "portal"))

from auth_store import AuthStore, initialize_users

with tempfile.TemporaryDirectory() as td:
    auth_dir = Path(td) / "auth"
    initialize_users(auth_dir, ["admin1", "admin2"], "harrow@dmin")
    store = AuthStore(auth_dir)
    store.ensure_runtime()

    assert store.authenticate("ADMIN1", "harrow@dmin") == "admin1"
    assert store.authenticate("admin1", "wrong") is None

    users_before = json.loads(store.users_path.read_text(encoding="utf-8"))["users"]
    session_key_before = store.session_key_path.read_bytes()
    assert store.add_user("Admin6", "Admin6-SecurePass!") == "admin6"
    assert store.authenticate("admin6", "Admin6-SecurePass!") == "admin6"
    users_after = json.loads(store.users_path.read_text(encoding="utf-8"))["users"]
    assert users_after["admin1"] == users_before["admin1"]
    assert users_after["admin2"] == users_before["admin2"]
    assert store.session_key_path.read_bytes() == session_key_before

    try:
        store.add_user("ADMIN6", "AnotherSecurePass!")
    except ValueError as exc:
        assert "already exists" in str(exc)
    else:
        raise AssertionError("duplicate usernames must be rejected case-insensitively")

    for invalid_username in ("", "admin user", "../admin", "@admin"):
        try:
            store.add_user(invalid_username, "AnotherSecurePass!")
        except ValueError as exc:
            assert "Username must be" in str(exc)
        else:
            raise AssertionError(f"invalid username was accepted: {invalid_username!r}")
    try:
        store.add_user("admin8", "short")
    except ValueError as exc:
        assert "at least 8" in str(exc)
    else:
        raise AssertionError("short add-user password was accepted")

    cli_environment = dict(os.environ)
    cli_environment["HARROW_NEW_USER_PASSWORD"] = "Admin7-SecurePass!"
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "portal" / "auth_store.py"),
            "add-user",
            "--auth-dir",
            str(auth_dir),
            "--user",
            "admin7",
            "--password-env",
            "HARROW_NEW_USER_PASSWORD",
        ],
        env=cli_environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "Added portal user: admin7"
    assert store.authenticate("admin7", "Admin7-SecurePass!") == "admin7"

    old_token = store.create_session("admin1", ttl_seconds=300)
    assert store.verify_session(old_token) == "admin1"

    store.change_password("admin1", "harrow@dmin", "NewSecurePass!2026")
    assert store.authenticate("admin1", "harrow@dmin") is None
    assert store.authenticate("admin1", "NewSecurePass!2026") == "admin1"
    assert store.verify_session(old_token) is None, "password change must invalidate older sessions"

    new_token = store.create_session("admin1", ttl_seconds=300)
    assert store.verify_session(new_token) == "admin1"

    # Reinitialization during an upgrade must preserve changed passwords.
    initialize_users(auth_dir, ["admin1", "admin2"], "harrow@dmin")
    assert store.authenticate("admin1", "NewSecurePass!2026") == "admin1"
    assert store.authenticate("admin6", "Admin6-SecurePass!") == "admin6"
    assert store.authenticate("admin7", "Admin7-SecurePass!") == "admin7"

print("Auth regression: add-user/login/session/change-password/preserve-users: PASS")
