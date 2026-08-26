#!/usr/bin/env python3
"""Read-only Jamf mobile-device query broker for the Harrow portal.

This service owns Jamf API credentials and listens only on 127.0.0.1. The web portal
calls it with a separate internal token, so the portal never receives the Jamf client secret.
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import threading
import time
from pathlib import Path
from typing import Optional
from urllib.parse import quote

from fastapi import FastAPI, Header, HTTPException, Query

from harrow_timebase import ControllerError, TimeBaseController, load_config, setup_logging

CONFIG_PATH = Path(os.environ.get("HARROW_CONFIG", "/etc/harrow-timebase/config.json"))
INTERNAL_TOKEN = os.environ.get("HARROW_INTERNAL_API_TOKEN", "").strip()
CFG = load_config(CONFIG_PATH)
LOGGER = setup_logging(CFG)
CONTROLLER = TimeBaseController(CFG, LOGGER, dry_run=False)
MASTER_TTL = int(CFG.get("device_query", {}).get("master_membership_ttl_seconds", 900))
MAX_RESULTS = int(CFG.get("device_query", {}).get("max_results", 50))

app = FastAPI(title="Harrow Jamf Device Query Broker", docs_url=None, redoc_url=None)
_master_lock = threading.Lock()
_master_serials: set[str] = set()
_master_expires = 0.0


def require_internal_token(value: Optional[str]) -> None:
    if not INTERNAL_TOKEN:
        raise HTTPException(status_code=503, detail="Internal API token is not configured")
    if not value or not secrets.compare_digest(value, INTERNAL_TOKEN):
        raise HTTPException(status_code=403, detail="Forbidden")


def _load_master_from_public_cache() -> set[str]:
    path = Path(CFG["paths"].get("master_cache_file", "/var/lib/harrow-timebase/public/master-serials.txt"))
    if not path.exists():
        return set()
    try:
        # Treat this as a short-lived optimization only. Master group membership is small
        # compared with full inventory and can be refreshed safely when the cache is stale.
        if time.time() - path.stat().st_mtime > max(MASTER_TTL, 60):
            return set()
        return {line.strip().upper() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()}
    except OSError:
        return set()


def master_serials(force: bool = False) -> set[str]:
    global _master_serials, _master_expires
    now = time.monotonic()
    if not force and _master_serials and now < _master_expires:
        return set(_master_serials)
    with _master_lock:
        now = time.monotonic()
        if not force and _master_serials and now < _master_expires:
            return set(_master_serials)
        cached = _load_master_from_public_cache()
        minimum = int(CFG["safety"]["min_master_devices"])
        maximum = int(CFG["safety"]["max_master_devices"])
        if minimum <= len(cached) <= maximum:
            serials = cached
        else:
            CONTROLLER.groups(refresh=True)
            group = CONTROLLER.master_group()
            if group.is_smart:
                raise ControllerError(f"{group.name} must be a Static Mobile Device Group")
            serials = CONTROLLER.master_members()
            if not (minimum <= len(serials) <= maximum):
                raise ControllerError(
                    f"Master group count {len(serials)} outside safety range {minimum}-{maximum}"
                )
            CONTROLLER.write_master_cache(serials)
        _master_serials = set(serials)
        _master_expires = time.monotonic() + max(MASTER_TTL, 60)
        return set(_master_serials)


def _dict(value) -> dict:
    return value if isinstance(value, dict) else {}


def normalize_modern_device(item: dict) -> dict:
    general = _dict(item.get("general"))
    user_loc = _dict(item.get("userAndLocation"))
    if not user_loc:
        user_loc = _dict(item.get("location"))
    hardware = _dict(item.get("hardware"))
    serial = str(general.get("serialNumber") or item.get("serialNumber") or "").strip().upper()
    return {
        "id": general.get("id") or item.get("id") or item.get("mobileDeviceId"),
        "serial_number": serial,
        "device_name": str(general.get("displayName") or general.get("name") or item.get("displayName") or "").strip(),
        "username": str(user_loc.get("username") or item.get("username") or "").strip(),
        "real_name": str(user_loc.get("realName") or user_loc.get("fullName") or "").strip(),
        "email": str(user_loc.get("emailAddress") or user_loc.get("email") or "").strip(),
        "room": str(user_loc.get("room") or "").strip(),
        "model": str(hardware.get("model") or hardware.get("modelDisplay") or general.get("model") or "").strip(),
        "last_inventory": str(general.get("lastInventoryUpdateDate") or general.get("lastInventoryUpdate") or general.get("lastContactDate") or "").strip(),
    }


def _rsql_literal(value: str) -> str:
    # Escape user-supplied wildcard/special characters. Wildcards used by the service
    # itself are added outside this escaped literal.
    return value.replace("\\", "\\\\").replace("*", "\\*").replace('"', '\\"')


def _mobile_inventory_query(filter_value: str, *, page_size: int = MAX_RESULTS) -> list[dict]:
    params = [
        ("page", 0),
        ("page-size", min(max(page_size, 1), MAX_RESULTS)),
        ("sort", "username:asc"),
        ("filter", filter_value),
        ("section", "GENERAL"),
        ("section", "USER_AND_LOCATION"),
        ("section", "HARDWARE"),
    ]
    response = CONTROLLER.jamf.request(
        "GET", "/api/v2/mobile-devices/detail", params=params, expected=(200,)
    )
    payload = response.json()
    rows = payload.get("results", []) if isinstance(payload, dict) else []
    return [normalize_modern_device(x) for x in rows if isinstance(x, dict)] if isinstance(rows, list) else []


def _filter_against_master(rows: list[dict], master: set[str]) -> list[dict]:
    seen = set()
    result = []
    for device in rows:
        serial = str(device.get("serial_number", "")).strip().upper()
        if not serial or serial not in master or serial in seen:
            continue
        seen.add(serial)
        result.append(device)
    return result[:MAX_RESULTS]


def _master_filtered(rows: list[dict]) -> list[dict]:
    master = master_serials()
    result = _filter_against_master(rows, master)
    # A recently added device may not yet be in the short-lived membership cache.
    # Refresh the static group once only when Jamf returned candidates but all were filtered out.
    if rows and not result:
        result = _filter_against_master(rows, master_serials(force=True))
    return result



def _normalize_classic_device(root) -> dict:
    general = root.find(".//general")
    location = root.find(".//location")
    hardware = root.find(".//hardware")
    general = general if general is not None else root
    location = location if location is not None else root
    hardware = hardware if hardware is not None else root
    serial = (
        general.findtext("serial_number")
        or root.findtext(".//serial_number")
        or ""
    ).strip().upper()
    return {
        "serial_number": serial,
        "device_name": (
            general.findtext("display_name")
            or general.findtext("device_name")
            or general.findtext("name")
            or ""
        ).strip(),
        "username": (location.findtext("username") or "").strip(),
        "email": (
            location.findtext("email_address")
            or location.findtext("email")
            or ""
        ).strip().lower(),
        "room": (location.findtext("room") or "").strip(),
        "model": (
            hardware.findtext("model")
            or general.findtext("model")
            or ""
        ).strip(),
        "last_inventory": (
            general.findtext("last_inventory_update")
            or general.findtext("last_inventory_update_epoch")
            or general.findtext("last_contact_time")
            or ""
        ).strip(),
    }


def _classic_device_by_serial(serial: str) -> dict:
    root = CONTROLLER.jamf.classic_xml(
        "GET",
        f"/JSSResource/mobiledevices/serialnumber/{quote(serial, safe='')}/subset/General&Location",
        expected=(200,),
    )
    device = _normalize_classic_device(root)
    if not device.get("serial_number"):
        device["serial_number"] = serial.strip().upper()
    return device


def _classic_device_by_id(device_id: str) -> dict:
    root = CONTROLLER.jamf.classic_xml(
        "GET",
        f"/JSSResource/mobiledevices/id/{quote(str(device_id), safe='')}/subset/General&Location",
        expected=(200,),
    )
    return _normalize_classic_device(root)


def _classic_search_exact_email(email_address: str) -> list[dict]:
    """Classic fallback for tenants whose v2 detail rows omit serialNumber."""
    email_address = email_address.strip().lower()
    root = CONTROLLER.jamf.classic_xml(
        "GET",
        f"/JSSResource/mobiledevices/match/{quote(email_address, safe='')}",
        expected=(200,),
    )
    master = master_serials()
    candidates: list[tuple[str, str]] = []
    for node in root.findall(".//mobile_device"):
        serial = (node.findtext("serial_number") or "").strip().upper()
        device_id = (node.findtext("id") or "").strip()
        if serial:
            candidates.append(("serial", serial))
        elif device_id:
            candidates.append(("id", device_id))

    result: list[dict] = []
    seen: set[str] = set()
    # Match is only a candidate search. Re-read Location and enforce exact Email + master membership.
    for kind, value in candidates[: max(MAX_RESULTS * 4, MAX_RESULTS)]:
        try:
            device = _classic_device_by_serial(value) if kind == "serial" else _classic_device_by_id(value)
        except ControllerError as exc:
            LOGGER.warning("Classic email candidate lookup failed %s=%s: %s", kind, value, exc)
            continue
        serial = str(device.get("serial_number", "")).strip().upper()
        email = str(device.get("email", "")).strip().lower()
        if not serial or serial not in master or serial in seen or email != email_address:
            continue
        seen.add(serial)
        result.append(device)
        if len(result) >= MAX_RESULTS:
            break

    LOGGER.info(
        "Classic Device Override email search: email=%s candidates=%d exact_master_matches=%d",
        email_address,
        len(candidates),
        len(result),
    )
    return result


def search_exact_email(email_address: str) -> list[dict]:
    """Return master-group devices whose Jamf inventory Email Address exactly matches.

    Prefer Jamf Pro v2 filtering. If the tenant returns rows without usable Serial
    Numbers (verified on this Jamf tenant) or returns no exact usable master result,
    fall back to the Classic mobile-device match + General&Location lookup.
    """
    email_address = email_address.strip().lower()
    literal = _rsql_literal(email_address)
    modern_rows: list[dict] = []
    try:
        raw_rows = _mobile_inventory_query(f'emailAddress=="{literal}"')
        modern_rows = _master_filtered(raw_rows)
        exact = [
            row for row in modern_rows
            if str(row.get("email", "")).strip().lower() == email_address
        ]
        if exact:
            return exact
        if raw_rows:
            LOGGER.warning(
                "Jamf Pro v2 email search returned %d row(s) but no usable exact master device; "
                "using Classic fallback (possible serialNumber omission)",
                len(raw_rows),
            )
    except ControllerError as exc:
        LOGGER.warning("Jamf Pro v2 email search failed; using Classic fallback: %s", exc)

    return _classic_search_exact_email(email_address)


def lookup_serial(serial: str) -> dict:
    serial = serial.strip().upper()
    if serial not in master_serials() and serial not in master_serials(force=True):
        raise HTTPException(status_code=404, detail="Device is not in Harrow-All-iPads")

    literal = _rsql_literal(serial)
    try:
        rows = _master_filtered(_mobile_inventory_query(f'serialNumber=="{literal}"', page_size=2))
        exact = [d for d in rows if str(d.get("serial_number", "")).upper() == serial]
        if exact:
            return exact[0]
        LOGGER.warning(
            "Jamf Pro v2 serial lookup returned no usable serial for %s; using Classic fallback",
            serial,
        )
    except ControllerError as exc:
        LOGGER.warning("Jamf Pro v2 serial lookup failed for %s; using Classic fallback: %s", serial, exc)

    try:
        device = _classic_device_by_serial(serial)
    except ControllerError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    if str(device.get("serial_number", "")).strip().upper() != serial:
        raise HTTPException(status_code=404, detail="Device is not present in Jamf mobile device inventory")
    if serial not in master_serials() and serial not in master_serials(force=True):
        raise HTTPException(status_code=404, detail="Device is not in Harrow-All-iPads")
    return device


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.get("/search")
def search(
    email: str = Query(..., min_length=5, max_length=254),
    x_internal_token: Optional[str] = Header(default=None),
):
    require_internal_token(x_internal_token)
    query = email.strip().lower()
    if not query or "@" not in query or any(ch.isspace() for ch in query):
        raise HTTPException(status_code=400, detail="A valid Email Address is required")
    try:
        rows = search_exact_email(query)
        return {"query": query, "mode": "exact-email", "count": len(rows), "devices": rows[:MAX_RESULTS]}
    except ControllerError as exc:
        LOGGER.error("Device email search failed query=%s: %s", query, exc)
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/device/{serial}")
def device(serial: str, x_internal_token: Optional[str] = Header(default=None)):
    require_internal_token(x_internal_token)
    try:
        return lookup_serial(serial)
    except HTTPException:
        raise
    except ControllerError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
