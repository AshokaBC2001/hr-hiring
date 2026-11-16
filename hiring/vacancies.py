"""Vacancies: places that open when MongoDB interns finish, and pool suggestions for them.

All logic here is deterministic Python code (the LLM never decides who fills a place).
Interns are queried live from TalentHub (MongoDB) — never duplicated in SQLite:

    MongoDB interns finishing within notice period  ->  open vacancy per area
    open vacancy                                     ->  top CVs from CV pool suggested
    HR officer approves a suggestion                 ->  candidate enters Interview flow
    candidate passes                                 ->  one vacancy place filled
    candidate fails / declined / closed              ->  next best CV suggested from pool
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from sqlalchemy import select

from . import config, db, talent_hub

# Suggestion states inside Vacancy.suggestions:
#   suggested  waiting for HR officer approval
#   approved   invited to interview
#   passed     selected / got the place
#   failed     declined, failed interview, or closed
#   taken      approved for another vacancy instead
LIVE = ("suggested", "approved", "passed")
STATE_LABELS = {
    "suggested": "Waiting for you",
    "approved": "Invited",
    "passed": "Got the place",
    "failed": "Not selected",
    "taken": "Took another place",
}


AUTO_CLOSE_MARKER = " - closed: no one is finishing now."


def _summary(c: db.Candidate, area: str) -> dict[str, Any]:
    v = (c.area_scores or {}).get(area) or {}
    return {
        "app_no": c.app_no,
        "name": c.name,
        "score": v.get("score", c.score),
        "gaps": v.get("gaps", []),
        "state": "suggested",
        "at": db.now(),
    }


def pool_for_area(
    area: str,
    skip: set[str] | frozenset[str] = frozenset(),
    pool_rows: list[db.Candidate] | None = None,
) -> list[db.Candidate]:
    """CVs waiting in the pool that score enough for ``area``, ranked best first."""
    candidates = db.candidates_in(config.POOL_STATUSES) if pool_rows is None else pool_rows
    rows = []
    for c in candidates:
        if c.app_no in skip or c.vacancy_id:
            continue
        v = (c.area_scores or {}).get(area)
        if v and v["score"] >= config.POOL_MIN_SCORE:
            rows.append((v["score"], -c.id, c))
    rows.sort(key=lambda x: (-x[0], -x[1]))
    return [c for _, _, c in rows]


def refresh_suggestions(v: db.Vacancy, pool_rows: list[db.Candidate] | None = None) -> int:
    """Top the suggestion list up from the pool. Returns count added."""
    if v.status != "open":
        return 0
    suggestions = [dict(s) for s in (v.suggestions or [])]
    live = [s for s in suggestions if s["state"] in LIVE]
    want = v.places + config.VACANCY_RESERVES
    if len(live) >= want:
        return 0
    used = {s["app_no"] for s in suggestions}
    added = []
    for c in pool_for_area(v.area, skip=used, pool_rows=pool_rows):
        if len(live) + len(added) >= want:
            break
        added.append(_summary(c, v.area))
    if added:
        db.update_vacancy(v.id, suggestions=suggestions + added)
    return len(added)


def find_and_update(today: date | None = None, intern_rows: list[dict[str, Any]] | None = None) -> dict[str, int]:
    """Check MongoDB interns via TalentHub API/Mongo and keep automatic vacancies up to date."""
    today = today or date.today()
    cutoff = (today + timedelta(days=config.VACANCY_NOTICE_DAYS)).isoformat()
    earliest = (today - timedelta(days=config.VACANCY_LOOKBACK_DAYS)).isoformat()
    area_names = [a.name for a in db.active_areas()]
    if intern_rows is None:
        intern_rows = talent_hub.interns_with_area(area_names, limit=0, today=today)

    with db.Session() as s:
        every = list(s.scalars(select(db.Vacancy)))
    done: set[str] = set()
    for v in every:
        # Auto-closed vacancies (extended end dates) may re-open later once their new date enters notice;
        # only filled or manually HR-closed vacancies permanently retire those trainee IDs.
        if v.status == "filled" or (v.status == "closed" and AUTO_CLOSE_MARKER not in (v.reason or "")):
            done |= set(v.trainee_ids or [])
    open_auto = {v.area: v for v in every if v.status == "open" and v.created_by == "Planner"}

    leaving: dict[str, list[dict[str, Any]]] = {}
    for row in intern_rows:
        if row.get("area") and row.get("end_date") and earliest <= row["end_date"] <= cutoff:
            if row["Trainee_ID"] not in done:
                leaving.setdefault(row["area"], []).append(row)

    pool_rows = db.candidates_in(config.POOL_STATUSES)
    created = updated = closed = 0
    for area, interns in sorted(leaving.items()):
        ids = sorted(i["Trainee_ID"] for i in interns)
        opens_on = min(i["end_date"] for i in interns)
        shown = ", ".join(f"{i['Trainee_ID']} on {i['end_date']}" for i in interns[:5])
        reason = f"{len(ids)} intern(s) finishing in {area}: {shown}" + (", and more" if len(ids) > 5 else "")
        v = open_auto.get(area)
        if v is None:
            with db.Session() as s:
                row = db.Vacancy(
                    area=area,
                    places=len(ids),
                    opens_on=opens_on,
                    reason=reason,
                    trainee_ids=ids,
                    suggestions=[],
                    status="open",
                    created_by="Planner",
                    created_at=db.now(),
                    updated_at=db.now(),
                )
                s.add(row)
                s.commit()
                v = row
            created += 1
        elif sorted(v.trainee_ids or []) != ids:
            new_places = max(len(ids), v.filled)
            new_status = "filled" if v.filled >= new_places else "open"
            db.update_vacancy(
                v.id,
                places=new_places,
                opens_on=opens_on,
                reason=reason,
                trainee_ids=ids,
                status=new_status,
            )
            v = db.get_vacancy(v.id)
            updated += 1
        if v and v.status == "open":
            refresh_suggestions(v, pool_rows=pool_rows)

    for area, v in open_auto.items():
        if area not in leaving and not v.filled and not any(
            s["state"] in ("approved", "passed") for s in (v.suggestions or [])
        ):
            base_reason = (v.reason or "").replace(AUTO_CLOSE_MARKER, "")
            db.update_vacancy(v.id, status="closed", reason=base_reason + AUTO_CLOSE_MARKER)
            closed += 1

    return {"created": created, "updated": updated, "closed": closed, "open": len(db.vacancies_in("open"))}


def suggest_for_open() -> dict[str, int]:
    """Ensure every open vacancy has enough pool suggestions."""
    added = waiting = 0
    pool_rows = db.candidates_in(config.POOL_STATUSES)
    for v in db.vacancies_in("open"):
        added += refresh_suggestions(v, pool_rows=pool_rows)
        fresh = db.get_vacancy(v.id)
        waiting += sum(1 for s in ((fresh.suggestions if fresh else []) or []) if s["state"] == "suggested")
    return {"suggestions_added": added, "waiting_for_approval": waiting}


def _set_state(v: db.Vacancy, app_no: str, state: str) -> list[dict[str, Any]]:
    rows = [dict(s) for s in (v.suggestions or [])]
    for s in rows:
        if s["app_no"] == app_no:
            s["state"] = state
            s["at"] = db.now()
    return rows


def mark_taken_elsewhere(app_no: str, keep_vacancy_id: int) -> None:
    """When a pool CV is approved for one vacancy, replace it on any other open vacancies."""
    for v in db.vacancies_in("open"):
        if v.id == keep_vacancy_id:
            continue
        if any(s["app_no"] == app_no and s["state"] == "suggested" for s in (v.suggestions or [])):
            db.update_vacancy(v.id, suggestions=_set_state(v, app_no, "taken"))
            fresh = db.get_vacancy(v.id)
            if fresh:
                refresh_suggestions(fresh)


def on_result(candidate_id: int) -> None:
    """Update linked vacancy when a candidate passes, fails, or is closed."""
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
        if v and refresh_suggestions(v):
            db.log_event(
                None,
                "Planner",
                f"{c.name} did not take the {v.area} place, so the next best CV from the pool is suggested.",
            )
