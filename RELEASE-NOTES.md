# Production R3 — 2026-08-25

This bundle incorporates fixes verified on the installed Harrow TimeBase server:

- **Installer permission boundary fixed:** `install-program.sh` gives both isolated service accounts execute-only traversal of shared parent directories, retains group-restricted files, normalizes installed ownership/modes, and validates access before starting services. `install.sh` remains a compatibility entry point.
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



## Device Override Email Search Update (2026-08-25)

- Device Override now searches Jamf Pro by exact `emailAddress` instead of Username.
- Search results remain restricted to `Harrow-All-iPads`.
- Submit revalidates the selected Serial against live Jamf inventory and verifies that its current Email Address still matches the searched Email before queuing `Room=200`.
- Manual override state/audit now records `email_address` in addition to Username, device name and Serial.
- Exact matching is intentional; there is no partial-email fallback. If one email maps to multiple master iPads, the portal lists the exact matches and requires an explicit device selection.
# Release Notes — Holiday Upload Portal Update

## Added

- `Upload Holidays` card on the same page as `Upload Absent Students`.
- Holiday CSV validation and normalization through `holiday_common.py`.
- Accepted date formats: `YYYY-MM-DD`, `DD/MM/YYYY`, `DD-MM-YYYY`.
- Duplicate holiday dates and invalid dates are blocked before activation.
- Previous `holidays.csv` is archived before replacement.
- Holiday file activation is atomic through a shared writable state path.
- Compatibility symlink keeps `/opt/harrow-timebase/holidays.csv` working for existing controller configurations.
- Current holiday count, upcoming holiday preview, and `Download Current holidays.csv` on the Portal.
- Holiday uploads are recorded in the existing status/history/audit workflow.
- `install.sh` migrates an existing holiday file automatically during upgrade.

## Attendance behavior

The existing `missing_attendance_policy = zero_absent` behavior is unchanged.

# Harrow TimeBase Portal Release

## Added

- FastAPI Attendance Upload Portal.
- Responsive Web UI for CSV upload, zero-absence confirmation, status, and history.
- Nginx HTTPS reverse proxy on TCP/8443 with Basic Authentication.
- Self-signed TLS certificate generation during first install; replaceable with an organization certificate.
- Separate unprivileged `harrow-upload` service account; Portal cannot read Jamf API credentials.
- `harrow-attendance-import.path` + `harrow-attendance-import.service` queue processing.
- CSV header normalization, duplicate removal, master-serial validation, maximum-absence safety validation.
- Automatic canonical filename `absent-YYYY-MM-DD.csv`.
- Atomic file replacement and archival of previous versions.
- Immediate `reconcile` for same-day school-day uploads between 08:00 and 15:59.
- Upload status JSON and append-only audit JSONL.
- `Harrow-All-iPads` serial cache refreshed by TimeBase preflight for fast Portal-side validation.
- `install.sh` installs OS/Python dependencies, Portal, Nginx, TLS, Basic Auth, and systemd upload services in one run.

## Existing behavior retained

- `missing_attendance_policy = zero_absent`.
- 07:00 all Room=100 / In-Harrow.
- 08:00 attendance application.
- 08:10 WiFi-Harrow Target=In-Harrow.
- 16:00 WiFi-Harrow target removed, then all Room=200 / Out-Harrow.
- Weekend and annual-holiday exclusion.
- Core Jamf schedule timers remain disabled until pilot validation is complete.

## Device Override / Live Jamf Email Address Search

- Added **Device Override** tab to search Jamf by Email Address and select an iPad.
- Search is now **live/on-demand** through a localhost read-only Device Query broker instead of a five-minute full inventory cache sync.
- The broker filters results to `Harrow-All-iPads` and revalidates the selected Serial before the Portal queues an override.
- Added persistent Manual Out-Harrow state until 16:00 so periodic reconciliation does not undo the exception.
- Added **Clear Override**, which removes the override and recalculates Room from current Attendance instead of blindly setting Room 100.
- Added `harrow-device-query.service` bound to `127.0.0.1:8091`.
- Added a random internal Portal-to-broker token; it is not a Jamf credential.
- Hardened Unix permissions: `harrow-upload` is not a member of the `harrow-timebase` group and cannot read the Jamf credential environment file.
- `install.sh` removes obsolete `harrow-inventory-sync.service/.timer` units during upgrade.

## Attendance Email Address Identity Update

- Changed Absent Students CSV identity from `serial_number` to `email_address` / `Email Address`.
- Portal accepts `Email Address`, `email_address`, `Email`, `emailaddress`, `user_email`, and `student_email` headers.
- Email addresses are normalized to lowercase and duplicates are removed case-insensitively.
- The privileged controller resolves Email -> iPad Serial from current Jamf Mobile Device Inventory before Room changes.
- Inventory resolution is limited to `Harrow-All-iPads` and uses paginated Jamf Pro API inventory data.
- Default `email_match_policy = unique`: missing or multi-device Email matches fail safely instead of choosing a device arbitrarily.
- Added `email_inventory_min_coverage = 0.95` safety gate before Email resolution.
- Canonical `absent-YYYY-MM-DD.csv` files now store Email Address rather than Serial Number.
- Existing `missing_attendance_policy = zero_absent`, Manual Device Override, Holiday Portal, and TimeBase schedule behavior remain unchanged.

## 2026-08-25 - Email Attendance API Wrapper Hotfix

- Fixed `JamfClient.classic_xml()` compatibility error: `unexpected keyword argument 'params'`.
- `classic_xml()` now accepts `params` and forwards query parameters through the common OAuth/retry request wrapper.
- Confirmed UTF-8 BOM Email Attendance CSVs (for example Excel-exported `Book1.csv`) are accepted via `utf-8-sig` parsing.
- This hotfix does not modify installed Jamf credentials, configuration, attendance history, holiday calendar, or manual override state.
