# R4 scheduler live test report — 10 September 2026

**Assessment:** The completed tests support a controlled deployment of this scheduler update to an existing R4 server. No application defect was found in the tested workflows. This is evidence from a five-device Jamf test group and an OrbStack Ubuntu VM, not a guarantee for an uninspected production host or a larger fleet. No production server deployment, Git commit, or push was performed.

## Environment and method

- Base: `codex/r4-login-hotfix`, commit `f07dcb9`.
- Update: local, uncommitted `codex/change-time-and-scheduler` work.
- Linux host: dedicated OrbStack Ubuntu 24.04 ARM64 VM `harrow-scheduler-test`, using real systemd, Nginx, Portal and importer services.
- Jamf: authorized test instance, exactly five devices in static group `Harrow-All-iPads` (group ID 11), five distinct email identities.
- Tested bundle and installed runtime hashes match the current workspace. The VM boot ID remained unchanged throughout.
- On resumption after the interruption, passed tests were not rerun. Existing journals/reports were collected, followed by read-only state and file-integrity checks.

## Upgrade and recovery — PASS

The actual R4 installation was upgraded using `apply-hotfix.sh`, then the hotfix was applied again successfully. The five replacement/current timers resumed enabled and active. Obsolete 07:00/08:00/08:10 timers were removed. The controller job-drain check used a real systemd oneshot running a 30-second sleep.

- Upgrade duration: **33.34 seconds** in that test.
- Observed Portal interruption: **29.59 seconds**, including waiting for the artificial long-running job.
- No reboot; Nginx remained running; Portal/query services recovered.
- Config/env contents, credentials, portal accounts/session key and Nginx configuration were unchanged.
- An injected failure in a separate hotfix test copy after file deployment returned exit 23 and successfully restored runtime, authentication/config contents and Portal/query services.

**Operational implication:** This is a no-reboot upgrade, not a zero-interruption Portal deployment. The Portal pauses while outstanding work drains and files are installed. Longer running jobs can cause a longer pause.

Evidence: [upgrade](test-results/2026-09-10/upgrade-report.json), [recovery](test-results/2026-09-10/rollback-report.json).

## Live Jamf behavior — PASS

The installed controller was exercised against the five actual test devices. Controller time was controlled within the test process; the host clock was not changed. Writes were restricted by the live test harness to Room fields on the snapshotted devices.

| Phase | In-Harrow | Out-Harrow |
|---|---:|---:|
| 07:59 | 0 | 5 |
| 08:00 | 5 | 0 |
| 08:19 | 5 | 0 |
| 08:20, two absent | 3 | 2 |
| 09:20 | 3 | 2 |
| 10:20 | 3 | 2 |
| Manual override | 2 | 3 |
| Clear manual override | 3 | 2 |
| Direct CSV replacement, one absent | 4 | 1 |
| 16:00 | 0 | 5 |
| Weekend | 0 | 5 |
| Holiday | 0 | 5 |

All 12 phases passed. Reconciliation with unchanged attendance issued no repeated Room PUTs. Direct CSV replacement invalidated the attendance cache. Original Room values were restored, and Wi-Fi profile scope matched the pre-test scope. A separate read-only verification before the interruption confirmed restoration against the original snapshot.

Evidence: [phase results](test-results/2026-09-10/live-jamf-report.json), [original restoration check](test-results/2026-09-10/final-restoration.json).

## Actual Portal → importer → Jamf — PASS

At **14:27:57 Bangkok time**, HTTPS login, CSV upload, the systemd queue importer and immediate reconciliation completed successfully. One absent student resulted in four In-Harrow and one Out-Harrow device. The device Room values were restored afterward.

Evidence: [Portal result](test-results/2026-09-10/portal-live-report.json).

## Clock-aligned timer — PASS, with explicit test scope

The existing journal proves the originally awaited firing occurred:

- **14:30:00.821169 +07:** systemd started `harrow-schedule-observer.service`.
- **14:30:00.828354 +07:** its timestamp command executed.
- **14:30:00.829558 +07:** systemd reported successful completion.

The witness used the exact production calendar expressions, `AccuracySec=1s`, and no randomized delay. Further recorded starts at 15:00, 15:30, 16:00, 16:30, 17:00 and 17:30 also occurred within the configured one-second accuracy. No rerun or additional wait was necessary after resuming.

**Service distinction:** The witness intentionally invoked `/usr/bin/date` through `harrow-schedule-observer.service`, with no Jamf calls. Separately, the effective installed `harrow-timebase-reconcile.timer` and `harrow-timebase-reconcile-extra.timer` both correctly target `harrow-timebase-reconcile.service`. Actual controller behavior was verified in the live tests above. A wall-clock 14:30 Jamf reconciliation was not forced during those tests.

Systemd validated the daily half-hour calendar and the 09:20/10:20 weekday calendar. The 09:20/10:20 calendar calculations and corresponding controller phases passed; those morning wall-clock firings were not observed live.

A later witness invocation at **21:45:08** followed a long gap and coincided with the Mac's logged full wake at **21:45:08**. This is consistent with host/VM sleep recovery. Calendar scheduling does not guarantee punctual execution while the host is suspended or otherwise unavailable.

Evidence: [observer journal](test-results/2026-09-10/calendar-observer.txt), [calendar calculations](test-results/2026-09-10/calendars.txt), [host wake](test-results/2026-09-10/host-wake.txt).

## State after the interruption

At **21:58:37 Bangkok time**, a fresh read-only check found all five devices at **Room 200**; the original pre-test snapshot had all five at Room 100. Group membership was unchanged. This differs from the successful restoration verified before the interruption.

The current Room 200 state is consistent with the normal after-16:00 state, but its source has not been established. The test VM's Jamf reconciliation timers and importer were stopped, its automatic-controller test gate remained in place, and there were no controller/importer journal entries after 14:30. The timer witness runs only `date` and cannot introduce Jamf changes. Current Room values were left untouched rather than overwriting a potentially legitimate later action.

The timing test passed; the later device-state change is a separate unresolved attribution, not evidence of a failed restoration during the earlier tests.

Evidence: [current read-only state](test-results/2026-09-10/post-interruption-state.json).

## Limits, issues and final sanity checks

- OrbStack applies global systemd overrides that relax several service hardening settings, including `ProtectSystem`, `ProtectHome` and related namespace restrictions. This VM verifies real scheduling/service orchestration but does not fully reproduce production's systemd sandbox enforcement. Check service logs and health on the destination host after deployment.
- Five-device results do not establish throughput or API limits for a larger fleet, nor do Jamf inventory/group checks prove physical-device payload delivery.
- Two test-harness setup issues were corrected earlier: the long-running-job test was changed from an invalid transient-unit creation to an existing template-instance override; calendar base-time calculations were expressed in UTC, which the VM's systemd accepts. Neither required an application change.
- Final source/runtime hash comparisons passed. Shell syntax and Git whitespace/conflict checks passed. No new functional tests or live Room mutations were repeated on resumption.
- The read-only observer timer was stopped after evidence collection. The VM remains available for inspection; its Jamf timers and importer remain stopped, with automatic Jamf writes gated. Evidence here excludes credentials and device-identifying snapshots.

Deployment instructions: [SCHEDULE-UPGRADE.md](SCHEDULE-UPGRADE.md). Deploy the complete tested bundle from a separate directory on the existing R4 host, preserve its configuration, and review the hotfix's final health and timer output. Do not describe this as zero Portal downtime or bypass the destination-host checks.
