#!/usr/bin/env python3
from __future__ import annotations

import ast
import importlib.util
import inspect
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def fail(msg: str) -> None:
    raise SystemExit(f"REGRESSION CHECK FAILED: {msg}")


def parse(path: Path) -> ast.Module:
    try:
        return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError as exc:
        fail(f"syntax error in {path.name}: {exc}")


def function_names(tree: ast.Module) -> set[str]:
    return {node.name for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}


# Syntax checks for all production Python files.
py_files = [
    ROOT / "harrow_timebase.py",
    ROOT / "attendance_common.py",
    ROOT / "holiday_common.py",
    ROOT / "timebase" / "__init__.py",
    ROOT / "timebase" / "controller" / "__init__.py",
    ROOT / "timebase" / "controller" / "types.py",
    ROOT / "timebase" / "controller" / "state.py",
    ROOT / "timebase" / "controller" / "attendance.py",
    ROOT / "timebase" / "controller" / "actions.py",
    ROOT / "attendance_importer.py",
    ROOT / "timebase" / "importer" / "__init__.py",
    ROOT / "timebase" / "importer" / "storage.py",
    ROOT / "timebase" / "importer" / "handlers.py",
    ROOT / "device_query_service.py",
    ROOT / "portal" / "attendance_portal.py",
    ROOT / "portal" / "auth_store.py",
]
for path in py_files:
    parse(path)

# classic_xml(params=...) compatibility must remain present.
ht_tree = parse(ROOT / "harrow_timebase.py")
classic = None
for node in ast.walk(ht_tree):
    if isinstance(node, ast.FunctionDef) and node.name == "classic_xml":
        classic = node
        break
if classic is None:
    fail("JamfClient.classic_xml not found")
kwonly = {arg.arg for arg in classic.args.kwonlyargs}
regular = {arg.arg for arg in classic.args.args}
if "params" not in kwonly | regular:
    fail("JamfClient.classic_xml must accept params")

attendance_tree = parse(ROOT / "timebase" / "controller" / "attendance.py")
required_controller_funcs = {
    "_master_inventory_email_index_classic",
    "_classic_location_for_serial",
    "_master_inventory_email_index",
}
missing = required_controller_funcs - function_names(attendance_tree)
if missing:
    fail(f"controller Classic fallback functions missing: {sorted(missing)}")

# The public controller remains compatible while responsibilities live in focused mixins.
controller_class = next(
    (node for node in ht_tree.body if isinstance(node, ast.ClassDef) and node.name == "TimeBaseController"),
    None,
)
if controller_class is None or len(controller_class.bases) < 3:
    fail("TimeBaseController must compose state, attendance, and action responsibilities")

# Importer orchestration must dispatch instead of containing every action workflow.
importer_tree = parse(ROOT / "attendance_importer.py")
handlers_tree = parse(ROOT / "timebase" / "importer" / "handlers.py")
required_handlers = {"handle_manual_override", "handle_holiday_upload", "handle_attendance", "handler_for"}
missing = required_handlers - function_names(handlers_tree)
if missing:
    fail(f"importer action handlers missing: {sorted(missing)}")

# Portal must contain EXDEV-safe replacement helper.
portal_tree = parse(ROOT / "portal" / "attendance_portal.py")
if "safe_replace" not in function_names(portal_tree):
    fail("portal safe_replace() EXDEV helper missing")
portal_text = (ROOT / "portal" / "attendance_portal.py").read_text(encoding="utf-8")
if "errno.EXDEV" not in portal_text:
    fail("portal EXDEV handling missing")

# Device broker must have Classic fallback for exact Email + serial revalidation.
dq_tree = parse(ROOT / "device_query_service.py")
required_broker_funcs = {
    "_classic_search_exact_email",
    "_classic_device_by_serial",
    "search_exact_email",
    "lookup_serial",
}
missing = required_broker_funcs - function_names(dq_tree)
if missing:
    fail(f"device broker fallback functions missing: {sorted(missing)}")

