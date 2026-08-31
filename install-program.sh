#!/usr/bin/env bash
set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AUTH_DIR=/var/lib/harrow-timebase/portal-auth
DEFAULT_ADMIN_PASSWORD="${HARROW_DEFAULT_ADMIN_PASSWORD:-harrow@dmin}"

if [[ $EUID -ne 0 ]]; then
  echo "Run as root: sudo bash install-program.sh" >&2
  exit 1
fi

install_os_dependencies() {
  if command -v apt-get >/dev/null 2>&1; then
    export DEBIAN_FRONTEND=noninteractive
    apt-get update
    apt-get install -y python3 python3-venv python3-pip nginx openssl
  elif command -v dnf >/dev/null 2>&1; then
    dnf install -y python3 python3-pip nginx openssl
  elif command -v yum >/dev/null 2>&1; then
    yum install -y python3 python3-pip nginx openssl
  else
    echo "Unsupported package manager. Install Python 3 + venv/pip, Nginx, and OpenSSL manually." >&2
    exit 1
  fi
}

install_os_dependencies

echo "Running bundle regression checks before installation..."
python3 "$SRC_DIR/tests/regression_check.py"
python3 "$SRC_DIR/tests/holiday_range_check.py"
python3 "$SRC_DIR/tests/upload_drag_drop_check.py"

if ! id harrow-timebase >/dev/null 2>&1; then
  useradd --system --home-dir /var/lib/harrow-timebase --create-home --shell /usr/sbin/nologin harrow-timebase
fi
if ! id harrow-upload >/dev/null 2>&1; then
  useradd --system --home-dir /var/lib/harrow-upload --create-home --shell /usr/sbin/nologin harrow-upload
fi
# Security boundary: harrow-upload must NOT be a member of harrow-timebase, because that group can read the Jamf credential file.
if command -v gpasswd >/dev/null 2>&1; then
  gpasswd -d harrow-upload harrow-timebase >/dev/null 2>&1 || true
fi

# Core directories.
install -d -m 0755 -o root -g root /opt/harrow-timebase
install -d -m 0750 -o harrow-timebase -g harrow-timebase /opt/harrow-timebase/attendance
install -d -m 0750 -o harrow-timebase -g harrow-timebase /opt/harrow-timebase/archive
install -d -m 0755 -o root -g root /opt/harrow-timebase/portal /opt/harrow-timebase/portal/templates /opt/harrow-timebase/portal/static
# Both service accounts need to traverse this parent. Child directories retain
# their stricter owner/group permissions, and the parent cannot be listed.
install -d -m 0711 -o harrow-timebase -g harrow-timebase /var/lib/harrow-timebase
install -d -m 2750 -o harrow-timebase -g harrow-upload /var/lib/harrow-timebase/shared
install -d -m 2770 -o harrow-upload -g harrow-timebase /var/lib/harrow-timebase/portal-staging
install -d -m 2770 -o harrow-upload -g harrow-timebase /var/lib/harrow-timebase/upload-queue
install -d -m 2750 -o harrow-timebase -g harrow-upload /var/lib/harrow-timebase/upload-status
install -d -m 2750 -o harrow-timebase -g harrow-upload /var/lib/harrow-timebase/public
install -d -m 0700 -o harrow-upload -g harrow-upload "$AUTH_DIR"
install -d -m 0750 -o harrow-timebase -g harrow-timebase /var/log/harrow-timebase
# harrow-timebase and harrow-upload read different group-restricted files here.
# Execute-only access for non-root users permits traversal without directory listing.
install -d -m 0711 -o root -g root /etc/harrow-timebase

# Application files.
install -m 0755 -o root -g root "$SRC_DIR/harrow_timebase.py" /opt/harrow-timebase/harrow_timebase.py
install -m 0755 -o root -g root "$SRC_DIR/attendance_importer.py" /opt/harrow-timebase/attendance_importer.py
install -m 0755 -o root -g root "$SRC_DIR/device_query_service.py" /opt/harrow-timebase/device_query_service.py
for module in attendance_common.py holiday_common.py; do
  install -m 0644 -o root -g root "$SRC_DIR/$module" "/opt/harrow-timebase/$module"
done
install -d -m 0755 -o root -g root \
  /opt/harrow-timebase/timebase \
  /opt/harrow-timebase/timebase/controller \
  /opt/harrow-timebase/timebase/importer
for module in \
  timebase/__init__.py \
  timebase/controller/__init__.py timebase/controller/types.py \
  timebase/controller/state.py timebase/controller/attendance.py timebase/controller/actions.py \
  timebase/importer/__init__.py timebase/importer/storage.py timebase/importer/handlers.py; do
  install -m 0644 -o root -g root "$SRC_DIR/$module" "/opt/harrow-timebase/$module"
