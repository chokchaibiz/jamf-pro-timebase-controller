# R4 Login + Schedule Update — 2026-09-10

This bundle incorporates fixes verified on the installed Harrow TimeBase server:

- **Portal application login:** five local administrator accounts, signed session cookies, password changes, logout, and old-session invalidation after a password change.
- **Complete hotfix deployment:** `apply-hotfix.sh` now installs all runtime modules, Portal templates/static files, and systemd units instead of replacing only `harrow_timebase.py`.
- **EXDEV-safe Portal queue handoff:** cross-filesystem queue moves no longer fail with `Invalid cross-device link`. The Portal writes/copies to a hidden file in the destination filesystem and exposes the final `.job.json` only after the copy is complete.
- **Classic API Email fallback:** Attendance Email → Serial resolution prefers `/api/v2/mobile-devices/detail`, but automatically falls back to Classic `Mobile Devices / Location` lookups when the tenant returns missing/blank `serialNumber`.
- **Classic API Device Override fallback:** live Email search and selected-Serial revalidation also fall back to Classic API when the v2 inventory response is unusable.
- **`classic_xml(params=...)` compatibility retained.**
- **UTF-8 BOM / Excel CSV support retained.**
- **Regression checks included:** the installer and hotfix validate syntax, BOM parsing, Classic fallback functions, EXDEV handling, and complete hotfix deployment before restarting services.
- Existing `/etc/harrow-timebase/config.json`, Jamf credentials, Attendance/History, Holidays, and Manual Override state are preserved by `apply-hotfix.sh`.

## Update an existing server

Use the complete scheduler bundle based on `codex/r4-login-hotfix`, extracted into a separate directory. This hotfix requires an existing R4 application-login installation and preserves its users, password hashes, session key and Nginx settings. See [SCHEDULE-UPGRADE.md](SCHEDULE-UPGRADE.md) before deployment.

```bash
sudo bash apply-hotfix.sh
```

Then verify:

```bash
sudo systemctl status harrow-device-query.service harrow-attendance-portal.service harrow-attendance-import.path
curl -fsS http://127.0.0.1:8091/healthz
curl -fsS http://127.0.0.1:8090/healthz
sudo journalctl -u harrow-attendance-import.service -u harrow-device-query.service -n 150 --no-pager
```


# Harrow Jamf Pro TimeBase Controller + Attendance Upload Portal — Production Implementation

> This release provides a browser-based TimeBase Upload Portal for both **Absent Students** and **Holiday Calendar**, application login, a systemd queue importer, automatic validation/archive, and immediate Jamf reconcile for same-day attendance uploads between 08:10–15:59.

This bundle implements the following desired state without The MUT:

- **ASSURE** configuration profile
  - Target: `In-Harrow`
  - Exclusion: `Out-Harrow`
- **In-Harrow** Smart Mobile Device Group: `Room = 100`
- **Out-Harrow** Smart Mobile Device Group: `Room = 200`
- **Harrow-All-iPads** Static Mobile Device Group: master list of Harrow iPads
- **WiFi-Harrow** profile management is deferred. `features.wifi_management_enabled` defaults to `false`, including in existing configs without this setting. Scheduled runs do not read or write this profile; existing scope is left unchanged.
- Weekends and dates in `holidays.csv` remain Out-Harrow.

## Daily state

| Bangkok time on school days | Desired state |
|---|---|
| Before 08:00 | All Room=200 / Out-Harrow |
| 08:00–08:09 | Room=100 / In-Harrow, except active manual Out-Harrow overrides |
| 08:10–15:59 | Attendance plus manual overrides: absent/overridden Room=200, others Room=100 |
| 16:00 onward | All Room=200 / Out-Harrow; clear manual overrides |

Regular reconciliation runs at **:00 and :30 outside Monday–Friday 08:00–16:00**, and **all day Saturday–Sunday**. Extra reconciliation runs Monday–Friday at **09:10 and 14:00**. The regular boot trigger (three minutes after boot) also skips the weekday 08:00–15:59 window. Holiday runs maintain Out-Harrow. Calendar timers use Bangkok time, one-second accuracy and no randomized delay. System load and active jobs can delay actual execution. Missed calendar occurrences are not replayed (`Persistent=false`); the boot run repairs current state outside the weekday exclusion window.

## Safety decisions

