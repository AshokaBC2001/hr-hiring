# suggest-candidates

Starts when: a vacancy is open
Done by: plain code (no model)

## What it does
For each open vacancy, suggests the strongest CVs waiting in the pool for that area:
- ranked by their score for that area, best first;
- only CVs scoring 50 or more for that area;
- the number of places plus 2 reserves, in case someone is not selected.

If a suggested candidate is not approved, fails the interview or drops out, the next best
CV from the pool is suggested in their place. When enough candidates have passed, the
vacancy is marked as filled.

Nobody is contacted. Every suggestion waits for the HR officer to approve it.
The settings are in hiring/config.py (POOL_MIN_SCORE, VACANCY_RESERVES).
