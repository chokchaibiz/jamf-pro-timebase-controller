#!/usr/bin/env bash
set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET_DIR=/opt/harrow-timebase
SYSTEMD_DIR=/etc/systemd/system
AUTH_DIR=/var/lib/harrow-timebase/portal-auth
DEFAULT_ADMIN_PASSWORD="${HARROW_DEFAULT_ADMIN_PASSWORD:-harrow@dmin}"

if [[ $EUID -ne 0 ]]; then
  echo "Run as root: sudo bash apply-hotfix.sh" >&2
  exit 1
fi
if [[ ! -d "$TARGET_DIR" || ! -f "$TARGET_DIR/harrow_timebase.py" ]]; then
  echo "Harrow TimeBase is not installed at $TARGET_DIR" >&2
  exit 1
fi
if ! id harrow-upload >/dev/null 2>&1; then
  echo "Required service account harrow-upload does not exist" >&2
  exit 1
fi

STAMP="$(date +%Y%m%d-%H%M%S)"
BACKUP="$TARGET_DIR/backups/hotfix-${STAMP}"
mkdir -p \
  "$BACKUP/portal/templates" "$BACKUP/portal/static" "$BACKUP/systemd" "$BACKUP/nginx" \
  "$BACKUP/timebase/controller" "$BACKUP/timebase/importer"

