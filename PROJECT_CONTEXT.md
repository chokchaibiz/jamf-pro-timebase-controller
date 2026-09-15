# Project Overview

Harrow TimeBase controls school-day policy for Harrow's Jamf-managed iPads. School administrators upload absence lists and holiday calendars, or request temporary device exceptions through an internal web portal. The system translates that information and Bangkok local time into Jamf Room values and smart-group membership, which in turn controls the existing ASSURE profile scope. It replaces manual bulk inventory changes without requiring staff to use SSH or know device serials.

Main flows: sign in/change password; upload absence CSV or explicitly confirm zero absences; replace/download the holiday calendar; search an exact inventory email and set/clear a manual Out-Harrow override; inspect job status/history. Scheduled reconciliation repairs drift independently of portal use.

This is an operational deployment bundle with R4 application authentication and scheduler upgrades, plus recorded small-pilot integration evidence. Repository naming such as “production” is not proof of deployment or fleet-scale validation. See `CURRENT_STATUS.md` for dated evidence and unfinished work.

# Technology Stack

- Python, with standard-library `csv`, JSON, `zoneinfo`, `fcntl`, XML, hashing/HMAC, and thread pools. Syntax requires Python 3.10+; no explicit supported-version matrix is declared. The recorded Linux pilot used Ubuntu 24.04.
- `requirements.txt`: requests `>=2.31,<3`, FastAPI `>=0.115`, HTTPX `>=0.27,<1`, Uvicorn `>=0.30`, python-multipart `>=0.0.9`, Jinja2 `>=3.1`. Requests handles synchronous HTTP; HTTPX supports FastAPI TestClient. There is no dependency lockfile.
- Server-rendered HTML/Jinja2, plain CSS and JavaScript; no Node package manifest or frontend build. Node is optional for JavaScript syntax verification.
- JSON/CSV/JSONL files provide persistence. There is no SQL database, ORM, SQL schema, or database migration directory.
- Linux systemd services/timers/path activation; Nginx TLS proxy; shell deployment scripts. No containers, cloud-hosting manifest, IaC, or CI workflow is tracked. OrbStack was a test environment, not an established production hosting requirement.
- Jamf Pro Cloud is the external integration. No payment, messaging, webhook, or external identity-provider integration is implemented.

# System Architecture

```text
Staff browser -> Nginx TLS :8443 -> Portal :8090 (harrow-upload)
                                    |-> staging + atomic queue JSON
                                    |       -> systemd.path -> importer (harrow-timebase)
                                    |            -> canonical CSV/state -> controller CLI
                                    |-> token-authenticated broker :8091 -> Jamf reads
                                    |<- shared status, calendar, overrides
systemd timers ------------------------> controller CLI -> Jamf Room updates
                                                        -> smart groups -> ASSURE scope
```

Both HTTP services bind to loopback in their service units. Portal cannot read Jamf credentials. The broker is read-only with respect to Jamf, though it writes a local master-membership cache. Importer and controller use the privileged service account, not root. Root is used for installation and service administration.

`harrow_timebase.py` owns configuration loading, logging, process locking, `JamfClient`, the public `TimeBaseController` facade, and CLI dispatch. Controller mixins split calendar/override state, email resolution, and actions/verification. The facade re-exports shared exceptions/value objects relied upon by other modules and tests.

Portal publishes completed `*.job.json` files into a watched directory. `attendance_importer.py` processes a sorted queue snapshot sequentially, dispatches by action, writes status plus audit, and removes processed queue/staging files. This is a filesystem queue, not FastAPI background tasks or a durable message broker. The controller subprocess takes the controller lock; importer preflight, CSV activation, and resolution-cache writes do not take that lock themselves. Do not assume an entire import is serialized with scheduled reconciliation.

The controller derives desired state, writes only devices missing from the appropriate current smart group, and polls Jamf smart-group recalculation. Jamf is authoritative for device inventory and membership; filesystem state is authoritative for attendance/calendar/overrides. There is no central in-memory frontend state store.

# Repository Structure

