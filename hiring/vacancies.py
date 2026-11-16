"""Vacancies: the places that open when interns finish, and who is suggested for them.

All of this is plain code. The model never decides who gets a place.

    interns finishing in the next few days  ->  an open vacancy for their area
    the vacancy                             ->  the top CVs from the pool are suggested
    the HR officer approves a suggestion    ->  the candidate joins the normal hiring flow
    the candidate passes the interview      ->  one place is filled
    the candidate fails or is closed        ->  the next best CV is suggested instead
"""
from __future__ import annotations

from datetime import date, timedelta

from . import config, db

# What a suggestion can be:
#   suggested  waiting for the HR officer
#   approved   invited to an interview
#   passed     got the place
#   failed     did not get the place (failed, declined or closed)
#   taken      approved for another area's vacancy instead
LIVE = ("suggested", "approved", "passed")
STATE_LABELS = {"suggested": "Waiting for you", "approved": "Invited", "passed": "Got the place",
                "failed": "Not selected", "taken": "Took another place"}


def _summary(c: db.Candidate, area: str) -> dict:
    v = (c.area_scores or {}).get(area) or {}
    return {"app_no": c.app_no, "name": c.name, "score": v.get("score", c.score),
            "gaps": v.get("gaps", []), "state": "suggested", "at": db.now()}


def pool_for_area(area: str, skip: set[str] = frozenset()) -> list[db.Candidate]:
    """CVs waiting in the pool that score enough for this area, best first."""
    rows = []
    for c in db.candidates_in(config.POOL_STATUSES):
        if c.app_no in skip or c.vacancy_id:
            continue
        v = (c.area_scores or {}).get(area)
        if v and v["score"] >= config.POOL_MIN_SCORE:
            rows.append((v["score"], -c.id, c))
    rows.sort(key=lambda x: (-x[0], -x[1]))
    return [c for _, _, c in rows]


def refresh_suggestions(v: db.Vacancy) -> int:
    """Tops the suggestion list back up from the pool. Returns how many were added."""
    if v.status != "open":
        return 0
    suggestions = [dict(s) for s in (v.suggestions or [])]
    live = [s for s in suggestions if s["state"] in LIVE]
    want = v.places + config.VACANCY_RESERVES
    if len(live) >= want:
        return 0
    used = {s["app_no"] for s in suggestions}
    added = []
    for c in pool_for_area(v.area, skip=used):
        if len(live) + len(added) >= want:
            break
        added.append(_summary(c, v.area))
    if added:
        db.update_vacancy(v.id, suggestions=suggestions + added)
    return len(added)


def find_and_update(today: date | None = None) -> dict:
    """Interns finishing within the notice period open a vacancy for their area.
    One open vacancy per area is kept up to date; the HR officer's own ones are left alone."""
    today = today or date.today()
    cutoff = (today + timedelta(days=config.VACANCY_NOTICE_DAYS)).isoformat()
    with db.Session() as s:
        every = list(s.scalars(db.select(db.Vacancy)))
    done = set()
    for v in every:
        if v.status in ("filled", "closed"):
            done |= set(v.trainee_ids or [])
    open_auto = {v.area: v for v in every if v.status == "open" and v.created_by == "Planner"}

    leaving: dict[str, list] = {}
    for i in db.interns_ending_by(cutoff):
        if i.trainee_id not in done:
            leaving.setdefault(i.area, []).append(i)

    created = updated = closed = 0
    for area, interns in sorted(leaving.items()):
        ids = sorted(i.trainee_id for i in interns)
        opens_on = min(i.end_date for i in interns)
        shown = ", ".join(f"{i.trainee_id} on {i.end_date}" for i in interns[:5])
        reason = f"{len(ids)} intern(s) finishing in {area}: {shown}" + (", and more" if len(ids) > 5 else "")
        v = open_auto.get(area)
        if v is None:
            with db.Session() as s:
                row = db.Vacancy(area=area, places=len(ids), opens_on=opens_on, reason=reason,
                                 trainee_ids=ids, suggestions=[], status="open", created_by="Planner",
                                 created_at=db.now(), updated_at=db.now())
                s.add(row)
                s.commit()
                v = row
            created += 1
        elif sorted(v.trainee_ids or []) != ids:
            db.update_vacancy(v.id, places=max(len(ids), v.filled), opens_on=opens_on, reason=reason,
                              trainee_ids=ids)
            v = db.get_vacancy(v.id)
            updated += 1
        refresh_suggestions(v)

    # An area whose interns all stayed on (an end date was extended) no longer needs its vacancy.
    for area, v in open_auto.items():
        if area not in leaving and not v.filled and not any(
                s["state"] in ("approved", "passed") for s in (v.suggestions or [])):
            db.update_vacancy(v.id, status="closed", reason=v.reason + " - closed: no one is finishing now.")
            closed += 1
    return {"created": created, "updated": updated, "closed": closed, "open": len(db.vacancies_in("open"))}


def suggest_for_open() -> dict:
    """Makes sure every open vacancy has enough suggestions."""
    added = waiting = 0
    for v in db.vacancies_in("open"):
        added += refresh_suggestions(v)
        waiting += sum(1 for s in (db.get_vacancy(v.id).suggestions or []) if s["state"] == "suggested")
    return {"suggestions_added": added, "waiting_for_approval": waiting}


def mark_taken_elsewhere(app_no: str, keep_vacancy_id: int) -> None:
    """One CV can be suggested for several areas. Once it is approved for one vacancy,
    the other vacancies get the next best CV instead."""
    for v in db.vacancies_in("open"):
        if v.id == keep_vacancy_id:
            continue
        if any(s["app_no"] == app_no and s["state"] == "suggested" for s in (v.suggestions or [])):
            db.update_vacancy(v.id, suggestions=_set_state(v, app_no, "taken"))
            refresh_suggestions(db.get_vacancy(v.id))


def _set_state(v: db.Vacancy, app_no: str, state: str) -> list:
    rows = [dict(s) for s in (v.suggestions or [])]
    for s in rows:
        if s["app_no"] == app_no:
            s["state"] = state
            s["at"] = db.now()
    return rows


def on_result(candidate_id: int) -> None:
    """Called after a candidate passes, fails or is closed. Keeps the vacancy up to date
    and, when someone drops out, suggests the next best CV from the pool."""
    c = db.get_candidate(candidate_id)
    if c is None or not c.vacancy_id:
        return
    v = db.get_vacancy(c.vacancy_id)
    if v is None:
        return
    if c.status == "passed" or (c.status == "closed" and c.work_id):
        rows = _set_state(v, c.app_no, "passed")
        filled = sum(1 for s in rows if s["state"] == "passed")
        done = filled >= v.places
        db.update_vacancy(v.id, suggestions=rows, filled=filled, status="filled" if done else v.status)
        if done:
            db.log_event(None, "Planner", f"The {v.area} vacancy is filled ({filled} of {v.places}).")
    elif c.status in ("failed", "closed"):
        db.update_vacancy(v.id, suggestions=_set_state(v, c.app_no, "failed"))
        v = db.get_vacancy(v.id)
        if refresh_suggestions(v):
            db.log_event(None, "Planner", f"{c.name} did not take the {v.area} place, "
                                          "so the next best CV from the pool is suggested.")
