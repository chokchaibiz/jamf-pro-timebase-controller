# Live schedule upgrade

All schedules use Asia/Bangkok. School start is 08:00, attendance 08:10, end of day 16:00. Reconcile runs daily at :00/:30, approximately three minutes after boot, and Monday–Friday at 09:10/14:00. Holidays stay Out-Harrow. Wi-Fi management is disabled by default.

## Deployment

This bundle is based on `codex/r4-login-hotfix` and upgrades an **existing R4 application-login installation**. It retains login, password changes, user creation, branding, drag-and-drop uploads and installer timezone support. It is not the R3-to-R4 authentication migration.

The schedule hotfix requires an existing `portal-auth/users.json` and valid `portal-auth/session.key`. It backs up those files after pausing the Portal, preserves their contents, and deploys `portal/auth_store.py` with the rest of the R4 runtime. It does not initialize default accounts, reset passwords, rotate the session key, reinstall dependencies, or rewrite Nginx configuration. R4's installed dependencies are checked through the authentication/Portal tests before any services are paused.

Extract the complete updated bundle into a **separate directory** on the installed server and run:

```bash
sudo bash apply-hotfix.sh
```

No reboot is required. The Portal and device query service are briefly unavailable during installation. Queue processing and timers pause; active importer/controller jobs are allowed to finish (up to 65 minutes), and the controller lock must become available before files change. Avoid starting manual controller/importer commands or changing deployment files during maintenance.

The script validates the source bundle and calendar expressions before pausing production. It saves runtime files, systemd definitions, service/timer state, and a root-only copy of `/etc/harrow-timebase` under `/opt/harrow-timebase/backups/hotfix-TIMESTAMP-PID`. Config and environment **contents are never rewritten**; existing permission repairs are retained. Attendance, holidays, queue/history and manual overrides are not replaced.

Old timer migration:

| Old timer | Replacement |
|---|---|
| `harrow-timebase-0700.timer` | `harrow-timebase-school-start.timer` at 08:00 |
| `harrow-timebase-0800.timer` | `harrow-timebase-attendance.timer` at 08:10 |
| `harrow-timebase-0810.timer` | Removed; Wi-Fi deferred |
| `harrow-timebase-reconcile.timer` | Same name, now clock-aligned |
| None | `harrow-timebase-reconcile-extra.timer`, initially inherits reconcile timer state |

Enabled and running states are migrated independently. Disabled/stopped pilot timers are not automatically enabled/started. Repeat runs preserve the replacement timers' own states. The 16:00 timer is retained. Check the output to confirm your intended production timers are active; an already-enabled but stopped timer remains stopped.

`features.wifi_management_enabled` defaults to `false` when absent from old configs. New example/production configs explicitly set it to `false`. No config edit is needed for existing installations. If explicitly `true`, the hotfix aborts before pausing services so it cannot silently deploy this disabled-Wi-Fi schedule with Wi-Fi enabled. The retained Wi-Fi implementation is for future work, not a new Wi-Fi timetable. Existing Jamf profile scope remains unchanged; disabling management does not remove an existing profile.

## Verification

```bash
systemctl list-timers --all 'harrow-timebase*'
systemctl status harrow-attendance-portal.service harrow-device-query.service harrow-attendance-import.path
systemd-analyze calendar '*-*-* *:00,30:00 Asia/Bangkok'
systemd-analyze calendar 'Mon..Fri *-*-* 09:10:00 Asia/Bangkok'
systemd-analyze calendar 'Mon..Fri *-*-* 14:00:00 Asia/Bangkok'
sudo journalctl -u harrow-timebase-reconcile.service -n 80 --no-pager
```

Starting the updated reconcile timer on a server already up for more than three minutes can immediately trigger its boot catch-up run.

The old 07:00/08:00/08:10 timers should be gone. New school-start/attendance timers should show 08:00/08:10. The server may display NEXT in its local timezone even though the calendar expressions use Bangkok time.

`:00` and `:30` are scheduled start times with one-second accuracy and no jitter. Work can start late under load or while another job holds the controller lock. Same-service triggers coalesce while that service runs. Daily actions calculate current state after obtaining the lock, so a late school-start run does not overwrite attendance. `Persistent=false` avoids replaying missed calendar slots; the boot reconciliation repairs current state.

Portal imports for today's school day reconcile immediately from 08:10 until before 16:00. Direct attendance CSV copies are read on the next scheduled run; there is no new directory watcher. Use a temporary filename followed by an atomic rename after transfer completes.

## Failure and recovery

Before jobs resume, a deployment failure attempts to restore the saved runtime and unit definitions, timer enablement, and prior service activity. Read the error output and verify health; filesystem/systemd failures can also prevent complete rollback. A timeout waiting for active work aborts before application files are replaced.

After the commit point (just before queue/timer resumption), a failed unit start leaves the **new runtime installed** and reports the backup location. Inspect `systemctl status`/`journalctl`, fix the failure, and start the affected unit. Re-running the hotfix is supported; consult the original backup's `unit-state.txt` for any timers that should be running but failed to resume.

For a later manual rollback, first stop all current TimeBase timers, the upload path, Portal and query service; let active controller/import jobs finish. Using the specific backup printed by the failed upgrade:

1. Restore runtime files from `runtime.tar` into `/opt/harrow-timebase` (remove newly introduced files listed in `runtime-files.txt` if absent from the archive).
2. Disable current timers. Restore backed-up unit files from `systemd/` into `/etc/systemd/system`, removing newly introduced units absent from the backup but listed in `unit-files.txt`.
3. Run `systemctl daemon-reload`, restore enabled/running states from `unit-state.txt`, and verify logs and next runs.

Do not restore configuration snapshots unless a separate config change needs reversal. Do not overwrite runtime while jobs are active. Rollback does not reverse Jamf changes already completed by a job.

The extra 14:00 trigger overlaps the regular :00 run. Both timers target the same
reconcile service; simultaneous triggers coalesce into one service activation.
The attendance timer at 08:10 runs attendance, not the retained legacy Wi-Fi action.
