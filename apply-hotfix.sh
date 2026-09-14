#!/usr/bin/env bash
# Live schedule migration. Production config/env and attendance/state are never rewritten.
set -Eeuo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET_DIR=/opt/harrow-timebase
SYSTEMD_DIR=/etc/systemd/system
CONFIG=/etc/harrow-timebase/config.json
AUTH_DIR=/var/lib/harrow-timebase/portal-auth

[[ $EUID -eq 0 ]] || { echo "Run as root: sudo bash apply-hotfix.sh" >&2; exit 1; }
[[ -f "$TARGET_DIR/harrow_timebase.py" ]] || { echo "TimeBase is not installed" >&2; exit 1; }
[[ "$SRC_DIR" != "$TARGET_DIR" ]] || { echo "Extract the hotfix into a separate directory first" >&2; exit 1; }
exec 8>/run/lock/harrow-timebase-hotfix.lock
flock -n 8 || { echo "Another hotfix is running" >&2; exit 1; }
PY="$TARGET_DIR/venv/bin/python"
[[ -x "$PY" ]] || PY=python3

# Fail before pausing production if the bundle/config is invalid.
echo "[1/6] Validating bundle and production configuration"
"$PY" - "$SRC_DIR" "$CONFIG" "$AUTH_DIR" <<'CHECK'
import json, sys
from pathlib import Path
root = Path(sys.argv[1])
for path in [root / 'harrow_timebase.py', root / 'attendance_importer.py',
             root / 'device_query_service.py', root / 'attendance_common.py',
             root / 'holiday_common.py', *root.glob('timebase/**/*.py'),
             *root.glob('portal/**/*.py')]:
    compile(path.read_text(), str(path), 'exec')
# This schedule hotfix targets an existing R4 login installation. Never create
# users or regenerate the session key as part of a scheduler upgrade.
auth = Path(sys.argv[3])
users = json.loads((auth / 'users.json').read_text())
if not isinstance(users, dict) or not isinstance(users.get('users'), dict) or not users['users']:
    raise SystemExit('Existing R4 portal users are required before this schedule hotfix')
if len((auth / 'session.key').read_bytes()) < 32:
    raise SystemExit('Existing R4 portal session key is invalid')
cfg = json.loads(Path(sys.argv[2]).read_text())
policy = str(cfg.get('attendance', {}).get('unmatched_email_policy', 'skip')).strip().lower()
if policy not in {'skip', 'error'}:
    raise SystemExit('attendance.unmatched_email_policy must be skip or error')
print('Attendance unmatched email policy after upgrade: ' + policy)
if cfg.get('timezone') != 'Asia/Bangkok':
    raise SystemExit('Schedule migration requires timezone=Asia/Bangkok')
if cfg.get('features', {}).get('wifi_management_enabled', False) is not False:
    raise SystemExit('Set features.wifi_management_enabled=false before this migration')
CHECK
for check in regression_check.py runtime_regression_check.py refactor_behavior_check.py holiday_range_check.py schedule_check.py unmatched_attendance_check.py auth_regression_check.py portal_auth_integration_check.py upload_drag_drop_check.py; do
  "$PY" "$SRC_DIR/tests/$check"