done
# Remove pre-package module names left by the first responsibility-split release.
rm -f \
  /opt/harrow-timebase/controller_types.py \
  /opt/harrow-timebase/controller_state.py \
  /opt/harrow-timebase/controller_attendance.py \
  /opt/harrow-timebase/controller_actions.py \
  /opt/harrow-timebase/importer_storage.py \
  /opt/harrow-timebase/importer_handlers.py
install -m 0644 -o root -g root "$SRC_DIR/requirements.txt" /opt/harrow-timebase/requirements.txt

# Persistent holiday calendar. Migrate the previous /opt file on upgrade, then keep
# a compatibility symlink so existing config.json files continue to work unchanged.
HOLIDAY_SHARED=/var/lib/harrow-timebase/shared/holidays.csv
if [[ ! -f "$HOLIDAY_SHARED" ]]; then
  if [[ -f /opt/harrow-timebase/holidays.csv && ! -L /opt/harrow-timebase/holidays.csv ]]; then
    install -m 0640 -o harrow-timebase -g harrow-upload /opt/harrow-timebase/holidays.csv "$HOLIDAY_SHARED"
  else
    install -m 0640 -o harrow-timebase -g harrow-upload "$SRC_DIR/holidays.csv" "$HOLIDAY_SHARED"
  fi
fi
chown harrow-timebase:harrow-upload "$HOLIDAY_SHARED"
chmod 0640 "$HOLIDAY_SHARED"
rm -f /opt/harrow-timebase/holidays.csv
ln -s "$HOLIDAY_SHARED" /opt/harrow-timebase/holidays.csv

# Persistent manual Out-Harrow override state. Portal can read it; only harrow-timebase writes it.
MANUAL_OVERRIDE_FILE=/var/lib/harrow-timebase/shared/manual-overrides.json
if [[ ! -f "$MANUAL_OVERRIDE_FILE" ]]; then
  printf '{"version":1,"overrides":{}}\n' > "$MANUAL_OVERRIDE_FILE"
fi
chown harrow-timebase:harrow-upload "$MANUAL_OVERRIDE_FILE"
chmod 0640 "$MANUAL_OVERRIDE_FILE"

# Internal token shared only between the unprivileged Portal and localhost Device Query broker.
INTERNAL_API_ENV=/var/lib/harrow-timebase/shared/internal-api.env
if [[ ! -f "$INTERNAL_API_ENV" ]]; then
  INTERNAL_API_TOKEN="$(python3 - <<'PY'
import secrets
print(secrets.token_urlsafe(40))
PY
)"
  printf 'HARROW_INTERNAL_API_TOKEN=%s\n' "$INTERNAL_API_TOKEN" > "$INTERNAL_API_ENV"
fi
chown harrow-timebase:harrow-upload "$INTERNAL_API_ENV"
chmod 0640 "$INTERNAL_API_ENV"

install -m 0644 "$SRC_DIR/portal/attendance_portal.py" /opt/harrow-timebase/portal/attendance_portal.py
install -m 0644 "$SRC_DIR/portal/auth_store.py" /opt/harrow-timebase/portal/auth_store.py
cp -a "$SRC_DIR/portal/templates/." /opt/harrow-timebase/portal/templates/
cp -a "$SRC_DIR/portal/static/." /opt/harrow-timebase/portal/static/
chown -R root:root /opt/harrow-timebase/portal
find /opt/harrow-timebase/portal -type d -exec chmod 0755 {} +
find /opt/harrow-timebase/portal -type f -exec chmod 0644 {} +

python3 -m venv /opt/harrow-timebase/venv
/opt/harrow-timebase/venv/bin/pip install --upgrade pip
/opt/harrow-timebase/venv/bin/pip install -r /opt/harrow-timebase/requirements.txt
chown -R root:root /opt/harrow-timebase/venv
chmod -R u=rwX,go=rX /opt/harrow-timebase/venv

# Initialize the application login once. Upgrades preserve existing users,
# password hashes, and the session signing key.
HARROW_DEFAULT_ADMIN_PASSWORD="$DEFAULT_ADMIN_PASSWORD" \
  /opt/harrow-timebase/venv/bin/python /opt/harrow-timebase/portal/auth_store.py init-users \
    --auth-dir "$AUTH_DIR" \
    --user admin1 --user admin2 --user admin3 --user admin4 --user admin5
chown -R harrow-upload:harrow-upload "$AUTH_DIR"
find "$AUTH_DIR" -type d -exec chmod 0700 {} +
find "$AUTH_DIR" -type f -exec chmod 0600 {} +

