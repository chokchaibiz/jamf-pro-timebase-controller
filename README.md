# Production R3 — 2026-08-25

This bundle incorporates fixes verified on the installed Harrow TimeBase server:

- **Complete hotfix deployment:** `apply-hotfix.sh` now installs all runtime modules, Portal templates/static files, and systemd units instead of replacing only `harrow_timebase.py`.
- **EXDEV-safe Portal queue handoff:** cross-filesystem queue moves no longer fail with `Invalid cross-device link`. The Portal writes/copies to a hidden file in the destination filesystem and exposes the final `.job.json` only after the copy is complete.
- **Classic API Email fallback:** Attendance Email → Serial resolution prefers `/api/v2/mobile-devices/detail`, but automatically falls back to Classic `Mobile Devices / Location` lookups when the tenant returns missing/blank `serialNumber`.
- **Classic API Device Override fallback:** live Email search and selected-Serial revalidation also fall back to Classic API when the v2 inventory response is unusable.
- **`classic_xml(params=...)` compatibility retained.**
- **UTF-8 BOM / Excel CSV support retained.**
- **Regression checks included:** the installer and hotfix validate syntax, BOM parsing, Classic fallback functions, EXDEV handling, and complete hotfix deployment before restarting services.
- Existing `/etc/harrow-timebase/config.json`, Jamf credentials, Attendance/History, Holidays, and Manual Override state are preserved by `apply-hotfix.sh`.

## Update an existing server

```bash
unzip harrow-timebase-r3-hotfix.zip
cd harrow-timebase-r3-hotfix
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

> This release provides a browser-based TimeBase Upload Portal for both **Absent Students** and **Holiday Calendar**, Nginx Basic Authentication, a systemd queue importer, automatic validation/archive, and immediate Jamf reconcile for same-day attendance uploads between 08:00–15:59.

This bundle implements the following desired state without The MUT:

- **ASSURE** configuration profile
  - Target: `In-Harrow`
  - Exclusion: `Out-Harrow`
- **In-Harrow** Smart Mobile Device Group: `Room = 100`
- **Out-Harrow** Smart Mobile Device Group: `Room = 200`
- **Harrow-All-iPads** Static Mobile Device Group: master list of Harrow iPads
- **WiFi-Harrow** configuration profile
  - 08:10 school days: add `In-Harrow` to Targets
  - 16:00 school days: remove `In-Harrow` from Targets
- Weekends and dates in `holidays.csv` remain in Out-Harrow / WiFi-Harrow OFF state.

## Daily state

| Local time (Asia/Bangkok) | Room / Smart Group | ASSURE | WiFi-Harrow |
|---|---|---|---|
| 16:00–06:59 | all Room=200 / Out-Harrow | off by exclusion | target removed |
| 07:00 | all Room=100 / In-Harrow | on | off |
| 08:00 | absent Room=200; present Room=100 | present on / absent off | off |
| 08:10–15:59 | attendance state | present on | target In-Harrow |
| 16:00 | WiFi target removed first; then all Room=200 | off | off |

## Safety decisions

1. API concurrency defaults to **4** and configuration rejects values over 5.
2. Room updates are **idempotent**: only devices not already in the desired Smart Group are PUT.
3. API requests use retry + exponential backoff for transient failures.
4. A process lock prevents overlapping 07:00/08:00/08:10/16:00/reconcile jobs.
5. `WiFi-Harrow` is fail-safe: it is not enabled unless today's attendance state is verified successfully (uploaded CSV, explicit zero-absence confirmation, or the configured `zero_absent` missing-file policy).
6. Production policy is `missing_attendance_policy = "zero_absent"`. If today’s `absent-YYYY-MM-DD.csv` is missing, the controller does **not** fail: it treats `Absent = 0`, repairs all master iPads to `Room=100 / In-Harrow`, writes a verified synthetic attendance marker, and allows the 08:10 workflow to continue.
7. Attendance Email addresses must resolve to valid iPads in `Harrow-All-iPads`; missing/ambiguous Email matches fail safely.
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

### Profile: WiFi-Harrow

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

The systemd timers explicitly use `Asia/Bangkok`, so the server can use another system timezone if required.

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

At 08:00 (and on immediate reconcile after a same-day upload), the privileged controller reads current Jamf Mobile Device Inventory for `Harrow-All-iPads`, builds an in-memory Email -> Serial index from `emailAddress`, and resolves each absent Email to its iPad before applying `Room = 200` / `Out-Harrow`. The source CSV therefore remains human/attendance-system friendly while Room updates still use the resolved iPad Serial internally.

Production default is `attendance.email_match_policy = "unique"`: every absent Email must resolve to exactly one iPad in `Harrow-All-iPads`. An Email that is missing from inventory or matches multiple master iPads causes that attendance import to fail safely rather than guessing a device. An optional `all_matches` policy exists for environments that intentionally assign multiple iPads to one Email.

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

The installer now installs Python dependencies, Nginx, `htpasswd`, the FastAPI Portal, upload queue/importer, systemd units, and the existing TimeBase controller. It enables only the Portal and attendance queue automatically; the 07:00/08:00/08:10/16:00 Jamf timers remain disabled until pilot validation is complete.

At the end of a first installation it prints:

```text
Portal URL : https://SERVER-IP:8443/
Portal user: harrow-admin
Portal password (shown once): <generated password>
```

The generated password is stored only as a bcrypt hash in `/etc/nginx/.harrow-timebase.htpasswd`. Change it with:

```bash
sudo htpasswd /etc/nginx/.harrow-timebase.htpasswd harrow-admin
sudo systemctl reload nginx
```

### User workflow

1. Open `https://SERVER-IP:8443/`.
2. Sign in with the Portal account.
3. Select Attendance Date. Today is the default.
4. Drag/drop or browse to any `.csv` filename. The user does not need to rename it.
5. The Portal normalizes supported Email headers (`Email Address`, `email_address`, `Email`, `emailaddress`, `user_email`, `student_email`) and removes duplicate Email addresses case-insensitively.
6. The queue importer performs authoritative live validation against Jamf: it scans current inventory for `Harrow-All-iPads`, resolves every Email to an iPad Serial, blocks missing/ambiguous matches, and only then writes the canonical attendance file.
7. An existing attendance file for the same date is archived before replacement.
8. Uploads for today between 08:00–15:59 on a school day automatically trigger `reconcile`; uploads before 08:00 wait for the normal scheduler; uploads after 16:00 are recorded without changing that day's Jamf state.

