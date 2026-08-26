# Migration: Absent Students CSV now uses Email Address

## New CSV format

```csv
Email Address
student001@harrowschool.ac.th
student014@harrowschool.ac.th
```

The Portal stores the canonical file as:

```csv
email_address
student001@harrowschool.ac.th
student014@harrowschool.ac.th
```

## What happens during processing

1. Read the absent Email addresses.
2. Read current Jamf Mobile Device Inventory for `Harrow-All-iPads`.
3. Build a case-insensitive `emailAddress -> Serial Number` index.
4. Require exactly one matching master iPad per Email by default.
5. Resolve absent students to iPad Serials.
6. Set those devices to `Room = 200` / `Out-Harrow`.
7. Keep all other master iPads at `Room = 100` / `In-Harrow`.
8. Cache the successful resolution against the attendance CSV SHA-256 for subsequent reconciles.

## Important upgrade note

Old attendance files whose only identity column is `serial_number` are no longer accepted by the Attendance upload workflow. Replace them with Email Address CSV files after upgrading.

The Device Override tab now searches Jamf by exact Email Address. After the administrator selects a matching iPad, Serial Number is still used internally for the actual Room update and Manual Override state.