| Location | Responsibility |
|---|---|
| `harrow_timebase.py`, `timebase/controller/` | Jamf client, public CLI, schedule and desired state, inventory resolution, verification |
| `attendance_importer.py`, `timebase/importer/` | Queue lifecycle, action handlers, archival and atomic persistence |
| `attendance_common.py`, `holiday_common.py` | Shared upload parsing and canonical CSV generation |
| `device_query_service.py` | Internal exact-email search and serial lookup with Classic API fallback |
| `portal/attendance_portal.py`, `portal/auth_store.py` | Web routes/auth middleware; local accounts and signed sessions |
| `portal/templates/`, `portal/static/` | Thai/English web UI, common page shell, CSS, upload JavaScript and crest |
| `config*.json`, `portal-config*.json`, `harrow-timebase.env.example` | Controller/portal deployment configuration and credential-variable template |
| `systemd/`, `nginx/` | Runtime users, sandbox permissions, activation, timer expressions, TLS proxy |
| `install-program.sh`, `install.sh`, `apply-hotfix.sh` | Fresh installation, compatibility wrapper, existing-R4 hotfix migration |
| `tests/`, `test-results/2026-09-10/` | Offline checks, opt-in live test harness, historical sanitized test evidence |
| `README.md`, `SCHEDULE-UPGRADE.md` | Operator reference and current hotfix/recovery procedure |
| `README-updated.md`, `RELEASE-NOTES.md`, `MIGRATION-EMAIL-ATTENDANCE.md` | Duplicate README and historical material; known stale claims are listed in status |
| `holidays.csv`, `examples/`, `attendance/` | Example calendars/absence data, not authoritative school records |

# Core Features and Important Business Logic

## Schedule and Jamf policy

Implemented in `timebase/controller/actions.py`, with date/calendar helpers in `state.py`.

| Bangkok time, school day | Desired master-device state |
|---|---|
| Before 08:00 | All Out-Harrow |
| 08:00–before 08:10 | All In-Harrow except active manual overrides |
| 08:10–before 16:00 | Out = resolved attendance absences union active overrides; In = master minus Out |
| From 16:00 | All Out-Harrow; clear overrides after successful verification |

Weekends and dates in the holiday calendar stay Out-Harrow. Preflight requires a valid, nonempty calendar even for actions on a weekend. School-start, attendance, and 1600 CLI actions all call `reconcile()` so delayed invocations do not replay the wrong phase.

`systemd/harrow-timebase-school-start.timer`, `harrow-timebase-attendance.timer`, and `harrow-timebase-1600.timer` schedule weekday 08:00, 08:10, 16:00 starts. Regular reconciliation schedules :00/:30 in weekday hours 00–07 and 16–23, and all weekend hours, plus three minutes after boot. Its timer targets `harrow-timebase@reconcile-regular.service`; the `reconcile-regular` CLI action skips Monday–Friday 08:00–15:59 after acquiring the lock and before preflight/Jamf calls. This also covers weekday holidays and delayed/boot runs. Extra weekday 09:10/14:00 runs retain `harrow-timebase-reconcile.service` and unrestricted `reconcile`; portal/manual and daily actions remain unrestricted. There is no regular 14:00 weekday slot. Calendars use Asia/Bangkok and `Persistent=false`. Separate controller jobs serialize at the file lock; an already-running job is not interrupted at 08:00. Scheduled times are starts, not completion guarantees.

Preflight checks the static master group, smart In/Out groups, Room criteria, master count bounds, and ASSURE target/exclusion. Shipped names are `Harrow-All-iPads`, `In-Harrow`, `Out-Harrow`; Room values are strings `100` and `200`. ASSURE is inspected, not edited. Its target must include In-Harrow and its exclusions Out-Harrow. Smart-group criterion checks look for the expected Room name/value; they do not exhaustively validate operators, additional criteria, or all profile scope semantics.

Wi-Fi profile management is retained behind `features.wifi_management_enabled`, default false. Disabled runs do not fetch its individual profile or change its scope. The old `0810` Wi-Fi action remains unscheduled; the 08:10 attendance timer invokes the separate `attendance` action. Optional enabled behavior changes the WiFi-Harrow In-Harrow target, blocks all-device scope, and can require a verified attendance hash marker. Disabling management does not remove an already-scoped profile.

## Attendance

Portal `upload_attendance` -> shared parser -> `handle_attendance` -> controller resolution/reconciliation. Uploads accept UTF-8/BOM CSV, email-header aliases, lowercase/trim identities, ignore blank cells, deduplicate, and reject malformed email syntax. Canonical output is `email_address` with one email per row. Serial-only legacy attendance is not accepted. Header-only CSV is an explicit zero-absence record.