For a zero-absence day, use **No Absent Students Today**. It creates a header-only CSV and a user-attributed audit record.

### Queue design

```text
Browser
  -> Nginx :8443 HTTPS + Basic Auth
  -> FastAPI (127.0.0.1:8090, user harrow-upload)
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
- WiFi-Harrow exists and is not scoped to All Mobile Devices
- holiday file is valid

## 8. Dry-run

```bash
sudo -u harrow-timebase bash -c '
  set -a; source /etc/harrow-timebase/harrow-timebase.env; set +a
  /opt/harrow-timebase/venv/bin/python \
    /opt/harrow-timebase/harrow_timebase.py \
    --config /etc/harrow-timebase/config.json --dry-run 0700
'
```

Dry-run reads Jamf and prints the writes it would perform, but does not PUT Room/profile changes.

## 9. Pilot

Before 2,000 devices, temporarily use a pilot Static Group and safety range, for example 5–20 devices. Validate:

1. `0700` moves pilot devices to Room 100 and In-Harrow.
2. ASSURE becomes assigned through In-Harrow.
3. `0800` resolves absent pilot Email addresses to their iPad Serials and changes only those devices to Room 200.
4. `0810` adds In-Harrow to WiFi-Harrow only after attendance verifies.
5. `1600` removes WiFi-Harrow target first, then moves all pilot devices to Room 200.
6. Home Wi-Fi behavior is tested on at least one absent/off-campus iPad before production rollout.

## 10. Manual action tests

```bash
sudo systemctl start harrow-timebase@0700.service
sudo systemctl start harrow-timebase@0800.service
sudo systemctl start harrow-timebase@0810.service
sudo systemctl start harrow-timebase@1600.service
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
sudo systemctl enable --now harrow-timebase-0700.timer
sudo systemctl enable --now harrow-timebase-0800.timer
sudo systemctl enable --now harrow-timebase-0810.timer
sudo systemctl enable --now harrow-timebase-1600.timer
sudo systemctl enable --now harrow-timebase-reconcile.timer
```

Check schedules:

```bash
systemctl list-timers 'harrow-timebase*'
systemd-analyze calendar 'Mon..Fri *-*-* 07:00:00 Asia/Bangkok'
systemd-analyze calendar 'Mon..Fri *-*-* 08:10:00 Asia/Bangkok'
```

## 12. Exact workflow

### 07:00

1. Check Monday–Friday and not annual holiday.
2. Run preflight.
3. Ensure WiFi-Harrow target In-Harrow is OFF.
4. Read `Harrow-All-iPads` membership.
5. Read current In-Harrow membership.
6. Only devices not already In-Harrow receive Room=100 PUT.
7. Poll Smart Groups until all master devices are In-Harrow and none are Out-Harrow.

### 08:00

1. Check today's attendance file. If it is missing and `missing_attendance_policy = "zero_absent"`, continue with `Absent = 0` instead of raising an error.
2. Build a current Email → Serial index from Jamf Inventory for `Harrow-All-iPads`.
3. Reject Email addresses that are missing or ambiguous under the configured match policy.
4. Reject implausibly high resolved-device absent ratio using the safety threshold.
5. Compute present and absent device sets.
6. Correct present devices to Room=100 if needed.
7. Correct absent devices to Room=200 if needed.
8. Verify exact master partition: Present=In-Harrow; Absent=Out-Harrow.
9. Write `/var/lib/harrow-timebase/attendance-YYYY-MM-DD.ok.json` containing attendance SHA-256.

### 08:10

1. Re-run/repair attendance state to recover from a transient 08:00 failure.
2. Verify attendance successfully.
3. Verify the current CSV SHA-256 matches the success marker.
4. Add In-Harrow to WiFi-Harrow Targets.
5. GET profile again and verify scope.

If an attendance CSV exists but is malformed, contains an Email that cannot be resolved uniquely to a master iPad, exceeds the absence safety threshold, or verification fails, WiFi-Harrow remains OFF. A **missing** CSV is not considered a failure when `missing_attendance_policy = "zero_absent"`; it represents zero absences.

### 16:00

1. Remove In-Harrow from WiFi-Harrow Targets.
2. Verify profile scope is OFF.
3. Read master/current Out-Harrow state.
4. Only devices not already Out-Harrow receive Room=200 PUT.
5. Poll until all master devices are Out-Harrow and none are In-Harrow.

## 13. Reconciliation

`harrow-timebase-reconcile.timer` runs after boot and every 30 minutes. It computes current desired state:

- weekend/holiday: WiFi OFF, all Room=200
- before 07:00: WiFi OFF, all Room=200
- 07:00–07:59: WiFi OFF, all Room=100
- 08:00–08:09: attendance partition, WiFi OFF
- 08:10–15:59: attendance partition, WiFi ON after attendance verification
- 16:00 onward: WiFi OFF, all Room=200

Operations are idempotent, so a correct state causes reads/verification without repeating all 2,000 writes.

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
6. Observe at least one full 07:00 → 08:00 → 08:10 → 16:00 lifecycle.
7. Confirm off-campus/home Wi-Fi recovery behavior before declaring production acceptance.

## 16. Rollback / emergency commands

Emergency WiFi-Harrow target removal:

```bash
sudo systemctl stop harrow-timebase-0810.timer harrow-timebase-reconcile.timer
sudo systemctl start harrow-timebase@1600.service
```

If you only want to remove the WiFi target without changing Room:

```bash
sudo -u harrow-timebase bash -c '
  set -a; source /etc/harrow-timebase/harrow-timebase.env; set +a
  /opt/harrow-timebase/venv/bin/python \
    /opt/harrow-timebase/harrow_timebase.py \
    --config /etc/harrow-timebase/config.json wifi-off
