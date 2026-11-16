# sync-interns

Starts when: the Planner runs
Done by: plain code (no model)

## What it does
Queries live intern records from MongoDB (`slt_talent_hub_sample.interns`) either directly via
`TALENT_HUB_MODE=mongo` or through the read-only TalentHub REST API (`GET /api/interns`),
keeping MongoDB as the single source of truth without creating a separate SQLite interns table.

For each record it reads the Talent Hub fields:
- `Trainee_ID`, `Trainee_Name`, `Training_StartDate`, `Training_EndDate`, `field_of_spec_name`, plus optional `Trainee_Email` and `Institute`

Each specialization (for example `"AI/ML"`) is matched to one of the active hiring areas. When
nothing matches, the intern is flagged as unmapped for the HR officer instead of being guessed.

Dates stored as UTC timestamps in MongoDB (for example `2026-05-19T00:00:00.000Z`) are converted
to Sri Lanka local dates (`UTC+5:30`).

## Checks
- A record with no `Trainee_ID` is skipped by Talent Hub validation.
- Existing open vacancies keep working even when Talent Hub API is temporarily offline.
- Production Talent Hub remains strictly read-only.