1. API concurrency defaults to **4** and configuration rejects values over 5.
2. Room updates are **idempotent**: only devices not already in the desired Smart Group are PUT.
3. API requests use retry + exponential backoff for transient failures.
4. A process lock prevents overlapping 08:00/08:10/16:00/reconcile jobs.
5. Wi-Fi management is disabled by default; the optional future implementation retains its attendance guard.
6. Production policy is `missing_attendance_policy = "zero_absent"`. If today’s `absent-YYYY-MM-DD.csv` is missing, the controller does **not** fail: it treats `Absent = 0`, repairs all master iPads to `Room=100 / In-Harrow`, writes a verified synthetic attendance marker during the attendance window.
7. Attendance Email addresses must resolve to valid iPads in `Harrow-All-iPads`; unmatched emails are skipped with warnings by default; ambiguous matches still fail safely.
8. Master group device count is protected by configurable minimum/maximum thresholds.
9. Smart Group membership is polled after updates until verification succeeds or times out.
10. `reconcile` runs periodically to repair drift or recover from a server restart/missed timer.

## 1. Jamf Pro preparation

Create/confirm these objects:

### Static Mobile Device Group

`Harrow-All-iPads`

Add all managed Harrow iPads. For approximately 2,000 devices, configure the safety bounds in `config.json`, for example 1,800–2,200.

### Smart Group: In-Harrow

Criteria:

- `Room`
- `is`
- `100`

### Smart Group: Out-Harrow

Criteria:

- `Room`
- `is`
- `200`

### Profile: ASSURE

Scope:

- Targets: `In-Harrow`
- Exclusions: `Out-Harrow`

The controller verifies this scope during preflight. It does not dynamically modify ASSURE.

### Profile: WiFi-Harrow (future use only)

Not required while Wi-Fi management is disabled. The following describes the retained optional implementation, not the active schedule.

Create the Wi-Fi/restriction payload in Jamf Pro UI first. Do not scope to All Mobile Devices.

The controller only adds/removes `In-Harrow` under Targets and preserves the rest of the profile XML.

## 2. API Role and API Client

Create an API Role and API Client. Minimum privileges for this implementation:

- Read Mobile Devices
- Update Mobile Devices
- Update Users
- Read Static Mobile Device Groups
- Read Smart Mobile Device Groups
- Read Mobile Device Configuration Profiles
- Update Mobile Device Configuration Profiles

Store the API Client `Client ID` and `Client Secret` only in `/etc/harrow-timebase/harrow-timebase.env`.


### Jamf tenant and credentials

This production bundle is preconfigured for:

`https://com7publiccompanyl14.jamfcloud.com`

The Git repository contains placeholder credentials only. Before installation, populate a private `harrow-timebase.env.production` file from `harrow-timebase.env.example`, or replace the placeholders in the installed `/etc/harrow-timebase/harrow-timebase.env`. The production env file is ignored by Git and must be distributed through an approved secret-management channel.

## 3. Server

Recommended baseline:

- Ubuntu Server 24.04 LTS or Rocky Linux 9
- 2–4 vCPU
- 4 GB RAM
- 20+ GB disk
- outbound HTTPS/443 to the Jamf Pro tenant
- DNS and NTP working

The installer sets the server timezone to `Asia/Bangkok`, and the scheduled systemd timers also specify `Asia/Bangkok` explicitly.

## 4. Install

Copy the bundle to the server and run:

```bash
sudo bash install-program.sh
```

The Jamf tenant URL is preconfigured, but API Client credentials must be supplied privately. After installation, verify them:

```bash
sudo grep 'jamf_url' /etc/harrow-timebase/config.json
sudo grep 'JAMF_CLIENT_ID' /etc/harrow-timebase/harrow-timebase.env
```

The official holiday calendar can now be maintained from the same Web Portal used for Absent Students; SSH editing is no longer required.

Do not print `JAMF_CLIENT_SECRET` to shared terminal logs or documentation.

Protect secrets:

```bash
sudo chown root:harrow-timebase /etc/harrow-timebase/harrow-timebase.env
sudo chmod 640 /etc/harrow-timebase/harrow-timebase.env
```

## 5. Holiday Calendar

The controller-compatible path remains:

`/opt/harrow-timebase/holidays.csv`

On installation this is maintained as a compatibility symlink to the writable shared state file used by the Portal/importer:

`/var/lib/harrow-timebase/shared/holidays.csv`

Preferred format:

```csv
date,end_date,description
2026-10-13,,Annual Holiday
2026-10-19,2026-10-23,Midterm Break
2026-12-10,,Constitution Day
```

`end_date` is optional. Leave it blank for a single holiday, or provide it to create an inclusive range. The older `date,description` format remains supported. The Web Portal accepts dates in `YYYY-MM-DD`, `DD/MM/YYYY`, or `DD-MM-YYYY` and expands ranges into canonical daily `YYYY-MM-DD` rows. Invalid dates, reversed ranges, overlapping dates, and ranges longer than 370 days are rejected. The previous holiday calendar is archived before every replacement. Saturday and Sunday are automatically non-school days and do not need rows in the CSV.

## 6. Attendance CSV / Web Upload Portal

The preferred production method is the Web Upload Portal. Staff do **not** need SSH access and do not need to manually name `absent-YYYY-MM-DD.csv`. The importer creates the canonical file automatically.

The controller ultimately reads:

`/opt/harrow-timebase/attendance/absent-YYYY-MM-DD.csv`

The attendance identity is now **Email Address**, not Serial Number. Example:

```csv
Email Address
student001@harrowschool.ac.th
student014@harrowschool.ac.th
```

Supported headers include `Email Address`, `email_address`, `Email`, `emailaddress`, `user_email`, and `student_email`. The Portal normalizes the stored canonical file to:

```csv
email_address
student001@harrowschool.ac.th
student014@harrowschool.ac.th
```

At 08:10 (and on immediate reconcile after a same-day upload), the privileged controller reads current Jamf Mobile Device Inventory for `Harrow-All-iPads`, builds an in-memory Email -> Serial index from `emailAddress`, and resolves each absent Email to its iPad before applying `Room = 200` / `Out-Harrow`. The source CSV therefore remains human/attendance-system friendly while Room updates still use the resolved iPad Serial internally.

Production default is `attendance.email_match_policy = "unique"`: every matched absent Email must resolve to exactly one iPad in `Harrow-All-iPads`. Unmatched emails are skipped with warnings by default (`attendance.unmatched_email_policy = "skip"`); set this to `"error"` for strict rejection. Multiple matches still cause the import to fail under `unique`. An optional `all_matches` policy exists for environments that intentionally assign multiple iPads to one Email.

If nobody is absent, a header-only file is still supported:

> With the production setting `missing_attendance_policy = "zero_absent"`, this file is optional. If the file is not present, the controller treats the day as zero absences and keeps/repairs all master iPads in `In-Harrow`.

```csv
email_address
```

With `missing_attendance_policy = "zero_absent"`, a missing file remains a valid zero-absence state. The Portal's **No Absent Students Today** action is still preferred because it creates an explicit audit trail.

## 6A. Attendance Upload Portal

### One-command installation

Run:

```bash
sudo bash install-program.sh
```

The installer installs Python dependencies, Nginx, the FastAPI Portal, application login, upload queue/importer, systemd units, and the existing TimeBase controller. It enables only the Portal and attendance queue automatically; the 08:00/08:10/16:00 Jamf timers remain disabled until pilot validation is complete.

At the end of a first installation it prints:

```text
Portal URL : https://SERVER-IP:8443/
Initial users: admin1, admin2, admin3, admin4, admin5
Initial password: harrow@dmin
```

Sign in with each account and immediately use the top-right user menu to change its password. Password hashes and the session signing key are stored with mode `0600` under `/var/lib/harrow-timebase/portal-auth`; installer and hotfix reruns preserve them.

For a fresh installation, the shared initial password can be overridden with `sudo env HARROW_DEFAULT_ADMIN_PASSWORD='your-password' bash install-program.sh`.

Add another full portal administrator without changing existing accounts:

```bash
sudo -u harrow-upload env HARROW_NEW_USER_PASSWORD='SecurePasswordHere' \
  /opt/harrow-timebase/venv/bin/python \
  /opt/harrow-timebase/portal/auth_store.py add-user \
  --auth-dir /var/lib/harrow-timebase/portal-auth \
  --user admin6 \
  --password-env HARROW_NEW_USER_PASSWORD
```

The username is normalized to lowercase, duplicates are rejected, and no service restart is required. Run the command as `harrow-upload` so the authentication files retain the correct ownership.

### User workflow