'
```

Disable all automatic jobs:

```bash
sudo systemctl disable --now \
  harrow-timebase-0700.timer \
  harrow-timebase-0800.timer \
  harrow-timebase-0810.timer \
  harrow-timebase-1600.timer \
  harrow-timebase-reconcile.timer
```

## 17. Important operational note for Wi-Fi restriction

If WiFi-Harrow contains a restriction that permits only MDM-installed Wi-Fi networks, test profile removal while the iPad is still connected to Harrow Wi-Fi. A device that leaves campus before receiving the profile-removal command may lose the path required to contact MDM. Consider removing the Wi-Fi restriction slightly before physical dismissal if operational policy permits it.


## Attendance Email-to-iPad resolution

Production configuration:

```json
"attendance": {
  "identity_field": "email_address",
  "email_match_policy": "unique",
  "inventory_page_size": 100
},
"safety": {
  "email_inventory_min_coverage": 0.95
}
```

The controller queries `/api/v2/mobile-devices/detail` using the `GENERAL` and `USER_AND_LOCATION` sections and limits the inventory scan to the `Harrow-All-iPads` group. It compares Email addresses case-insensitively. Before resolving attendance, at least 95% of the expected master devices must be visible in the inventory query; otherwise the controller aborts the attendance operation to avoid treating an incomplete inventory response as authoritative.

Resolution outcomes:

- **1 matching master iPad**: use that Serial and make it `Room = 200`.
- **0 matches**: fail the attendance import/reconcile and report the Email.
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
7. At 08:10, the WiFi-Harrow attendance guard accepts that verified state and can enable the `In-Harrow` target.

If a valid absent CSV appears later, the next `0800`, `0810`, or reconcile run reads the real file, applies the actual absent list to `Room = 200`, rewrites the marker with the real file SHA-256, and verifies the corrected state.

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
Nginx HTTPS + Basic Auth
        |
        v
Attendance Portal (harrow-upload)
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