Resolution is limited to the master set. It prefers paginated `/api/v2/mobile-devices/detail` with GENERAL/USER_AND_LOCATION sections, then Classic Location lookups for missing serials/emails or failed modern requests. The fallback exists for observed tenant responses with absent/null serialNumber. Coverage counts inventory devices successfully seen, not the proportion with populated email fields.

Default `email_match_policy=unique` rejects ambiguous multi-device matches; `all_matches` deliberately permits them. `unmatched_email_policy=skip` warns and continues (including zero matches); explicit `error` rejects. Original normalized emails remain in canonical storage. Partial resolution records include unresolved emails and are never cache hits, allowing later inventory recovery. Complete resolutions are reused by file path/hash while all cached serials remain in the master set.

Missing-file policy is separately configured: shipped `zero_absent` treats a missing file as no attendance absences, using a deterministic synthetic hash; omitted policy defaults to `error` in `load_config`. Manual overrides still apply. Default absence limit is 50% of master devices; default inventory coverage minimum is 95%; shipped master bounds are 1,800–2,200. These are configuration, not observed fleet size.

Importer validates before archiving/replacing attendance, writes a resolution cache, and removes the old success marker. Same-day school-day imports started between 08:10 and 16:00 invoke immediate reconciliation; other dates/times are stored without immediate attendance writes. Direct canonical CSV replacement has no watcher; the next scheduled run reads it. Post-write smart-group verification precedes a new success marker.

## Holidays

`holiday_common.py` accepts single dates or inclusive `end_date` ranges, date formats YYYY-MM-DD/DD-MM-YYYY/DD/MM/YYYY, at most 370 days per range and 2,000 expanded dates, and rejects overlaps, reversed ranges, missing dates, or an empty calendar. Canonical storage is daily `date,description` rows. Upload replaces the entire calendar, archives the previous file, resolves the compatibility symlink, and by default reconciles immediately (`handle_holiday_upload`). Portal collapses consecutive same-description dates for display.

## Manual overrides and device lookup

Portal searches exact case-insensitive email through the broker, displays only master devices, and rereads the selected serial/email before queueing. Broker uses modern filtered inventory with Classic match/General&Location fallback; a short-lived master cache limits membership lookups (default 900 seconds). Results default to at most 50.

Controller independently checks current master membership and permits new overrides only on school days from 08:00 until before 16:00. Override records expire at that day's 16:00 and remain separate from absence CSV. Clearing an override reconciles from attendance/current phase; it does not blindly write Room 100. At queue execution the controller rechecks membership/time, but does not revalidate the recorded email again.

## Status/history

Job JSON drives `/status/{job_id}` and recent history; nonterminal pages refresh every three seconds. Import results distinguish `SUCCESS`, `SUCCESS_WAITING_SCHEDULE`, `FAILED`, and `IMPORTED_RECONCILE_FAILED`. An activated file can survive failed reconciliation; a later reconciler can repair device state. Status records represent that import attempt and are not automatically rewritten when a later scheduled retry succeeds. Recent history reads up to 100 status files, not audit JSONL.

# Data Model

There are no relational tables or database migrations. Contracts are defined by the readers/writers below, without a central schema/version migration system.

| Entity / installed default | Fields and relationships |
|---|---|
| Jamf mobile device | Serial is the write identity, normalized uppercase. Inventory email joins attendance; devices must belong to static master set. Room drives smart-group membership. |
| `/opt/harrow-timebase/attendance/absent-YYYY-MM-DD.csv` | Date keyed by filename; canonical email identities. Previous versions archived in `/opt/harrow-timebase/archive/YYYY/MM/`. |
| `/var/lib/harrow-timebase/shared/holidays.csv` | Unique daily dates and descriptions; installer maintains `/opt/harrow-timebase/holidays.csv` symlink. Holiday archives live under `archive/holidays/YYYY/`. |
| `upload-queue/<job_id>.job.json` under state root | Timestamp/random job ID; action, job_type, submission metadata, selected date/staged filename or device metadata. `queue_job` and `ACTION_HANDLERS` define action contracts. |
| `upload-status/<job_id>.json` under state root | Original job plus processing/completion times, status/message, counts, archive/final paths, optional reconcile exit/output; matched/skipped counts and skipped emails on partial imports. |
| `/var/log/harrow-timebase/upload-audit.jsonl` | Append-only job-result records. Not a transactional database audit or tamper-proof ledger. |
| `attendance-YYYY-MM-DD.resolution.json` under state root | Date, source path/SHA-256, emails, resolved serials, unresolved emails, resolved time. Links canonical attendance to Jamf identities. |
| `attendance-YYYY-MM-DD.ok.json` under state root | Source path/hash, date, absent-device count and verified time, written after partition verification. |
| `shared/manual-overrides.json` under state root | Version 1, overrides keyed by serial, optional updated_at; records include serial/email/username/device, submitted_by/reason, created_at/expires_at. |
| `public/master-serials.txt` under state root | One serial per line; refreshed by preflight/broker, not a full device inventory. |
| `portal-auth/users.json` under state root | Version 1, users keyed by normalized username; salt/hash/iterations/session_version/disabled plus optional timestamps. |
| `portal-auth/session.key`, `.users.lock` | Private signing key and account mutation/session-version locking. Never copy contents into docs or fixtures. |