# UTF-8 BOM / Excel-style Email Address regression check.
spec = importlib.util.spec_from_file_location("attendance_common", ROOT / "attendance_common.py")
module = importlib.util.module_from_spec(spec)
sys.modules["attendance_common"] = module
assert spec and spec.loader
spec.loader.exec_module(module)
sample = "\ufeffEmail Address\r\nHarrow02@HarrowBangkok.th\r\nharrow04@harrowbangkok.th\r\n".encode("utf-8")
parsed = module.parse_csv_bytes(sample)
if tuple(parsed.emails) != (
    "harrow02@harrowbangkok.th",
    "harrow04@harrowbangkok.th",
):
    fail(f"UTF-8 BOM Email CSV parse mismatch: {parsed.emails!r}")

# Hotfix must deploy every runtime module rather than harrow_timebase.py only.
hotfix = (ROOT / "apply-hotfix.sh").read_text(encoding="utf-8")
for required in (
    "harrow_timebase.py",
    "attendance_common.py",
    "holiday_common.py",
    "attendance_importer.py",
    "timebase/controller/types.py",
    "timebase/controller/state.py",
    "timebase/controller/attendance.py",
    "timebase/controller/actions.py",
    "timebase/importer/storage.py",
    "timebase/importer/handlers.py",
    "device_query_service.py",
    "portal/attendance_portal.py",
    "portal/auth_store.py",
    "portal/templates",
    "systemd",
):
    if required not in hotfix:
        fail(f"apply-hotfix.sh does not deploy {required}")

# Installer parent directories must be traversable by both deliberately separate
# service accounts while individual config/state files remain group-restricted.
installer = (ROOT / "install-program.sh").read_text(encoding="utf-8")
for required in (
    "install -d -m 0711 -o harrow-timebase -g harrow-timebase /var/lib/harrow-timebase",
    "install -d -m 0711 -o root -g root /etc/harrow-timebase",
    "assert_user_access harrow-timebase",
    "assert_user_access harrow-upload",
    "chown root:root /etc/harrow-timebase/portal.json",
    "chmod 0644 /etc/harrow-timebase/portal.json",
    "Runtime permission checks: PASS",
    "AUTH_DIR=/var/lib/harrow-timebase/portal-auth",
    "auth_store.py",
    "portal_auth_integration_check.py",
    "upload_drag_drop_check.py",
):
    if required not in installer:
        fail(f"install-program.sh permission safeguard missing: {required}")

legacy_installer = (ROOT / "install.sh").read_text(encoding="utf-8")
if 'exec bash "$SRC_DIR/install-program.sh" "$@"' not in legacy_installer:
    fail("install.sh must delegate to install-program.sh")

for required in (
    "chmod 0711 /etc/harrow-timebase",
    "chmod 0711 /var/lib/harrow-timebase",
    "chown root:root /etc/harrow-timebase/portal.json",
    "chmod 0644 /etc/harrow-timebase/portal.json",
    "harrow-timebase cannot read portal.json",
    "portal/auth_store.py",
    "Initializing application login accounts",
    "portal_auth_integration_check.py",
    "upload_drag_drop_check.py",
):
    if required not in hotfix:
        fail(f"apply-hotfix.sh permission repair missing: {required}")

portal_source = (ROOT / "portal" / "attendance_portal.py").read_text(encoding="utf-8")
for required in ("portal_authentication", 'app.get("/login"', 'app.post("/logout"', 'app.post("/change-password"'):
    if required not in portal_source:
        fail(f"portal authentication behavior missing: {required}")

auth_source = (ROOT / "portal" / "auth_store.py").read_text(encoding="utf-8")
for required in ("def add_user", 'sub.add_parser("add-user")', "HARROW_NEW_USER_PASSWORD"):
    if required not in auth_source:
        fail(f"portal add-user behavior missing: {required}")

for nginx_config in (ROOT / "nginx" / "harrow-timebase.conf", ROOT / "nginx" / "harrow-timebase-https.example.conf"):
    if "auth_basic" in nginx_config.read_text(encoding="utf-8"):
        fail(f"obsolete Nginx Basic Authentication remains in {nginx_config.name}")

print("Regression checks: PASS")
