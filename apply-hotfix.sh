#!/usr/bin/env bash
set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET_DIR=/opt/harrow-timebase
SYSTEMD_DIR=/etc/systemd/system

if [[ $EUID -ne 0 ]]; then
  echo "Run as root: sudo bash apply-hotfix.sh" >&2
  exit 1
fi
if [[ ! -d "$TARGET_DIR" || ! -f "$TARGET_DIR/harrow_timebase.py" ]]; then
  echo "Harrow TimeBase is not installed at $TARGET_DIR" >&2
  exit 1
fi

STAMP="$(date +%Y%m%d-%H%M%S)"
BACKUP="$TARGET_DIR/backups/hotfix-${STAMP}"
mkdir -p \
  "$BACKUP/portal/templates" "$BACKUP/portal/static" "$BACKUP/systemd" \
  "$BACKUP/timebase/controller" "$BACKUP/timebase/importer"

echo "[1/7] Backing up current runtime files to $BACKUP"
for file in \
  harrow_timebase.py attendance_common.py holiday_common.py \
  controller_types.py controller_state.py controller_attendance.py controller_actions.py \
  importer_storage.py importer_handlers.py \
  timebase/__init__.py \
  timebase/controller/__init__.py timebase/controller/types.py \
  timebase/controller/state.py timebase/controller/attendance.py timebase/controller/actions.py \
  timebase/importer/__init__.py timebase/importer/storage.py timebase/importer/handlers.py \
  attendance_importer.py \
  device_query_service.py requirements.txt; do
  [[ -f "$TARGET_DIR/$file" ]] && cp -a "$TARGET_DIR/$file" "$BACKUP/$file"