State root defaults to `/var/lib/harrow-timebase`. Atomic file replacement prevents partial individual records, but does not make CSV/cache/status/audit changes a single transaction. Permission/ownership and systemd ReadWritePaths are part of this data architecture.

# APIs and Integrations

| Boundary | Routes/actions and implementation |
|---|---|
| Portal auth | GET/POST `/login`, POST `/logout`, GET/POST `/change-password`; `portal/attendance_portal.py`, `portal/auth_store.py` |
| Attendance/calendar | GET `/`, POST `/upload`, `/zero`, `/upload-holidays`, GET `/holidays/current` |
| Override UI | GET `/device-override?q=...`, POST `/device-override/submit`, `/device-override/clear` |
| Job UI | GET `/status/{job_id}`, `/history`; all above feature routes require a session |
| Health/static | Public GET `/healthz` and `/static/*`; health is process liveness, not full Jamf/queue readiness |
| Broker | GET `/search?email=...`, `/device/{serial}` require `X-Internal-Token`; GET `/healthz` is public on loopback; `device_query_service.py` |
| Controller CLI | `preflight`, `school-start`, `attendance`, `1600`, `reconcile`, `reconcile-regular`, `verify`, `manual-out`, `manual-clear`, retained `0810`, `wifi-on`, `wifi-off`; `harrow_timebase.py` |
| Jamf authentication | POST `/api/v1/oauth/token`, client_credentials; cached bearer tokens with early refresh and a token lock |
| Jamf inventory | GET `/api/v2/mobile-devices/detail`; Classic `/JSSResource/mobiledevices/...` match, serial/id and subset reads |
| Jamf groups/profiles | Classic `/JSSResource/mobiledevicegroups` and `/mobiledeviceconfigurationprofiles`, plus `/id/{id}` reads; device Room PUT by serial and optional profile XML PUT |

`JamfClient` supplies per-thread Requests sessions, transient-request retries/backoff, token refresh, and dry-run write guards; batch Room updates add retry rounds. No webhooks, RPC framework, or JavaScript server actions exist. Internal write requests are filesystem jobs and a controller subprocess whose installed executable/config paths are hardcoded in `run_controller`.

# Authentication and Authorization

`AuthStore` uses PBKDF2-HMAC-SHA256 (600,000 iterations, per-user random salt) and constant-time comparisons. Signed cookie payloads include username, session version, issuance/expiry, and nonce; they are signed, not encrypted. Every protected request checks the current user record and disabled/session-version state. Password changes increment the version and issue a new cookie, invalidating older sessions. Logout deletes the browser cookie but does not revoke a copied token server-side.

Cookies default to HttpOnly, Secure, SameSite=Lax and eight hours. Middleware allows login/health/static anonymously; other routes redirect to login. Redirect targets are constrained to local paths. POST forms require the configured constant form token; it is rendered in the public login page as well as authenticated forms and is not a user-specific token or authorization credential.

All enabled accounts have the same administrator capabilities; no role tiers, per-job ownership authorization, SSO, MFA, or application rate limiting is implemented. The installer creates five initial accounts and preserves existing accounts/signing keys on rerun. Add-user validates normalized username uniqueness and minimum eight-character passwords; initialization has weaker validation than add-user.

Unix service isolation, loopback binding, internal token, TLS, and trusted administration-network access are intended operational boundaries. Shared directories use setgid ownership so portal and importer can exchange files without sharing Jamf credential access; auth files are private to the portal account. Inspect `install-program.sh` and each service unit before altering these permissions.

# Architecture Decisions

