# Regular reconciliation window — 2026-09-15

Implemented and verified on `codex/reconcile-outside-school-hours`, based on `main` at `4758383`. This release contains the reconciliation change and related documentation/tests; consult Git history and remote refs for publication state. No server deployment was performed. The pre-existing untracked AI-DEVELOPMENT-GUIDE.md and its local-work status entry below are preserved outside the release.

- Regular timer calendars now run at :00/:30 in weekday hours 00–07 and 16–23 and all day Saturday/Sunday. Extra weekday 09:10/14:00 and daily 08:00/08:10/16:00 actions are unchanged.
- Regular timer (including its three-minute boot trigger) targets the existing `harrow-timebase@.service` template with a new `reconcile-regular` CLI action. The action checks local weekday/time after acquiring the controller lock and skips weekday 08:00–15:59 before preflight/Jamf access. The restriction includes weekday holidays. Already-running work is allowed to finish.
- Extra runs keep the unrestricted reconcile service. Portal/manual reconciliation remains unrestricted; direct CSV copies have fewer scheduled application/retry opportunities during school hours. The regular 14:00 weekday slot no longer exists.
- No new runtime modules or config settings. Installers/hotfix already ship the template and controller files and drain template jobs. Hotfix migration tests now cover restoring the old full-day regular timer/target on failure, installing the new target/template on success, and preserving timer/config/auth state.
- Updated README copies, SCHEDULE-UPGRADE.md, release notes and PROJECT_CONTEXT.md; AGENTS.md working rules unchanged.

Verification complete: all seven mocked hotfix migration tests passed on Ubuntu 24.04 / Python 3.12, including their nested nine portable offline scripts. The schedule suite now has 13 tests covering all weekdays/weekends, exact exclusion boundaries, weekday holidays, delayed lock acquisition, CLI dispatch, separate timer targets and unchanged portal/daily flows. Linux systemd calendar calculations confirm the next weekday regular run after 07:30 is 16:00 and weekend half-hourly slots. All three shell syntax checks and git diff --check pass. The macOS run had seven script passes and two dependency failures (missing Jinja2/FastAPI); both scripts subsequently passed in the restored Linux virtualenv. No real wall-clock timer firing or live Jamf operation was tested. Mocked hotfix tests do not establish production service/sandbox behavior. Both `harrow-offline-schedule-check` and the Jamf-connected `harrow-scheduler-test` VM are stopped. No production deployment, queues, or timer activation occurred.

Environment note: macOS /usr/bin/git is blocked by the Xcode license state; the installed CommandLineTools Git binary works and was used without changing license settings.

---

# AI development guide — 2026-09-14

Added `AI-DEVELOPMENT-GUIDE.md` as a reusable project-specific reference with the three-file workflow, task/acceptance criteria, copy-ready prompts, offline versus live testing limits, Git/release steps, hotfix deployment boundaries, and handoff/documentation maintenance. This is a documentation-only change on `main` after `4758383`; no application behavior changed. Not committed, pushed, or deployed by this task.

Verification: checked local Markdown link targets, code-fence balance, consistency with repository test/deployment instructions, and whitespace. Application tests were not rerun for this documentation-only task. AGENTS.md and PROJECT_CONTEXT.md remain unchanged; the historical stash is preserved.

---

# Schedule update — 2026-09-14

Schedule feature committed as `8edab47` on `codex/attendance-0810-schedule`, merged into `main` as `9bc3f99`, and both branches pushed to GitHub on 2026-09-14. Current checkout is `main`; the feature branch is preserved. Not deployed to a server by this task.

Context handoff: AGENTS.md, PROJECT_CONTEXT.md and CURRENT_STATUS.md are added to version control in a separate documentation commit after the schedule merge. They were excluded from the schedule commit. No runtime changes accompany this documentation update; the historical stash remains untouched.

- Attendance phase/timer and same-day portal import triggers now begin at 08:10 Bangkok time.
- Extra weekday reconciliation now uses two calendar entries: 09:10 and 14:00. Regular :00/:30, 08:00 school start, 16:00 end-of-day, manual override window and holiday/Wi-Fi policies are unchanged. The two 14:00 triggers target the same service and coalesce.
- Hotfix retains the existing pause/drain/backup/install/daemon-reload/restore flow and prints the new schedule. Production config/env and auth contents are preserved. No reboot is required; portal/query service briefly pause.
- Added mocked migration coverage for upgrading already-installed named timers from 08:20/09:20/10:20 and restoring their old definitions after an injected failure. Updated schedule boundary/portal tests and opt-in live harness times; the live harness was not run.
- Updated current operator docs, portal time guidance and architecture schedule. Historical reports below and under test-results remain historical.