done
[[ -f "$TARGET_DIR/portal/attendance_portal.py" ]] && cp -a "$TARGET_DIR/portal/attendance_portal.py" "$BACKUP/portal/attendance_portal.py"
[[ -d "$TARGET_DIR/portal/templates" ]] && cp -a "$TARGET_DIR/portal/templates/." "$BACKUP/portal/templates/"
[[ -d "$TARGET_DIR/portal/static" ]] && cp -a "$TARGET_DIR/portal/static/." "$BACKUP/portal/static/"
for unit in "$SRC_DIR"/systemd/*; do
  name="$(basename "$unit")"
  [[ -f "$SYSTEMD_DIR/$name" ]] && cp -a "$SYSTEMD_DIR/$name" "$BACKUP/systemd/$name"
done

# IMPORTANT: do not touch /etc/harrow-timebase/config.json, credentials,
# holidays.csv, attendance files, audit/history, or manual override state.
echo "[2/7] Installing complete application module set"
install -m 0755 "$SRC_DIR/harrow_timebase.py" "$TARGET_DIR/harrow_timebase.py"
install -m 0755 "$SRC_DIR/attendance_importer.py" "$TARGET_DIR/attendance_importer.py"
install -m 0755 "$SRC_DIR/device_query_service.py" "$TARGET_DIR/device_query_service.py"
for module in attendance_common.py holiday_common.py; do
  install -m 0644 "$SRC_DIR/$module" "$TARGET_DIR/$module"
done
install -d -m 0755 \
  "$TARGET_DIR/timebase" \
  "$TARGET_DIR/timebase/controller" \
  "$TARGET_DIR/timebase/importer"
for module in \
  timebase/__init__.py \
  timebase/controller/__init__.py timebase/controller/types.py \
  timebase/controller/state.py timebase/controller/attendance.py timebase/controller/actions.py \
  timebase/importer/__init__.py timebase/importer/storage.py timebase/importer/handlers.py; do
  install -m 0644 "$SRC_DIR/$module" "$TARGET_DIR/$module"
done
rm -f \
  "$TARGET_DIR/controller_types.py" \
  "$TARGET_DIR/controller_state.py" \
  "$TARGET_DIR/controller_attendance.py" \
  "$TARGET_DIR/controller_actions.py" \
  "$TARGET_DIR/importer_storage.py" \
  "$TARGET_DIR/importer_handlers.py"
install -m 0644 "$SRC_DIR/requirements.txt" "$TARGET_DIR/requirements.txt"
install -d -m 0755 "$TARGET_DIR/portal" "$TARGET_DIR/portal/templates" "$TARGET_DIR/portal/static"
install -m 0644 "$SRC_DIR/portal/attendance_portal.py" "$TARGET_DIR/portal/attendance_portal.py"
cp -a "$SRC_DIR/portal/templates/." "$TARGET_DIR/portal/templates/"
cp -a "$SRC_DIR/portal/static/." "$TARGET_DIR/portal/static/"
chown -R root:root "$TARGET_DIR/portal"
find "$TARGET_DIR/portal" -type d -exec chmod 0755 {} +
find "$TARGET_DIR/portal" -type f -exec chmod 0644 {} +

# Repair the cross-service traversal/read permissions without changing config
# contents or credentials. The importer and Portal both read portal.json.
if [[ -d /etc/harrow-timebase ]]; then
  chown root:root /etc/harrow-timebase
  chmod 0711 /etc/harrow-timebase
fi
if [[ -d /var/lib/harrow-timebase ]]; then
  chown harrow-timebase:harrow-timebase /var/lib/harrow-timebase
  chmod 0711 /var/lib/harrow-timebase
fi
if [[ -f /etc/harrow-timebase/portal.json ]]; then
  chown root:root /etc/harrow-timebase/portal.json
  chmod 0644 /etc/harrow-timebase/portal.json
fi
if command -v runuser >/dev/null 2>&1; then
  runuser -u harrow-timebase -- test -r /etc/harrow-timebase/portal.json || {
    echo "Permission repair failed: harrow-timebase cannot read portal.json" >&2
    exit 1
  }
  runuser -u harrow-upload -- test -r /etc/harrow-timebase/portal.json || {
    echo "Permission repair failed: harrow-upload cannot read portal.json" >&2
    exit 1
  }
fi

# Install updated service definitions, but do not enable core 07:00/08:00/08:10/16:00 timers.
echo "[3/7] Installing systemd units"
for unit in "$SRC_DIR"/systemd/*; do
  install -m 0644 "$unit" "$SYSTEMD_DIR/$(basename "$unit")"
done
systemctl daemon-reload

PY="$TARGET_DIR/venv/bin/python"
[[ -x "$PY" ]] || PY=python3

echo "[4/7] Running source compile checks (no __pycache__ write required)"
"$PY" - "$TARGET_DIR" <<'PY'
from pathlib import Path
import sys
root = Path(sys.argv[1])
files = [
    root / "harrow_timebase.py",
    root / "attendance_common.py",
    root / "holiday_common.py",
    root / "timebase" / "__init__.py",
    root / "timebase" / "controller" / "__init__.py",
    root / "timebase" / "controller" / "types.py",
    root / "timebase" / "controller" / "state.py",
    root / "timebase" / "controller" / "attendance.py",
    root / "timebase" / "controller" / "actions.py",
    root / "attendance_importer.py",
    root / "timebase" / "importer" / "__init__.py",
    root / "timebase" / "importer" / "storage.py",
    root / "timebase" / "importer" / "handlers.py",
    root / "device_query_service.py",
    root / "portal" / "attendance_portal.py",
]
for path in files:
    source = path.read_text(encoding="utf-8")
    compile(source, str(path), "exec")
print("Compile checks: PASS")
PY

# Bundle-level regression checks catch the three production regressions that were
# observed on the real server: incomplete hotfix deployment, EXDEV queue moves,
# and missing Classic Email/Serial fallback.
echo "[5/7] Running regression checks"
"$PY" "$SRC_DIR/tests/regression_check.py"
"$PY" "$SRC_DIR/tests/runtime_regression_check.py"
"$PY" "$SRC_DIR/tests/refactor_behavior_check.py"
"$PY" "$SRC_DIR/tests/holiday_range_check.py"

echo "[6/7] Restarting portal/query/queue services"
systemctl restart harrow-device-query.service
systemctl restart harrow-attendance-portal.service
systemctl restart harrow-attendance-import.path

# importer is path-triggered oneshot; do not force-run a queued job here.
# Existing enabled core timers remain in their prior enabled/disabled state.
echo "[7/7] Verifying service health"
systemctl --no-pager --full status harrow-device-query.service harrow-attendance-portal.service harrow-attendance-import.path || true
if command -v curl >/dev/null 2>&1; then
  curl -fsS http://127.0.0.1:8091/healthz >/dev/null && echo "Device Query health: PASS" || echo "Device Query health: WARNING"
  curl -fsS http://127.0.0.1:8090/healthz >/dev/null && echo "Portal health: PASS" || echo "Portal health: WARNING"
fi

echo
echo "Production R3 hotfix installed successfully."
echo "Backup: $BACKUP"
echo "Preserved: config, Jamf credentials, attendance/history, holidays, manual overrides."
echo "Next: re-upload a pilot Email attendance CSV and verify importer/controller logs."