1. Open `https://SERVER-IP:8443/`.
2. Sign in with the Portal account.
3. Select Attendance Date. Today is the default.
4. Drag/drop or browse to any `.csv` filename. The user does not need to rename it.
5. The Portal normalizes supported Email headers (`Email Address`, `email_address`, `Email`, `emailaddress`, `user_email`, `student_email`) and removes duplicate Email addresses case-insensitively.
6. The queue importer performs authoritative live validation against Jamf: it scans current inventory for `Harrow-All-iPads`, resolves every Email to an iPad Serial, warns and skips unmatched emails, blocks ambiguous matches, and only then writes the canonical attendance file.
7. An existing attendance file for the same date is archived before replacement.
8. Uploads for today between 08:10–15:59 on a school day automatically trigger `reconcile`; uploads before 08:10 wait for the normal scheduler; uploads after 16:00 are recorded without changing that day's Jamf state.

For a zero-absence day, use **No Absent Students Today**. It creates a header-only CSV and a user-attributed audit record.

### Queue design

```text
Browser
  -> Nginx :8443 HTTPS
  -> FastAPI application login (127.0.0.1:8090, user harrow-upload)
  -> /var/lib/harrow-timebase/portal-staging
  -> /var/lib/harrow-timebase/upload-queue/*.job.json
  -> harrow-attendance-import.path
  -> harrow-attendance-import.service (user harrow-timebase)
  -> Resolve Email Address against live Jamf inventory / Harrow-All-iPads
  -> Archive previous CSV
  -> atomic replace /opt/harrow-timebase/attendance/absent-YYYY-MM-DD.csv
  -> reconcile when appropriate
```

The web process has no access to `/etc/harrow-timebase/harrow-timebase.env` and therefore cannot read the Jamf Client Secret.

### Upload status and history

Portal job status is written to:

`/var/lib/harrow-timebase/upload-status/`

Audit JSONL is written to:

`/var/log/harrow-timebase/upload-audit.jsonl`

Replaced attendance files are archived under:

`/opt/harrow-timebase/archive/YYYY/MM/`

### Services

```bash
systemctl status harrow-attendance-portal.service
systemctl status harrow-attendance-import.path
systemctl status harrow-attendance-import.service
systemctl status harrow-device-query.service

journalctl -u harrow-attendance-portal.service -f
journalctl -u harrow-attendance-import.service -f
journalctl -u harrow-device-query.service -f
```

### HTTPS