Verification: all nine portable offline check scripts passed in the existing macOS test virtualenv, including ten schedule tests and nine unmatched-email tests. All three shell scripts pass `bash -n`; `git diff --check` passes. Linux systemd calendar validation confirms weekday 08:10, 09:10 and 14:00 Asia/Bangkok. All seven Linux mocked hotfix tests passed on Ubuntu 24.04 / Python 3.12, including existing-schedule upgrade/rollback, repeat deployment, disabled timer preservation and strict-policy preservation. The nested portable checks also passed on Linux (optional Node syntax check remains covered by the macOS run). These tests use a temporary server tree and mocked privileged/systemd commands, not live service activation or Jamf. The fresh `harrow-offline-schedule-check` VM had no installed Jamf credentials/services; it was stopped after testing. Calendar expressions were calculated with real Linux systemd; actual wall-clock timer firings were not observed.

AGENTS.md retains its existing working rules. PROJECT_CONTEXT.md and this status file reflect the revised schedule and completed verification. The historical stash is preserved. The existing Jamf-connected `harrow-scheduler-test` VM remains stopped; no production deployment, real queues, timers, or Jamf operations were performed.

---

The following is the preserved September 12 snapshot; its environment failures and “no active work” statements describe that earlier task.

# Current Project Status

Snapshot: 2026-09-12, inspected `main` at `f0bd310` (matching the local `origin/main` tracking ref; no remote fetch). Working tree was clean at inspection. This task adds only the three root context documents; no application behavior, configuration, deployment, or runtime data was changed.

R4 login, revised scheduler, and unmatched-email handling are merged. No current implementation task or unmerged feature branch is established by the checkout. Actual production deployment of this revision is not confirmed.

# Recently Completed

- **2026-09-11 — `d3824dc`, merged as `f0bd310`:** unmatched attendance emails skip with warnings by default; explicit strict mode survives upgrades. Canonical CSV retains unmatched identities; partial resolution retries against inventory; status/history displays skipped counts. Added offline regression and hotfix-policy checks.
- **2026-09-10 — `87e26fc`, merged as `aaeb830`:** 08:00 school start, 08:20 attendance, 16:00 end-of-day; clock-aligned and extra reconciliation; Wi-Fi default off; delayed actions calculate current phase; safe existing-R4 hotfix with state preservation and recovery. Includes recorded live test evidence.
- **2026-08-27 through 2026-09-01:** inclusive holiday ranges (`8fab16f`), application authentication (`7ffdc46`), add-user CLI (`8715b6d`), functional CSV drag/drop (`739c9bd`), login crest (`24dced7`), installer timezone (`f07dcb9`). These are included in current main.
- **2026-09-12 context handoff:** created `AGENTS.md` for working rules, `PROJECT_CONTEXT.md` for architecture/contracts, and this dated working-state record.

# In Progress

**Confirmed active feature work:** none established. Local scheduler/unmatched branches and corresponding remote tracking branches point to commits already merged into main; do not infer ongoing work from their names. No TODO/FIXME markers or explicit unfinished feature plan were found in application source.

**Preserved work:** `stash@{0}` is labeled “Scheduler work before rebasing onto R4 login hotfix.” It contains older scheduler/deployment/documentation changes relative to the pre-login base. It was inspected by summary only, not applied, dropped, or compared line-by-line for unique remaining work. Treat it as preserved historical work; ask its owner before disposal or reuse.

**Likely follow-up, not confirmed assigned work:** deployment validation and documentation cleanup implied by the gaps below. The repository does not establish an owner or schedule.

# Next Likely Tasks

1. Correct stale portal upload guidance and historical migration/release claims in a separate scoped change; details below. Decide whether both README copies must remain synchronized and how VERSION is maintained.
2. Establish a complete isolated dependency environment and finish the unavailable checks listed under Verification. Use Linux for hotfix migration behavior/systemd verification.
3. Before deploying, confirm current host R4 auth, config policies, official holiday data, timer state, and sandbox permissions; follow `SCHEDULE-UPGRADE.md` with the complete bundle.
4. Review the skip-policy behavior with operators, especially all-unmatched uploads, and verify warning visibility on the actual target environment. Existing September 10 live evidence predates the September 11 change.
5. If broader rollout is intended, validate realistic fleet load/API limits and physical-device policy delivery. Five-device tests do not establish these.

# Known Bugs / Issues

- **Confirmed stale UI guidance:** `portal/templates/index.html` still says missing email matches produce an error, while the implementation defaults to warning/skip. `status.html` and `history.html` implement the new warnings. Left unchanged because this task is documentation-only.
- **Historical documentation conflicts:** `RELEASE-NOTES.md` includes the old 07:00/08:00/08:10 schedule and an R4 statement that the hotfix changes Nginx Basic Auth. Current `apply-hotfix.sh` requires existing R4 auth and preserves Nginx. `MIGRATION-EMAIL-ATTENDANCE.md` does not explain current skip/partial-cache policy. Later code and `SCHEDULE-UPGRADE.md` take precedence.
- **README overstatements:** `README.md` and identical `README-updated.md` describe fresh inventory resolution more generally than the complete-cache behavior permits; their missing-file explanation omits manual overrides in some passages. Their “Disable all automatic jobs” block stops timers only, leaving portal/import path able to trigger writes. Consult the implementation and full maintenance procedure.
- **Release identifier drift:** `VERSION` still says `2026.08.29-r4-login`, despite September scheduler and attendance-policy commits. Do not use it alone to identify deployed behavior.
- **Unresolved historical device-state attribution:** September 10 evidence records all five test devices restored to their original Room values, then a later read-only check found all at Room 200. Cause was not established; report says test VM automatic Jamf writes were gated/stopped. Do not infer failed initial restoration or “repair” those devices from this old snapshot.