| Choice and evidence | Reason and change implications |
|---|---|
| Room -> smart groups -> ASSURE (`actions.py`, Jamf configuration in README) | Uses existing profile target/exclusion relationships instead of directly editing ASSURE. Changing Room/group meanings affects school policy; broader original rationale is not recorded. |
| Separate portal, query broker, privileged queue importer | Source comments explicitly isolate Jamf secrets from the web process. Preserve both filesystem and network boundaries. |
| Email CSV identity, serial writes (`attendance_common.py`, `attendance.py`) | README explains staff-friendly identity while Jamf writes remain serial-based. Modern/Classic paths and cache semantics must move together. |
| Mixins and handler dispatch (`timebase/`, regression tests) | Responsibility split is explicit and tested. Preserve the public facade and dependency-light shared types. |
| File persistence and systemd activation | Implementation fits the Linux deployment bundle; no recorded comparison with a database/message broker. Scaling/concurrency changes require revisiting multi-file consistency. |
| Current-time reconciliation and nonpersistent timers | Comments/tests explicitly prevent delayed jobs undoing a later phase. Preserve time checks in both controller and importer. |
| On-demand broker and resolution caching | README explicitly avoids repeated full inventory polling. Complete cache entries reduce API load, while partial entries retry inventory recovery. |
| Wi-Fi feature flag default off | September scheduler change explicitly defers management while retaining optional code. Product criteria for re-enabling are unknown. |
| Stateful hotfix migration | Preserve operational configuration/auth and independent timer enabled/running states. New modules require deployment-list updates; rollback cannot reverse completed Jamf changes. |

# Development Conventions

Use existing Python function/class naming and focused modules, dataclasses for parsed data/context/results, and explicit action maps. Config and jobs remain dictionaries with boundary validation rather than Pydantic domain models. Jinja pages extend the common shell (login has its own page); HTML forms use server redirects/errors and shared CSS. Keep Thai/English text and escaping consistent, and preserve `data-file-*` attributes used by upload JavaScript/tests.

Controller errors have shared exit codes: configuration 2, preflight 3, API/controller 4, verification 5, attendance 6; unexpected CLI failures return 99. `EXIT_LOCK=75` exists but lock timeout currently raises generic `ControllerError` (4). Logging uses stdout/journald plus a rotating 20 MiB file with ten backups; importer adds JSONL audit. Queue boundary catches handler exceptions, records failure, and removes the job; it does not retry the job automatically.

Tests mix `unittest`, direct assertions, AST/source-contract checks, temporary directories, and mocked Jamf calls. Use the exact script commands in `AGENTS.md`; a generic pytest discovery command is not the repository test contract.

# Environment and Configuration

Never put secret values in these context files. The JSON examples and production JSON files are identical pairs at the inspected revision; both contain deployment paths and a specific Jamf tenant URL. They are not safe isolated-development defaults. Installer prefers production-named files when present and preserves existing installed configuration.

| Variable | Purpose / source |
|---|---|
| `JAMF_CLIENT_ID`, `JAMF_CLIENT_SECRET` | Required by JamfClient, including dry-run; installed `/etc/harrow-timebase/harrow-timebase.env`, root:harrow-timebase 0640 |
| `HARROW_CONFIG` | Broker controller-config path; CLI/importer use `--config` instead |
| `PORTAL_CONFIG` | Portal JSON path read at module import; default `/etc/harrow-timebase/portal.json` |
| `HARROW_PORTAL_FORM_TOKEN` | Required for successful form submissions; generated into `/etc/harrow-timebase/portal.env` |
| `HARROW_INTERNAL_API_TOKEN` | Shared portal/broker token; generated into `shared/internal-api.env` |
| `HARROW_AUTH_DIR` | Auth directory; defaults to state-root `portal-auth` |
| `HARROW_SESSION_COOKIE` | Cookie name override; default `harrow_timebase_session` |
| `HARROW_SESSION_TTL_SECONDS` | Cookie/session TTL; default 28800 |
| `HARROW_AUTH_COOKIE_SECURE` | Secure cookie switch, true by default; false only for isolated local HTTP development |
| `HARROW_DEFAULT_ADMIN_PASSWORD` | Installer/init-users initial password source; explicitly supply privately because installer has a hardcoded fallback |
| `HARROW_NEW_USER_PASSWORD` | Default add-user password variable; `--password-env` permits a different variable name |
| `PYTHONPATH` | Installed portal needs project root for shared modules; module import also needs portal directory for `auth_store` |
| `HARROW_RUNTIME_ROOT` | Live test harness only: selects installed runtime import root |