echo "Running runtime regression checks..."
/opt/harrow-timebase/venv/bin/python "$SRC_DIR/tests/runtime_regression_check.py"
/opt/harrow-timebase/venv/bin/python "$SRC_DIR/tests/refactor_behavior_check.py"
/opt/harrow-timebase/venv/bin/python "$SRC_DIR/tests/holiday_range_check.py"
/opt/harrow-timebase/venv/bin/python "$SRC_DIR/tests/auth_regression_check.py"
/opt/harrow-timebase/venv/bin/python "$SRC_DIR/tests/portal_auth_integration_check.py"
/opt/harrow-timebase/venv/bin/python "$SRC_DIR/tests/upload_drag_drop_check.py"

# Prefer production configuration when bundled; preserve an existing installed config.
CONFIG_SOURCE="$SRC_DIR/config.example.json"
ENV_SOURCE="$SRC_DIR/harrow-timebase.env.example"
PORTAL_CONFIG_SOURCE="$SRC_DIR/portal-config.example.json"
[[ -f "$SRC_DIR/config.production.json" ]] && CONFIG_SOURCE="$SRC_DIR/config.production.json"
[[ -f "$SRC_DIR/harrow-timebase.env.production" ]] && ENV_SOURCE="$SRC_DIR/harrow-timebase.env.production"
[[ -f "$SRC_DIR/portal-config.production.json" ]] && PORTAL_CONFIG_SOURCE="$SRC_DIR/portal-config.production.json"

if [[ ! -f /etc/harrow-timebase/config.json ]]; then
  install -m 0640 -o root -g harrow-timebase "$CONFIG_SOURCE" /etc/harrow-timebase/config.json
fi
if [[ ! -f /etc/harrow-timebase/harrow-timebase.env ]]; then
  install -m 0640 -o root -g harrow-timebase "$ENV_SOURCE" /etc/harrow-timebase/harrow-timebase.env
fi
if [[ ! -f /etc/harrow-timebase/portal.json ]]; then
  install -m 0644 -o root -g root "$PORTAL_CONFIG_SOURCE" /etc/harrow-timebase/portal.json
fi
chown root:harrow-timebase \
  /etc/harrow-timebase/config.json \
  /etc/harrow-timebase/harrow-timebase.env
chmod 0640 \
  /etc/harrow-timebase/config.json \
  /etc/harrow-timebase/harrow-timebase.env
# portal.json contains paths and service settings, not credentials. Both isolated
# service accounts need it: the Portal reads presentation settings and the
# privileged importer reads queue/status/archive paths.
chown root:root /etc/harrow-timebase/portal.json
chmod 0644 /etc/harrow-timebase/portal.json

# Generate a CSRF-like form token that is only available to the portal process.
if [[ ! -f /etc/harrow-timebase/portal.env ]]; then
  FORM_TOKEN="$(python3 - <<'PY'
import secrets
print(secrets.token_urlsafe(32))
PY
)"
  umask 0027
  printf 'HARROW_PORTAL_FORM_TOKEN=%s\n' "$FORM_TOKEN" > /etc/harrow-timebase/portal.env
  chown root:harrow-upload /etc/harrow-timebase/portal.env
  chmod 0640 /etc/harrow-timebase/portal.env
fi
chown root:harrow-upload /etc/harrow-timebase/portal.env
chmod 0640 /etc/harrow-timebase/portal.env

assert_user_access() {
  local user="$1"
  local permission="$2"
  local path="$3"
  if command -v runuser >/dev/null 2>&1; then
    if ! runuser -u "$user" -- test "-$permission" "$path"; then
      echo "Permission check failed: user=$user requires -$permission $path" >&2
      exit 1
    fi
  elif command -v su >/dev/null 2>&1; then
    if ! su -s /bin/sh -c "test -$permission '$path'" "$user"; then
      echo "Permission check failed: user=$user requires -$permission $path" >&2
      exit 1
    fi
  else
    echo "Cannot validate service permissions: runuser or su is required" >&2
    exit 1
  fi
}

echo "Validating runtime permissions for both service accounts..."
for path in \
  /etc/harrow-timebase/config.json \
  /etc/harrow-timebase/harrow-timebase.env \
  /etc/harrow-timebase/portal.json \
  /var/lib/harrow-timebase/shared/internal-api.env; do
  assert_user_access harrow-timebase r "$path"
done
for path in \
  /etc/harrow-timebase/portal.json \
  /etc/harrow-timebase/portal.env \
  /var/lib/harrow-timebase/shared/internal-api.env \
  /var/lib/harrow-timebase/shared/holidays.csv \
  "$AUTH_DIR/users.json" \
  "$AUTH_DIR/session.key" \
  /opt/harrow-timebase/portal/attendance_portal.py; do
  assert_user_access harrow-upload r "$path"
done
for path in \
  /opt/harrow-timebase/attendance \
  /opt/harrow-timebase/archive \
  /var/lib/harrow-timebase \
  /var/log/harrow-timebase; do
  assert_user_access harrow-timebase x "$path"
  assert_user_access harrow-timebase w "$path"