Additional enduring implementation risks, including cache invalidation, file concurrency, bootstrap credentials, and error/status limitations, are documented in `PROJECT_CONTEXT.md` under Known Technical Debt.

# Blockers

No blocker to the documentation task remains. Current production host/access/configuration and rollout ownership cannot be determined from repository contents; they block claims about production readiness, not local source work.

## Verification at this snapshot

On this workspace's Python **3.14.7**:

- Passed: `regression_check.py`, `runtime_regression_check.py`, `refactor_behavior_check.py`, `holiday_range_check.py`, `schedule_check.py`, `auth_regression_check.py`, `upload_drag_drop_check.py` (all under `tests/`).
- `unmatched_attendance_check.py`: ran nine tests; eight passed, template test errored because `jinja2` is missing. This is an environment dependency failure, not evidence that template behavior is wrong.
- `portal_auth_integration_check.py`: could not start because `fastapi` is missing.
- All three shell scripts passed `bash -n`. JavaScript syntax was checked through the upload check with installed Node.
- `hotfix_migration_check.py` was not run here: its nested tests need the missing dependencies, and its deployment harness expects Linux/GNU shell tooling. Do not label it passed based on syntax checks.
- No dependencies were installed, services started, Jamf calls made, or live tests rerun. No configured lint/type/build pipeline exists.

Final documentation verification checked repository paths, commands, internal consistency, secret-value exclusion, whitespace, and that only the three requested files changed.

# Incomplete / Experimental Areas

- **Deliberately deferred:** Wi-Fi management code and legacy `0810` action remain, but the feature defaults off and has no 08:10 timer. This is intentional, not a failed implementation.
- **Example deployment data:** root `holidays.csv` has example descriptions and is used by fresh installation until replaced. The official school calendar cannot be inferred from it. Credential environment template is not a working Jamf setup.
- **Local-only limits:** no turnkey local full-stack simulator or database fixture exists. Offline tests use mocks; importer subprocess paths are fixed to the installed server layout.
- **Live test limits:** `LIVE-TEST-REPORT.md` and `test-results/2026-09-10/` cover an OrbStack Ubuntu VM and five authorized devices, including real portal/importer/Jamf flow, upgrade/repeat/rollback, and twelve schedule phases. The timestamp witness invoked `date`, not Jamf; 09:20/10:20 wall-clock firings were calculated rather than observed. OrbStack relaxed production sandbox settings. These are historical results, not current runtime measurements.
- The live report's “local, uncommitted” and hash-equality statements describe its capture time; git history now shows the scheduler merged, followed by the unmatched-email update. Do not treat that report as a hash attestation of current HEAD.

# Important Recent Decisions

- The current phase is authoritative after lock acquisition; delayed scheduled jobs cannot restore an earlier phase.
- Wi-Fi absent/false means no management, including in older configurations. The hotfix rejects explicit enabled Wi-Fi before pausing services.
- Omitted unmatched policy now means skip. Explicit `error`, ambiguity under `unique`, coverage and absence safeguards remain enforced. Partial matches retain source emails and retry; full matches retain cache reuse.
- Hotfix preserves production config/env, accounts/key, Nginx, and independent timer enablement/activity. It targets existing R4 installations, with no reboot and a temporary portal pause. Automatic rollback ends before jobs resume.

# Handoff Notes

Start with `AGENTS.md` and the architecture/feature map in `PROJECT_CONTEXT.md`, then inspect the relevant code. For attendance work, read shared parsing, controller resolution/cache, importer activation, and warning templates together. For schedules, read controller actions/state, importer trigger windows, and all timer definitions together.

Do not revert the R4 login/security boundary, EXDEV-safe queue publication, Classic inventory fallbacks, holiday symlink/range support, delayed-phase safeguards, or partial-resolution cache bypass. Preserve the old stash and historical evidence; neither authorizes replaying production changes.

Fragile boundaries are cache freshness, concurrent file activation versus controller locking, cross-user permissions, and deployment module completeness. Read the code before assuming a FAILED job left the canonical file untouched. Check actual host state before deployment or restoration, especially the original hotfix backup's unit-state records. Production scale, sandbox behavior, physical-device delivery, and the later five-device Room change remain unverified/unexplained here.

# Update Rules

Before finishing a meaningful development task, update this file if working state changed. Date observations, identify branch/commit when useful, and separate confirmed active work from inference. Move completed work out of In Progress; record checks actually run, failures, unavailable checks, blockers, and remaining verification. Preserve relevant user work and handoff warnings. Keep durable architecture in `PROJECT_CONTEXT.md` and operating rules in `AGENTS.md`; do not turn this file into a duplicated design manual or invented roadmap.
