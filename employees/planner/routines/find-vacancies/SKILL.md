# find-vacancies

Starts when: the intern records are up to date
Done by: plain code (no model)

## What it does
Looks for interns whose last day is within the next 7 days, and groups them by area.
Each group opens a vacancy, for example "MLOps: 3 places from 2027-01-15".

- If more interns in the same area are finishing, the open vacancy is updated.
- If an end date is extended and nobody is finishing after all, the vacancy is closed again.
- A place that has already been filled or closed is never re-opened.

The HR officer can also open a vacancy by hand, for example when an intern leaves early.
The number of days is in hiring/config.py (VACANCY_NOTICE_DAYS).