The default one-click configuration listens on HTTPS TCP/8443. The installer creates a self-signed TLS certificate so the Portal is encrypted immediately; the first browser connection will show a certificate warning. Replace the generated certificate with an organization-trusted certificate (or terminate TLS at the organization's existing reverse proxy/load balancer) for normal production use. An example is included at:

`nginx/harrow-timebase-https.example.conf`

Restrict TCP/8443 to the authorized school administration network if a host/network firewall is used.

## 6B. Holiday Upload on the Same Portal

The home page contains a third card named **Upload Holidays** directly below the Absent Students controls. Staff can maintain the annual holiday calendar without SSH access.

### User workflow

1. Open the same Portal URL: `https://SERVER-IP:8443/`.
2. In **Upload Holidays**, drag/drop a `.csv` file. The filename can be anything; `holidays.csv` is recommended.
3. The Portal validates the header, date format, inclusive ranges, overlapping dates, UTF-8 encoding, and that at least one holiday is present.
4. The Portal expands ranges and normalizes the calendar to canonical daily `date,description` rows.
5. The user confirms replacement.
6. The importer archives the previous calendar under `/opt/harrow-timebase/archive/holidays/YYYY/` and atomically activates the new calendar.
7. By default the importer triggers `reconcile` immediately after activation, so a change affecting today is applied without waiting for the next timer. This can be disabled with `reconcile_on_holiday_upload: false` in `portal.json`.

Supported examples:

```csv
date,end_date,description
2026-10-13,,Annual Holiday
19/10/2026,23/10/2026,Midterm Break
10-12-2026,,Constitution Day
2026-12-31,,School Holiday
```

The stored file is expanded and normalized to:

```csv
date,description
2026-10-13,Annual Holiday
2026-10-19,Midterm Break
2026-10-20,Midterm Break
2026-10-21,Midterm Break
2026-10-22,Midterm Break
2026-10-23,Midterm Break
2026-12-10,Constitution Day
2026-12-31,School Holiday
```

The same card also displays the current total number of holiday dates, up to 12 upcoming holidays (consecutive dates with the same description are shown as a range), the last-update time, and a **Download Current holidays.csv** button. Holiday replacements also appear in the common Upload History and Audit Log.

## 7. Preflight

Load the environment and run:

```bash
sudo -u harrow-timebase bash -c '
  set -a
  source /etc/harrow-timebase/harrow-timebase.env
  set +a
  /opt/harrow-timebase/venv/bin/python \
    /opt/harrow-timebase/harrow_timebase.py \
    --config /etc/harrow-timebase/config.json preflight
'
```

Preflight checks:

- OAuth token works
- all three groups exist
- master group is Static
- In/Out groups are Smart
- In-Harrow contains Room=100 criterion
- Out-Harrow contains Room=200 criterion
- master device count is inside safety range
- ASSURE targets In-Harrow
- ASSURE excludes Out-Harrow
- If Wi-Fi management is explicitly enabled in future, WiFi-Harrow exists and is not scoped to All Mobile Devices
- holiday file is valid

## 8. Dry-run

```bash
sudo -u harrow-timebase bash -c '
  set -a; source /etc/harrow-timebase/harrow-timebase.env; set +a
  /opt/harrow-timebase/venv/bin/python \
    /opt/harrow-timebase/harrow_timebase.py \
    --config /etc/harrow-timebase/config.json --dry-run reconcile
'
```

Dry-run reads Jamf and prints the writes it would perform, but does not PUT Room/profile changes.

## 9. Pilot

Before 2,000 devices, temporarily use a pilot Static Group and safety range, for example 5–20 devices. Validate:

1. At 08:00, confirm pilot devices move In-Harrow, except manual overrides.
2. At 08:10, confirm attendance moves absent devices Out-Harrow.
3. Check same-day Portal imports after 08:10 trigger immediate reconciliation.
4. At 16:00, confirm all devices move Out-Harrow and overrides clear.
5. Confirm Wi-Fi profile scope stays unchanged throughout.

Manual reconciliation uses the current Bangkok time; it cannot force a different schedule phase:

```bash
sudo systemctl start harrow-timebase-reconcile.service
```

View logs:

```bash
journalctl -u 'harrow-timebase*' -f
sudo tail -f /var/log/harrow-timebase/harrow-timebase.log
```

## 11. Enable timers

Only after the pilot succeeds:

```bash
sudo systemctl enable --now harrow-timebase-school-start.timer
sudo systemctl enable --now harrow-timebase-attendance.timer
sudo systemctl enable --now harrow-timebase-1600.timer
sudo systemctl enable --now harrow-timebase-reconcile.timer
sudo systemctl enable --now harrow-timebase-reconcile-extra.timer
```

Check schedules:

```bash
systemctl list-timers 'harrow-timebase*'
systemd-analyze calendar 'Mon..Fri *-*-* 08:00:00 Asia/Bangkok'
systemd-analyze calendar 'Mon..Fri *-*-* 08:10:00 Asia/Bangkok'
systemd-analyze calendar 'Mon..Fri *-*-* 00..07,16..23:00,30:00 Asia/Bangkok'
systemd-analyze calendar 'Sat,Sun *-*-* *:00,30:00 Asia/Bangkok'
systemd-analyze calendar 'Mon..Fri *-*-* 09:10:00 Asia/Bangkok'
systemd-analyze calendar 'Mon..Fri *-*-* 14:00:00 Asia/Bangkok'
```

## 12. Exact workflow

### 08:00 school start

Run preflight, read master/current group membership, set all master devices In-Harrow except active manual Out-Harrow overrides, and verify Smart Group membership.

### 08:10 attendance

Read today's `absent-YYYY-MM-DD.csv`, resolve emails to master iPads, validate matches and absence safety limits, combine absences with manual overrides, repair Room values, verify group membership, and write the attendance SHA-256 marker. Under production `zero_absent` policy, a missing file means zero absences; malformed files still fail; unmatched emails are skipped by default.

### 16:00 school end

Set all master devices Out-Harrow, verify membership, and clear overrides.

The daily entry points reconcile **current time** after acquiring the controller lock. A delayed 08:00 job cannot undo attendance after 08:10, and a delayed attendance job cannot restore school-day state after 16:00. Jobs sharing 08:00 or 16:00 are serialized by the existing lock; if lock wait expires, later reconciliation can retry.

## 13. Reconciliation

`harrow-timebase-reconcile.timer` schedules :00/:30 in weekday hours 00–07 and 16–23 and all weekend hours, plus `OnBootSec=3min`. It targets `harrow-timebase@reconcile-regular.service`, whose CLI action skips weekday 08:00–15:59 after acquiring the controller lock, before preflight/Jamf calls. `harrow-timebase-reconcile-extra.timer` runs 09:10 and 14:00 Monday–Friday through the unrestricted `harrow-timebase-reconcile.service`. Separate controller jobs share the existing lock. Calendar slots are scheduled starts, not a guarantee that Jamf work completes at that time.

Desired state follows the Daily state table above. Reconciliation on weekends and holidays sets all devices Out-Harrow. Wi-Fi profile management is disabled. Operations are idempotent: a correct state causes reads/verification without repeating all device writes.

Direct server uploads are supported: atomically replace the canonical attendance CSV after transfer finishes, with ownership/read permissions for `harrow-timebase`. The next attendance/reconciliation run reads it. To apply immediately, start `harrow-timebase-reconcile.service`; there is no watcher on the attendance directory.

## Live schedule hotfix

See [SCHEDULE-UPGRADE.md](SCHEDULE-UPGRADE.md) for migration, configuration preservation, verification and recovery. Run the complete bundle's `apply-hotfix.sh` from a separate directory on the installed server; do not copy only timer files or only the controller.

## 14. Retry and scale

Default `concurrency=4`.

Each API request retries transient HTTP 408/425/429/500/502/503/504 conditions with exponential backoff and jitter. The batch also has retry rounds for devices still failing after request-level retries.

Do not configure concurrency above 5.

## 15. Production rollout sequence

Recommended rollout gates:

1. API role/client validation.
2. 5–20 device isolated pilot.
3. 100 device controlled pilot.
4. 500 device load validation.
5. Full ~2,000 device master group.
6. Observe at least one full 08:00 → 08:10 → 16:00 lifecycle.
7. Confirm Wi-Fi scope has not changed.

## 16. Rollback / emergency commands

Wi-Fi management is disabled, including the retained `wifi-on` / `wifi-off` commands. If an existing Jamf Wi-Fi profile needs changes, manage its scope in Jamf; disabling this feature does not remove a previously scoped profile.

Disable all automatic jobs:

```bash
sudo systemctl disable --now \
  harrow-timebase-school-start.timer \
  harrow-timebase-attendance.timer \
  harrow-timebase-1600.timer \
  harrow-timebase-reconcile.timer \
  harrow-timebase-reconcile-extra.timer
```

## 17. Important operational note for Wi-Fi restriction

If WiFi-Harrow contains a restriction that permits only MDM-installed Wi-Fi networks, test profile removal while the iPad is still connected to Harrow Wi-Fi. A device that leaves campus before receiving the profile-removal command may lose the path required to contact MDM. Consider removing the Wi-Fi restriction slightly before physical dismissal if operational policy permits it.


## Attendance Email-to-iPad resolution

Production configuration:

```json
"attendance": {
  "identity_field": "email_address",
  "email_match_policy": "unique",
  "unmatched_email_policy": "skip",
  "inventory_page_size": 100
},
"safety": {
  "email_inventory_min_coverage": 0.95
}
```

The controller queries `/api/v2/mobile-devices/detail` using the `GENERAL` and `USER_AND_LOCATION` sections and limits the inventory scan to the `Harrow-All-iPads` group. It compares Email addresses case-insensitively. Before resolving attendance, at least 95% of the expected master devices must be visible in the inventory query; otherwise the controller aborts the attendance operation to avoid treating an incomplete inventory response as authoritative.

Resolution outcomes:

- **1 matching master iPad**: use that Serial and make it `Room = 200`.
- **0 matches**: warn and skip the Email; continue applying matched absences. Explicit `unmatched_email_policy="error"` restores rejection.
- **More than 1 match** with `unique`: fail and report all matching Serials.
- **More than 1 match** with `all_matches`: all matching master iPads become `Room = 200`.

After a successful resolution, the controller stores `/var/lib/harrow-timebase/attendance-YYYY-MM-DD.resolution.json` with the attendance file SHA-256 and resolved Serials. Periodic reconciliation reuses this cache while the CSV is unchanged, avoiding repeated full inventory scans of ~2,000 devices. Replacing or editing the attendance CSV changes the SHA-256 and automatically forces a fresh Jamf Email resolution.

## Missing Attendance CSV policy (updated)

Production configuration:

```json
"safety": {
  "missing_attendance_policy": "zero_absent"
}
```

Behavior on a school day when `/opt/harrow-timebase/attendance/absent-YYYY-MM-DD.csv` does not exist:

1. Log a warning only; the job exits normally.
2. Treat `Absent = 0`.
3. Desired attendance partition becomes `Present = Harrow-All-iPads`, `Absent = empty`.
4. Repair any master device not already in `In-Harrow` by setting `Room = 100`.
5. Verify `In-Harrow = all master devices` and `Out-Harrow = 0` for the master set.
6. Write `attendance-YYYY-MM-DD.ok.json` using a deterministic synthetic SHA-256 marker for the missing-file/zero-absence state.
7. Wi-Fi scope remains unchanged; management is disabled.

If a valid absent CSV appears later, the next `attendance` or reconcile run during 08:10–15:59 reads the real file, applies the actual absent list to `Room = 200`, rewrites the marker with the real file SHA-256, and verifies the corrected state.

## 18. Device Override tab — Live Email Address Search and force Out-Harrow

The Portal includes a **Device Override** tab for intra-day exceptions, such as a student leaving school early or a device that must leave the normal school restriction state without editing the attendance CSV.

### Why this is a persistent override

Do **not** simply PUT `Room=200` once from the web page. The TimeBase reconciler is state-based and could otherwise return that iPad to `Room=100` on its next run. The Portal therefore submits a `manual_out` job. The privileged controller records a manual override and includes it in desired-state calculation until 16:00.

```text
Effective Out-Harrow = Attendance Absent + Active Manual Overrides
In-Harrow            = Harrow-All-iPads - Effective Out-Harrow
```

At 16:00 all iPads are moved to Room 200 and the day's manual overrides are purged.

### Live Jamf search architecture

The Portal does **not** keep a full Jamf mobile-device inventory cache and does not poll the full inventory every five minutes. Search is on-demand.

```text
Browser / School Staff
        |
        v
Nginx HTTPS
        |
        v
Attendance Portal + Application Login (harrow-upload)
        |
        | X-Internal-Token
        | localhost only
        v
Device Query Broker 127.0.0.1:8091 (harrow-timebase)
        |
        | Jamf OAuth Client Credentials
        v
Jamf Pro
```

The broker first performs a live filtered request against `GET /api/v2/mobile-devices/detail` using the entered Email Address and requests only the sections needed for display. Device Override uses exact `emailAddress` matching only; it intentionally does not perform a wildcard identity fallback. Results are filtered again so only devices in `Harrow-All-iPads` can be returned.

Before a selected Serial is queued for an override, the Portal asks the broker to re-read that Serial from Jamf and confirms that its current `emailAddress` still exactly matches the Email that the administrator searched. The controller then validates membership in `Harrow-All-iPads` again before writing the manual override and changing Room. This gives three validation layers: exact live Email search, live Serial+Email revalidation, and controller preflight/master membership validation.

### Security boundary

`harrow-upload` is deliberately **not** a member of the `harrow-timebase` Unix group and cannot read `/etc/harrow-timebase/harrow-timebase.env` containing Jamf Client credentials. A separate random internal token is stored at:

```text
/var/lib/harrow-timebase/shared/internal-api.env
```

It is readable by the Portal and the local query broker, but it is not a Jamf credential. The broker listens only on `127.0.0.1:8091`; Nginx does not expose that port.

### User workflow

1. Open **Device Override**.
2. Enter the student's Email Address from Jamf inventory and click **Search Jamf**.
3. Verify Email Address, Username, Device Name, Serial Number, current Room, model and inventory value when supplied by Jamf.
4. Select exactly one iPad.
5. Optionally enter a reason/note.
6. Click **Set Selected Device to Out-Harrow**.
7. Portal revalidates the selected Serial live with the localhost query broker.
8. Portal queues a `manual_out` job; the privileged importer calls the controller.
9. Controller confirms the Serial is in `Harrow-All-iPads`, stores the override, and reconciles immediately.
10. The iPad remains `Room=200` / `Out-Harrow` until 16:00 or until an administrator clicks **Clear**.

### Clear Override

The **Active Manual Overrides** table contains a **Clear** button. Clear does not blindly set Room 100. It removes the manual override and runs reconciliation:

- If the device is Present according to the current attendance state, it returns to Room 100 / In-Harrow.
- If the device's Email Address is still in today's absent CSV and resolves to that iPad, it remains Room 200 / Out-Harrow.

### New service and files

```text
/opt/harrow-timebase/device_query_service.py
/var/lib/harrow-timebase/shared/internal-api.env
/var/lib/harrow-timebase/shared/manual-overrides.json

/etc/systemd/system/harrow-device-query.service
```

Useful commands:

```bash
sudo systemctl status harrow-device-query.service
sudo journalctl -u harrow-device-query.service -n 100 --no-pager
curl -s http://127.0.0.1:8091/healthz
```

Older `harrow-inventory-sync.service/.timer` units are disabled and removed automatically by `install-program.sh` during upgrade. The legacy `install.sh` entry point delegates to the same installer.

### API privileges

The read-only query broker requires **Read Mobile Devices**. The actual Room change continues through the existing controller and uses the existing Mobile Device update privileges. No Jamf write credential is placed in the Portal process.

### Manual CLI emergency equivalents

Set one pilot iPad Out-Harrow until 16:00:

```bash
sudo -u harrow-timebase bash -c '
set -a
source /etc/harrow-timebase/harrow-timebase.env
set +a
/opt/harrow-timebase/venv/bin/python /opt/harrow-timebase/harrow_timebase.py \
  --config /etc/harrow-timebase/config.json \
  --serial SERIALNUMBER \
  --email-address student001@harrowbangkok.th \
  --username student001 \
  --submitted-by admin \
  --reason "Student left school early" \
  manual-out
'
```

Clear it:

```bash
sudo -u harrow-timebase bash -c '
set -a
source /etc/harrow-timebase/harrow-timebase.env
set +a
/opt/harrow-timebase/venv/bin/python /opt/harrow-timebase/harrow_timebase.py \
  --config /etc/harrow-timebase/config.json \
  --serial SERIALNUMBER \
  manual-clear
'
```

### Recommended operating rule

Use **Absent Students** for a student who is absent for the day. Use **Device Override** for an exception that occurs during the day. This keeps Attendance as the normal source of truth while preserving an explicit, auditable intra-day override.


### Unmatched attendance emails and live upgrade

The skip policy applies equally to portal uploads and direct copies to
`/opt/harrow-timebase/attendance/absent-YYYY-MM-DD.csv`. Direct copies are picked
up at the next scheduled reconciliation; copying does not itself start a job.
Transfer to a temporary filename in that directory, then rename it to the final
filename so reconciliation cannot read an incomplete upload.

The canonical CSV retains all submitted emails. Partial resolutions are recorded
with their unmatched emails and are never reused as a cache hit: later runs
retry inventory resolution even if the CSV has not changed. Fully resolved
files retain the existing cache behavior. Portal status/history show skipped
counts, and the status page and controller journal list skipped emails.
If every email is unmatched, zero devices are considered absent, with a warning;
manual overrides still apply. Inventory coverage, absence fraction, malformed
CSV, ambiguous matches under `unique`, and Jamf write checks remain enforced.

Run `sudo bash apply-hotfix.sh` from a separate checkout of this version on the
existing R4 server. It validates the policy before pausing jobs, backs up runtime
and configuration, drains active jobs, replaces code, and resumes the services
and timers. No server reboot is needed; the portal briefly restarts. Existing
config/env files, credentials, accounts and session keys are preserved exactly.
An omitted `attendance.unmatched_email_policy` now defaults to `skip`; an explicit
`error` remains strict. The hotfix keeps its existing rollback behavior.

Only the extra timer schedules reconciliation at 14:00 on weekdays. Regular
reconciliation is excluded during that window; weekend regular checks still run all day.
The attendance timer at 08:10 runs attendance, not the retained legacy Wi-Fi action.

A weekday direct CSV replacement at 10:00 waits until the 14:00 extra run unless
an operator triggers unrestricted reconciliation. After 14:00 there is no further
scheduled attendance application that day: the 16:00 action sets all devices Out.
Same-day school-day portal uploads still reconcile immediately from 08:10 until
before 16:00. Failed attendance/extra jobs have fewer automatic retries during
school hours. A job already running before 08:00 is allowed to finish; the guard
does not terminate active Jamf work. Weekday holidays use the same restricted
regular schedule; remaining weekday actions still apply holiday Out-Harrow state.
