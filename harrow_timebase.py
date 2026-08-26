#!/usr/bin/env python3
"""Harrow Jamf Pro TimeBase Controller.

Actions:
  preflight  - Validate Jamf objects, group criteria, scope, credentials and local data.
  0700       - School day only: set all Harrow iPads Room=100 and verify In-Harrow.
  0800       - School day only: resolve absent Email Address CSV to iPads, then apply Room=200; missing CSV can mean zero absent by policy.
  0810       - School day only: ensure attendance state is valid, then add In-Harrow to WiFi-Harrow scope.
  1600       - School day only: remove In-Harrow from WiFi-Harrow scope, then set all Harrow iPads Room=200.
  reconcile  - Calculate desired state from Bangkok local date/time and repair drift idempotently.
  verify     - Report/validate current state without changing Jamf.
  wifi-on    - Manually add In-Harrow to WiFi-Harrow targets (guarded by attendance by default).
  wifi-off   - Manually remove In-Harrow from WiFi-Harrow targets.
  manual-out - Persist one iPad as Out-Harrow until 16:00 (or clear).
  manual-clear - Remove a manual Out-Harrow override.

The controller uses OAuth client_credentials to obtain a bearer token, and the
Jamf Classic API for mobile-device Room updates and mobile device profile scope.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import random
import sys
import threading
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional, Sequence, Set, Tuple
from urllib.parse import quote
from zoneinfo import ZoneInfo
import xml.etree.ElementTree as ET

import requests

from timebase.controller import (
    ControllerActionsMixin,
    ControllerAttendanceMixin,
    ControllerStateMixin,
    EXIT_API,
    EXIT_ATTENDANCE,
    EXIT_CONFIG,
    EXIT_LOCK,
    EXIT_OK,
    EXIT_PREFLIGHT,
    EXIT_VERIFY,
    AttendanceError,
    BatchResult,
    ConfigError,
    ControllerError,
    GroupInfo,
    PreflightError,
    VerificationError,
)


class FileLock:
    def __init__(self, path: Path, wait_seconds: int, logger: logging.Logger):
        self.path = path
        self.wait_seconds = wait_seconds
        self.logger = logger
        self.handle = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = open(self.path, "a+", encoding="utf-8")
        deadline = time.monotonic() + self.wait_seconds
        while True:
            try:
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.handle.seek(0)
                self.handle.truncate()
                self.handle.write(f"pid={os.getpid()} started={datetime.now(timezone.utc).isoformat()}\n")
                self.handle.flush()
                return self
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise ControllerError(
                        f"Could not acquire controller lock within {self.wait_seconds}s: {self.path}"
                    )
                time.sleep(2)

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.handle:
            try:
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            finally:
                self.handle.close()


class JamfClient:
    RETRY_STATUS = {408, 425, 429, 500, 502, 503, 504}

    def __init__(self, cfg: dict, logger: logging.Logger, dry_run: bool = False):
        self.cfg = cfg
        self.logger = logger
        self.dry_run = dry_run
        self.base_url = cfg["jamf_url"].rstrip("/")
        self.client_id = os.getenv("JAMF_CLIENT_ID", "").strip()
        self.client_secret = os.getenv("JAMF_CLIENT_SECRET", "").strip()
        if not self.client_id or not self.client_secret:
            raise ConfigError("JAMF_CLIENT_ID and JAMF_CLIENT_SECRET must be set in the environment")

        perf = cfg["performance"]
        self.timeout = int(perf["request_timeout_seconds"])
        self.retry_attempts = int(perf["retry_attempts"])
        self._token: Optional[str] = None
        self._token_expires_at = 0.0
        self._token_lock = threading.Lock()
        self._thread_local = threading.local()

    def _session(self) -> requests.Session:
        session = getattr(self._thread_local, "session", None)
        if session is None:
            session = requests.Session()
            session.headers.update({"User-Agent": "Harrow-TimeBase/1.0"})
            self._thread_local.session = session
        return session

    def _obtain_token(self, force: bool = False) -> str:
        # Refresh at least 60 seconds before token expiry.
        if not force and self._token and time.time() < self._token_expires_at - 60:
            return self._token
        with self._token_lock:
            if not force and self._token and time.time() < self._token_expires_at - 60:
                return self._token
            url = f"{self.base_url}/api/v1/oauth/token"
            response = requests.post(
                url,
                data={
                    "grant_type": "client_credentials",
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=self.timeout,
            )
            if response.status_code != 200:
                raise ControllerError(
                    f"OAuth token request failed HTTP {response.status_code}: {response.text[:500]}"
                )
            payload = response.json()
            token = payload.get("access_token")
            expires_in = int(payload.get("expires_in", 1200))
            if not token:
                raise ControllerError("OAuth response did not contain access_token")
            self._token = token
            self._token_expires_at = time.time() + max(expires_in, 120)
            self.logger.info("Obtained Jamf bearer token; expires_in=%ss", expires_in)
            return token

    @staticmethod
    def _retry_delay(response: Optional[requests.Response], attempt: int) -> float:
        if response is not None:
            retry_after = response.headers.get("Retry-After")
            if retry_after:
                try:
                    return min(float(retry_after), 60.0)
                except ValueError:
                    pass
        return min((2 ** attempt) + random.uniform(0.0, 1.0), 30.0)

    def request(
        self,
        method: str,
        path: str,
        *,
        headers: Optional[dict] = None,
        data=None,
        json_body=None,
        params=None,
        expected: Sequence[int] = (200,),
        allow_dry_run_write: bool = False,
    ) -> requests.Response:
        method = method.upper()
        if self.dry_run and method in {"POST", "PUT", "PATCH", "DELETE"} and not allow_dry_run_write:
            raise ControllerError(f"Dry-run blocked unexpected direct write: {method} {path}")

        last_error = None
        for attempt in range(self.retry_attempts):
            token = self._obtain_token()
            merged_headers = {"Authorization": f"Bearer {token}"}
            if headers:
                merged_headers.update(headers)
            response = None
            try:
                response = self._session().request(
                    method,
                    f"{self.base_url}{path}",
                    headers=merged_headers,
                    data=data,
                    json=json_body,
                    params=params,
                    timeout=self.timeout,
                )
                if response.status_code in expected:
                    return response
                if response.status_code == 401 and attempt == 0:
                    self.logger.warning("HTTP 401 for %s %s; refreshing token", method, path)
                    self._obtain_token(force=True)
                    continue
                if response.status_code not in self.RETRY_STATUS:
                    raise ControllerError(
                        f"{method} {path} failed HTTP {response.status_code}: {response.text[:1000]}"
                    )
                last_error = f"HTTP {response.status_code}: {response.text[:300]}"
            except requests.RequestException as exc:
                last_error = repr(exc)

            if attempt < self.retry_attempts - 1:
                delay = self._retry_delay(response, attempt)
                self.logger.warning(
                    "Retrying %s %s after %.1fs (attempt %d/%d; %s)",
                    method,
                    path,
                    delay,
                    attempt + 1,
                    self.retry_attempts,
                    last_error,
                )
                time.sleep(delay)

        raise ControllerError(f"{method} {path} failed after retries: {last_error}")

    def classic_xml(self, method: str, path: str, *, xml_body: Optional[bytes] = None,
                    params=None, expected: Sequence[int] = (200,)) -> ET.Element:
        """Call a Jamf Classic API endpoint and parse its XML response.

        ``params`` is accepted and forwarded to :meth:`request` so callers can use
        query-string options without bypassing the shared OAuth/retry wrapper.
        Keeping this argument here also preserves compatibility with older/newer
        controller components that call ``classic_xml(..., params=...)``.
        """
        response = self.request(
            method,
            path,
            headers={
                "Accept": "application/xml",
                **({"Content-Type": "application/xml"} if xml_body is not None else {}),
            },
            data=xml_body,
            params=params,
            expected=expected,
        )
        if not response.content:
            return ET.Element("empty")
        try:
            return ET.fromstring(response.content)
        except ET.ParseError as exc:
            raise ControllerError(f"Invalid XML from Jamf {path}: {exc}") from exc

    def list_groups(self) -> Dict[str, GroupInfo]:
        root = self.classic_xml("GET", "/JSSResource/mobiledevicegroups", expected=(200,))
        result: Dict[str, GroupInfo] = {}
        for node in root.findall(".//mobile_device_group"):
            name = (node.findtext("name") or "").strip()
            raw_id = (node.findtext("id") or "").strip()
            raw_smart = (node.findtext("is_smart") or "false").strip().lower()
            if name and raw_id.isdigit():
                result[name] = GroupInfo(int(raw_id), name, raw_smart == "true")
        return result

    def get_group_xml(self, group_id: int) -> ET.Element:
        return self.classic_xml(
            "GET", f"/JSSResource/mobiledevicegroups/id/{group_id}", expected=(200,)
        )

    @staticmethod
    def group_members_from_xml(root: ET.Element) -> Set[str]:
        serials = set()
        mobile_devices = root.find("mobile_devices")
        if mobile_devices is None:
            mobile_devices = root.find(".//mobile_devices")
        if mobile_devices is None:
            return serials
        for device in mobile_devices.findall("mobile_device"):
            serial = (device.findtext("serial_number") or "").strip().upper()
            if serial:
                serials.add(serial)
        return serials

    def group_members(self, group_id: int) -> Set[str]:
        return self.group_members_from_xml(self.get_group_xml(group_id))

    @staticmethod
    def group_criteria(root: ET.Element) -> List[Tuple[str, str, str]]:
        criteria = []
        for criterion in root.findall(".//criteria/criterion"):
            criteria.append(
                (
                    (criterion.findtext("name") or "").strip(),
                    (criterion.findtext("search_type") or "").strip(),
                    (criterion.findtext("value") or "").strip(),
                )
            )
        return criteria

    def update_room(self, serial: str, room: str) -> None:
        body = (
            f"<mobile_device><location><room>{escape_xml(room)}</room></location></mobile_device>"
        ).encode("utf-8")
        if self.dry_run:
            self.logger.info("DRY-RUN Room update: %s -> %s", serial, room)
            return
        self.request(
            "PUT",
            f"/JSSResource/mobiledevices/serialnumber/{quote(serial, safe='')}",
            headers={"Content-Type": "application/xml", "Accept": "application/xml"},
            data=body,
            expected=(200, 201),
        )

    def list_profiles(self) -> Dict[str, int]:
        root = self.classic_xml(
            "GET", "/JSSResource/mobiledeviceconfigurationprofiles", expected=(200,)
        )
        result: Dict[str, int] = {}
        for node in root.findall(".//configuration_profile"):
            name = (node.findtext("name") or "").strip()
            raw_id = (node.findtext("id") or "").strip()
            if name and raw_id.isdigit():
                result[name] = int(raw_id)
        return result

    def get_profile_xml(self, profile_id: int) -> ET.Element:
        return self.classic_xml(
            "GET",
            f"/JSSResource/mobiledeviceconfigurationprofiles/id/{profile_id}",
            expected=(200,),
        )

    @staticmethod
    def profile_target_groups(root: ET.Element) -> Dict[str, Optional[int]]:
        scope = root.find("scope")
        result: Dict[str, Optional[int]] = {}
        if scope is None:
            return result
        groups = scope.find("mobile_device_groups")
        if groups is None:
            return result
        for item in groups.findall("mobile_device_group"):
            name = (item.findtext("name") or "").strip()
            raw_id = (item.findtext("id") or "").strip()
            if name:
                result[name] = int(raw_id) if raw_id.isdigit() else None
        return result

    @staticmethod
    def profile_exclusion_groups(root: ET.Element) -> Dict[str, Optional[int]]:
        scope = root.find("scope")
        result: Dict[str, Optional[int]] = {}
        if scope is None:
            return result
        exclusions = scope.find("exclusions")
        if exclusions is None:
            return result
        groups = exclusions.find("mobile_device_groups")
        if groups is None:
            return result
        for item in groups.findall("mobile_device_group"):
            name = (item.findtext("name") or "").strip()
            raw_id = (item.findtext("id") or "").strip()
            if name:
                result[name] = int(raw_id) if raw_id.isdigit() else None
        return result

    def set_profile_target_group(
        self, profile_id: int, group: GroupInfo, enabled: bool
    ) -> bool:
        root = self.get_profile_xml(profile_id)
        scope = root.find("scope")
        if scope is None:
            raise PreflightError(f"Profile ID {profile_id} has no <scope> element")

        all_mobile = scope.find("all_mobile_devices")
        if all_mobile is not None and (all_mobile.text or "").strip().lower() == "true":
            raise PreflightError(
                "Refusing WiFi-Harrow scope mutation because all_mobile_devices=true"
            )

        groups = scope.find("mobile_device_groups")
        if groups is None:
            groups = ET.SubElement(scope, "mobile_device_groups")

        found = []
        for item in groups.findall("mobile_device_group"):
            item_name = (item.findtext("name") or "").strip()
            item_id = (item.findtext("id") or "").strip()
            if item_name == group.name or item_id == str(group.id):
                found.append(item)

        changed = False
        if enabled and not found:
            item = ET.SubElement(groups, "mobile_device_group")
            ET.SubElement(item, "id").text = str(group.id)
            ET.SubElement(item, "name").text = group.name
            changed = True
        elif not enabled and found:
            for item in found:
                groups.remove(item)
            changed = True

        if not changed:
            self.logger.info(
                "WiFi profile target is already %s for group %s",
                "ON" if enabled else "OFF",
                group.name,
            )
            return False

        xml_body = ET.tostring(root, encoding="utf-8", xml_declaration=True)
        if self.dry_run:
            self.logger.info(
                "DRY-RUN WiFi profile scope: group %s -> %s", group.name, enabled
            )
            return True

        self.request(
            "PUT",
            f"/JSSResource/mobiledeviceconfigurationprofiles/id/{profile_id}",
            headers={"Content-Type": "application/xml", "Accept": "application/xml"},
            data=xml_body,
            expected=(200, 201),
        )
        return True


def escape_xml(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def load_config(path: Path) -> dict:
    try:
        cfg = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"Config not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"Invalid JSON config {path}: {exc}") from exc

    required_top = ["jamf_url", "timezone", "groups", "profiles", "rooms", "paths", "performance", "safety"]
    for key in required_top:
        if key not in cfg:
            raise ConfigError(f"Missing config key: {key}")
    if not str(cfg["jamf_url"]).startswith("https://"):
        raise ConfigError("jamf_url must use https://")
    concurrency = int(cfg["performance"].get("concurrency", 4))
    if concurrency < 1 or concurrency > 5:
        raise ConfigError("performance.concurrency must be between 1 and 5")

    attendance_policy = str(cfg["safety"].get("missing_attendance_policy", "error")).strip().lower()
    if attendance_policy not in {"error", "zero_absent"}:
        raise ConfigError(
            "safety.missing_attendance_policy must be either 'error' or 'zero_absent'"
        )
    cfg["safety"]["missing_attendance_policy"] = attendance_policy

    email_policy = str(cfg.get("attendance", {}).get("email_match_policy", "unique")).strip().lower()
    if email_policy not in {"unique", "all_matches"}:
        raise ConfigError("attendance.email_match_policy must be either 'unique' or 'all_matches'")
    cfg.setdefault("attendance", {})["email_match_policy"] = email_policy

    min_coverage = float(cfg["safety"].get("email_inventory_min_coverage", 0.95))
    if not (0.5 <= min_coverage <= 1.0):
        raise ConfigError("safety.email_inventory_min_coverage must be between 0.5 and 1.0")
    cfg["safety"]["email_inventory_min_coverage"] = min_coverage
    return cfg


def setup_logging(cfg: dict, verbose: bool = False) -> logging.Logger:
    logger = logging.getLogger("harrow-timebase")
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    logger.handlers.clear()
    formatter = logging.Formatter(
        fmt="%(asctime)s %(levelname)s %(threadName)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )

    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(formatter)
    logger.addHandler(stream)

    log_path = Path(cfg["paths"]["log_file"])
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            log_path, maxBytes=20 * 1024 * 1024, backupCount=10, encoding="utf-8"
        )
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
    except PermissionError:
        logger.warning("Cannot write log file %s; journald/stdout only", log_path)
    return logger


class TimeBaseController(
    ControllerActionsMixin,
    ControllerAttendanceMixin,
    ControllerStateMixin,
):
    def __init__(self, cfg: dict, logger: logging.Logger, dry_run: bool = False):
        self.cfg = cfg
        self.logger = logger
        self.dry_run = dry_run
        self.tz = ZoneInfo(cfg["timezone"])
        self.jamf = JamfClient(cfg, logger, dry_run=dry_run)
        self.group_cache: Optional[Dict[str, GroupInfo]] = None
        self.profile_cache: Optional[Dict[str, int]] = None

    @property
    def group_names(self) -> dict:
        return self.cfg["groups"]

    @property
    def profile_names(self) -> dict:
        return self.cfg["profiles"]

    @property
    def rooms(self) -> dict:
        return self.cfg["rooms"]

    def now(self) -> datetime:
        return datetime.now(self.tz)

    def groups(self, refresh: bool = False) -> Dict[str, GroupInfo]:
        if refresh or self.group_cache is None:
            self.group_cache = self.jamf.list_groups()
        return self.group_cache

    def profiles(self, refresh: bool = False) -> Dict[str, int]:
        if refresh or self.profile_cache is None:
            self.profile_cache = self.jamf.list_profiles()
        return self.profile_cache

    def require_group(self, name: str) -> GroupInfo:
        group = self.groups().get(name)
        if not group:
            raise PreflightError(f"Required mobile device group not found: {name}")
        return group

    def require_profile(self, name: str) -> int:
        profile_id = self.profiles().get(name)
        if profile_id is None:
            raise PreflightError(f"Required mobile device configuration profile not found: {name}")
        return profile_id

    def master_group(self) -> GroupInfo:
        return self.require_group(self.group_names["master"])

    def in_group(self) -> GroupInfo:
        return self.require_group(self.group_names["in_harrow"])

    def out_group(self) -> GroupInfo:
        return self.require_group(self.group_names["out_harrow"])

    def master_members(self) -> Set[str]:
        return self.jamf.group_members(self.master_group().id)

    def write_master_cache(self, serials: Set[str]) -> None:
        cache_path = Path(
            self.cfg["paths"].get(
                "master_cache_file",
                "/var/lib/harrow-timebase/public/master-serials.txt",
            )
        )
        if self.dry_run:
            self.logger.info("DRY-RUN would update master serial cache %s (%d serials)", cache_path, len(serials))
            return
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = cache_path.with_name(f".{cache_path.name}.{os.getpid()}.tmp")
        tmp.write_text("\n".join(sorted(serials)) + "\n", encoding="utf-8")
        os.chmod(tmp, 0o640)
        os.replace(tmp, cache_path)
        self.logger.info("Master serial cache updated: %s (%d serials)", cache_path, len(serials))

    def current_in_out(self) -> Tuple[Set[str], Set[str]]:
        in_members = self.jamf.group_members(self.in_group().id)
        out_members = self.jamf.group_members(self.out_group().id)
        return in_members, out_members

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Harrow Jamf Pro TimeBase Controller")
    parser.add_argument(
        "--config",
        default="/etc/harrow-timebase/config.json",
        help="Path to JSON configuration",
    )
    parser.add_argument("--dry-run", action="store_true", help="Do not perform Jamf writes")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--serial", default="", help="Serial number for manual override actions")
    parser.add_argument("--email-address", default="", help="Inventory email address for audit metadata")
    parser.add_argument("--username", default="", help="Inventory username for audit metadata")
    parser.add_argument("--device-name", default="", help="Device display name for audit metadata")
    parser.add_argument("--submitted-by", default="", help="Portal user for audit metadata")
    parser.add_argument("--reason", default="", help="Optional manual override reason")
    parser.add_argument(
        "action",
        choices=["preflight", "0700", "0800", "0810", "1600", "reconcile", "verify", "wifi-on", "wifi-off", "manual-out", "manual-clear"],
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        cfg = load_config(Path(args.config))
        logger = setup_logging(cfg, args.verbose)
        lock_path = Path(cfg["paths"]["lock_file"])
        lock_wait = int(cfg["performance"]["lock_wait_seconds"])
        with FileLock(lock_path, lock_wait, logger):
            controller = TimeBaseController(cfg, logger, dry_run=args.dry_run)
            if args.action == "preflight":
                result = controller.preflight()
                print(json.dumps(result, indent=2, sort_keys=True))
            elif args.action == "0700":
                controller.action_0700()
            elif args.action == "0800":
                controller.action_0800()
            elif args.action == "0810":
                controller.action_0810()
            elif args.action == "1600":
                controller.action_1600()
            elif args.action == "reconcile":
                controller.reconcile()
            elif args.action == "verify":
                result = controller.verify_current()
                print(json.dumps(result, indent=2, sort_keys=True))
            elif args.action == "wifi-on":
                controller.preflight()
                controller.set_wifi_scope(True, enforce_attendance_guard=True)
            elif args.action == "wifi-off":
                controller.preflight()
                controller.set_wifi_scope(False, enforce_attendance_guard=False)
            elif args.action == "manual-out":
                if not args.serial:
                    raise ConfigError("--serial is required for manual-out")
                controller.preflight()
                record = controller.set_manual_override(
                    args.serial, email_address=args.email_address, username=args.username,
                    device_name=args.device_name, submitted_by=args.submitted_by, reason=args.reason,
                )
                controller.reconcile(preflight=False)
                print(json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True))
            elif args.action == "manual-clear":
                if not args.serial:
                    raise ConfigError("--serial is required for manual-clear")
                controller.preflight()
                existed = controller.clear_manual_override(args.serial)
                controller.reconcile(preflight=False)
                print(json.dumps({"serial_number": args.serial.upper(), "cleared": existed}, indent=2, sort_keys=True))
        return EXIT_OK
    except ControllerError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return getattr(exc, "exit_code", EXIT_API)
    except Exception as exc:
        print(f"UNHANDLED ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 99


if __name__ == "__main__":
    raise SystemExit(main())
