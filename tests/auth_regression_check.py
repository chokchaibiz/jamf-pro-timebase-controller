#!/usr/bin/env python3
from __future__ import annotations

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

print("Auth regression: login/session/change-password/preserve-users: PASS")