done
for unit in "$SRC_DIR"/systemd/*.timer; do
  while IFS= read -r calendar; do
    systemd-analyze calendar "${calendar#OnCalendar=}" >/dev/null
  done < <(sed -n '/^OnCalendar=/p' "$unit")
done

STAMP="$(date +%Y%m%d-%H%M%S)-$$"
BACKUP="$TARGET_DIR/backups/hotfix-${STAMP}"
install -d -m 0700 "$BACKUP" "$BACKUP/systemd"
# Snapshots contain credentials; keep the entire backup accessible only to root.
cp -a /etc/harrow-timebase "$BACKUP/config"
TIMERS=(harrow-timebase-0700.timer harrow-timebase-0800.timer harrow-timebase-0810.timer
        harrow-timebase-school-start.timer harrow-timebase-attendance.timer
        harrow-timebase-1600.timer harrow-timebase-reconcile.timer harrow-timebase-reconcile-extra.timer)
SERVICES=(harrow-attendance-import.path harrow-attendance-portal.service harrow-device-query.service)
for unit in "${TIMERS[@]}" "${SERVICES[@]}"; do
  enabled_state="$(systemctl is-enabled "$unit" 2>/dev/null || true)"
  active_state="$(systemctl is-active "$unit" 2>/dev/null || true)"
  enabled_state="${enabled_state:-not-found}"
  active_state="${active_state:-inactive}"
  if [[ "$enabled_state" == masked* ]]; then
    echo "Resolve masked unit $unit before applying the hotfix" >&2
    exit 1
  fi
  printf '%s %s %s\n' "$unit" "$enabled_state" "$active_state" >> "$BACKUP/unit-state.txt"
done
enabled() { awk -v unit="$1" '$1 == unit {print $2}' "$BACKUP/unit-state.txt"; }
active() { awk -v unit="$1" '$1 == unit {print $3}' "$BACKUP/unit-state.txt"; }

restore_activity() {
  for unit in "${SERVICES[@]}" "${TIMERS[@]}"; do
    if [[ "$(active "$unit")" == active || "$(active "$unit")" == activating ]]; then
      systemctl start "$unit"
    fi
  done
}
mutated=false
paused=false
recover() {
  rc=$?
  trap - EXIT INT TERM
  if (( rc != 0 )); then
    echo "Hotfix failed; backup: $BACKUP" >&2
    set +e
    if $mutated; then
      systemctl stop harrow-attendance-portal.service harrow-device-query.service
      for unit in "${TIMERS[@]}"; do
        systemctl disable "$unit" >/dev/null 2>&1 || true
      done
      # No new jobs are released until deployment and checks have succeeded.
      while IFS= read -r file; do rm -f "$TARGET_DIR/$file"; done < "$BACKUP/runtime-files.txt"
      tar -xpf "$BACKUP/runtime.tar" -C "$TARGET_DIR"
      while IFS= read -r unit; do
        rm -f "$SYSTEMD_DIR/$unit"
        [[ ! -f "$BACKUP/systemd/$unit" ]] || cp -a "$BACKUP/systemd/$unit" "$SYSTEMD_DIR/$unit"
      done < "$BACKUP/unit-files.txt"
      systemctl daemon-reload
      for unit in "${TIMERS[@]}"; do
        case "$(enabled "$unit")" in
          enabled) systemctl enable "$unit" ;;
          enabled-runtime) systemctl enable --runtime "$unit" ;;
        esac
      done
    fi
    flock -u 9 2>/dev/null || true
    if $paused; then restore_activity; fi
    echo "Previous runtime restored where deployment had started; inspect service health and $BACKUP/unit-state.txt." >&2
  fi
  exit "$rc"
}
trap recover EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

echo "[2/6] Pausing triggers; allowing active jobs to finish"
paused=true
for unit in "${TIMERS[@]}" "${SERVICES[@]}"; do
  if [[ "$(active "$unit")" == active || "$(active "$unit")" == activating ]]; then
    systemctl stop "$unit"
  fi
done
# Includes both old and new template instances, and any manually started instance.
deadline=$((SECONDS + 3900))
while true; do
  active_jobs="$(systemctl list-units --state=active,activating,deactivating --no-legend --plain \
    'harrow-timebase@*.service' harrow-timebase-reconcile.service harrow-attendance-import.service)" || {
      echo "Cannot inspect active jobs; aborting" >&2
      exit 1
    }
  [[ -n "$active_jobs" ]] || break
  if (( SECONDS >= deadline )); then
    echo "Jobs did not finish within 65 minutes; aborting without replacing runtime" >&2
    exit 1
  fi
  sleep 2
done
# Also wait for controller invocations launched outside systemd.
LOCK_FILE="$("$PY" -c 'import json,sys; print(json.load(open(sys.argv[1]))["paths"]["lock_file"])' "$CONFIG")"
exec 9>>"$LOCK_FILE"
# A pilot may not have run the controller yet; don't leave a root-owned new lock.
chown harrow-timebase:harrow-timebase "$LOCK_FILE"
chmod 0640 "$LOCK_FILE"
flock -w 300 9 || { echo "Controller lock still busy; aborting" >&2; exit 1; }

echo "[3/6] Backing up runtime and timer definitions"
cp -a "$AUTH_DIR" "$BACKUP/portal-auth"
"$PY" - "$SRC_DIR" "$TARGET_DIR" "$BACKUP" <<'MANIFEST'
from pathlib import Path
import sys, tarfile
source, target, backup = map(Path, sys.argv[1:])
files = set()
for base in (source, target):
    files.update(p.name for p in base.glob('*.py'))
    for directory in ('timebase', 'portal'):
        files.update(str(p.relative_to(base)) for p in (base / directory).rglob('*') if p.is_file())
files.add('requirements.txt')
(backup / 'runtime-files.txt').write_text(''.join(f'{p}\n' for p in sorted(files)))
with tarfile.open(backup / 'runtime.tar', 'w') as archive:
    for name in sorted(files):
        if (target / name).is_file():
            archive.add(target / name, arcname=name)
MANIFEST
for unit in "$SRC_DIR"/systemd/* "$SYSTEMD_DIR"/harrow-timebase-*.timer; do
  [[ -f "$unit" ]] || continue
  name="$(basename "$unit")"
  echo "$name" >> "$BACKUP/unit-files.txt"
  [[ ! -f "$SYSTEMD_DIR/$name" ]] || cp -a "$SYSTEMD_DIR/$name" "$BACKUP/systemd/$name"
done
sort -u "$BACKUP/unit-files.txt" -o "$BACKUP/unit-files.txt"
mutated=true
echo "[4/6] Installing application and schedule"
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


for unit in "$SRC_DIR"/systemd/*; do
  install -m 0644 "$unit" "$SYSTEMD_DIR/$(basename "$unit")"
done
for unit in harrow-timebase-0700.timer harrow-timebase-0800.timer harrow-timebase-0810.timer; do
  systemctl disable "$unit" >/dev/null 2>&1 || true
  rm -f "$SYSTEMD_DIR/$unit"
done
systemctl daemon-reload
# Use installed imports, not the source checkout, for deployment completeness.
(cd "$TARGET_DIR" && "$PY" -c 'from harrow_timebase import TimeBaseController; from attendance_importer import process_job')

# Preserve enablement and runtime activity independently; never enable a pilot's disabled timers.
map_timer() {
  case "$1" in
    harrow-timebase-school-start.timer) echo harrow-timebase-0700.timer ;;
    harrow-timebase-attendance.timer) echo harrow-timebase-0800.timer ;;
    harrow-timebase-reconcile-extra.timer) echo harrow-timebase-reconcile.timer ;;
    *) echo "$1" ;;
  esac
}
NEW_TIMERS=(harrow-timebase-school-start.timer harrow-timebase-attendance.timer
            harrow-timebase-1600.timer harrow-timebase-reconcile.timer harrow-timebase-reconcile-extra.timer)
for unit in "${NEW_TIMERS[@]}"; do
  prior="$unit"
  # On repeat runs, preserve the new timer's own state, even if disabled.
  if [[ ! -f "$BACKUP/systemd/$unit" ]]; then prior="$(map_timer "$unit")"; fi
  case "$(enabled "$prior")" in
    enabled) systemctl enable "$unit" ;;
    enabled-runtime) systemctl enable --runtime "$unit" ;;
  esac
done

echo "[5/6] Checking service startup before resuming scheduled jobs"
for unit in harrow-device-query.service harrow-attendance-portal.service; do
  if [[ "$(active "$unit")" == active || "$(active "$unit")" == activating ]]; then
    systemctl start "$unit"
    systemctl is-active --quiet "$unit"
  fi
done
if command -v curl >/dev/null 2>&1; then
  for entry in 'harrow-device-query.service 8091' 'harrow-attendance-portal.service 8090'; do
    read -r unit port <<< "$entry"
    if [[ "$(active "$unit")" == active || "$(active "$unit")" == activating ]]; then
      curl -fsS --retry 5 --retry-connrefused --retry-delay 1 --max-time 5 \
        "http://127.0.0.1:$port/healthz" >/dev/null
    fi
  done
fi
# Release the controller lock before any path/timer can launch a job.
flock -u 9
exec 9>&-
# Commit point: rollback is no longer automatic once new jobs may change Jamf/state.
trap - EXIT INT TERM
trap 'echo "Resume failed: runtime is installed. Inspect unit status and unit-state.txt in $BACKUP; retry the failed start after fixing it." >&2' ERR
mutated=false

echo "[6/6] Resuming queue and timers"
if [[ "$(active "harrow-attendance-import.path")" == active ]]; then
  systemctl start harrow-attendance-import.path
fi
for unit in "${NEW_TIMERS[@]}"; do
  prior="$unit"
  if [[ ! -f "$BACKUP/systemd/$unit" ]]; then prior="$(map_timer "$unit")"; fi
  if [[ "$(active "$prior")" == active || "$(active "$prior")" == activating ]]; then
    systemctl start "$unit"
    systemctl is-active --quiet "$unit"
  fi
done
systemctl list-timers --all --no-pager 'harrow-timebase*'
echo "Schedule hotfix installed. Backup: $BACKUP"
echo "Preserved: config/env contents, credentials, R4 portal accounts/session key, Nginx, attendance/history, holidays and overrides."
echo "Wi-Fi management defaults OFF. No reboot required."
echo "If resuming a unit fails, fix it and start that unit; do not restore code while jobs are running."

echo "Attendance: unmatched emails default to skip; an explicit attendance.unmatched_email_policy=error remains strict."

echo "Bangkok schedule: school start 08:00; attendance 08:10; extra reconcile 09:10/14:00; regular reconcile :00/:30; end of day 16:00."