echo "[1/10] Backing up current runtime files to $BACKUP"
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
[[ -f "$TARGET_DIR/portal/auth_store.py" ]] && cp -a "$TARGET_DIR/portal/auth_store.py" "$BACKUP/portal/auth_store.py"
[[ -d "$TARGET_DIR/portal/templates" ]] && cp -a "$TARGET_DIR/portal/templates/." "$BACKUP/portal/templates/"
[[ -d "$TARGET_DIR/portal/static" ]] && cp -a "$TARGET_DIR/portal/static/." "$BACKUP/portal/static/"
[[ -d "$AUTH_DIR" ]] && cp -a "$AUTH_DIR" "$BACKUP/portal-auth"
for unit in "$SRC_DIR"/systemd/*; do
  name="$(basename "$unit")"
  [[ -f "$SYSTEMD_DIR/$name" ]] && cp -a "$SYSTEMD_DIR/$name" "$BACKUP/systemd/$name"
done

# IMPORTANT: do not touch /etc/harrow-timebase/config.json, credentials,
# holidays.csv, attendance files, audit/history, or manual override state.
echo "[2/10] Installing complete application module set"
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
install -m 0644 "$SRC_DIR/portal/auth_store.py" "$TARGET_DIR/portal/auth_store.py"
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

PY="$TARGET_DIR/venv/bin/python"
PIP="$TARGET_DIR/venv/bin/pip"
[[ -x "$PY" ]] || PY=python3

echo "[3/10] Updating Python dependencies"
if [[ -x "$PIP" ]]; then
  "$PIP" install -r "$TARGET_DIR/requirements.txt"
else
  echo "WARNING: Installed virtual environment has no pip; dependency update skipped." >&2
fi

echo "[4/10] Initializing application login accounts"
install -d -o harrow-upload -g harrow-upload -m 0700 "$AUTH_DIR"
HARROW_DEFAULT_ADMIN_PASSWORD="$DEFAULT_ADMIN_PASSWORD" \
  "$PY" "$TARGET_DIR/portal/auth_store.py" init-users \
    --auth-dir "$AUTH_DIR" \
    --user admin1 --user admin2 --user admin3 --user admin4 --user admin5
chown -R harrow-upload:harrow-upload "$AUTH_DIR"
find "$AUTH_DIR" -type d -exec chmod 0700 {} +
find "$AUTH_DIR" -type f -exec chmod 0600 {} +

# Install updated service definitions, but do not enable core 07:00/08:00/08:10/16:00 timers.
echo "[5/10] Installing systemd units"
for unit in "$SRC_DIR"/systemd/*; do
  install -m 0644 "$unit" "$SYSTEMD_DIR/$(basename "$unit")"
done
systemctl daemon-reload

echo "[6/10] Running source compile checks (no __pycache__ write required)"
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
    root / "portal" / "auth_store.py",
]
for path in files:
    source = path.read_text(encoding="utf-8")
    compile(source, str(path), "exec")
print("Compile checks: PASS")
PY

# Bundle-level regression checks catch the three production regressions that were
# observed on the real server: incomplete hotfix deployment, EXDEV queue moves,
# and missing Classic Email/Serial fallback.
echo "[7/10] Running regression checks"
"$PY" "$SRC_DIR/tests/regression_check.py"
"$PY" "$SRC_DIR/tests/runtime_regression_check.py"
"$PY" "$SRC_DIR/tests/refactor_behavior_check.py"
"$PY" "$SRC_DIR/tests/holiday_range_check.py"
"$PY" "$SRC_DIR/tests/auth_regression_check.py"
"$PY" "$SRC_DIR/tests/portal_auth_integration_check.py"

echo "[8/10] Restarting portal/query/queue services"
systemctl restart harrow-device-query.service
systemctl restart harrow-attendance-portal.service
systemctl restart harrow-attendance-import.path

echo "[9/10] Switching Nginx from Basic Auth to application login"
NGINX_CHANGED=0
NGINX_FOUND=0
declare -A SEEN_NGINX=()
declare -a CHANGED_NGINX=()
for nginx_root in /etc/nginx/sites-enabled /etc/nginx/sites-available /etc/nginx/conf.d; do
  [[ -d "$nginx_root" ]] || continue
  while IFS= read -r -d '' candidate; do
    real="$(readlink -f "$candidate" 2>/dev/null || true)"
    [[ -n "$real" && -f "$real" ]] || continue
    [[ -z "${SEEN_NGINX[$real]:-}" ]] || continue
    SEEN_NGINX[$real]=1
    if grep -Eq 'proxy_pass[[:space:]]+http://127\.0\.0\.1:8090' "$real"; then
      NGINX_FOUND=1
      if grep -Eq '^[[:space:]]*auth_basic(_user_file)?[[:space:]]+' "$real"; then
        safe_name="$(printf '%s' "$real" | sed 's#/#__#g')"
        cp -a "$real" "$BACKUP/nginx/$safe_name"
        cp -a "$real" "${real}.pre-app-login-${STAMP}"
        sed -i -E 's/^([[:space:]]*)(auth_basic(_user_file)?[[:space:]]+)/\1# Application login disabled: \2/' "$real"
        CHANGED_NGINX+=("$real")
        NGINX_CHANGED=1
        echo "  Disabled Basic Auth in: $real"
      fi
    fi
  done < <(find "$nginx_root" -maxdepth 2 \( -type f -o -type l \) -print0 2>/dev/null)
done

if [[ "$NGINX_CHANGED" -eq 1 ]] && command -v nginx >/dev/null 2>&1; then
  if ! nginx -t; then
    echo "Nginx validation failed; restoring pre-hotfix configuration" >&2
    for real in "${CHANGED_NGINX[@]}"; do
      backup="${real}.pre-app-login-${STAMP}"
      [[ -f "$backup" ]] && cp -a "$backup" "$real"
    done
    nginx -t || true
    exit 1
  fi
  systemctl reload nginx
elif [[ "$NGINX_FOUND" -eq 0 ]]; then
  echo "WARNING: Could not locate an Nginx config proxying to 127.0.0.1:8090." >&2
elif [[ "$NGINX_CHANGED" -eq 0 ]]; then
  echo "  Harrow Nginx proxy found; no active Basic Auth directives required removal."
fi

# importer is path-triggered oneshot; do not force-run a queued job here.
# Existing enabled core timers remain in their prior enabled/disabled state.
echo "[10/10] Verifying service health"
systemctl --no-pager --full status harrow-device-query.service harrow-attendance-portal.service harrow-attendance-import.path || true
if command -v curl >/dev/null 2>&1; then
  curl -fsS http://127.0.0.1:8091/healthz >/dev/null && echo "Device Query health: PASS" || echo "Device Query health: WARNING"
  curl -fsS http://127.0.0.1:8090/healthz >/dev/null && echo "Portal health: PASS" || echo "Portal health: WARNING"
fi

echo
echo "Harrow TimeBase application login hotfix installed successfully."
echo "Backup: $BACKUP"
echo "Initial accounts: admin1, admin2, admin3, admin4, admin5"
echo "Initial password: $DEFAULT_ADMIN_PASSWORD"
echo "Existing application passwords are preserved on future hotfix runs."
echo "Preserved: config, Jamf credentials, attendance/history, holidays, manual overrides."
echo "Next: sign in and immediately change the default password for every account."