done
for path in \
  /var/lib/harrow-timebase/portal-staging \
  /var/lib/harrow-timebase/upload-queue; do
  assert_user_access harrow-upload x "$path"
  assert_user_access harrow-upload w "$path"
done
assert_user_access harrow-upload x /var/lib/harrow-timebase/upload-status
assert_user_access harrow-upload x /var/lib/harrow-timebase/public
assert_user_access harrow-upload x "$AUTH_DIR"
assert_user_access harrow-upload w "$AUTH_DIR"
echo "Runtime permission checks: PASS"

# Remove obsolete full-inventory polling units from older bundle versions.
systemctl disable --now harrow-inventory-sync.timer harrow-inventory-sync.service >/dev/null 2>&1 || true
rm -f /etc/systemd/system/harrow-inventory-sync.timer /etc/systemd/system/harrow-inventory-sync.service
rm -f /opt/harrow-timebase/inventory_sync.py

# systemd units.
for unit in "$SRC_DIR"/systemd/*; do
  install -m 0644 "$unit" "/etc/systemd/system/$(basename "$unit")"
done

# Nginx reverse proxy and TLS. Authentication is handled by the portal.
install -m 0644 "$SRC_DIR/nginx/harrow-timebase.conf" /etc/nginx/conf.d/harrow-timebase.conf
install -d -m 0700 /etc/nginx/harrow-timebase-tls
if [[ ! -f /etc/nginx/harrow-timebase-tls/server.crt || ! -f /etc/nginx/harrow-timebase-tls/server.key ]]; then
  TLS_CN="$(hostname -f 2>/dev/null || hostname)"
  openssl req -x509 -nodes -newkey rsa:2048 -sha256 -days 825 \
    -keyout /etc/nginx/harrow-timebase-tls/server.key \
    -out /etc/nginx/harrow-timebase-tls/server.crt \
    -subj "/CN=${TLS_CN}" >/dev/null 2>&1
  chmod 0600 /etc/nginx/harrow-timebase-tls/server.key
  chmod 0644 /etc/nginx/harrow-timebase-tls/server.crt
fi
if command -v restorecon >/dev/null 2>&1; then
  restorecon -RF /etc/nginx/harrow-timebase-tls /etc/nginx/conf.d/harrow-timebase.conf || true
fi
nginx -t
systemctl daemon-reload
if command -v getenforce >/dev/null 2>&1 && [[ "$(getenforce)" == "Enforcing" ]] && command -v setsebool >/dev/null 2>&1; then
  setsebool -P httpd_can_network_connect 1
fi
systemctl enable --now nginx
systemctl restart nginx
systemctl enable --now harrow-attendance-portal.service
systemctl enable --now harrow-attendance-import.path
systemctl enable --now harrow-device-query.service

SERVER_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
[[ -z "$SERVER_IP" ]] && SERVER_IP="SERVER-IP"

echo
echo "============================================================"
echo "Harrow TimeBase + Attendance Upload Portal installed"
echo "============================================================"
echo "Portal URL : https://${SERVER_IP}:8443/"
echo "Initial users: admin1, admin2, admin3, admin4, admin5"
echo "Initial password: ${DEFAULT_ADMIN_PASSWORD}"
echo "Existing application passwords are preserved on future installer runs."
cat <<'MSG'

Portal and upload queue are running now.
The first connection may show a browser warning because the installer creates a self-signed TLS certificate. Replace it with an organization-trusted certificate for normal production use.
Core Jamf 07:00/08:00/08:10/16:00 timers are NOT enabled automatically.

RECOMMENDED NEXT STEPS:
  1. Sign in and immediately change the default password for every portal account.
  2. Upload/verify the official holidays.csv from the Upload Holidays card.
  3. Run Jamf preflight and verify the localhost Device Query broker:
       sudo systemctl start harrow-timebase@preflight.service
       sudo journalctl -u harrow-timebase@preflight.service -u harrow-device-query.service -n 150 --no-pager
       curl -s http://127.0.0.1:8091/healthz
  4. Test Upload + Device Override with pilot devices.
  5. Verify portal/import/query services:
       systemctl status harrow-attendance-portal.service harrow-attendance-import.path harrow-device-query.service
  6. When pilot validation is complete, enable core timers:
       sudo systemctl enable --now \
         harrow-timebase-0700.timer \
         harrow-timebase-0800.timer \
         harrow-timebase-0810.timer \
         harrow-timebase-1600.timer \
         harrow-timebase-reconcile.timer

If host firewall is enabled, allow TCP/8443 only from the school's trusted admin network.
For production Internet/routed access, terminate TLS (HTTPS) at Nginx or the organization's reverse proxy/load balancer.
MSG
