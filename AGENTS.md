# AI Agent Working Instructions

## Before Making Changes

1. Read `AGENTS.md`, `PROJECT_CONTEXT.md`, and `CURRENT_STATUS.md` in that order.
2. Check `git status`, branch/history, and any task-specific instructions. Preserve unrelated edits and stashes.
3. Inspect the actual implementation related to the task, including its callers, tests, configuration, filesystem schemas, Jamf APIs, and dependencies. Current code is authoritative; context documents may lag.
4. Identify whether the task touches the portal, importer, controller, broker, or deployment boundary before changing behavior.

## Repository Rules

- Keep `harrow_timebase.py` as the public controller/CLI facade. Domain responsibilities live in `timebase/controller/{actions,attendance,state}.py`; shared exceptions/value objects live in dependency-light `types.py`.
- Keep queue orchestration in `attendance_importer.py`, action handlers in `timebase/importer/handlers.py`, and filesystem operations in `storage.py`. Preserve public imports used by the tests and services.
- The portal runs as `harrow-upload`; importer/controller/broker run as `harrow-timebase`. Never give the portal Jamf credentials or membership in the `harrow-timebase` Unix group. Broker access is loopback plus `X-Internal-Token`.
- Use `JamfClient` for Jamf OAuth/retries/XML calls. Preserve `classic_xml(params=...)`, Classic inventory fallbacks, master-group filtering, concurrency bounds, and post-write group verification.
- Validate CSV uploads with `attendance_common.py` / `holiday_common.py`, and authoritative inventory policy with `validate_email_resolution`. Direct controller CSV parsing is a separate path: check both when changing attendance validation.
- Filesystem JSON/CSV records are service contracts. Preserve atomic replacement, EXDEV-safe queue publication, modes/group ownership, archive behavior, and compatibility with older status records.
- Follow Python snake_case functions/modules, PascalCase classes, existing type annotations/dataclasses, and explicit action dispatch. There is no ORM or frontend component library to extend.
- Portal pages use FastAPI form handlers, redirects, and Jinja templates; authenticated pages extend `portal/templates/base.html`. Retain form-token checks and template escaping. Styling lives in `portal/static/app.css`; upload interactions use `upload.js` and matching `data-file-*` hooks.
- Use existing controller exceptions/exit behavior, HTTP errors/template errors at web boundaries, and recorded status/audit failures at the queue boundary. Follow the `harrow-timebase` logger pattern; never log credentials or signed cookies. Attendance/device logs can contain personal information.
- Tests are executable `tests/*_check.py` scripts, with mocks and temporary directories for offline behavior. Some tests assert source structure; inspect those contracts before reorganizing code.

## Change Discipline

Prefer small, focused changes. Avoid unrelated edits, unnecessary refactors, new libraries where existing tools suffice, and new abstractions without need. Preserve backward compatibility unless explicitly asked to change it. Never hardcode or expose credentials, including the existing installer bootstrap password. Treat auth, permissions, Jamf writes, migration scripts, and destructive operations as sensitive.

## Verification

From the repository root, with dependencies installed, run the relevant checks (portable offline checks below):

```bash
for check in regression_check runtime_regression_check refactor_behavior_check holiday_range_check schedule_check unmatched_attendance_check auth_regression_check portal_auth_integration_check upload_drag_drop_check; do
  python3 "tests/${check}.py" || break
done
for script in install.sh install-program.sh apply-hotfix.sh; do
  bash -n "$script" || break
done
git diff --check
```

For deployment changes, also run `python3 tests/hotfix_migration_check.py` in a Linux environment with Bash/GNU tools; it uses a temporary server tree and mocked privileged commands. On Linux, validate changed timer expressions with `systemd-analyze calendar '<OnCalendar expression>'`. See `PROJECT_CONTEXT.md` for deployment checks and manual flows.

No configured linter, static type-checker, build command, CI workflow, or database migration runner exists. `regression_check.py` parses production Python syntax; it is not a type check. `upload_drag_drop_check.py` runs `node --check` when Node is available. Report failures and skipped/unavailable checks accurately.

## Documentation Maintenance

- Update `PROJECT_CONTEXT.md` when architecture, major features, integrations, dependencies, or important conventions change.
- Update `CURRENT_STATUS.md` before completing meaningful development work when active work, blockers, unfinished tasks, or project state change; record verification and its limits.
- Update `AGENTS.md` only when repository working rules change. Keep detailed architecture and transient status in their respective files.

## Important Warnings

- Never run installers, enable timers, process production queues, or run live Jamf tests as routine verification. These can change real iPads. `tests/live_jamf_schedule_check.py` explicitly requires an authorized five-device test group and `--allow-room-writes`; preserve its recovery snapshots.
- `--dry-run` still needs credentials and calls Jamf reads/OAuth; it also uses local logging/locking. `preflight` and `verify` can refresh local cache. Neither is an offline test; `verify` is a status report, not full desired-state verification.
- Schedule changes span controller phases, importer trigger windows, override guards, systemd timers, tests, and documentation. Delayed scheduled actions must reconcile the current phase.
- Wi-Fi management defaults OFF, including omitted settings. Disabling it leaves existing Jamf profile scope unchanged. Preserve this behavior.
- Missing attendance and unmatched emails are different policies. Shipped configs use `zero_absent`; unmatched defaults to `skip`. Both can result in zero attendance absences; manual overrides still apply. Do not silently change these defaults.
- Deploy the complete runtime: both installers contain explicit module lists. `apply-hotfix.sh` targets an existing R4 login installation, preserves config/auth and timer state, and has a rollback commit point. Read `SCHEDULE-UPGRADE.md` before changing it.
- Preserve the holiday compatibility symlink and runtime-generated auth/state/cache/audit files. Do not replace them with repository examples. The root `holidays.csv` is example data.
