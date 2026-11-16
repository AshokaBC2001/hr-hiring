# sync-interns

Starts when: the Planner runs
Done by: plain code (no model)

## What it does
Reads the intern records from Talent Hub and copies them into our own register, so the
system keeps working even when Talent Hub cannot be reached.

For each record it keeps five fields:
- Trainee ID, Name, Start date, End date, Specialization

The specialization (for example "AI/ML") is matched to one of our areas. When nothing
matches, the intern is listed for the HR officer instead of being guessed.

Dates arrive in UTC (for example 2026-05-19T00:00:00.000Z) and are read as local dates.

## Checks
- A record with no Trainee ID is skipped.
- The same Trainee ID is never stored twice.
- Nothing is ever written back to Talent Hub.