Controller JSON sections: `jamf_url`, `timezone`, `groups`, `profiles`, `rooms`, `paths`, `performance`, `safety` are required; attendance/device_query/features supply additional policies described above. Validation is partial: do not assume a parsed JSON file has every valid nested field. Portal JSON controls shared paths, upload size (2 MiB default), date window (seven past/thirty future days), broker URL/timeout, and holiday reconciliation. Its listen/public port/scheme metadata does not change the hardcoded systemd/Nginx listeners.

# Local Development

Use an isolated virtualenv; no database setup or build step is required:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
```

Run offline checks from `AGENTS.md` before connecting to a tenant. Portal imports eagerly load config and require initialized users, so simply importing it without setup fails. For an isolated local UI session, the following is a development adaptation of repository configuration, not a shipped setup script:

```bash
export TIMEBASE_DEV_DIR="$(mktemp -d /tmp/harrow-dev.XXXXXX)"
python - <<'PY'
import json, os, secrets
from pathlib import Path
root = Path(os.environ['TIMEBASE_DEV_DIR'])
cfg = json.loads(Path('portal-config.example.json').read_text())
for key in cfg['paths']:
    cfg['paths'][key] = str(root / key)
for key in ('staging_dir', 'queue_dir', 'status_dir', 'archive_dir'):
    Path(cfg['paths'][key]).mkdir()
Path(cfg['paths']['holiday_file']).write_text(Path('holidays.csv').read_text())
(root / 'portal.json').write_text(json.dumps(cfg))
(root / 'form-token').write_text(secrets.token_urlsafe(32))
PY
export PORTAL_CONFIG="$TIMEBASE_DEV_DIR/portal.json"
export HARROW_AUTH_DIR="$TIMEBASE_DEV_DIR/auth"
export HARROW_AUTH_COOKIE_SECURE=false
export HARROW_PORTAL_FORM_TOKEN="$(cat "$TIMEBASE_DEV_DIR/form-token")"
python - <<'PY'
import getpass, os, sys
from pathlib import Path
sys.path.insert(0, 'portal')
from auth_store import initialize_users
initialize_users(Path(os.environ['HARROW_AUTH_DIR']), ['local-admin'], getpass.getpass('Local portal password: '))
PY
PYTHONPATH="$PWD:$PWD/portal" python -m uvicorn attendance_portal:app --host 127.0.0.1 --port 8090
```

Open `http://127.0.0.1:8090`. This supports login/forms/staging only: there is no local importer watcher, and device search needs a separately configured broker/internal token. Do not start the importer against these queued files expecting a local-only simulation; its subprocess hardcodes `/opt` and `/etc` paths.

For an explicitly authorized isolated Jamf integration environment, copy `config.example.json`, replace its tenant/group/safety bounds and every runtime path, supply credentials privately, and point `HARROW_CONFIG` at that file. Start broker with `python -m uvicorn device_query_service:app --host 127.0.0.1 --port 8091`; set the same internal token on portal/broker and restart the portal after environment changes. Full service/queue testing needs Linux installation or mocks, as in the existing tests. No lint/type/build command is configured; syntax and test verification are documented in `AGENTS.md`.

# Deployment

Fresh install: on the intended Linux server, privately provide credentials/bootstrap password, then `sudo bash install-program.sh` (`install.sh` delegates). It installs OS/Python dependencies, changes server timezone, creates service users/directories, migrates holiday storage, initializes auth once, creates internal/form tokens and a self-signed TLS certificate, installs Nginx/systemd, and starts portal/broker/queue. Core controller timers are deliberately not enabled automatically on a fresh install. Existing enabled units require separate inspection; the full installer is not the controlled scheduler-upgrade path.

Existing R4 upgrade: read `SCHEDULE-UPGRADE.md`, extract the complete bundle separately from `/opt/harrow-timebase`, then run `sudo bash apply-hotfix.sh`. It requires existing valid accounts/signing key, Bangkok timezone, Wi-Fi disabled, and installed dependencies. It validates/tests before pausing services, drains active work, acquires the controller lock, backs up runtime/config/auth/unit state, installs the full runtime and migrates old timers. It preserves config/env contents and Nginx, does not reinstall dependencies, and independently restores enabled/running timer states. Before job resumption it attempts rollback on failure; after its commit point, fix failed starts with the new runtime installed. Backups contain credentials and must remain root-only.

