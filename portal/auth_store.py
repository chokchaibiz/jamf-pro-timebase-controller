#!/usr/bin/env python3
"""Local authentication store for the Harrow TimeBase portal.

Passwords are PBKDF2-HMAC-SHA256 hashes. Sessions are stateless HMAC-signed
cookies whose session version is checked against the user database, so a
password change invalidates older sessions for that account.
"""
from __future__ import annotations

import argparse
import base64
import fcntl
import hashlib
import hmac
import json
import os
import re
import secrets
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional

PBKDF2_ITERATIONS = 600_000
SESSION_TTL_SECONDS = 8 * 60 * 60
USERNAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


def _b64e(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64d(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def _hash_password(password: str, salt: bytes, iterations: int = PBKDF2_ITERATIONS) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations, dklen=32)


class AuthStore:
    def __init__(self, auth_dir: Path | str):
        self.auth_dir = Path(auth_dir)
        self.users_path = self.auth_dir / "users.json"
        self.session_key_path = self.auth_dir / "session.key"
        self.lock_path = self.auth_dir / ".users.lock"

    def ensure_runtime(self) -> None:
        self.auth_dir.mkdir(parents=True, exist_ok=True)
        if not self.session_key_path.exists():
            self._atomic_write_bytes(self.session_key_path, secrets.token_bytes(32), mode=0o600)
        elif self.session_key_path.stat().st_size < 32:
            raise RuntimeError("Portal session key is invalid")
        if not self.users_path.exists():
            raise RuntimeError("Portal user database is not initialized")

    @contextmanager
    def _locked(self) -> Iterator[None]:
        self.auth_dir.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        with os.fdopen(fd, "r+") as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def _atomic_write_bytes(path: Path, data: bytes, *, mode: int = 0o600) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            os.chmod(tmp, mode)
            os.replace(tmp, path)
        finally:
            try:
                tmp.unlink()
            except FileNotFoundError:
                pass

    def _read_users_unlocked(self) -> dict[str, Any]:
        try:
            data = json.loads(self.users_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Portal user database is unreadable: {exc}") from exc
        if not isinstance(data, dict) or not isinstance(data.get("users"), dict):
            raise RuntimeError("Portal user database has invalid format")
        return data

    def _write_users_unlocked(self, data: dict[str, Any]) -> None:
        payload = (json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
        self._atomic_write_bytes(self.users_path, payload, mode=0o600)

    @staticmethod
    def normalize_username(username: str) -> str:
        return username.strip().lower()

    @classmethod
    def validate_new_username(cls, username: str) -> str:
        normalized = cls.normalize_username(username)
        if not USERNAME_PATTERN.fullmatch(normalized):
            raise ValueError(
                "Username must be 1-64 characters and contain only lowercase letters, "
                "numbers, dots, underscores, or hyphens"
            )
        return normalized

    def add_user(self, username: str, password: str) -> str:
        username = self.validate_new_username(username)
        if len(password) < 8:
            raise ValueError("Password must contain at least 8 characters")
        with self._locked():
            data = self._read_users_unlocked()
            if username in data["users"]:
                raise ValueError(f"Portal user already exists: {username}")
            record = build_user_record(password)
            record["created_at"] = int(time.time())
            data["users"][username] = record
            self._write_users_unlocked(data)
        return username

    def authenticate(self, username: str, password: str) -> Optional[str]:
        username = self.normalize_username(username)
        if not username or not password:
            return None
        with self._locked():
            data = self._read_users_unlocked()
            record = data["users"].get(username)
            if not isinstance(record, dict) or record.get("disabled"):
                return None
            try:
                salt = _b64d(str(record["salt"]))
                expected = _b64d(str(record["password_hash"]))
                iterations = int(record.get("iterations", PBKDF2_ITERATIONS))
            except (KeyError, ValueError, TypeError):
                return None
            actual = _hash_password(password, salt, iterations)
            return username if hmac.compare_digest(actual, expected) else None

    def change_password(self, username: str, current_password: str, new_password: str) -> None:
        username = self.normalize_username(username)
        if len(new_password) < 8:
            raise ValueError("รหัสผ่านใหม่ต้องมีอย่างน้อย 8 ตัวอักษร")
        if current_password == new_password:
            raise ValueError("รหัสผ่านใหม่ต้องไม่เหมือนรหัสผ่านปัจจุบัน")
        with self._locked():
            data = self._read_users_unlocked()
            record = data["users"].get(username)
            if not isinstance(record, dict) or record.get("disabled"):
                raise ValueError("ไม่พบบัญชีผู้ใช้")
            try:
                old_salt = _b64d(str(record["salt"]))
                old_hash = _b64d(str(record["password_hash"]))
                old_iterations = int(record.get("iterations", PBKDF2_ITERATIONS))
            except (KeyError, ValueError, TypeError) as exc:
                raise ValueError("ข้อมูลบัญชีผู้ใช้ไม่ถูกต้อง") from exc
            if not hmac.compare_digest(_hash_password(current_password, old_salt, old_iterations), old_hash):
                raise ValueError("Current password ไม่ถูกต้อง")
            salt = secrets.token_bytes(16)
            record["salt"] = _b64e(salt)
            record["password_hash"] = _b64e(_hash_password(new_password, salt, PBKDF2_ITERATIONS))
            record["iterations"] = PBKDF2_ITERATIONS
            record["session_version"] = int(record.get("session_version", 1)) + 1
            record["password_changed_at"] = int(time.time())
            self._write_users_unlocked(data)

    def _session_key(self) -> bytes:
        try:
            key = self.session_key_path.read_bytes()
        except OSError as exc:
            raise RuntimeError(f"Cannot read portal session key: {exc}") from exc
        if len(key) < 32:
            raise RuntimeError("Portal session key is invalid")
        return key

    def _session_version(self, username: str) -> Optional[int]:
        with self._locked():
            data = self._read_users_unlocked()
            record = data["users"].get(username)
            if not isinstance(record, dict) or record.get("disabled"):
                return None
            return int(record.get("session_version", 1))

    def create_session(self, username: str, *, ttl_seconds: int = SESSION_TTL_SECONDS) -> str:
        username = self.normalize_username(username)
        session_version = self._session_version(username)
        if session_version is None:
            raise ValueError("Unknown user")
        issued_at = int(time.time())
        payload = {
            "u": username,
            "sv": session_version,
            "iat": issued_at,
            "exp": issued_at + int(ttl_seconds),
            "n": secrets.token_urlsafe(12),
        }
        body = _b64e(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))
        signature = _b64e(hmac.new(self._session_key(), body.encode("ascii"), hashlib.sha256).digest())
        return f"{body}.{signature}"

    def verify_session(self, token: str) -> Optional[str]:
        try:
            body, supplied_sig = token.split(".", 1)
            expected_sig = _b64e(hmac.new(self._session_key(), body.encode("ascii"), hashlib.sha256).digest())
            if not hmac.compare_digest(supplied_sig, expected_sig):
                return None
            payload = json.loads(_b64d(body).decode("utf-8"))
            username = self.normalize_username(str(payload["u"]))
            if int(payload["exp"]) < int(time.time()):
                return None
            current_version = self._session_version(username)
            if current_version is None or int(payload.get("sv", -1)) != current_version:
                return None
            return username
        except (ValueError, KeyError, TypeError, json.JSONDecodeError, UnicodeDecodeError):
            return None


def build_user_record(password: str) -> dict[str, Any]:
    salt = secrets.token_bytes(16)
    return {
        "salt": _b64e(salt),
        "password_hash": _b64e(_hash_password(password, salt, PBKDF2_ITERATIONS)),
        "iterations": PBKDF2_ITERATIONS,
        "session_version": 1,
        "disabled": False,
    }


def initialize_users(auth_dir: Path, usernames: list[str], default_password: str, *, overwrite: bool = False) -> None:
    store = AuthStore(auth_dir)
    auth_dir.mkdir(parents=True, exist_ok=True)
    if store.users_path.exists() and not overwrite:
        store.ensure_runtime()
        print(f"Portal users already exist: {store.users_path} (preserved)")
        return
    data = {
        "version": 1,
        "users": {AuthStore.normalize_username(name): build_user_record(default_password) for name in usernames},
    }
    store._write_users_unlocked(data)
    if not store.session_key_path.exists():
        store._atomic_write_bytes(store.session_key_path, secrets.token_bytes(32), mode=0o600)
    print(f"Initialized {len(usernames)} portal users in {store.users_path}")


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init-users")
    init.add_argument("--auth-dir", required=True)
    init.add_argument("--user", action="append", dest="users", required=True)
    init.add_argument("--password-env", default="HARROW_DEFAULT_ADMIN_PASSWORD")
    init.add_argument("--overwrite", action="store_true")
    add = sub.add_parser("add-user")
    add.add_argument("--auth-dir", required=True)
    add.add_argument("--user", required=True)
    add.add_argument("--password-env", default="HARROW_NEW_USER_PASSWORD")
    args = parser.parse_args()
    if args.command == "init-users":
        password = os.environ.get(args.password_env, "")
        if not password:
            raise SystemExit(f"Environment variable {args.password_env} is required")
        initialize_users(Path(args.auth_dir), args.users, password, overwrite=args.overwrite)
    elif args.command == "add-user":
        password = os.environ.get(args.password_env, "")
        if not password:
            raise SystemExit(f"Environment variable {args.password_env} is required")
        store = AuthStore(Path(args.auth_dir))
        store.ensure_runtime()
        try:
            username = store.add_user(args.user, password)
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
        print(f"Added portal user: {username}")


if __name__ == "__main__":
    main()