Destination-host verification (read-only checks):

```bash
systemctl status harrow-attendance-portal.service harrow-device-query.service harrow-attendance-import.path
systemctl list-timers --all 'harrow-timebase*'
curl -fsS http://127.0.0.1:8090/healthz
curl -fsS http://127.0.0.1:8091/healthz
sudo journalctl -u harrow-timebase@reconcile-regular.service -u harrow-attendance-import.service -u harrow-timebase-reconcile.service -n 100 --no-pager
sudo nginx -t
systemd-analyze calendar 'Mon..Fri *-*-* 00..07,16..23:00,30:00 Asia/Bangkok'
systemd-analyze calendar 'Sat,Sun *-*-* *:00,30:00 Asia/Bangkok'
systemd-analyze calendar 'Mon..Fri *-*-* 09:10:00 Asia/Bangkok'
systemd-analyze calendar 'Mon..Fri *-*-* 14:00:00 Asia/Bangkok'
```

Manual pilot flows: login/password change and old-session rejection; browse and drag/drop both CSV types; zero-absence confirmation; partial-match warnings and strict rejection; archive/status/audit results; exact-email override/clear; boundary/holiday reconciliation; unchanged Wi-Fi scope. Separate Jamf group verification from physical-device payload delivery. Do not enable timers or run writing pilot flows without authorization for that environment. TLS trust, firewall/network restrictions, fleet scale, and effective service sandboxing need host-specific verification.

# Known Technical Debt

- Filesystem persistence has no transaction across attendance/cache/marker/audit files. Importer activation is outside the controller lock; independent manual import processes are not serialized by an importer lock. Concurrency/interruption behavior needs care.
- Complete attendance resolution caches have no inventory TTL, email-policy fingerprint, or full master-membership fingerprint. Same-file inventory reassignment, added devices, or policy changes may not invalidate a complete cache. Partial caches deliberately retry every run, increasing API load.
- Direct controller attendance parsing duplicates header/normalization logic and checks only for `@`; portal/importer uses the stronger shared email regex. Behavior can differ for malformed direct files or multiple alias columns.
- Auth lacks rate limiting/MFA/role separation; installer has a hardcoded shared initial-password fallback and prints the selected bootstrap password. Do not propagate it. Initializer validation is weaker than add-user/password-change validation.
- Importer can activate attendance then record generic `FAILED` if subprocess execution raises (for example timeout); ordinary nonzero reconcile exits use `IMPORTED_RECONCILE_FAILED`. Inspect canonical state before retrying. Holiday handler handles this distinction more explicitly.
- Unknown/malformed missing status records render as perpetually QUEUED; malformed queue JSON is logged/deleted without a normal failed status. No dead-letter queue, retention/cleanup job, or health check for stalled work is tracked. History sorts all status filenames before limiting results.
- Some tests check strings/AST structure rather than execution. Drag/drop checks do not drive a browser; broker fallback tests largely check presence, while controller fallback has mocked runtime coverage. No CI or pinned dependency environment ensures repeatability.
- Hardcoded installation paths, explicit deployment lists, and duplicate README files add maintenance coupling. Historical documentation conflicts and release-label drift are tracked in `CURRENT_STATUS.md`.

# Areas Requiring Extra Caution

Schedule edits span controller, importer, overrides, timers, and both README copies. Identity edits span shared parser, direct CSV reader, modern/Classic resolution, caches, broker, and status counts. Permission edits span filesystem setgid ownership, parent traversal, systemd read/write allowlists, and account isolation. New modules must ship through both deployment scripts. Auth upgrades must preserve users and signing key. Jamf preflight/group verification checks server inventory state, not whether physical iPads received profiles.

# Open Questions

- What host/version is actually deployed, with what timer activity, effective sandbox overrides, fleet size, and trusted TLS/network setup?
- Who owns the official holiday calendar and verifies bootstrap accounts have been secured?
- Is zero matched attendance intentionally acceptable for every deployment, or should some sites explicitly select strict mode?
- What is the required cache freshness when devices are reassigned without a CSV change?
- What are the supported Python/dependency versions and operational retention/backup requirements for student/device audit data?
- Is Wi-Fi re-enablement planned, and what pilot/physical-device criteria would authorize it? No active timetable is defined.
